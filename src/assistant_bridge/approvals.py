"""Shows ampi-tools approval requests to the owner as Telegram buttons and passes their answer back.

Only this process (the bridge's main process) may answer: ampi-tools checks the caller's PID, so a
`claude` run can queue a request but never approve it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from . import chatlog
from .services import ServiceError, ask
from .telegram import TelegramError

if TYPE_CHECKING:
    from .bot import ProfileBot

log = logging.getLogger(__name__)
Ask = Callable[[str, dict], Awaitable[dict]]
MAX_TEXT = 3800


def request_text(item: dict) -> str:
    text = f"🔐 Approve? (request #{item['id']})\n\n{item['summary']}"
    if item.get("reason"):
        text += f"\n\nWhy: {item['reason']}"
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n[…cut]"
    return text


class ApprovalRelay:
    def __init__(self, bot: "ProfileBot", socket: str, ask_fn: Ask = ask, interval: float = 3.0):
        self.bot = bot
        self.socket = socket
        self.ask = ask_fn
        self.interval = interval
        self._texts: dict[int, str] = {}  # message_id -> request text, to append the outcome
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
            text = request_text(item)
            buttons = [[
                {"text": "✅ Approve", "callback_data": f"ap:{item['id']}:y"},
                {"text": "❌ Reject", "callback_data": f"ap:{item['id']}:n"},
            ]]
            try:
                sent = await self.bot.api.send_buttons(self.bot.profile.owner_chat_id, text, buttons)
            except TelegramError as e:
                log.warning("could not send approval request #%s: %s", item["id"], e)
                continue
            self._texts[sent.get("message_id", 0)] = text
            await self.ask(self.socket, {"cmd": "approvals.notified", "id": item["id"]})

    async def handle_callback(self, cq: dict) -> None:
        user = (cq.get("from") or {}).get("id")
        data = cq.get("data") or ""
        message = cq.get("message") or {}
        if user not in self.bot.profile.owner_ids:
            log.warning("approval button pressed by a stranger (id=%s); ignored", user)
            return
        try:
            _, raw_id, answer = data.split(":")
            approval_id = int(raw_id)
        except ValueError:
            return
        approve = answer == "y"
        try:
            await self.bot.api.answer_callback_query(cq["id"], "Working on it…" if approve else "Rejected")
        except TelegramError:
            pass
        try:
            resp = await self.ask(self.socket, {"cmd": "approvals.decide", "id": approval_id, "approve": approve})
        except ServiceError as e:
            resp = {"ok": False, "error": str(e)}
        if not resp.get("ok"):
            outcome = f"⚠️ {resp.get('error', 'failed')}"
        else:
            mark = {"done": "✅ Done", "rejected": "❌ Rejected", "failed": "⚠️ Failed"}.get(
                resp.get("status", ""), "ℹ️"
            )
            outcome = f"{mark}\n{resp.get('text', '')}".strip()
        original = self._texts.pop(message.get("message_id", 0), None) or message.get("text") or ""
        text = f"{original}\n\n{outcome}"[:4000]
        # Recorded in the main chat's log so later sessions know what was approved and what happened.
        log_dir = chatlog.conversation_dir(self.bot.profile.workspace, self.bot.profile.owner_chat_id, 0, None, None)
        chatlog.append(log_dir, "Approvals", text)
        chat_id = (message.get("chat") or {}).get("id", self.bot.profile.owner_chat_id)
        try:
            await self.bot.api.edit_message_text(chat_id, message.get("message_id"), text)
        except TelegramError:
            await self.bot._send_text(chat_id, 0, outcome, markdown=False)
