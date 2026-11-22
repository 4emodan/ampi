"""/info in the owner's private chat: what's stored in personal info (masked) and the "always for this site"
permissions, with a button to remove each. Values never pass through here; entries are managed on the server."""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from typing import Any

from .services import ServiceError, ask

log = logging.getLogger(__name__)
BUTTON_TTL = 3600


class InfoUI:
    def __init__(self, bot: Any, tools_socket: str, ask_fn=ask):
        self.bot = bot
        self.socket = tools_socket
        self.ask = ask_fn
        self.buttons: dict[str, tuple[str, str, int | None, float]] = {}  # nonce -> (origin, fields, message, at)

    async def cmd_info(self, chat_id: int, thread_id: int) -> None:
        try:
            resp = await self.ask(self.socket, {"cmd": "info.overview"}, timeout=20)
        except ServiceError as e:
            await self.bot._send_text(chat_id, thread_id, f"Personal info isn't reachable: {e}", markdown=False)
            return
        if not resp.get("ok"):
            await self.bot._send_text(chat_id, thread_id, f"Personal info: {resp.get('error')}", markdown=False)
            return
        lines = ["Personal info (masked; values never leave the server)", ""] + [f"• {x}" for x in resp.get("lines", [])]
        grants = resp.get("grants") or []
        lines += ["", "Filled without asking (30 days):" if grants else "No sites may fill your details without asking."]
        rows, made = [], []
        for g in grants[:20]:
            until = time.strftime("%Y-%m-%d", time.gmtime(g["expires"]))
            lines.append(f"• {g['origin']}: {g['fields']} (until {until})")
            nonce = secrets.token_hex(6)
            made.append((nonce, g["origin"], g["fields"]))
            rows.append([{"text": f"🗑 {g['origin'][:40]}", "callback_data": f"pi:{nonce}"}])
        lines += ["", "Add or change entries on the server: ampi-tools info-set NAME (see docs, section 8e)."]
        text = "\n".join(lines)[:3900]
        if not rows:
            await self.bot._send_text(chat_id, thread_id, text, markdown=False)
            return
        sent = await self.bot.api.send_buttons(chat_id, text, rows)
        now = time.time()
        for k in [k for k, v in self.buttons.items() if now - v[3] > BUTTON_TTL]:
            self.buttons.pop(k, None)
        for nonce, origin, fields in made:  # valid only on the message they were sent with
            self.buttons[nonce] = (origin, fields, sent.get("message_id"), now)

    async def handle_callback(self, cq: dict) -> None:
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        reply = None
        try:
            if user not in self.bot.profile.owner_ids or (message.get("chat") or {}).get("type") != "private":
                return
            entry = self.buttons.pop((cq.get("data") or "")[3:], None)
            if entry is None or time.time() - entry[3] > BUTTON_TTL or entry[2] != message.get("message_id"):
                reply = "These buttons expired; send /info again."
                return
            origin, fields, _, _ = entry
            try:
                resp = await self.ask(self.socket, {"cmd": "info.revoke_grant", "origin": origin, "fields": fields},
                                      timeout=20)
            except ServiceError as e:
                reply = f"Failed: {e}"
                return
            reply = "Removed" if resp.get("ok") and resp.get("removed") else "Already gone"
            with contextlib.suppress(Exception):
                await self.bot.api.edit_message_text(
                    self.bot.profile.owner_chat_id, message["message_id"],
                    f"{message.get('text', '')}\n\n→ removed: {origin} ({fields})"[:4000])
        finally:
            with contextlib.suppress(Exception):
                await self.bot.api.answer_callback_query(cq["id"], reply)
