"""One Telegram bot = one profile. Routes messages to Claude sessions and sends replies back."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import chatlog
from .chatlog import CHATS_DIR
from .claude_runner import ClaudeRunner, RunResult
from .config import Group, Profile, Watcher
from .store import Conversation, Store, now_iso
from .telegram import TelegramError
from .text import markdown_to_html, split_markdown

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API download limit on Telegram's servers
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
SEND_FILE_RE = re.compile(r"^\s*SEND_FILE:\s*(.+?)\s*$", re.M)
NO_REPLY = "NO_REPLY"  # in groups, Claude answers this when there is nothing to say

COMMANDS = [
    {"command": "new", "description": "Start a new conversation (new topic)"},
    {"command": "reset", "description": "Forget this conversation's context"},
    {"command": "cancel", "description": "Stop the current task"},
    {"command": "model", "description": "Show or set the model for this conversation"},
    {"command": "status", "description": "Show this conversation's status"},
    {"command": "help", "description": "How to use this assistant"},
]

HELP_TEXT = (
    "Write to me like in any chat. Each topic is a separate conversation with its own memory of what was said.\n\n"
    "/new [title] – start a new conversation\n"
    "/reset – forget this conversation's context\n"
    "/cancel – stop the current task\n"
    "/model [name|default] – show or change the model here\n"
    "/status – session info and usage\n\n"
    "You can send photos and files; I save them to my workspace and can read them."
)


def bridge_system_prompt(profile: Profile) -> str:
    return (
        f'You are a personal assistant reached through a Telegram bot (profile "{profile.name}"). '
        "Your replies are shown in Telegram: keep them concise and use simple Markdown only "
        "(bold, italic, inline code, code blocks, links, bullet lists). Tables do not render; use lists instead.\n"
        "Files the user sends are saved under inbox/ in your working directory; the message lists their paths. "
        "You can read images and text files directly. You cannot listen to audio or watch video; say so if "
        "asked, unless a tool in this workspace can convert them.\n"
        "To send a file to the user, put a line `SEND_FILE: <path relative to the working directory>` "
        "on its own line in your reply. Only files inside the working directory can be sent.\n"
        "Nobody can approve tool permissions or answer questions while you work: if something is denied or you "
        "need a decision, finish and say what you need in your reply.\n\n"
        + _conversations_prompt(profile)
    )


TELEGRAM_SKILL = Path(".claude/skills/telegram/SKILL.md")  # installed by deploy/install-tg-reader.sh


def _conversations_prompt(profile: Profile) -> str:
    lines = [
        "You talk to the owner in several places, each with its own session. Every message there and every "
        f"reply of yours is logged under {CHATS_DIR}/ in your working directory, one Markdown file per day "
        "(YYYY-MM-DD.md, entries in time order):",
        f"- {CHATS_DIR}/private/main/ – the owner's private chat with you; "
        f"{CHATS_DIR}/private/topic-*/ – its topics",
    ]
    for g in profile.groups:
        lines.append(
            f"- {CHATS_DIR}/{g.name}/ – the Telegram group \"{g.name}\", where you act on everything the owner "
            f"posts or forwards (your instructions there: groups/{g.name}.md)"
        )
    for w in profile.watchers:
        lines.append(
            f"- {CHATS_DIR}/watch-{w.name}/ – runs of your watcher \"{w.name}\" ({w.kind}"
            + (f": {w.query}" if w.query else "")
            + f"; instructions: watchers/{w.name}.md). Its reports to the owner are also in the main chat log."
        )
    example = profile.groups[0].name if profile.groups else "the other chat"
    if (profile.workspace / TELEGRAM_SKILL).is_file():
        history = (
            "These logs only start when logging began and only cover chats with you. For anything else "
            "(older messages, any other chat, group or channel of the owner) use the telegram skill "
            "(`tg-read`), which reads the owner's own Telegram account."
        )
    else:
        history = (
            "They are the only history you have: Telegram doesn't let you fetch older messages, so anything "
            "from before logging started is not available."
        )
    lines.append(
        f"When the owner refers to another conversation or a group (e.g. \"read the last 100 messages in "
        f"{example}\"), read those logs: newest files first, Grep to search. {history} Notes you saved "
        "(notes/) are shared by all conversations."
    )
    return "\n".join(lines)


class TelegramLike(Protocol):
    async def get_me(self) -> dict: ...
    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]: ...
    async def set_my_commands(self, commands: list[dict]) -> None: ...
    async def send_message(
        self, chat_id: int, text: str, thread_id: int | None = None, parse_mode: str | None = None
    ) -> dict: ...
    async def send_chat_action(self, chat_id: int, action: str, thread_id: int | None = None) -> None: ...
    async def create_forum_topic(self, chat_id: int, name: str) -> dict: ...
    async def get_file(self, file_id: str) -> dict: ...
    async def download_file(self, file_path: str, dest: Path) -> None: ...
    async def send_file(
        self, chat_id: int, path: Path, thread_id: int | None = None, as_photo: bool = False
    ) -> dict: ...


Key = tuple[int, int]  # (chat_id, thread_id)


class ProfileBot:
    def __init__(self, profile: Profile, api: TelegramLike, store: Store, runner: ClaudeRunner):
        self.profile = profile
        self.api = api
        self.store = store
        self.runner = runner
        self.system_prompt = bridge_system_prompt(profile)
        self._locks: dict[Key, asyncio.Lock] = {}
        self._running: dict[Key, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[Key, list[dict]] = {}  # group messages waiting to be handled as one batch
        self._flush_timers: dict[Key, asyncio.Task] = {}
        self.username: str | None = None
        self.approvals: Any = None  # ApprovalRelay, set by main when [tools] approvals = true

    # --- polling --------------------------------------------------------------------

    async def poll_forever(self) -> None:
        me = await self.api.get_me()
        self.username = me.get("username")
        log.info("[%s] connected as @%s", self.profile.name, me.get("username"))
        with contextlib.suppress(TelegramError):
            await self.api.set_my_commands(COMMANDS)
        offset = self.store.get_offset(self.profile.name)
        while True:
            try:
                updates = await self.api.get_updates(offset)
            except TelegramError as e:
                log.warning("[%s] getUpdates failed: %s", self.profile.name, e)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.store.set_offset(self.profile.name, offset)
                message = update.get("message")
                if message:
                    self._spawn(self.handle_message(message))
                callback = update.get("callback_query")
                if callback and self.approvals is not None and (callback.get("data") or "").startswith("ap:"):
                    self._spawn(self.approvals.handle_callback(callback))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("[%s] message handler failed", self.profile.name, exc_info=t.exception())

        task.add_done_callback(done)
        return task

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- message handling -------------------------------------------------------------

    async def handle_message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        chat_type = chat.get("type")
        is_owner = user.get("id") in self.profile.owner_ids

        group: Group | None = None
        if chat_type in ("group", "supergroup"):
            if msg.get("migrate_to_chat_id"):
                log.warning(
                    "[%s] group %s was upgraded to a supergroup with chat_id=%s; update chat_id in config.toml",
                    self.profile.name, chat.get("id"), msg["migrate_to_chat_id"],
                )
                return
            group = self.profile.group(chat.get("id"))
            if group is None:
                if is_owner:
                    log.warning(
                        "[%s] message in a group that is not in the config: chat_id=%s title=%r "
                        "(add it under [[profiles.groups]] to use it)",
                        self.profile.name, chat.get("id"), chat.get("title"),
                    )
                return
        elif chat_type != "private":
            return

        if not is_owner:
            log.warning(
                "[%s] ignored message from unauthorized user id=%s username=%s",
                self.profile.name,
                user.get("id"),
                user.get("username"),
            )
            return

        chat_id = chat["id"]
        thread_id = msg.get("message_thread_id", 0) if msg.get("is_topic_message") else 0
        key = (chat_id, thread_id)
        text = (msg.get("text") or msg.get("caption") or "").strip()

        if text.startswith("/") and not msg.get("forward_origin"):
            command, _, args = text[1:].partition(" ")
            command, _, target = command.partition("@")
            if target and self.username and target.lower() != self.username.lower():
                return  # a command for another bot in the same group
            handler = getattr(self, f"_cmd_{command.lower()}", None)
            if handler is not None:
                await handler(chat_id, thread_id, args.strip())
                return

        if group is not None:
            self._queue_group_message(key, group, msg, chat.get("title") or group.name)
            return
        await self._process(key, [msg], None, "")

    def _queue_group_message(self, key: Key, group: Group, msg: dict, title: str) -> None:
        """Collect group messages; handle them together once none has arrived for batch_seconds."""
        self._pending.setdefault(key, []).append(msg)
        timer = self._flush_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        self._flush_timers[key] = self._spawn(self._flush_after(key, group, title))

    async def _flush_after(self, key: Key, group: Group, title: str) -> None:
        await asyncio.sleep(group.batch_seconds)
        # From here on this task is no longer a timer: new messages start a new batch instead of cancelling it.
        if self._flush_timers.get(key) is asyncio.current_task():
            del self._flush_timers[key]
        msgs = self._pending.pop(key, [])
        if msgs:
            await self._process(key, msgs, group, title)

    async def _process(self, key: Key, msgs: list[dict], group: Group | None, title: str) -> None:
        chat_id, thread_id = key
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            bodies = []
            for m in msgs:
                body = await self._build_prompt(m)
                if body:
                    bodies.append((m, body))
            if not bodies:
                return
            prompt = _join_items([b for _, b in bodies])
            if group is not None and group.fresh_session:
                conv = self.store.reset_session(self.profile.name, chat_id, thread_id)
            else:
                conv = self.store.get_or_create(self.profile.name, chat_id, thread_id)
            log_dir = chatlog.conversation_dir(
                self.profile.workspace, chat_id, thread_id, group.name if group else None, conv.title
            )
            for m, body in bodies:
                chatlog.append(log_dir, "Owner", body, chatlog.message_time(m))
            system_prompt = self.system_prompt
            if group is not None:
                system_prompt += "\n\n" + self._group_prompt(group, title)
            self._running[key] = asyncio.current_task()  # type: ignore[assignment]
            typing = asyncio.create_task(self._keep_typing(chat_id, thread_id))
            started_at = now_iso()
            try:
                result = await self.runner.run_turn(
                    prompt,
                    cwd=self.profile.workspace,
                    session_id=conv.session_id,
                    started=conv.started,
                    model=conv.model or self.profile.model,
                    allowed_tools=self.profile.allowed_tools,
                    append_system_prompt=system_prompt,
                    extra_args=self.profile.extra_args,
                )
            except asyncio.CancelledError:
                self._record(conv, started_at, RunResult(ok=False, text="", error="cancelled"), prompt)
                chatlog.append(log_dir, "Assistant", "(stopped by /cancel)")
                with contextlib.suppress(Exception):
                    await self._send_text(chat_id, thread_id, "⏹ Stopped.", markdown=False)
                return
            finally:
                typing.cancel()
                self._running.pop(key, None)

            if result.ok or result.text:
                # Claude Code created the session; later turns resume it.
                self.store.mark_started(self.profile.name, chat_id, thread_id, conv.session_id)
            self._record(conv, started_at, result, prompt)
            reply = (result.text or "").strip()
            if not result.ok:
                reply = (reply + "\n\n" if reply else "") + f"⚠️ {result.error or 'error'}"
            if reply and reply != NO_REPLY:
                chatlog.append(log_dir, "Assistant", reply)
            await self._deliver(chat_id, thread_id, result, quiet_ok=group is not None)

    # --- watchers (see watchers.py) ------------------------------------------------------

    async def notify(self, text: str) -> None:
        """A short plain message to the owner's private chat."""
        try:
            await self._send_text(self.profile.owner_chat_id, 0, text, markdown=False)
        except TelegramError as e:
            log.warning("[%s] could not notify the owner: %s", self.profile.name, e)

    async def run_watcher(self, watcher: Watcher, prompt: str) -> None:
        """One Claude run for a watcher event, in its own fresh session; the reply goes to the owner."""
        chat_id = self.profile.owner_chat_id
        key = (chat_id, watcher_thread_id(watcher.name))
        async with self._locks.setdefault(key, asyncio.Lock()):
            conv = self.store.reset_session(self.profile.name, *key)
            log_dir = chatlog.conversation_dir(self.profile.workspace, chat_id, 0, f"watch-{watcher.name}", None)
            chatlog.append(log_dir, "Watcher", prompt)
            started_at = now_iso()
            result = await self.runner.run_turn(
                prompt,
                cwd=self.profile.workspace,
                session_id=conv.session_id,
                started=False,
                model=self.profile.model,
                allowed_tools=self.profile.allowed_tools,
                append_system_prompt=self.system_prompt + "\n\n" + self._watcher_prompt(watcher),
                extra_args=self.profile.extra_args,
            )
            if result.ok or result.text:
                self.store.mark_started(self.profile.name, *key, conv.session_id)
            self._record(conv, started_at, result, prompt)
            reply = (result.text or "").strip()
            if result.ok and reply == NO_REPLY:
                return
            shown = reply if result.ok else (reply + "\n\n" if reply else "") + f"⚠️ {result.error or 'error'}"
            chatlog.append(log_dir, "Assistant", shown)
            main_dir = chatlog.conversation_dir(self.profile.workspace, chat_id, 0, None, None)
            chatlog.append(main_dir, f"Assistant (watcher {watcher.name})", shown)
            prefixed = replace(result, text=f"🔔 **{watcher.name}**\n\n{reply}" if reply else reply)
            await self._deliver(chat_id, 0, prefixed)

    def _watcher_prompt(self, watcher: Watcher) -> str:
        path = watcher.instructions_path(self.profile.workspace)
        rel = path.relative_to(self.profile.workspace).as_posix()
        try:
            instructions = path.read_text(encoding="utf-8").strip()
        except OSError:
            instructions = ""
        if not instructions:
            instructions = f"(No instructions yet: {rel} is missing.) Tell the owner briefly what is new."
        return (
            f'This run was started by your watcher "{watcher.name}" ({watcher.kind}), not by a message from '
            "the owner. Your reply is sent to the owner's private chat as a notification, so make it short "
            "and worth reading. Changes that need approval (sending, deleting, changing Todoist tasks) "
            "become Approve/Reject buttons for the owner; batch them into one request. Your instructions "
            f"for this watcher (from {rel}; edit that file when the owner asks you to change them):\n\n"
            f"{instructions}\n\n"
            f"If there is nothing worth telling the owner, reply with exactly {NO_REPLY} and nothing else."
        )

    def _group_prompt(self, group: Group, title: str) -> str:
        path = group.instructions_path(self.profile.workspace)
        rel = path.relative_to(self.profile.workspace).as_posix()
        try:
            instructions = path.read_text(encoding="utf-8").strip()
        except OSError:
            instructions = ""
        if not instructions:
            instructions = (
                f"(No instructions yet; {rel} is missing or empty.) Save each item to notes/{group.name}.md "
                "with the date, a one-line summary and the source link, and reply with that one-line summary."
            )
        return (
            f'This run handles messages from the Telegram group "{title}". The owner posts or forwards '
            "things there for you to act on; each message below is one item. "
            f"Your instructions for this group (from {rel}; edit that file when the owner asks you to change "
            "how you handle this group):\n\n"
            f"{instructions}\n\n"
            f"If there is nothing worth telling the owner, reply with exactly {NO_REPLY} and nothing else."
        )

    def _record(self, conv: Conversation, started_at: str, result: RunResult, prompt: str) -> None:
        self.store.record_run(
            conv,
            started_at=started_at,
            ok=result.ok,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
            error=result.error,
            prompt=prompt,
        )

    async def _keep_typing(self, chat_id: int, thread_id: int) -> None:
        while True:
            with contextlib.suppress(TelegramError):
                await self.api.send_chat_action(chat_id, "typing", thread_id)
            await asyncio.sleep(4.5)

    # --- incoming content ---------------------------------------------------------------

    async def _build_prompt(self, msg: dict) -> str:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        parts: list[str] = []
        forwarded = _forward_note(msg)
        if forwarded:
            parts.append(forwarded)
        reply = msg.get("reply_to_message") or {}
        quoted = (reply.get("text") or reply.get("caption") or "").strip()
        if quoted and not reply.get("forum_topic_created"):
            parts.append(f"[Replying to: {quoted[:1000]}]")
        if text:
            parts.append(text)
        links = _hidden_links(msg)
        if links:
            parts.append("[Links in the message:]\n" + "\n".join(f"- {u}" for u in links))

        location = msg.get("location")
        if location:
            parts.append(f"[Shared location: {location.get('latitude')}, {location.get('longitude')}]")
        contact = msg.get("contact")
        if contact:
            name = " ".join(filter(None, [contact.get("first_name"), contact.get("last_name")]))
            parts.append(f"[Shared contact: {name}, {contact.get('phone_number')}]")

        notes = await self._save_attachments(msg)
        if notes:
            parts.append("[Attachments saved in your working directory:]\n" + "\n".join(notes))
        if parts == [forwarded]:
            return ""  # a forward with nothing we can read (e.g. a sticker)
        return "\n\n".join(parts).strip()

    async def _save_attachments(self, msg: dict) -> list[str]:
        stamp = datetime.now().strftime("%Y-%m-%d/%H%M%S")
        specs: list[tuple[dict, str, str]] = []  # (file object, file name, kind)
        if msg.get("photo"):
            photo = msg["photo"][-1]  # largest size
            specs.append((photo, f"photo_{photo.get('file_unique_id', 'x')}.jpg", "image"))
        for field, kind, default_ext in (
            ("document", "file", ""),
            ("voice", "voice message", ".ogg"),
            ("audio", "audio", ".mp3"),
            ("video", "video", ".mp4"),
            ("video_note", "video message", ".mp4"),
            ("animation", "animation", ".mp4"),
        ):
            obj = msg.get(field)
            if obj:
                name = obj.get("file_name") or f"{field}_{obj.get('file_unique_id', 'x')}{default_ext}"
                specs.append((obj, name, kind))

        notes = []
        for obj, name, kind in specs:
            rel = Path("inbox") / f"{stamp}_{_safe_name(name)}"
            size = obj.get("file_size") or 0
            if size > MAX_DOWNLOAD_BYTES:
                notes.append(f"- {name} ({kind}, {size // 1_000_000} MB): too large to download through the Bot API")
                continue
            try:
                info = await self.api.get_file(obj["file_id"])
                await self.api.download_file(info["file_path"], self.profile.workspace / rel)
            except (TelegramError, KeyError) as e:
                log.warning("[%s] could not download %s: %s", self.profile.name, name, e)
                notes.append(f"- {name} ({kind}): download failed")
                continue
            notes.append(f"- {rel.as_posix()} ({kind})")
        return notes

    # --- outgoing content ---------------------------------------------------------------

    async def _deliver(self, chat_id: int, thread_id: int, result: RunResult, quiet_ok: bool = False) -> None:
        text = result.text or ""
        files = [m.group(1).strip("`'\"") for m in SEND_FILE_RE.finditer(text)]
        text = SEND_FILE_RE.sub("", text).strip()
        if quiet_ok and result.ok and text == NO_REPLY:
            text = ""
            if not files:
                return

        if not result.ok:
            reason = (result.error or "unknown error").strip()
            text = (text + "\n\n" if text and text != reason else "") + f"⚠️ {reason[:1500]}"
        elif not text and not files:
            text = "(no reply)"
        if text:
            await self._send_text(chat_id, thread_id, text, markdown=result.ok)

        for name in files:
            await self._send_workspace_file(chat_id, thread_id, name)

    async def _send_text(self, chat_id: int, thread_id: int, text: str, markdown: bool = True) -> None:
        for chunk in split_markdown(text):
            if markdown:
                try:
                    await self.api.send_message(chat_id, markdown_to_html(chunk), thread_id, parse_mode="HTML")
                    continue
                except TelegramError as e:
                    if e.error_code != 400:
                        raise
                    log.info("[%s] HTML rejected (%s), sending plain text", self.profile.name, e.description)
            await self.api.send_message(chat_id, chunk, thread_id)

    async def _send_workspace_file(self, chat_id: int, thread_id: int, name: str) -> None:
        root = self.profile.workspace.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            await self._send_text(chat_id, thread_id, f"⚠️ Not sending {name}: it is outside the workspace.", False)
            return
        if not path.is_file():
            await self._send_text(chat_id, thread_id, f"⚠️ File not found: {name}", False)
            return
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            await self._send_text(chat_id, thread_id, f"⚠️ {name} is too large for Telegram (max 50 MB).", False)
            return
        as_photo = path.suffix.lower() in PHOTO_SUFFIXES and size <= MAX_PHOTO_BYTES
        try:
            await self.api.send_file(chat_id, path, thread_id, as_photo=as_photo)
        except TelegramError as e:
            await self._send_text(chat_id, thread_id, f"⚠️ Could not send {name}: {e.description}", False)

    # --- commands ---------------------------------------------------------------------

    async def _cmd_start(self, chat_id: int, thread_id: int, args: str) -> None:
        await self._send_text(chat_id, thread_id, HELP_TEXT, markdown=False)

    _cmd_help = _cmd_start

    async def _cmd_new(self, chat_id: int, thread_id: int, args: str) -> None:
        title = args or datetime.now().strftime("Chat %d %b %H:%M")
        if chat_id < 0:  # a group: topics there are the group's own business
            self.store.reset_session(self.profile.name, chat_id, thread_id)
            await self._send_text(chat_id, thread_id, "Started a fresh conversation here.", markdown=False)
            return
        try:
            topic = await self.api.create_forum_topic(chat_id, title)
        except TelegramError as e:
            log.info("[%s] createForumTopic failed (%s); resetting current chat", self.profile.name, e.description)
            self.store.reset_session(self.profile.name, chat_id, thread_id)
            await self._send_text(
                chat_id,
                thread_id,
                "Started a fresh conversation here. To keep several conversations side by side, "
                "turn on topics for this bot in @BotFather.",
                markdown=False,
            )
            return
        new_thread = topic["message_thread_id"]
        self.store.get_or_create(self.profile.name, chat_id, new_thread, title=title)
        await self._send_text(chat_id, new_thread, f"New conversation: {title}", markdown=False)

    async def _cmd_reset(self, chat_id: int, thread_id: int, args: str) -> None:
        if (chat_id, thread_id) in self._running:
            await self._send_text(chat_id, thread_id, "A task is still running here. Use /cancel first.", False)
            return
        self.store.reset_session(self.profile.name, chat_id, thread_id)
        await self._send_text(chat_id, thread_id, "Context cleared. The next message starts fresh.", False)

    async def _cmd_cancel(self, chat_id: int, thread_id: int, args: str) -> None:
        task = self._running.get((chat_id, thread_id))
        if task is None:
            await self._send_text(chat_id, thread_id, "Nothing is running here.", markdown=False)
            return
        task.cancel()

    async def _cmd_model(self, chat_id: int, thread_id: int, args: str) -> None:
        name = self.profile.name
        if not args:
            conv = self.store.get_or_create(name, chat_id, thread_id)
            current = conv.model or self.profile.model or "Claude Code default"
            await self._send_text(chat_id, thread_id, f"Model here: {current}", markdown=False)
            return
        if not re.fullmatch(r"[A-Za-z0-9._\[\]-]{1,64}", args):
            await self._send_text(chat_id, thread_id, "That doesn't look like a model name.", markdown=False)
            return
        model = None if args.lower() == "default" else args
        self.store.set_model(name, chat_id, thread_id, model)
        shown = model or self.profile.model or "Claude Code default"
        await self._send_text(chat_id, thread_id, f"Model for this conversation: {shown}", markdown=False)

    async def _cmd_status(self, chat_id: int, thread_id: int, args: str) -> None:
        conv = self.store.get_or_create(self.profile.name, chat_id, thread_id)
        stats = self.store.run_stats(self.profile.name, chat_id, thread_id)
        lines = [
            f"Profile: {self.profile.name}",
            f"Conversation: {conv.title or ('main chat' if thread_id == 0 else f'topic {thread_id}')}",
            f"Session: {conv.session_id[:8]}… ({'active' if conv.started else 'not started'})",
            f"Model: {conv.model or self.profile.model or 'Claude Code default'}",
            f"Running now: {'yes' if (chat_id, thread_id) in self._running else 'no'}",
            f"Runs: {stats.runs} ({stats.failed} failed)",
        ]
        if stats.cost_usd:
            lines.append(f"Estimated API-equivalent cost: ${stats.cost_usd:.2f} (covered by your plan)")
        await self._send_text(chat_id, thread_id, "\n".join(lines), markdown=False)


def watcher_thread_id(name: str) -> int:
    """A stable negative pseudo-topic id for a watcher's conversation (real topic ids are positive)."""
    return -1 - zlib.crc32(name.encode()) % 1_000_000_000


def _join_items(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return "\n\n".join(f"--- Item {i} of {len(items)} ---\n{body}" for i, body in enumerate(items, 1))


def _forward_note(msg: dict) -> str | None:
    origin = msg.get("forward_origin")
    if not origin:
        return None
    kind = origin.get("type")
    link = ""
    if kind == "user":
        u = origin.get("sender_user") or {}
        source = " ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or "a user"
        if u.get("username"):
            source += f" (@{u['username']})"
    elif kind == "hidden_user":
        source = origin.get("sender_user_name") or "a user"
    elif kind in ("chat", "channel"):
        c = origin.get("chat") or origin.get("sender_chat") or {}
        source = c.get("title") or "a chat"
        if c.get("username"):
            source += f" (@{c['username']})"
            if origin.get("message_id"):
                link = f", https://t.me/{c['username']}/{origin['message_id']}"
    else:
        source = "somewhere"
    when = ""
    if isinstance(origin.get("date"), int):
        when = ", originally posted " + datetime.fromtimestamp(origin["date"], timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    return f"[Forwarded from {source}{when}{link}]"


def _hidden_links(msg: dict) -> list[str]:
    """URLs behind link text (text_link entities); plain URLs are already in the text."""
    urls: list[str] = []
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        url = ent.get("url") if ent.get("type") == "text_link" else None
        if url and url not in urls:
            urls.append(url)
    return urls


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", Path(name).name).strip("._")
    return (name or "file")[:100]
