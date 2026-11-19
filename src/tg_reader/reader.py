"""Answers read-only requests against a Telethon client.

Only `iter_dialogs` and `iter_messages` are ever called, neither of which marks anything as read.
The client is duck-typed so tests can pass a fake.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_LIMIT = 2000
DIALOG_SCAN = 1000  # how many dialogs to look through when resolving a chat by name


class UserError(Exception):
    """A bad request; the message is shown to the caller as is."""


def parse_when(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC). A bare date used as an upper bound means the end of that day."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise UserError(f"bad date {value!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM (UTC)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        dt += timedelta(days=1)
    return dt


def _limit(value: Any, default: int) -> int:
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise UserError(f"bad limit {value!r}") from None
    if n < 1:
        raise UserError("limit must be at least 1")
    return min(n, MAX_LIMIT)


def display_name(entity: Any) -> str:
    if entity is None:
        return "?"
    title = getattr(entity, "title", None)
    if title:
        return title
    name = " ".join(p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p)
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return str(getattr(entity, "id", "?"))


def _kind(dialog: Any) -> str:
    if getattr(dialog, "is_user", False):
        return "bot" if getattr(getattr(dialog, "entity", None), "bot", False) else "person"
    if getattr(dialog, "is_channel", False) and not getattr(dialog, "is_group", False):
        return "channel"
    return "group"


def _media_note(msg: Any) -> str | None:
    action = getattr(msg, "action", None)
    if action is not None:
        return f"[service: {type(action).__name__.removeprefix('MessageAction')}]"
    if getattr(msg, "photo", None):
        return "[photo]"
    if getattr(msg, "voice", None):
        return "[voice message]"
    if getattr(msg, "video_note", None):
        return "[video message]"
    if getattr(msg, "video", None):
        return "[video]"
    if getattr(msg, "gif", None):
        return "[GIF]"
    sticker = getattr(msg, "sticker", None)
    if sticker:
        return "[sticker]"
    poll = getattr(msg, "poll", None)
    if poll is not None:
        question = getattr(getattr(poll, "poll", None), "question", "")
        return f"[poll: {getattr(question, 'text', question)}]"
    if getattr(msg, "document", None):
        name = getattr(getattr(msg, "file", None), "name", None)
        return f"[file: {name}]" if name else "[file]"
    preview = getattr(msg, "web_preview", None)
    if preview is not None:
        url = getattr(preview, "url", None)
        return f"[link preview: {url}]" if url else None
    media = getattr(msg, "media", None)
    if media is not None:
        return f"[{type(media).__name__.removeprefix('MessageMedia')}]"
    return None


def _forward_note(msg: Any) -> str | None:
    fwd = getattr(msg, "forward", None)
    if fwd is None:
        return None
    for source in (getattr(fwd, "chat", None), getattr(fwd, "sender", None)):
        if source is not None:
            return f"forwarded from {display_name(source)}"
    from_name = getattr(fwd, "from_name", None)
    return f"forwarded from {from_name}" if from_name else "forwarded"


def _hidden_links(msg: Any) -> list[str]:
    text = getattr(msg, "message", None) or ""
    urls = []
    for entity in getattr(msg, "entities", None) or ():
        url = getattr(entity, "url", None)
        if url and url not in text and url not in urls:
            urls.append(url)
    return urls


def format_message(msg: Any, chat_name: str | None = None) -> str:
    when = msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    who = "Me" if getattr(msg, "out", False) else (
        getattr(msg, "post_author", None) or display_name(getattr(msg, "sender", None))
    )
    head = f"### {when} UTC · {who} (#{msg.id})"
    if chat_name:
        head += f" in {chat_name}"
    lines = [head]
    notes = [n for n in (_forward_note(msg),) if n]
    reply_to = getattr(msg, "reply_to_msg_id", None)
    if reply_to:
        notes.append(f"reply to #{reply_to}")
    if notes:
        lines.append(f"({'; '.join(notes)})")
    text = getattr(msg, "message", None)
    if text:
        lines.append(text)
    media = _media_note(msg)
    if media:
        lines.append(media)
    links = _hidden_links(msg)
    if links:
        lines.append("[links: " + " ".join(links) + "]")
    return "\n".join(lines)


class Reader:
    def __init__(self, client: Any):
        self.client = client

    async def handle(self, request: dict) -> dict:
        """Never raises: errors come back as {"ok": False, "error": ...}."""
        try:
            cmd = request.get("cmd")
            if cmd == "chats":
                text = await self.chats(request.get("limit"), request.get("query"))
            elif cmd == "history":
                text = await self.history(
                    request.get("chat") or "",
                    request.get("limit"),
                    request.get("since"),
                    request.get("until"),
                    request.get("search"),
                )
            elif cmd == "search":
                text = await self.search(request.get("query") or "", request.get("limit"))
            else:
                raise UserError(f"unknown command {cmd!r}")
            return {"ok": True, "text": text}
        except UserError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # Telethon errors (flood wait, private chat, ...)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def chats(self, limit: Any = None, query: str | None = None) -> str:
        n = _limit(limit, 50)
        q = (query or "").strip().lower()
        rows = []
        async for d in self.client.iter_dialogs(limit=DIALOG_SCAN if q else n):
            if q and q not in (d.name or "").lower():
                continue
            username = getattr(getattr(d, "entity", None), "username", None)
            when = d.date.astimezone(timezone.utc).strftime("%Y-%m-%d") if getattr(d, "date", None) else "-"
            parts = [f"{d.name}", _kind(d), f"id {d.id}"]
            if username:
                parts.append(f"@{username}")
            if getattr(d, "unread_count", 0):
                parts.append(f"{d.unread_count} unread")
            parts.append(f"last {when}")
            rows.append("- " + " · ".join(parts))
            if len(rows) >= n:
                break
        if not rows:
            return f"No chats matching {query!r}." if q else "No chats."
        return "\n".join(rows)

    async def resolve(self, chat: str) -> tuple[Any, str]:
        """Finds a chat by id, @username or (part of) its name. Returns (entity, display name)."""
        s = chat.strip()
        if not s:
            raise UserError("which chat? Give its name, @username or id (see `tg-read chats`)")
        if s.lower() in ("me", "saved", "saved messages"):
            return "me", "Saved Messages"
        want_id = int(s) if re.fullmatch(r"-?\d+", s) else None
        want_user = s[1:].lower() if s.startswith("@") else None
        exact, partial = [], []
        async for d in self.client.iter_dialogs(limit=DIALOG_SCAN):
            name = d.name or ""
            username = (getattr(getattr(d, "entity", None), "username", None) or "").lower()
            if want_id is not None:
                if d.id == want_id or getattr(getattr(d, "entity", None), "id", None) == want_id:
                    return d.entity, name
            elif want_user is not None:
                if username == want_user:
                    return d.entity, name
            elif name.lower() == s.lower():
                exact.append(d)
            elif s.lower() in name.lower():
                partial.append(d)
        if want_user is not None:
            return s, s  # not in the dialog list; let Telegram resolve the public username
        matches = exact or partial
        if len(matches) == 1:
            return matches[0].entity, matches[0].name
        if not matches:
            raise UserError(f"no chat matches {chat!r}; list them with `tg-read chats -q <part of the name>`")
        options = "\n".join(f"- {d.name} ({_kind(d)}, id {d.id})" for d in matches[:15])
        raise UserError(f"{len(matches)} chats match {chat!r}; use the id:\n{options}")

    async def history(
        self,
        chat: str,
        limit: Any = None,
        since: str | None = None,
        until: str | None = None,
        search: str | None = None,
    ) -> str:
        n = _limit(limit, 100)
        start = parse_when(since)
        end = parse_when(until, end_of_day=True)
        entity, name = await self.resolve(chat)
        kwargs: dict[str, Any] = {"limit": None if start else n + 1}  # one extra shows there are more
        if end:
            kwargs["offset_date"] = end
        if search:
            kwargs["search"] = search
        messages = []
        more = False
        async for m in self.client.iter_messages(entity, **kwargs):
            if start and m.date < start:
                break
            if len(messages) >= n:
                more = True
                break
            messages.append(m)
        messages.reverse()  # oldest first, like reading the chat
        what = f'messages matching "{search}"' if search else "messages"
        header = f"# {name} — {len(messages)} {what}"
        if messages:
            first = messages[0].date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            last = messages[-1].date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            header += f", {first} to {last} UTC"
        body = [header]
        if more:
            body.append(
                f"(limit {n} reached; there are older ones: use --until {first[:10]} or a larger -n)"
            )
        body += [format_message(m) for m in messages]
        return "\n\n".join(body)

    async def search(self, query: str, limit: Any = None) -> str:
        if not query.strip():
            raise UserError("search for what?")
        n = _limit(limit, 50)
        found = []
        async for m in self.client.iter_messages(None, search=query, limit=n):
            found.append(format_message(m, chat_name=display_name(getattr(m, "chat", None))))
        if not found:
            return f'Nothing found for "{query}".'
        return "\n\n".join([f'# {len(found)} messages matching "{query}" (newest first)'] + found)
