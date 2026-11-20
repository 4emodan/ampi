"""Telegram writes as the owner (send, edit, delete), with per-chat permissions and one-time requests.

The owner's Telegram login lives with tg-reader; ampi-tools reaches its write socket (/run/tg-writer), which
answers only this user. Everything Ampi writes goes out from the owner's own account, so:

- Whether a request may run without asking is decided here, from where it really came from (the
  process ancestry ampi-tools checks, not anything the caller says): only a run in the owner's private
  chat can write without asking, only plain short text, only in chats the owner allowed, within daily caps.
  Everything else becomes a one-time request with Approve / Reject buttons (and, for a single message to a
  person or small group, "always allow here" for 30 days).
- Chats are fixed (numeric id, title, kind) before the owner sees the request; a run never re-resolves a
  name. Messages to edit or delete must be the owner's, text only for edits, and unchanged since shown.
- Every automatic write is reported to the owner in their chat with Ampi.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ops as _ops
from .meta import check_text, exact_block, mixed_scripts, text_extras
from .ops import FIELDS, OPS, PRE_RESOLVE, Op, Services, _choice, _text, clean
from .todoist import ToolError

DEFAULT_SOCKET = "/run/tg-writer/tg-write.sock"
WRITER_TIMEOUT = 45  # resolve (25) + messages (25) + write (45) stays under the client's 110 s
MAX_TELEGRAM = 4096          # Telegram's own limit (UTF-16 units): automatic writes
MAX_APPROVAL_SEND = 3000     # what still fits in one approval message
MAX_APPROVAL_EDIT = 2000     # the old text is shown too
AUTO_PER_CHAT_DAY = 10
AUTO_PER_DAY = 20
AUTO_GROUP_MAX_MEMBERS = 50
SELF_EDIT_WINDOW = 600       # a message Ampi sent can be corrected without asking for 10 minutes
DUPLICATE_WINDOW = 600
GRANT_DAYS = 30
EDIT_LIMIT_HOURS = 48
TG_PENDING_CAP = 3
WRITE_OPS = ("telegram.send", "telegram.edit", "telegram.delete")
ACTIONS = ("send", "edit", "delete")


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _msg_id(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 2**31:
        raise ToolError("message ids are numbers from `tg-read history` (#123)")
    return v


def _msg_ids(v: Any) -> list[int]:
    if not isinstance(v, list) or not 1 <= len(v) <= 20:
        raise ToolError("give 1-20 message ids")
    return list(dict.fromkeys(_msg_id(x) for x in v))


FIELDS.update({
    "chat": _text(300, True), "message": _msg_id, "messages": _msg_ids, "reply_to_msg": _msg_id,
    "action": _choice(*ACTIONS),
})


# --- text ---------------------------------------------------------------------------------------

def check_message(text: str) -> str:
    """What gets sent must be exactly what the owner reads, with nowhere to hide data: the same rules as
    public posts (meta.check_text: plain characters, single spaces, no invisible or variation characters
    except what emoji need, NFC), plus Telegram's own length limit."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError("the message is empty")
    check_text(text, 100_000, False)
    if utf16_len(text) > MAX_TELEGRAM:
        raise ToolError(f"the message is too long for Telegram ({utf16_len(text)} characters, max {MAX_TELEGRAM})")
    return text


_RISKY = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.|tg:|t\.me|telegram\.(?:me|dog)|"   # links and Telegram deep links
    r"\w[\w-]*(?:\.[\w-]+)*\.[^\W\d_]{2,}|"                            # bare domains, any alphabet (сайт.рф)
    r"[\w.+-]+@[\w-]+\.[\w.]+|(?<![\w.])@\w{2,}|"                      # emails, @mentions
    r"\d{4,}|\d[\d .,'\-]{2,}\d|\+\d)",                                  # codes, numbers with separators, phones
    re.IGNORECASE)


def auto_safe(text: str) -> bool:
    """Plain conversational text: nothing Telegram turns into a link, nobody notified, no codes or
    numbers that could carry private data, no bot command. Anything else is shown to the owner first."""
    return not (_RISKY.search(text) or text.lstrip().startswith("/") or mixed_scripts(text))


def text_hash(peer: int, text: str) -> str:
    return hashlib.sha256(f"{peer}\x00{text}".encode()).hexdigest()[:32]


# --- where requests come from ---------------------------------------------------------------------

def proc_stat(pid: int, proc: Path = Path("/proc")) -> tuple[int, int] | None:
    """(parent pid, start time) of a process, or None if it is gone."""
    try:
        raw = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    fields = raw[raw.rfind(")") + 2:].split()
    try:
        return int(fields[1]), int(fields[19])
    except (IndexError, ValueError):
        return None


@dataclass
class RunRegistry:
    """Claude runs the bridge started, by (pid, start time), with where they came from: "private" (the
    owner's chat), "group:NAME" or "watcher:NAME". A request's origin is found by walking up from the
    caller's pid; anything that doesn't lead to a registered run is "unknown"."""

    proc: Path = Path("/proc")
    runs: dict[tuple[int, int], str] = field(default_factory=dict)

    def register(self, pid: int, origin: str) -> None:
        st = proc_stat(pid, self.proc)
        if st is None:
            raise ToolError("no such process")
        self.runs[(pid, st[1])] = origin
        self._prune()

    def unregister(self, pid: int) -> None:
        self.runs = {k: v for k, v in self.runs.items() if k[0] != pid}

    def _prune(self) -> None:
        self.runs = {k: v for k, v in self.runs.items() if (proc_stat(k[0], self.proc) or (0, -1))[1] == k[1]}

    def origin_of(self, pid: int) -> str:
        seen = 0
        while pid > 1 and seen < 64:
            st = proc_stat(pid, self.proc)
            if st is None:
                return "unknown"
            origin = self.runs.get((pid, st[1]))
            if origin is not None:
                return origin
            pid, seen = st[0], seen + 1
        return "unknown"


# --- permissions, log and notices -------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramRules:
    """[telegram] in /home/ampitools/config.toml (root-owned: Claude can't change it)."""

    default: str = "ask"
    allow: dict = field(default_factory=dict)   # chat key (int peer id or "me") -> frozenset of actions
    never: frozenset = frozenset()

    @staticmethod
    def parse(raw: Any) -> "TelegramRules":
        if raw is None:
            return TelegramRules()
        if not isinstance(raw, dict):
            raise ValueError("[telegram] must be a table")
        unknown = set(raw) - {"default", "chats"}
        if unknown:
            raise ValueError(f"[telegram]: unknown keys {sorted(unknown)}")
        default = raw.get("default", "ask")
        if default not in ("ask", "never"):
            raise ValueError('[telegram] default must be "ask" or "never"')
        allow, never = {}, set()
        for i, c in enumerate(raw.get("chats", [])):
            where = f"[[telegram.chats]] #{i + 1}"
            if not isinstance(c, dict) or set(c) - {"chat", "allow", "never", "name"}:
                raise ValueError(f"{where}: use chat, allow, never and an optional name")
            key = c.get("chat")
            if key != "me" and (isinstance(key, bool) or not isinstance(key, int)):
                raise ValueError(f'{where}: chat must be the numeric id from `tg-read chats`, or "me"')
            acts = c.get("allow", [])
            if not isinstance(acts, list) or set(acts) - {"send", "edit"}:
                raise ValueError(f'{where}: allow can list "send" and "edit" (deleting always asks you)')
            if c.get("never"):
                never.add(key)
            elif acts:
                allow[key] = frozenset(acts)
        return TelegramRules(default, allow, frozenset(never))


_TG_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (peer_id INTEGER, action TEXT, title TEXT, created REAL, expires REAL,
    PRIMARY KEY (peer_id, action));
CREATE TABLE IF NOT EXISTS protected (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS writes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, peer_id INTEGER, title TEXT,
    action TEXT, message_ids TEXT, text_hash TEXT, auto INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, text TEXT,
    sent INTEGER DEFAULT 0);
"""


class TelegramStore:
    def __init__(self, path: Path | str, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_TG_SCHEMA)
        self.db.commit()

    # grants
    def grant(self, peer: int, action: str, title: str) -> None:
        now = self.clock()
        self.db.execute("INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?, ?)",
                        (peer, action, title, now, now + GRANT_DAYS * 86400))
        self.db.commit()

    def granted(self, peer: int, action: str) -> bool:
        row = self.db.execute("SELECT expires FROM grants WHERE peer_id=? AND action=?", (peer, action)).fetchone()
        return bool(row and row[0] > self.clock())

    def grants(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM grants WHERE expires > ? ORDER BY title", (self.clock(),))
        return [dict(r) for r in rows]

    def revoke(self, peer: int, action: str | None) -> int:
        if action:
            cur = self.db.execute("DELETE FROM grants WHERE peer_id=? AND action=?", (peer, action))
        else:
            cur = self.db.execute("DELETE FROM grants WHERE peer_id=?", (peer,))
        self.db.commit()
        return cur.rowcount

    # protected chats (the assistant's own groups and watched chats)
    def set_protected(self, ids: list[int], complete: bool) -> None:
        self.db.execute("DELETE FROM protected")
        self.db.executemany("INSERT OR IGNORE INTO protected VALUES (?)", [(i,) for i in ids])
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('protect_state', ?)",
                        ("complete" if complete else "incomplete",))
        self.db.commit()

    def protected(self) -> list[int]:
        state = self.db.execute("SELECT value FROM meta WHERE key='protect_state'").fetchone()
        if state is None:
            raise ToolError("writing to Telegram isn't available until the assistant has started and told the tools "
                            "which chats it reads itself")
        if state[0] != "complete":
            raise ToolError("writing to Telegram waits until every Telegram watcher has done its first check")
        return [r[0] for r in self.db.execute("SELECT id FROM protected")]

    # writes
    def log_write(self, peer: int, title: str, action: str, ids: list[int], thash: str, auto: bool,
                  status: str) -> None:
        self.db.execute("INSERT INTO writes (ts, peer_id, title, action, message_ids, text_hash, auto, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.clock(), peer, title, action, json.dumps(ids), thash, int(auto), status))
        self.db.commit()

    def auto_count(self, peer: int | None = None) -> int:
        q = "SELECT COUNT(*) FROM writes WHERE auto=1 AND ts > ?"
        args: list[Any] = [self.clock() - 86400]
        if peer is not None:
            q, args = q + " AND peer_id=?", args + [peer]
        return self.db.execute(q, args).fetchone()[0]

    def duplicate(self, peer: int, thash: str) -> bool:
        row = self.db.execute("SELECT 1 FROM writes WHERE peer_id=? AND text_hash=? AND ts > ? AND status != "
                              "'failed'", (peer, thash, self.clock() - DUPLICATE_WINDOW)).fetchone()
        return row is not None

    def sent_recently_by_ampi(self, peer: int, mid: int) -> bool:
        rows = self.db.execute("SELECT message_ids FROM writes WHERE peer_id=? AND action='send' AND status='done' "
                               "AND ts > ?", (peer, self.clock() - SELF_EDIT_WINDOW))
        return any(mid in json.loads(r[0]) for r in rows)

    def recent_writes(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM writes ORDER BY id DESC LIMIT ?", (limit,))]

    # notices for the owner (posted by the bridge's relay)
    def notice(self, text: str) -> None:
        self.db.execute("INSERT INTO notices (created, text) VALUES (?, ?)", (self.clock(), text))
        self.db.commit()

    def unsent_notices(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, text FROM notices WHERE sent=0 ORDER BY id LIMIT 20")]

    def ack_notice(self, notice_id: int) -> None:
        self.db.execute("UPDATE notices SET sent=1 WHERE id=?", (notice_id,))
        self.db.commit()


class TgWriterClient:
    def __init__(self, store: TelegramStore, path: str | None = None):
        self.store = store
        self.path = path or os.environ.get("TG_WRITER_SOCKET") or DEFAULT_SOCKET

    async def call(self, payload: dict, timeout: float = WRITER_TIMEOUT) -> dict:
        """One request to tg-reader's write socket. The protected chats go with every request (the writer
        refuses without them). A write that times out or gets no answer has an unknown outcome."""
        payload = {**payload, "protected": self.store.protected()}
        writes = payload.get("cmd") in ("send", "edit", "delete")
        try:
            rd, wr = await asyncio.wait_for(asyncio.open_unix_connection(self.path, limit=4 * 1024 * 1024), 10)
        except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
            raise ToolError("Telegram writing isn't running (tg-reader on the server)") from None
        except PermissionError:
            raise ToolError("ampi-tools can't reach tg-reader's write socket (group tgwrite)") from None
        try:
            wr.write(json.dumps(payload).encode() + b"\n")
            await wr.drain()
            line = await asyncio.wait_for(rd.readline(), timeout)
        except (asyncio.TimeoutError, OSError):
            line = b""
        finally:
            wr.close()
        if not line:
            if writes:
                return {"ok": False, "unknown": True}
            raise ToolError("tg-reader didn't answer")
        try:
            reply = json.loads(line)
        except ValueError:
            return {"ok": False, "unknown": True} if writes else {"ok": False, "error": "broken reply"}
        return reply if isinstance(reply, dict) else {"ok": False, "error": "broken reply"}


@dataclass
class TelegramSide:
    client: TgWriterClient
    store: TelegramStore
    rules: TelegramRules


def _tg(s: Services) -> TelegramSide:
    side = getattr(s, "telegram_side", None)
    if side is None:
        raise ToolError("Telegram writing isn't set up (tg-reader and the tgwrite group on the server)")
    return side


async def _ask(s: Services, payload: dict) -> dict:
    reply = await _tg(s).client.call(payload, timeout=25)  # lookups only; writes go through client.call
    if not reply.get("ok"):
        raise ToolError(reply.get("error", "Telegram refused"))
    return reply


# --- resolving a request (before the owner sees it) --------------------------------------------------

def _rule_key(chat: dict) -> Any:
    return "me" if chat.get("kind") == "saved" else chat["peer_id"]


def _check_never(side: TelegramSide, chat: dict) -> None:
    key = _rule_key(chat)
    if key in side.rules.never or chat["peer_id"] in side.rules.never:
        raise ToolError("the owner doesn't allow writing in that chat")


async def _resolve(s: Services, a: dict, op: str) -> dict:
    side = _tg(s)
    if not a.get("chat"):
        raise ToolError("which chat? Give its exact name, @username, id (from `tg-read chats`) or \"me\"")
    chat = await _ask(s, {"cmd": "resolve", "chat": a["chat"]})
    chat = {k: chat.get(k) for k in ("peer_id", "title", "kind", "username", "members", "admin", "forum",
                                     "last_date", "contact")}
    _check_never(side, chat)
    r: dict[str, Any] = {"chat": chat, "requested": time.time()}
    if op in ("telegram.send", "telegram.edit"):
        r["text"] = check_message(a.get("text") or "")
        if chat["kind"] in ("group", "channel") and r["text"].lstrip().startswith("/"):
            raise ToolError("text starting with / would be a bot command from your account in that chat; rephrase")
    if op == "telegram.send":
        if a.get("reply_to_msg"):
            (snap,) = (await _ask(s, {"cmd": "messages", "peer_id": chat["peer_id"],
                                       "ids": [a["reply_to_msg"]]}))["messages"]
            if not snap.get("exists"):
                raise ToolError(f"message #{a['reply_to_msg']} isn't in that chat")
            r["reply"] = snap
        if side.store.duplicate(chat["peer_id"], text_hash(chat["peer_id"], r["text"])):
            raise ToolError("the same text went (or is waiting to go) to that chat in the last 10 minutes; check "
                            "`tg-read history` before sending again")
    elif op == "telegram.edit":
        (snap,) = (await _ask(s, {"cmd": "messages", "peer_id": chat["peer_id"], "ids": [a["message"]]}))["messages"]
        _own(snap)
        if snap.get("media"):
            raise ToolError("that message has an attachment; only text messages can be edited")
        if snap["text"] == r["text"]:
            raise ToolError("the new text is the same as the current one")
        if chat["kind"] not in ("saved", "channel"):
            sent = datetime.fromisoformat(snap["date"])
            if (datetime.now(timezone.utc) - sent).total_seconds() > EDIT_LIMIT_HOURS * 3600:
                raise ToolError("Telegram only allows editing messages for 48 hours in chats and groups")
        r["message"] = snap
    elif op == "telegram.delete":
        snaps = (await _ask(s, {"cmd": "messages", "peer_id": chat["peer_id"], "ids": a["messages"]}))["messages"]
        for snap in snaps:
            _own(snap)
        r["messages"] = snaps
    return r


def _own(snap: dict) -> None:
    if not snap.get("exists"):
        raise ToolError(f"message #{snap.get('id')} isn't in that chat")
    if not snap.get("out"):
        raise ToolError(f"message #{snap['id']} isn't yours; only your own messages can be changed or deleted")


async def pre_resolve_telegram(s: Services, a: dict, op: str) -> dict:
    return {"resolved": await _resolve(s, a, op)}


for _name in WRITE_OPS:
    PRE_RESOLVE[_name] = (lambda name: (lambda s, a: pre_resolve_telegram(s, a, name)))(_name)


# --- deciding: automatic or a one-time request ---------------------------------------------------

def _allowed(side: TelegramSide, chat: dict, action: str) -> bool:
    keys = {_rule_key(chat), chat["peer_id"]}
    return any(action in side.rules.allow.get(k, ()) for k in keys) or side.store.granted(chat["peer_id"], action)


def auto_eligible_chat(chat: dict) -> bool:
    if chat.get("kind") == "saved":
        return True
    if chat.get("kind") == "person":
        return chat.get("contact") is True  # not strangers who happened to write to the owner
    members = chat.get("members")
    return chat.get("kind") == "group" and isinstance(members, int) and 0 < members <= AUTO_GROUP_MAX_MEMBERS


def needs_approval(s: Services, o: dict, origin: str) -> bool:
    """False only when every condition for writing without asking holds (see the module docstring)."""
    side = _tg(s)
    r = o["args"]["resolved"]
    chat = r["chat"]
    if side.rules.default == "never" and not any(_allowed(side, chat, a) for a in ACTIONS):
        raise ToolError("the owner doesn't allow writing in that chat")
    if origin != "private" or not auto_eligible_chat(chat):
        return True
    if side.store.auto_count() >= AUTO_PER_DAY or side.store.auto_count(chat["peer_id"]) >= AUTO_PER_CHAT_DAY:
        return True
    if o["op"] == "telegram.send":
        return not (_allowed(side, chat, "send") and auto_safe(r["text"]))
    if o["op"] == "telegram.edit":
        own_fresh = side.store.sent_recently_by_ampi(chat["peer_id"], r["message"]["id"])
        allowed = _allowed(side, chat, "edit") or _allowed(side, chat, "send")
        return not (own_fresh and allowed and auto_safe(r["text"]))
    return True  # deleting always asks


def grant_offer(s: Services, ops: list[dict], origin: str) -> list[dict] | None:
    """The "always allow here" button: one message, from the owner's chat, to a person/saved/small group."""
    if origin != "private" or len(ops) != 1 or ops[0]["op"] != "telegram.send":
        return None
    chat = ops[0]["args"]["resolved"]["chat"]
    if not auto_eligible_chat(chat):
        return None
    return [{"peer_id": chat["peer_id"], "action": "send", "title": safe_title(chat.get("title"))}]


def note_requested(s: Services, o: dict) -> None:
    r = o["args"]["resolved"]
    chat = r["chat"]
    _tg(s).store.log_write(chat["peer_id"], chat.get("title") or "", "send", [],
                           text_hash(chat["peer_id"], r["text"]), False, "requested")


def check_approval_size(o: dict) -> None:
    r = o["args"]["resolved"]
    if o["op"] == "telegram.send" and utf16_len(r["text"]) > MAX_APPROVAL_SEND:
        raise ToolError(f"messages that need the owner's approval can be at most {MAX_APPROVAL_SEND} characters "
                        f"(this one has {utf16_len(r['text'])}); split it into several")
    if o["op"] == "telegram.edit" and utf16_len(r["text"]) > MAX_APPROVAL_EDIT:
        raise ToolError(f"edits that need the owner's approval can be at most {MAX_APPROVAL_EDIT} characters")


# --- the approval text ------------------------------------------------------------------------------

_TITLE_TRICKS = re.compile(r"[\"'“”‘’«»„‟‹›()\[\]{}<>（）【】]")


def safe_title(title: Any, limit: int = 60) -> str:
    """Chat titles are chosen by other people: no quotes or brackets that could fake the text around them."""
    return clean(_TITLE_TRICKS.sub(" ", str(title or "")), limit) or "(no name)"


def chat_line(chat: dict) -> str:
    if chat.get("kind") == "saved":
        return "your Saved Messages"
    kind = chat.get("kind") or "chat"
    if kind == "person":
        kind = "person in your contacts" if chat.get("contact") else "person NOT in your contacts"
    bits = [f"{kind} id {chat['peer_id']}"]
    if chat.get("username"):
        bits.append("@" + clean(chat["username"], 40))
    members = chat.get("members")
    public = "PUBLIC " if chat.get("username") else ""
    if chat.get("kind") == "channel":
        bits.append(f"{public}channel, {members} subscribers" if members else f"{public}channel".strip())
    elif members:
        bits.append(f"{public}group, {members} members" if public else f"{members} members")
    elif chat.get("kind") == "group":
        bits.append("member count unknown" + (", PUBLIC" if public else ""))
    if chat.get("admin"):
        bits.append("you're an admin")
    if chat.get("forum"):
        bits.append("forum: goes to the General topic unless it's a reply")
    return f"[{', '.join(bits)}] named: {safe_title(chat.get('title'))}"


def _snippet(snap: dict, limit: int = 120) -> str:
    text = clean(snap.get("text") or "", limit) or (snap.get("media") or "(empty)")
    return f"#{snap.get('id')} from {snap.get('date', '')[:16].replace('T', ' ')} UTC: {text}"


def _extras(text: str) -> str:
    extras = text_extras(text)
    return ("\n" + "\n".join(f"  {e}" for e in extras)) if extras else ""


async def describe_send(s: Services, a: dict) -> str:
    r = a["resolved"]
    head = f"Send a Telegram message AS YOU (your own account, not Ampi's bot) to {chat_line(r['chat'])}"
    if r.get("reply"):
        head += f"\n  replying to {_snippet(r['reply'])}"
    return f"{head}:\n{exact_block(r['text'])}{_extras(r['text'])}"


async def describe_edit(s: Services, a: dict) -> str:
    r = a["resolved"]
    old = r["message"]
    shown_old = old["text"] if utf16_len(old["text"]) <= 600 else old["text"][:600] + " […]"
    return (f"EDIT your Telegram message #{old['id']} (from {old['date'][:16].replace('T', ' ')} UTC) in "
            f"{chat_line(r['chat'])}.\nNow:\n{exact_block(shown_old)}\nWould become:\n{exact_block(r['text'])}"
            f"{_extras(r['text'])}")


async def describe_delete(s: Services, a: dict) -> str:
    r = a["resolved"]
    lines = [f"DELETE {len(r['messages'])} of your Telegram messages FOR EVERYONE in {chat_line(r['chat'])}:"]
    lines += [f"  {_snippet(m)}" for m in r["messages"]]
    return "\n".join(lines)


# --- running ---------------------------------------------------------------------------------------

def _unknown(chat: dict) -> ToolError:
    return ToolError(f"no answer from Telegram in time: the outcome is unknown and it MAY have gone through. Check "
                     f"`tg-read history {chat['peer_id']} -n 5` before trying again")


def _notify_auto(side: TelegramSide, verb: str, chat: dict, text: str, mid: int) -> None:
    shown = text if utf16_len(text) <= 1000 else text[:1000] + " […]"
    side.store.notice(f"📤 {verb} as you in {chat_line(chat)} (#{mid}), without asking (you allowed it):\n"
                      f"{exact_block(shown)}")


def _blocked(side: TelegramSide, reply: dict) -> None:
    if reply.get("blocked"):
        side.store.notice(f"⛔ Telegram limited your account ({reply.get('telegram_error')}); the assistant "
                          f"stopped writing until {reply.get('blocked_until')}.")


def _recheck_auto(side: TelegramSide, chat: dict, thash: str | None) -> None:
    """Right before an automatic write: the caps and the duplicate guard again (other runs may have written
    since this request was checked)."""
    if side.store.auto_count() >= AUTO_PER_DAY or side.store.auto_count(chat["peer_id"]) >= AUTO_PER_CHAT_DAY:
        raise ToolError("the daily limit of messages without asking is reached; ask the owner instead")
    if thash and side.store.duplicate(chat["peer_id"], thash):
        raise ToolError("the same text went to that chat in the last 10 minutes; check `tg-read history`")


async def tg_send(s: Services, a: dict) -> str:
    side, r = _tg(s), a["resolved"]
    chat, auto = r["chat"], bool(r.get("auto"))
    _check_never(side, chat)
    thash = text_hash(chat["peer_id"], r["text"])
    if auto:
        _recheck_auto(side, chat, thash)
    payload = {"cmd": "send", "peer_id": chat["peer_id"], "text": r["text"]}
    if r.get("reply"):
        payload["reply_to"] = r["reply"]["id"]
    reply = await side.client.call(payload)
    if reply.get("unknown"):
        side.store.log_write(chat["peer_id"], chat.get("title") or "", "send", [], thash, auto, "unknown")
        raise _unknown(chat)
    if not reply.get("ok"):
        side.store.log_write(chat["peer_id"], chat.get("title") or "", "send", [], thash, auto, "failed")
        _blocked(side, reply)
        raise ToolError(reply.get("error", "Telegram refused"))
    mid = reply["message_id"]
    side.store.log_write(chat["peer_id"], chat.get("title") or "", "send", [mid], thash, auto, "done")
    if auto:
        _notify_auto(side, "Sent", chat, r["text"], mid)
    return f"Sent as you to {chat_line(chat)} (message #{mid})."


async def tg_edit(s: Services, a: dict) -> str:
    side, r = _tg(s), a["resolved"]
    chat, auto, old = r["chat"], bool(r.get("auto")), r["message"]
    _check_never(side, chat)
    if auto:
        _recheck_auto(side, chat, None)
    reply = await side.client.call({"cmd": "edit", "peer_id": chat["peer_id"], "message_id": old["id"],
                                    "text": r["text"], "expect": {str(old["id"]): old["hash"]}})
    thash = text_hash(chat["peer_id"], r["text"])
    if reply.get("unknown"):
        side.store.log_write(chat["peer_id"], chat.get("title") or "", "edit", [old["id"]], thash, auto, "unknown")
        raise _unknown(chat)
    if not reply.get("ok"):
        side.store.log_write(chat["peer_id"], chat.get("title") or "", "edit", [old["id"]], thash, auto, "failed")
        _blocked(side, reply)
        raise ToolError(reply.get("error", "Telegram refused"))
    side.store.log_write(chat["peer_id"], chat.get("title") or "", "edit", [old["id"]], thash, auto, "done")
    if auto:
        _notify_auto(side, "Edited", chat, r["text"], old["id"])
    return f"Edited message #{old['id']} in {chat_line(chat)}."


async def tg_delete(s: Services, a: dict) -> str:
    side, r = _tg(s), a["resolved"]
    chat, msgs = r["chat"], r["messages"]
    _check_never(side, chat)
    ids = [m["id"] for m in msgs]
    reply = await side.client.call({"cmd": "delete", "peer_id": chat["peer_id"], "ids": ids,
                                    "expect": {str(m["id"]): m["hash"] for m in msgs}})
    if reply.get("unknown"):
        side.store.log_write(chat["peer_id"], chat.get("title") or "", "delete", ids, "", False, "unknown")
        raise _unknown(chat)
    if not reply.get("ok"):
        side.store.log_write(chat["peer_id"], chat.get("title") or "", "delete", ids, "", False, "failed")
        _blocked(side, reply)
        raise ToolError(reply.get("error", "Telegram refused"))
    side.store.log_write(chat["peer_id"], chat.get("title") or "", "delete", ids, "", False, "done")
    return f"Deleted {len(ids)} message(s) for everyone in {chat_line(chat)}."


async def tg_permissions(s: Services, a: dict) -> str:
    side = _tg(s)
    lines = ["Telegram writing (always from the owner's own account):",
             f"- default: {'ask the owner (one-time request)' if side.rules.default == 'ask' else 'not allowed'}"]
    for key, acts in sorted(side.rules.allow.items(), key=lambda kv: str(kv[0])):
        lines.append(f"- chat {key}: {' and '.join(sorted(acts))} without asking (config)")
    for key in sorted(side.rules.never, key=str):
        lines.append(f"- chat {key}: never (config)")
    for g in side.store.grants():
        until = datetime.fromtimestamp(g["expires"], timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"- {safe_title(g['title'])} (id {g['peer_id']}): {g['action']} without asking until {until} "
                     "(owner tapped “always”)")
    lines.append(f"Without asking only from the owner's private chat, only plain text (no links, @mentions, long "
                 f"numbers or /commands), to contacts, Saved Messages or groups of up to {AUTO_GROUP_MAX_MEMBERS} "
                 f"members; at most {AUTO_PER_CHAT_DAY} per chat and {AUTO_PER_DAY} in total a day. Deleting always "
                 "asks. Anything else becomes a one-time request to the owner.")
    return "\n".join(lines)


async def tg_log(s: Services, a: dict) -> str:
    rows = _tg(s).store.recent_writes(a.get("limit", 20))
    if not rows:
        return "No Telegram writes yet."
    out = []
    for w in rows:
        when = datetime.fromtimestamp(w["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        ids = ", ".join(f"#{i}" for i in json.loads(w["message_ids"] or "[]"))
        how = "waiting for the owner" if w["status"] == "requested" else \
            f"{'automatic' if w['auto'] else 'approved'} · {w['status']}"
        out.append(f"- {when} UTC {w['action']} in {safe_title(w['title'])} {ids} · {how}")
    return "\n".join(out)


async def tg_revoke(s: Services, a: dict) -> str:
    side = _tg(s)
    if a.get("origin_checked") != "private":
        raise ToolError("permissions can only be changed from the owner's private chat")
    want = (a.get("chat") or "").strip()
    if re.fullmatch(r"-?\d{1,20}", want):  # by id straight from the list (works even if the chat is gone)
        peer, title = int(want), want
    else:
        chat = await _ask(s, {"cmd": "resolve", "chat": want})
        peer, title = chat["peer_id"], chat.get("title") or want
    n = side.store.revoke(peer, a.get("action"))
    return f"Removed {n} permission(s) for {safe_title(title)}." if n else \
        "There was no tapped permission for that chat (config permissions are changed on the server)."


TELEGRAM_OPS = [
    Op("telegram.send", True, tg_send, describe_send, "SEND Telegram message as you"),
    Op("telegram.edit", True, tg_edit, describe_edit, "EDIT your Telegram message"),
    Op("telegram.delete", True, tg_delete, describe_delete, "DELETE your Telegram messages"),
    Op("telegram.permissions", False, tg_permissions),
    Op("telegram.log", False, tg_log),
    Op("telegram.revoke", False, tg_revoke),
]
OPS.update({op.name: op for op in TELEGRAM_OPS})
_ops.INTERNAL.add("origin_checked")
