"""Answers read requests against a Telethon client (writing lives in writer.py, for ampi-tools only).

Reading only calls `iter_dialogs`, `iter_messages` and `get_messages`, none of which marks anything as
read. Messages the assistant itself sent are labelled "Me (sent by Ampi)", so they are never mistaken for
the owner's own words. The client is duck-typed so tests can pass a fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_LIMIT = 2000
DIALOG_SCAN = 1000  # how many dialogs to look through when resolving a chat by name
DIALOG_TTL = 300  # seconds the chat list is cached
REQUESTS_PER_MINUTE = 20  # cost units per minute for the assistant; one unit is about one API call
WATCHER_UNITS_PER_MINUTE = 20  # separate budget for the bridge's watchers ("since"), so Claude can't starve them
MAX_SEARCH = 200
RESOLVES_PER_DAY = 20  # lookups of @usernames that are not in the chat list
SINCE_BATCH = 200
# Chats the assistant may never read: 777000 is Telegram's own service chat with login codes.
# More can be added with TG_READER_DENY (comma-separated ids, e.g. a bank's OTP bot).
ALWAYS_DENIED = frozenset({777000})


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


def format_message(msg: Any, chat_name: str | None = None, by_ampi: bool = False) -> str:
    when = msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    who = ("Me (sent by Ampi)" if by_ampi else "Me") if getattr(msg, "out", False) else (
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


def forwarded_from(msg: Any) -> set[int]:
    """Every account or chat id a forwarded message names as where it came from."""
    out: set[int] = set()
    fwd = getattr(msg, "forward", None) or getattr(msg, "fwd_from", None)
    for f in (fwd, getattr(fwd, "original_fwd", None)):
        for key in ("sender_id", "chat_id"):
            v = getattr(f, key, None)
            if isinstance(v, int):
                out.add(v)
        for key in ("from_id", "saved_from_peer", "saved_from_id", "sender", "chat"):
            peer = getattr(f, key, None)
            for attr in ("user_id", "id"):
                v = getattr(peer, attr, None)
                if isinstance(v, int):
                    out.add(v)
    return out


class Reader:
    """All requests are served one at a time and rate-limited: Telegram's flood limits apply to the
    owner's whole account, so a looping or hijacked caller must not be able to get it restricted."""

    def __init__(self, client: Any, clock: Callable[[], float] = time.monotonic, deny: frozenset[int] = frozenset()):
        self.client = client
        self.deny = ALWAYS_DENIED | deny
        self.clock = clock
        self._lock = asyncio.Lock()
        self._dialogs: tuple[float, list] | None = None
        self._calls: dict[str, deque[float]] = {"assistant": deque(), "watchers": deque()}
        self._cooldown_until = 0.0
        self._resolves: deque[float] = deque()
        self._resolved: dict[str, int] = {}  # @username -> peer id, so repeat lookups cost nothing
        self.ampi_sent: set[tuple[int, int]] = set()  # (peer id, message id) the assistant sent (writer.py)
        self.self_id: int | None = None  # the account's own id (Saved Messages)
        # The bridge's own bots: their chats with the owner hold social-mode replies, approval requests and message
        # summaries (other people's text), which must not come back into runs that have the web.
        self.own_bots: set[int] = set()
        self.social = False        # a web session was configured at some point
        self._told = True          # ampi-tools said which bots are its own (or there is no ampi-tools)
        self._state: Path | None = None

    # --- the bridge's own bots (told by ampi-tools through the write socket, remembered on disk) ---------------

    def guard(self, path: Path, required: bool) -> None:
        """`required`: ampi-tools is installed here, so nothing is read until it said which bots are the bridge's."""
        self._state = path
        try:
            raw = json.loads(path.read_text())
            bots = [int(b) for b in raw["bots"]]
            self._apply(bots, raw["social"] is True)
            self._told = True
        except (OSError, ValueError, TypeError, KeyError):
            self._told = not required

    def _apply(self, bots: list[int], social: bool) -> None:
        self.own_bots |= set(bots)  # never shrinks: an old bot's chat still holds its messages
        self.social = self.social or social
        self.deny = frozenset(self.deny | self.own_bots)

    def set_own_bots(self, bots: list[int], social: bool) -> None:
        before = (set(self.own_bots), self.social, self._told)
        self._apply(bots, social)
        self._told = True
        if self._state is not None and before != (self.own_bots, self.social, True):
            tmp = self._state.with_name(self._state.name + ".tmp")
            tmp.write_text(json.dumps({"bots": sorted(self.own_bots), "social": self.social}))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._state)

    def own_bots_active(self) -> bool:
        return self._told and bool(self.own_bots)

    def _own_text(self, msg: Any) -> bool:
        """The message repeats something the bridge's bot sent (forwarded, by anyone, to anywhere)."""
        return bool(self.own_bots & forwarded_from(msg))

    def _format(self, msg: Any, **kw: Any) -> str:
        if not self._own_text(msg):
            return format_message(msg, **kw)
        head = format_message(msg, **kw).split("\n", 1)[0]
        return f"{head}\n(forwarded from Ampi's own chat: not shown)"

    def _by_ampi(self, peer_id: int | None, msg: Any) -> bool:
        peer = self.self_id if peer_id is None else peer_id
        return (peer, msg.id) in self.ampi_sent

    async def handle(self, request: dict) -> dict:
        """Never raises: errors come back as {"ok": False, "error": ...}."""
        async with self._lock:
            try:
                if not self._told or (self.social and not self.own_bots):
                    raise UserError("reading Telegram waits: Ampi's own bot isn't known yet, and its chat must stay "
                                    "out (ampi-tools and claude-assistant tell it when they run)")
                self._admit(request)
                return await self._dispatch(request)
            except UserError as e:
                return {"ok": False, "error": str(e)}
            except Exception as e:  # Telethon errors (flood wait, private chat, ...)
                wait = getattr(e, "seconds", None)
                if isinstance(wait, int) and wait > 0:  # FloodWaitError: back off for the whole account
                    self._cooldown_until = self.clock() + wait
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _admit(self, request: dict) -> None:
        """Charges the request about one unit per 100 messages it may fetch (Telegram pages by 100)."""
        now = self.clock()
        if now < self._cooldown_until:
            raise UserError(f"Telegram asked to slow down; try again in {int(self._cooldown_until - now) + 1} s")
        cmd = request.get("cmd")
        limit = request.get("limit")
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 100
        if cmd == "search":
            limit = min(limit, MAX_SEARCH)
        cost = max(1, min(-(-limit // 100), MAX_LIMIT // 100)) if cmd in ("history", "search") else 1
        bucket, budget = (("watchers", WATCHER_UNITS_PER_MINUTE) if cmd == "since"
                          else ("assistant", REQUESTS_PER_MINUTE))
        calls = self._calls[bucket]
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) + cost > budget:
            raise UserError("too many Telegram requests in the last minute; wait a minute (big fetches count more)")
        calls.extend([now] * cost)

    async def _dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "chats":
            text = await self.chats(request.get("limit"), _opt_str(request, "query"))
        elif cmd == "history":
            text = await self.history(
                _opt_str(request, "chat") or "",
                request.get("limit"),
                _opt_str(request, "since"),
                _opt_str(request, "until"),
                _opt_str(request, "search"),
                request.get("before_id"),
            )
        elif cmd == "search":
            text = await self.search(_opt_str(request, "query") or "", request.get("limit"))
        elif cmd == "since":
            chat = request.get("chat")
            if isinstance(chat, int) and not isinstance(chat, bool):
                chat = str(chat)
            elif not isinstance(chat, str):
                raise UserError("chat must be a name or id")
            return {"ok": True, **await self.since(chat, request.get("after_id"))}
        else:
            raise UserError(f"unknown command {cmd!r}")
        return {"ok": True, "text": text}

    async def dialogs(self) -> list:
        """The chat list, cached for a few minutes (each fetch costs several API calls)."""
        now = self.clock()
        if self._dialogs is None or now - self._dialogs[0] > DIALOG_TTL:
            self._dialogs = (now, [d async for d in self.client.iter_dialogs(limit=DIALOG_SCAN)])
        return self._dialogs[1]

    async def chats(self, limit: Any = None, query: str | None = None) -> str:
        n = _limit(limit, 50)
        q = (query or "").strip().lower()
        rows = []
        for d in await self.dialogs():
            if d.id in self.deny:
                continue
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

    async def resolve(self, chat: str) -> tuple[Any, str, int | None]:
        """Finds a chat by id (as `tg-read chats` shows it), @username or (part of) its name.
        Returns (entity, display name, peer id or None)."""
        s = chat.strip()
        if not s:
            raise UserError("which chat? Give its name, @username or id (see `tg-read chats`)")
        if s.lower() in ("me", "saved", "saved messages"):
            return "me", "Saved Messages", None
        want_id = int(s) if re.fullmatch(r"-?\d{1,20}", s) else None
        want_user = s[1:].lower() if s.startswith("@") else None
        if want_user is not None and not re.fullmatch(r"[a-z0-9_]{4,32}", want_user):
            raise UserError(f"{s!r} isn't a valid @username")
        exact, partial = [], []
        for d in await self.dialogs():
            name = d.name or ""
            username = (getattr(getattr(d, "entity", None), "username", None) or "").lower()
            if d.id in self.deny:
                if want_id == d.id or (want_user and want_user == username):
                    raise UserError("that chat is off limits")
                continue
            if want_id is not None:
                if d.id == want_id:
                    return d.entity, name, d.id
            elif want_user is not None:
                if username == want_user:
                    return d.entity, name, d.id
            elif name.lower() == s.lower():
                exact.append(d)
            elif s.lower() in name.lower():
                partial.append(d)
        if want_id is not None:
            raise UserError(f"no chat with id {want_id} in your chat list (use the id `tg-read chats` shows)")
        if want_user is not None:
            # Not one of your chats: Telegram resolves the public username. That call has strict flood
            # limits, so new lookups are capped per day; known ones are remembered.
            peer_id = self._resolved.get(want_user)
            if peer_id is None:
                now = self.clock()
                while self._resolves and now - self._resolves[0] > 86400:
                    self._resolves.popleft()
                if len(self._resolves) >= RESOLVES_PER_DAY:
                    raise UserError("too many lookups of unknown @usernames today")
                self._resolves.append(now)
                peer_id = await self.client.get_peer_id(s)
                self._resolved[want_user] = peer_id
            if peer_id in self.deny:
                raise UserError("that chat is off limits")
            return s, s, peer_id
        matches = exact or partial
        if len(matches) == 1:
            return matches[0].entity, matches[0].name, matches[0].id
        if not matches:
            raise UserError(f"no chat matches {chat!r}; list them with: tg-read chats -q PART-OF-NAME")
        options = "\n".join(f"- {d.name} ({_kind(d)}, id {d.id})" for d in matches[:15])
        raise UserError(f"{len(matches)} chats match {chat!r}; use the id:\n{options}")

    async def history(
        self,
        chat: str,
        limit: Any = None,
        since: str | None = None,
        until: str | None = None,
        search: str | None = None,
        before_id: Any = None,
    ) -> str:
        n = _limit(limit, 100)
        start = parse_when(since)
        end = parse_when(until, end_of_day=True)
        before = _opt_int(before_id, "before_id")
        entity, name, peer_id = await self.resolve(chat)
        kwargs: dict[str, Any] = {"limit": None if start else n + 1}  # one extra shows there are more
        if end:
            kwargs["offset_date"] = end
        if before:
            kwargs["offset_id"] = before
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
            body.append(f"(limit {n} reached; for older ones run again with --before-id {messages[0].id})")
        body += [self._format(m, by_ampi=self._by_ampi(peer_id, m)) for m in messages]
        return "\n\n".join(body)

    async def since(self, chat: str, after_id: Any = None, limit: int = SINCE_BATCH) -> dict:
        """Messages after `after_id`, oldest first, at most `limit` (for watchers: call again while
        "more" is true). Without after_id, only reports the newest id. Also returns the chat's peer id,
        so the caller can keep following the same chat by id even if it is renamed."""
        entity, name, peer_id = await self.resolve(chat)
        after = _opt_int(after_id, "after_id")
        if after is None:
            newest = [m async for m in self.client.iter_messages(entity, limit=1)]
            return {"text": "", "count": 0, "last_id": newest[0].id if newest else 0, "chat": name,
                    "peer_id": peer_id, "more": False}
        fetched = [m async for m in self.client.iter_messages(entity, min_id=after, reverse=True, limit=limit + 1)]
        more = len(fetched) > limit
        messages = fetched[:limit]
        last_id = max([after] + [m.id for m in messages])
        text = "\n\n".join(self._format(m, by_ampi=self._by_ampi(peer_id, m)) for m in messages)
        return {"text": text, "count": len(messages), "last_id": last_id, "chat": name, "peer_id": peer_id,
                "more": more}

    async def search(self, query: str, limit: Any = None) -> str:
        if not query.strip():
            raise UserError("search for what?")
        n = min(_limit(limit, 50), MAX_SEARCH)
        found = []
        async for m in self.client.iter_messages(None, search=query, limit=n):
            peer = getattr(m, "chat_id", None)
            if peer is None:
                peer = getattr(getattr(m, "peer_id", None), "user_id", None)
            if peer in self.deny or (peer is None and self.own_bots):  # an unknown chat could be the own bot's
                continue
            found.append(self._format(m, chat_name=display_name(getattr(m, "chat", None)),
                                        by_ampi=self._by_ampi(getattr(m, "chat_id", None), m)))
        if not found:
            return f'Nothing found for "{query}".'
        return "\n\n".join([f'# {len(found)} messages matching "{query}" (newest first)'] + found)


def _opt_str(request: dict, key: str) -> str | None:
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise UserError(f"{key} must be a short string")
    return value


def _opt_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63:
        raise UserError(f"{key} must be a message id")
    return value
