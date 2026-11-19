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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from . import chatlog
from .services import ServiceError, ask
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


def request_text(item: dict) -> str:
    text = f"🔐 Approve? (request #{item['id']})\n\n{item['summary']}"
    if item.get("reason"):
        text += f"\n\nThe assistant says why: {item['reason']}"
    return text


class ApprovalRelay:
    def __init__(self, bot: "ProfileBot", socket: str, ask_fn: Ask = ask, interval: float = 3.0):
        self.bot = bot
        self.socket = socket
        self.ask = ask_fn
        self.interval = interval
        self._sent: set[int] = set()  # requests shown already in this process (never send one twice)
        self._down = False

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

    async def tick(self) -> None:
        resp = await self.ask(self.socket, {"cmd": "approvals.pending"})
        if not resp.get("ok"):
            raise ServiceError(resp.get("error", "approvals.pending failed"))
        for item in resp.get("items", []):
            approval_id = item["id"]
            if approval_id in self._sent:
                continue
            nonce = item.get("nonce") or ""
            # The summary may be up to ~3300 characters; it is never cut (ampi-tools refuses bigger ones).
            text = request_text(item)
            buttons = [[
                {"text": "✅ Approve", "callback_data": f"ap:{approval_id}:{nonce}:y"},
                {"text": "❌ Reject", "callback_data": f"ap:{approval_id}:{nonce}:n"},
            ]]
            try:
                sent = await self.bot.api.send_buttons(self.bot.profile.owner_chat_id, text, buttons)
            except TelegramError as e:
                log.warning("could not send approval request #%s: %s", approval_id, e)
                if e.error_code is not None and 400 <= e.error_code < 500:
                    self._sent.add(approval_id)  # e.g. the owner blocked the bot: don't retry every 3 s
                continue
            self._sent.add(approval_id)
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
        approve = answer == "y"
        await self.answer_quietly(cq, "Working on it…" if approve else "Rejected")
        payload = {"cmd": "approvals.decide", "id": approval_id, "approve": approve, "nonce": nonce,
                   "message_id": message.get("message_id")}
        try:
            resp = await self.ask(self.socket, payload, timeout=DECIDE_TIMEOUT)
        except ServiceError as e:
            resp = {"ok": False, "error": f"no answer from the tools service ({e}); the outcome is unknown. "
                                          "Ask me to check `approvals` before trying again."}
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
        log_dir = chatlog.conversation_dir(self.bot.profile.workspace, self.bot.profile.owner_chat_id, 0, None, None)
        chatlog.append(log_dir, "Approvals", text)
        try:
            await self.bot.api.edit_message_text(chat["id"], message.get("message_id"), text)
        except TelegramError:
            await self.bot._send_text(chat["id"], 0, outcome, markdown=False)


def callback_is_approval(cq: dict[str, Any]) -> bool:
    return (cq.get("data") or "").startswith("ap:")
