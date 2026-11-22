"""/watchers and /watcher NAME in Telegram, with Pause / Resume / Run now / Delete buttons for the owner."""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from typing import Any

from ampi_tools import watch_spec as ws

from .control import list_text, show_text

log = logging.getLogger(__name__)

BUTTON_TTL = 3600


class WatchUI:
    def __init__(self, bot: Any, engine: Any):
        self.bot = bot
        self.engine = engine
        self.buttons: dict[str, tuple[str, str, int | None, float]] = {}  # nonce -> (name, action, message, at)

    async def cmd_watchers(self, chat_id: int, thread_id: int) -> None:
        text = await list_text(self.engine)
        await self.bot._send_text(chat_id, thread_id, "Watchers\n\n" + text +
                                  "\n\nDetails and buttons: /watcher NAME", markdown=False)

    async def cmd_watcher(self, chat_id: int, thread_id: int, name: str) -> None:
        name = name.strip()
        if not ws.NAME_RE.fullmatch(name):
            await self.bot._send_text(chat_id, thread_id, "Which one? /watcher NAME (see /watchers)", markdown=False)
            return
        text = await show_text(self.engine, name)
        w = self.engine.watchers.get(name)
        actions = []
        if w is not None:
            actions.append(("pause", "⏸ Pause") if w.status == "active" else ("resume", "▶️ Resume"))
            actions.append(("run", "⚡ Run now"))
            actions.append(("delete", "🗑 Delete"))
        if not actions:
            await self.bot._send_text(chat_id, thread_id, text, markdown=False)
            return
        row, made = [], []
        for action, label in actions:
            nonce = secrets.token_hex(6)
            made.append((nonce, action))
            row.append({"text": label, "callback_data": f"wt:{nonce}"})
        sent = await self.bot.api.send_buttons(chat_id, text[:3900], [row])
        now = time.time()
        for k in [k for k, v in self.buttons.items() if now - v[3] > BUTTON_TTL]:
            self.buttons.pop(k, None)
        for nonce, action in made:  # only valid on the message they were sent with
            self.buttons[nonce] = (name, action, sent.get("message_id"), now)

    async def handle_callback(self, cq: dict) -> None:
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        reply = None
        try:
            if user not in self.bot.profile.owner_ids or (message.get("chat") or {}).get("type") != "private":
                return
            nonce = (cq.get("data") or "")[3:]
            entry = self.buttons.get(nonce)
            if entry is None or time.time() - entry[3] > BUTTON_TTL or entry[2] != message.get("message_id"):
                reply = "These buttons expired; send /watcher NAME again."
                return
            name, action, _, _ = entry
            e = self.engine
            if action == "pause":
                r = await e.tools({"cmd": "watch.status", "name": name, "status": "paused"})
                reply = "Paused" if r.get("ok") else f"Failed: {r.get('error')}"
            elif action == "resume":
                r = await e.tools({"cmd": "watch.status", "name": name, "status": "active"})
                reply = "Resumed" if r.get("ok") else f"Failed: {r.get('error')}"
            elif action == "run":
                reply = e.manual(name, "the owner (button)", owner=True)
            elif action == "delete":
                confirm = secrets.token_hex(6)
                self.buttons[confirm] = (name, "delete!", None, time.time())
                sent = await self.bot.api.send_buttons(
                    self.bot.profile.owner_chat_id, f"Delete the watcher {name}? Its grants end; its history is kept.",
                    [[{"text": "🗑 Yes, delete", "callback_data": f"wt:{confirm}"}]])
                self.buttons[confirm] = (name, "delete!", sent.get("message_id"), time.time())
                reply = "Confirm below"
            elif action == "delete!":
                r = await e.tools({"cmd": "watch.delete", "name": name})
                reply = "Deleted" if r.get("ok") else f"Failed: {r.get('error')}"
            for k in [k for k, v in self.buttons.items() if v[0] == name and v[2] == message.get("message_id")]:
                self.buttons.pop(k, None)
            e.mark_dirty()
            await e.sync()
            with contextlib.suppress(Exception):
                await self.bot.api.edit_message_text(self.bot.profile.owner_chat_id, message["message_id"],
                                                     f"{message.get('text', '')}\n\n→ {reply}"[:4000])
        finally:
            with contextlib.suppress(Exception):
                await self.bot.api.answer_callback_query(cq["id"], reply)
