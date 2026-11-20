"""Write requests (send, edit, delete) against the owner's Telegram account, for ampi-tools only.

The policy (which chats, whether the owner approved, where the request came from) lives in ampi-tools.
This layer keeps the hard limits that hold whatever ampi-tools asks:

- only chats in the owner's chat list, found exactly (id, @username, "me" or a unique exact name);
- never bots (the bridge's own bot would take the message as an owner command; BotFather could revoke
  its token), never Telegram's login-code chat or TG_READER_DENY, never the chats ampi-tools marks as
  protected (the assistant's own groups and watched chats), and nothing at all until that list arrives;
- plain text only; only the account's own text messages can be edited or deleted, and only while they
  are exactly as they were when the owner saw them;
- a write budget (per minute, per day, distinct chats per day) kept on disk, and a 24-hour stop when
  Telegram signals spam limits, so the account stays clear of flood restrictions.

Everything goes through the Reader's lock and client, so reads and writes never interleave. Message text,
chat names and ids are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import timezone
from pathlib import Path
from typing import Any

from .reader import Reader, UserError, _kind, _media_note

MAX_TEXT = 4096  # Telegram's limit, in UTF-16 code units
MAX_IDS = 20
WRITES_PER_MINUTE = 10
WRITES_PER_DAY = 150
CHATS_PER_DAY = 10
SPAM_BLOCK = 24 * 3600
BOTFATHER = 93372553
MAX_SENT_KEPT = 5000
SPAM_ERRORS = {"PeerFloodError", "FloodWaitError", "FloodPremiumWaitError"}
LOCK_WAIT = 20  # seconds to wait behind a read before answering "busy, nothing was sent"


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def message_hash(peer: int, msg: Any) -> str:
    """Changes when the message's text, attachment, author side or edit time changes."""
    edit = getattr(msg, "edit_date", None)
    parts = [str(peer), str(msg.id), "out" if getattr(msg, "out", False) else "in",
             getattr(msg, "message", None) or "", _media_note(msg) or "", edit.isoformat() if edit else ""]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]


def snapshot(peer: int, msg: Any | None, wanted_id: int) -> dict:
    if msg is None:
        return {"id": wanted_id, "exists": False}
    date = getattr(msg, "date", None)
    return {
        "id": msg.id, "exists": True, "out": bool(getattr(msg, "out", False)),
        "date": date.astimezone(timezone.utc).isoformat(timespec="seconds") if date else "",
        "edited": bool(getattr(msg, "edit_date", None)), "text": getattr(msg, "message", None) or "",
        "media": _media_note(msg) or "", "hash": message_hash(peer, msg),
    }


def _peer(request: dict) -> int:
    v = request.get("peer_id")
    if isinstance(v, bool) or not isinstance(v, int):
        raise UserError("peer_id must be a number")
    return v


def _msg_id(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 2**31:
        raise UserError("message ids are positive numbers")
    return v


def _msg_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_IDS:
        raise UserError(f"give 1-{MAX_IDS} message ids")
    return list(dict.fromkeys(_msg_id(v) for v in value))


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UserError("the message text is empty")
    if utf16_len(value) > MAX_TEXT:
        raise UserError(f"the message is too long for Telegram (max {MAX_TEXT} characters)")
    return value


class Writer:
    def __init__(self, reader: Reader, self_id: int, state_path: Path | str | None = None, clock=time.time):
        self.reader = reader
        self.client = reader.client
        self.self_id = self_id
        self.clock = clock
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load()
        reader.ampi_sent = {(int(p), int(i)) for p, i in self.state["sent"]}

    # --- persisted limits ---------------------------------------------------------------------

    def _load(self) -> dict:
        state = {"writes": [], "chats": [], "blocked_until": 0.0, "sent": []}
        if self.state_path and self.state_path.is_file():
            try:
                raw = json.loads(self.state_path.read_text())
                for key in state:
                    if isinstance(raw.get(key), type(state[key])):
                        state[key] = raw[key]
            except (OSError, ValueError):
                state["blocked_until"] = self.clock() + 3600  # unreadable state: be careful for an hour
        return state

    def _save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def _charge(self, peer: int) -> None:
        now = self.clock()
        if now < self.state["blocked_until"]:
            left = int(self.state["blocked_until"] - now) // 60 + 1
            raise UserError(f"writing is paused for {left} more minutes: Telegram signalled spam/flood limits on "
                            "your account")
        if self.reader.clock() < self.reader._cooldown_until:
            raise UserError("Telegram asked to slow down; try again in a minute")
        self.state["writes"] = [t for t in self.state["writes"] if now - t < 86400]
        self.state["chats"] = [[t, p] for t, p in self.state["chats"] if now - t < 86400]
        if sum(now - t < 60 for t in self.state["writes"]) >= WRITES_PER_MINUTE:
            raise UserError(f"at most {WRITES_PER_MINUTE} Telegram writes a minute; try again shortly")
        if len(self.state["writes"]) >= WRITES_PER_DAY:
            raise UserError(f"at most {WRITES_PER_DAY} Telegram writes a day, to keep your account safe")
        chats = {p for _, p in self.state["chats"]}
        if peer not in chats and len(chats) >= CHATS_PER_DAY:
            raise UserError(f"at most {CHATS_PER_DAY} different chats a day get messages from the assistant")
        self.state["writes"].append(now)
        if peer not in chats:
            self.state["chats"].append([now, peer])
        self._save()

    def _record_sent(self, peer: int, mid: int) -> None:
        self.state["sent"] = (self.state["sent"] + [[peer, mid]])[-MAX_SENT_KEPT:]
        self.reader.ampi_sent.add((peer, mid))
        self._save()

    def _telegram_error(self, e: Exception) -> dict:
        name = type(e).__name__
        if name in SPAM_ERRORS:
            # PeerFlood is Telegram's spam signal: a full day. A flood wait: ten times the asked wait, >= 1 h.
            wait = getattr(e, "seconds", 0) if name != "PeerFloodError" else 0
            pause = SPAM_BLOCK if name == "PeerFloodError" else max(int(wait or 0) * 10, 3600)
            self.state["blocked_until"] = self.clock() + pause
            self._save()
            until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(self.state["blocked_until"]))
            return {"ok": False, "error": f"Telegram limited this account ({name}); writing is paused until {until}",
                    "telegram_error": name, "blocked": True, "blocked_until": until}
        if name == "SlowModeWaitError":
            return {"ok": False, "error": f"this chat has slow mode; wait {getattr(e, 'seconds', '?')} s before "
                                          "writing there again", "telegram_error": name}
        messages = {
            "MessageNotModifiedError": "the new text is the same as the old one",
            "MessageEditTimeExpiredError": "Telegram no longer allows editing this message (too old)",
            "ChatWriteForbiddenError": "you can't write in this chat",
            "ChatAdminRequiredError": "this needs admin rights in that chat",
            "UserBannedInChannelError": "your account can't write in this group or channel",
            "MessageIdInvalidError": "that message doesn't exist or can't be changed",
            "MessageDeleteForbiddenError": "Telegram doesn't allow deleting that message",
            "TopicClosedError": "that forum topic is closed",
        }
        return {"ok": False, "error": messages.get(name, f"Telegram refused: {name}"), "telegram_error": name}

    # --- requests -----------------------------------------------------------------------------

    async def handle(self, request: dict) -> dict:
        """Never raises: errors come back as {"ok": False, "error": ...}. Waiting for a long read to finish
        is bounded, and a request that never got the lock reports that nothing was sent."""
        try:
            await asyncio.wait_for(self.reader._lock.acquire(), LOCK_WAIT)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "the Telegram connection is busy with a long read; nothing was sent, try "
                                          "again in a minute", "not_sent": True}
        try:
            return await self._dispatch(request)
        except UserError as e:
            return {"ok": False, "error": str(e), "not_sent": True}
        except Exception as e:  # Telethon errors
            return self._telegram_error(e)
        finally:
            self.reader._lock.release()

    async def _dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")
        protected = request.get("protected")
        if not isinstance(protected, list) or not protected or \
                not all(isinstance(i, int) and not isinstance(i, bool) for i in protected):
            raise UserError("writing isn't available until the assistant has told the tools which chats it "
                            "reads itself (restart claude-assistant)")
        protected_ids = set(protected)
        if cmd == "resolve":
            chat = request.get("chat")
            if not isinstance(chat, str) or not chat.strip() or len(chat) > 300:
                raise UserError("chat must be a name, @username or id")
            found = await self.resolve(chat)
            self._allowed(found["peer_id"], protected_ids)
            return {"ok": True, **found}
        peer = self._allowed(_peer(request), protected_ids)
        d = await self._dialog(peer)
        if cmd == "messages":
            self.reader._admit({"cmd": "messages"})
            return {"ok": True, "messages": await self.messages(peer, d, _msg_ids(request.get("ids")))}
        if cmd not in ("send", "edit", "delete"):
            raise UserError(f"unknown command {cmd!r}")
        self._charge(peer)
        if cmd == "send":
            reply_to = request.get("reply_to")
            return {"ok": True, **await self.send(peer, d, _text(request.get("text")),
                                                  None if reply_to is None else _msg_id(reply_to))}
        if cmd == "edit":
            return {"ok": True, **await self.edit(peer, d, _msg_id(request.get("message_id")),
                                                  _text(request.get("text")), request.get("expect"))}
        return {"ok": True, **await self.delete(peer, d, _msg_ids(request.get("ids")), request.get("expect"))}

    def _allowed(self, peer: int, protected: set[int]) -> int:
        if peer in self.reader.deny or peer == BOTFATHER:
            raise UserError("that chat is off limits")
        if peer in protected:
            raise UserError("the assistant reads that chat itself (one of its groups or watched chats); a message "
                            "from your account there would count as your own command, so it can't write there")
        return peer

    async def _dialog(self, peer: int) -> Any:
        """The chat as it appears in the owner's chat list; writing elsewhere is refused."""
        if peer == self.self_id:
            return None
        for d in await self.reader.dialogs():
            if d.id == peer:
                if _kind(d) == "bot" or getattr(getattr(d, "entity", None), "bot", False):
                    raise UserError("messages to bots aren't sent from your account")
                return d
        raise UserError("that chat isn't in your chat list (writing only goes to chats you already have)")

    async def resolve(self, chat: str) -> dict:
        s = chat.strip()
        if s.lower() in ("me", "saved", "saved messages") or s == str(self.self_id):
            return {"peer_id": self.self_id, "title": "Saved Messages", "kind": "saved", "username": None,
                    "members": None, "admin": True, "forum": False, "last_date": None}
        dialogs = await self.reader.dialogs()
        if s.lstrip("-").isdigit():
            matches = [d for d in dialogs if d.id == int(s)]
        elif s.startswith("@"):
            matches = [d for d in dialogs
                       if (getattr(getattr(d, "entity", None), "username", None) or "").lower() == s[1:].lower()]
        else:
            matches = [d for d in dialogs if (d.name or "").strip().lower() == s.lower()]
            if not matches:
                close = [d for d in dialogs if s.lower() in (d.name or "").lower()][:10]
                hint = "".join(f"\n- {d.name} ({_kind(d)}, id {d.id})" for d in close)
                raise UserError(f"no chat is named exactly {chat!r}" + (f"; did you mean (use the id):{hint}" if
                                                                         hint else "; see `tg-read chats`"))
        if not matches:
            raise UserError(f"{chat!r} isn't in your chat list (writing only goes to chats you already have)")
        if len(matches) > 1:
            options = "".join(f"\n- {d.name} ({_kind(d)}, id {d.id})" for d in matches[:10])
            raise UserError(f"{len(matches)} chats are named {chat!r}; use the id:{options}")
        d = matches[0]
        e = getattr(d, "entity", None)
        kind = "bot" if getattr(e, "bot", False) else _kind(d)
        if kind == "bot":
            raise UserError("messages to bots aren't sent from your account")
        if getattr(e, "migrated_to", None) is not None or getattr(e, "deactivated", False):
            raise UserError("that group was upgraded or closed; use the new group's id from `tg-read chats`")
        admin = bool(getattr(e, "creator", False) or getattr(e, "admin_rights", None))
        return {"peer_id": d.id, "title": d.name or "", "kind": kind, "username": getattr(e, "username", None),
                "members": getattr(e, "participants_count", None), "admin": admin,
                "forum": bool(getattr(e, "forum", False)),
                "last_date": d.date.astimezone(timezone.utc).isoformat(timespec="minutes") if getattr(d, "date", None)
                else None}

    async def _entity(self, peer: int, d: Any) -> Any:
        return "me" if d is None else d.entity

    async def messages(self, peer: int, d: Any, ids: list[int]) -> list[dict]:
        found = await self.client.get_messages(await self._entity(peer, d), ids=ids)
        by_id = {m.id: m for m in found or [] if m is not None}
        return [{**snapshot(peer, by_id.get(i), i), "by_ampi": (peer, i) in self.reader.ampi_sent} for i in ids]

    async def send(self, peer: int, d: Any, text: str, reply_to: int | None) -> dict:
        if reply_to is not None:
            (target,) = await self.messages(peer, d, [reply_to])
            if not target["exists"]:
                raise UserError(f"message #{reply_to} isn't in that chat (any more)")
        msg = await self.client.send_message(await self._entity(peer, d), text, reply_to=reply_to,
                                             parse_mode=None, link_preview=False)
        self._record_sent(peer, msg.id)
        return {"message_id": msg.id, "date": snapshot(peer, msg, msg.id)["date"]}

    async def _own_unchanged(self, peer: int, d: Any, ids: list[int], expect: Any, *, text_only: bool) -> None:
        if not isinstance(expect, dict):
            raise UserError("missing the expected message state")
        for snap in await self.messages(peer, d, ids):
            if not snap["exists"]:
                raise UserError(f"message #{snap['id']} doesn't exist any more")
            if not snap["out"]:
                raise UserError(f"message #{snap['id']} isn't yours; only your own messages can be changed")
            if text_only and snap["media"]:
                raise UserError(f"message #{snap['id']} has an attachment; only text messages can be edited")
            if snap["hash"] != expect.get(str(snap["id"])):
                raise UserError(f"message #{snap['id']} changed since it was shown to you; nothing was done")

    async def edit(self, peer: int, d: Any, mid: int, text: str, expect: Any) -> dict:
        await self._own_unchanged(peer, d, [mid], expect, text_only=True)
        msg = await self.client.edit_message(await self._entity(peer, d), mid, text, parse_mode=None,
                                             link_preview=False)
        return {"message_id": mid, "hash": message_hash(peer, msg) if msg is not None else None}

    async def delete(self, peer: int, d: Any, ids: list[int], expect: Any) -> dict:
        await self._own_unchanged(peer, d, ids, expect, text_only=False)
        affected = await self.client.delete_messages(await self._entity(peer, d), ids, revoke=True)
        count = sum(getattr(a, "pts_count", 0) or 0 for a in (affected or []))
        if count == 0:
            raise UserError("Telegram deleted nothing (the messages may already be gone)")
        return {"deleted": ids, "count": count}
