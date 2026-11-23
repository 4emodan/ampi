"""Shows ampi-tools approval requests to the owner as Telegram buttons and passes their answer back.

Only this process (the bridge's main process) may answer: ampi-tools checks the caller's PID, so a
`claude` run can queue a request but never approve it. Each button also carries a random nonce that
only this process learns, and ampi-tools checks that the tap came from the very message the relay
sent. So a button forged with a stolen bot token can't approve anything, and neither can a real
approval message whose text was edited afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from . import chatlog
from .services import ServiceError, ServiceUnavailable, ask
from .telegram import TelegramError

if TYPE_CHECKING:
    from .bot import ProfileBot

log = logging.getLogger(__name__)
Ask = Callable[..., Awaitable[dict]]
MAX_MESSAGE = 4000  # Telegram allows 4096 UTF-16 units; leave room
DECIDE_TIMEOUT = 900  # running an approved batch can take a while


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def fit(text: str, limit: int) -> str:
    """Cut text to `limit` UTF-16 units, marking the cut."""
    if utf16_len(text) <= limit:
        return text
    while utf16_len(text) > limit - 12:
        text = text[: max(0, len(text) - max(1, len(text) // 20))]
    return text + "\n[…cut]"


CHOICE_KEY = re.compile(r"[a-z]{1,12}")
CONFIRM_WINDOW = 30  # seconds for the second tap on a high-risk choice


def choices_of(item: dict) -> list[dict]:
    """The request's own buttons (watchers v2), if valid; else [] (the default Approve / Reject)."""
    raw = item.get("choices")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict) or not CHOICE_KEY.fullmatch(str(c.get("key", ""))) or not isinstance(c.get("label"), str):
            return []
        out.append({"key": c["key"], "label": c["label"][:40]})
    return out


def request_text(item: dict) -> str:
    head = "🔐 Your decision" if choices_of(item) else "🔐 Approve?"
    text = f"{head} (request #{item['id']})\n\n{item['summary']}"
    if item.get("reason"):
        text += f"\n\nThe assistant says why: {item['reason']}"
    if item.get("grant_label"):
        text += f"\n\n“✅ + always here” also allows: {item['grant_label']}."
    return text


def buttons_for(item: dict) -> list[list[dict]]:
    approval_id, nonce = item["id"], item.get("nonce") or ""
    choices = choices_of(item)
    if choices:  # one per row, so none is tapped by mistake; data stays well under Telegram's 64 bytes
        return [[{"text": c["label"], "callback_data": f"ap:{approval_id}:{nonce}:x{c['key']}"}] for c in choices]
    rows = [[
        {"text": "✅ Approve", "callback_data": f"ap:{approval_id}:{nonce}:y"},
        {"text": "❌ Reject", "callback_data": f"ap:{approval_id}:{nonce}:n"},
    ]]
    if item.get("grant_label"):  # its own row, so it isn't tapped by mistake
        rows.append([{"text": "✅ + always here", "callback_data": f"ap:{approval_id}:{nonce}:a"}])
    return rows


PROTECT_EVERY = 300  # seconds; also right after (re)connecting


class ApprovalRelay:
    def __init__(self, bot: "ProfileBot", socket: str, ask_fn: Ask = ask, interval: float = 3.0,
                 protected: Callable[[], tuple[list[int], bool]] | None = None, clock=None):
        self.bot = bot
        self.socket = socket
        self.ask = ask_fn
        self.interval = interval
        self.protected = protected
        self.own_bots: list[int] = []  # every profile's bot (set by main)
        self._clock = clock or time.monotonic
        self._sent: set[int] = set()  # requests shown already in this process (never send one twice)
        self._confirm_keys: dict[int, set[str]] = {}  # request -> choices that need a second tap (high risk)
        self._armed: dict[tuple[int, str], float] = {}  # (request, choice) -> time of the first tap
        self._down = False
        self._protected_at: float | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
                if self._down:
                    log.info("ampi-tools is reachable again")
                self._down = False
            except ServiceError as e:
                if not self._down:
                    log.info("approvals: %s (will keep trying)", e)
                self._down = True
            except Exception:
                log.exception("approval relay failed")
            await asyncio.sleep(self.interval)

    async def send_protected(self) -> None:
        """Tells ampi-tools which chats the assistant reads itself (Telegram writes there are refused)."""
        if self.protected is None:
            return
        now = self._clock()
        if self._protected_at is not None and not self._down and now - self._protected_at < PROTECT_EVERY:
            return
        ids, complete = self.protected()
        resp = await self.ask(self.socket, {"cmd": "telegram.protect", "ids": ids, "complete": complete,
                                            **({"bots": self.own_bots} if self.own_bots else {})}, timeout=10)
        if resp.get("ok"):
            # Incomplete (a Telegram watcher hasn't found its chat yet): try again in a minute.
            self._protected_at = now if complete else now - PROTECT_EVERY + 60

    async def send_notices(self, notices: list[dict]) -> None:
        """Reports of automatic Telegram writes etc. A notice is acknowledged only once it was really sent."""
        for n in notices:
            try:
                await self.bot.api.send_message(self.bot.profile.owner_chat_id,
                                                fit(str(n.get("text", "")), MAX_MESSAGE))
            except TelegramError as e:
                log.warning("could not send notice #%s: %s", n.get("id"), e)
                return  # try again next tick
            await self.ask(self.socket, {"cmd": "notices.ack", "id": n["id"]}, timeout=10)

    async def tick(self) -> None:
        await self.send_protected()
        resp = await self.ask(self.socket, {"cmd": "approvals.pending"})
        if not resp.get("ok"):
            raise ServiceError(resp.get("error", "approvals.pending failed"))
        await self.send_notices(resp.get("notices") or [])
        for item in resp.get("items", []):
            approval_id = item["id"]
            if approval_id in self._sent:
                continue
            # The summary may be up to ~3300 characters; it is never cut (ampi-tools refuses bigger ones).
            text = request_text(item)
            buttons = buttons_for(item)
            try:
                sent = await self.bot.api.send_buttons(self.bot.profile.owner_chat_id, text, buttons)
            except TelegramError as e:
                log.warning("could not send approval request #%s: %s", approval_id, e)
                if e.error_code is not None and 400 <= e.error_code < 500 and e.error_code != 429:
                    # Permanent (e.g. a bad request): don't retry every 3 s; tell the owner without buttons.
                    self._sent.add(approval_id)
                    await self.bot.notify(f"⚠️ Approval request #{approval_id} couldn't be shown ({e.description}); "
                                          "it was not carried out.")
                continue  # 429 / network: try again on the next tick
            self._sent.add(approval_id)
            if item.get("social"):
                self.bot.store.add_social_message(self.bot.profile.owner_chat_id, sent.get("message_id"),
                                                  sent.get("text") or text)
            if isinstance(item.get("confirm_keys"), list):
                self._confirm_keys[approval_id] = {k for k in item["confirm_keys"] if isinstance(k, str)}
            notified = await self.ask(
                self.socket, {"cmd": "approvals.notified", "id": approval_id, "message_id": sent.get("message_id")}
            )
            if not notified.get("ok"):
                log.warning("ampi-tools didn't record request #%s as shown: %s", approval_id, notified.get("error"))

    async def answer_quietly(self, cq: dict, text: str | None = None) -> None:
        try:
            await self.bot.api.answer_callback_query(cq["id"], text)
        except (TelegramError, KeyError):
            pass

    async def handle_callback(self, cq: dict) -> None:
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        if user not in self.bot.profile.owner_ids:
            log.warning("approval button pressed by a stranger (id=%s); ignored", user)
            await self.answer_quietly(cq)
            return
        if chat.get("type") != "private" or chat.get("id") != self.bot.profile.owner_chat_id:
            await self.answer_quietly(cq, "Not an approval message.")
            return
        if message.get("edit_date"):
            # The relay never edits a request before it is answered, so an edited one was tampered with.
            await self.answer_quietly(cq, "This message was changed; ignored.")
            log.warning("approval button on an edited message (id=%s); ignored", message.get("message_id"))
            return
        try:
            _, raw_id, nonce, answer = (cq.get("data") or "").split(":")
            approval_id = int(raw_id)
        except ValueError:
            await self.answer_quietly(cq)
            return
        choice = answer[1:] if answer.startswith("x") else None
        if choice is not None and not CHOICE_KEY.fullmatch(choice):
            await self.answer_quietly(cq)
            return
        if choice is None and answer not in ("y", "a", "n"):  # anything unexpected is ignored, never taken as an answer
            await self.answer_quietly(cq)
            return
        confirmed = False
        if choice is not None:
            now = self._clock()
            armed = self._armed.get((approval_id, choice))
            confirmed = armed is not None and now - armed <= CONFIRM_WINDOW
            if choice in self._confirm_keys.get(approval_id, set()) and not confirmed:
                self._armed[(approval_id, choice)] = now
                await self.answer_quietly(cq, f"🔴 High risk: tap the same button again within {CONFIRM_WINDOW} s "
                                              "to confirm.")
                return
            self._armed.pop((approval_id, choice), None)
            approve = choice != "reject"
        else:
            approve = answer in ("y", "a")
        await self.answer_quietly(cq, "Working on it…" if approve else "Rejected")
        payload = {"cmd": "approvals.decide", "id": approval_id, "approve": approve, "nonce": nonce,
                   "message_id": message.get("message_id"), "always": answer == "a"}
        if choice is not None:
            payload.update(choice=choice, confirmed=confirmed)
        try:
            resp = await self.ask(self.socket, payload, timeout=DECIDE_TIMEOUT)
        except ServiceUnavailable:
            # Nothing was sent, so nothing happened: keep the buttons so the owner can tap again.
            await self.bot.notify(f"⚠️ The tools service isn't reachable right now; request #{approval_id} is "
                                  "still waiting. Tap the button again in a minute.")
            return
        except ServiceError as e:
            resp = {"ok": False, "error": f"no answer from the tools service ({e}); the outcome is unknown. "
                                          "Ask me to check `approvals` before trying again."}
        if resp.get("need_confirm") and choice is not None:
            # The relay didn't know this needs a second tap (e.g. it restarted): nothing happened yet.
            self._armed[(approval_id, choice)] = self._clock()
            await self.bot.notify(f"🔴 Request #{approval_id} is high risk: tap the same button again within "
                                  f"{CONFIRM_WINDOW} s to confirm.")
            return
        if resp.get("ok") and resp.get("watcher"):
            # A watcher was turned on (or its action decided): let the engine pick it up now, not in a minute.
            ui = getattr(self.bot, "watch_ui", None)
            if ui is not None:
                ui.engine.mark_dirty()
                asyncio.get_running_loop().create_task(ui.engine.sync_soon())
        if not resp.get("ok"):
            outcome = f"⚠️ Request #{approval_id}: {resp.get('error', 'failed')}"
        else:
            mark = {"done": "✅ Done", "rejected": "❌ Rejected", "failed": "⚠️ Failed"}.get(
                resp.get("status", ""), "ℹ️"
            )
            outcome = f"{mark} (request #{approval_id})\n{resp.get('text', '')}".strip()
        outcome = fit(outcome, 1500)
        original = message.get("text") or ""
        text = fit(original, MAX_MESSAGE - utf16_len(outcome) - 4) + "\n\n" + outcome
        # A request from social mode quotes strangers (captions, names, comments): not in the logs later runs read.
        social = bool(resp.get("social")) or "Asked for by a social-mode conversation" in original \
            or self.bot.store.is_social_message(chat["id"], message.get("message_id"))
        if social:  # forwarded later, the answered request is recognised too
            self.bot.store.add_social_message(chat["id"], message.get("message_id"), text)
        else:
            log_dir = chatlog.conversation_dir(self.bot.profile.workspace, self.bot.profile.owner_chat_id, 0, None,
                                               None)
            chatlog.append(log_dir, "Approvals", text)
        try:
            await self.bot.api.edit_message_text(chat["id"], message.get("message_id"), text)
        except TelegramError:
            await self.bot._send_text(chat["id"], 0, outcome, markdown=False, **({"social": True} if social else {}))


def callback_is_approval(cq: dict[str, Any]) -> bool:
    return (cq.get("data") or "").startswith("ap:")
