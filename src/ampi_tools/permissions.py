"""What the owner allows, set in the hub: per action ("gmail.star"), per whole adapter ("gmail.*") and per booking script
("flow:NAME"), for the owner's chats and groups ("chat"), for one topic ("topic:PROFILE/private|GROUP/THREAD", thread 0 = outside
topics) and for each watcher ("watcher:PROFILE/NAME"). "changes.*" stands for everything that changes something.

    off      refused, not asked
    ask      asks every time (no standing permission applies)
    default  the built-in rules
    report   (watchers) done without asking; the owner gets a report afterwards

Only ampi-tools writes the database. Rules here only ever add to the built-in limits:
- "chat" can only be made stricter, and a topic only stricter than "chat";
- "report" exists for the actions in REPORTABLE only. For those that go somewhere (REPORT_ONLY; todoist.add: which
  project) it decides how the owner hears of it; the destination still needs the watcher's own permission;
- actions are judged by what they do: removing the label INBOX is archiving (see effects).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import watch_spec as ws
from .todoist import ToolError

RANK = {"off": 0, "ask": 1, "default": 2, "report": 3}
# Reach nobody else and can be undone (trash: for 30 days).
REPORTABLE = frozenset({"gmail.label", "gmail.archive", "gmail.mark_read", "gmail.mark_unread", "gmail.star", "gmail.untrash",
                        "gmail.trash", "gmail.draft"})
REPORT_ONLY = frozenset({"todoist.add", "todoist.move"})  # reported when the watcher may do it; never allowed by this alone
MAX_ITEMS = 25          # emails per request without asking
TRASH_PER_DAY = 50      # emails per watcher
ITEMS_PER_DAY = 500     # the same, all its Gmail changes together
TRASH_MAX_AGE = 3 * 24 * 3600
ADAPTERS = ("todoist", "gmail", "youtube", "threads", "instagram", "igweb", "thweb", "telegram", "maps", "wolt", "info",
            "checkout", "payment", "phone", "signin")
# Not ampi-tools' own: the engine leaves these tools out of a watcher's AI step.
OUTSIDE_TOOLS = {"tg-read": ("Bash(tg-read:*)",), "web": ("WebSearch", "WebFetch"), "browse": ("Bash(browse:*)",)}
# "changes.*": what changes something of the owner's or acts in their name. Not: looking around in the checkout browser and
# filling in, which lead to a press that is one of these.
CHANGES = frozenset(ws.WRITE_OPS) - {"checkout.snapshot", "checkout.open", "checkout.click", "checkout.type", "checkout.choose",
                                     "checkout.back", "checkout.close", "checkout.fill", "info.reveal", "info.collect"} | {
    "phone.notify", "phone.alarm", "phone.timer", "phone.cancel", "info.save_place", "info.revoke", "telegram.revoke",
    "gmail.download", "signin.fill", "signin.code", "signin.session"}
# Already confirmed in their own way (a fingerprint on the phone), or their result is for Ampi: no "ask each time" for these.
NO_ASK = frozenset({"signin.fill", "signin.code", "signin.session", "gmail.download", "telegram.revoke", "info.revoke"})
MAX_ROLE = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (scope TEXT NOT NULL, op TEXT NOT NULL, mode TEXT NOT NULL, changed REAL NOT NULL,
    PRIMARY KEY (scope, op));
CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, scope TEXT NOT NULL,
    op TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, how TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topics (scope TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',
    seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliver (watcher TEXT PRIMARY KEY, topic TEXT NOT NULL);
"""

SCOPE_RE = re.compile(r"chat|topic:[a-z0-9][a-z0-9_-]{0,31}/[a-z0-9][a-z0-9_-]{0,31}/\d{1,12}|watcher:[a-z0-9][a-z0-9_-]{0,31}/[a-z0-9][a-z0-9_-]{0,39}")
FLOW_RE = re.compile(r"flow:[a-z0-9][a-z0-9_-]{0,39}")

# Mail an outside text would like to see gone: kept in the inbox when an AI step wants it hidden without asking.
SECURITY_RE = re.compile(
    r"secur|sign(ed)?[ -]?in|sign-?on|log(ged)?[ -]?in|password|passcode|passkey|verif|(one[- ]time|access|login|"
    r"confirmation|launch) code|\botp\b|\byour\b.{0,25}\bcode\b|\bcode\b.{0,12}\d{4}|\d{4,8}\b.{0,15}\bcode|2fa|two[- ]factor|"
    r"2[- ]step|suspicious|unusual|unauthori[sz]|fraud|new device|device (was |is |has been )?(added|linked)|recover|"
    r"account (was|has|will|is)|(critical|account|bank) alert|forwarding|api key|(address|number|phone|email) (was |has been )?chang|"
    r"sim swap|withdraw|new payee|безопасност|парол|\bвход(а|е)?\b|вошли|код.{0,12}\d{4}|ваш.{0,12}\bкод|подтвер[дж]|подозрительн|"
    r"мошенни|восстановл|ασφ[αά]λει|κωδικ|σύνδεση|επαλήθευσ|ύποπτ", re.I)


def watcher_scope(profile: str, name: str) -> str:
    return f"watcher:{profile}/{name}"


def topic_scope(profile: str, place: str, thread: int) -> str:
    return f"topic:{profile}/{place}/{thread}"


def kind(scope: str) -> str:
    return scope.split(":", 1)[0]


def private_thread(scope: str) -> tuple[str, int] | None:
    """(profile, thread) of a topic of the owner's private chat."""
    m = re.fullmatch(r"topic:([a-z0-9_-]+)/private/(\d+)", scope or "")
    return (m[1], int(m[2])) if m else None


def effects(op: str, args: dict) -> list[str]:
    """The settings an action falls under: itself, and whatever it amounts to."""
    out = [op]
    if op == "gmail.label":
        add, remove = ([str(x).strip().upper() for x in args.get(k) or []] for k in ("add", "remove"))
        out += [e for cond, e in (("INBOX" in remove, "gmail.archive"), ("INBOX" in add, "gmail.untrash"),
                                  ("UNREAD" in remove, "gmail.mark_read"), ("UNREAD" in add, "gmail.mark_unread"),
                                  ("STARRED" in add, "gmail.star"), ("STARRED" in remove, "gmail.unstar")) if cond]
    elif op == "checkout.flow":
        r = args.get("resolved")
        out.append(f"flow:{args.get('flow') or (r.get('flow') if isinstance(r, dict) else None)}")
    return out


def _label(op: str, args: dict, key: str) -> set[str]:
    return {str(x).strip().upper() for x in args.get(key) or []} if op == "gmail.label" else set()


def hides(op: str, args: dict) -> bool:
    return bool({"gmail.archive", "gmail.trash", "gmail.mark_read"} & set(effects(op, args))) \
        or any(x.startswith("CATEGORY_") for x in _label(op, args, "add"))


def unguards(op: str, args: dict) -> bool:
    """Takes away what keeps an email from being hidden (check_hiding)."""
    return "gmail.unstar" in effects(op, args) or "IMPORTANT" in _label(op, args, "remove")


def strongest(op: str, args: dict) -> str:
    """What the report calls it."""
    found = effects(op, args)
    return next((e for e in ("gmail.trash", "gmail.archive", "gmail.unstar", "gmail.star", "gmail.mark_read", "gmail.mark_unread",
                             "gmail.untrash") if e in found), op)


def change_text(op: str, args: dict) -> str:
    if op != "gmail.label":
        return ""
    return "; ".join(f"{w} {', '.join(map(str, args[k]))}" for k, w in (("add", "added"), ("remove", "removed")) if args.get(k))


def allowed_modes(scope: str, op: str, ops: Any, critical: Any) -> list[str]:
    """What the owner may choose. ops: every operation name; critical: those that ask by themselves."""
    if op == "changes.*":
        return [] if kind(scope) == "watcher" else ["default", "ask", "off"]
    if op.endswith(".*"):
        known = ADAPTERS if scope == "chat" else (*ADAPTERS, *OUTSIDE_TOOLS)
        if kind(scope) == "watcher":
            known = tuple(k for k in known if k != "browse")
        return ["default", "off"] if op[:-2] in known else []
    if FLOW_RE.fullmatch(op):
        return ["default", "off"] if kind(scope) != "watcher" else []
    if op not in ops or (kind(scope) == "watcher" and op in ws.NOT_FOR_WATCHERS):
        return []
    modes = ["default", "off"]
    # (An approved action's result goes to the owner, not back to Ampi: no use for the steps of a checkout, or reading.)
    adapter = op.split(".", 1)[0]
    if adapter != "info" and (op in critical if adapter == "checkout" else ws.is_write(op) or op in critical) \
            and not (adapter == "checkout" and kind(scope) == "watcher"):
        modes.insert(1, "ask")
    if kind(scope) == "watcher" and op in REPORTABLE | REPORT_ONLY:
        modes.append("report")
    return modes


class Permissions:
    def __init__(self, path: Path | str, clock=time.time):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self.clock = clock

    def get(self, scope: str, op: str) -> str:
        row = self.db.execute("SELECT mode FROM settings WHERE scope=? AND op=?", (scope, op)).fetchone()
        if row is None:
            return "default"
        return row["mode"] if row["mode"] in RANK else "ask"

    def all(self, scope: str) -> dict[str, str]:
        return {r["op"]: r["mode"] for r in self.db.execute("SELECT op, mode FROM settings WHERE scope=?", (scope,))}

    def mode(self, scope: str, op: str, args: dict | None = None) -> str:
        """The strictest of everything the action falls under (for a topic: of "chat" too); "report" only if all of it is."""
        if kind(scope) == "topic":
            return min(self._mode(scope, op, args), self._mode("chat", op, args), key=RANK.__getitem__)
        return self._mode(scope, op, args)

    def _mode(self, scope: str, op: str, args: dict | None = None) -> str:
        if op in CHANGES and kind(scope) != "watcher":
            all_changes = self.get(scope, "changes.*")
            if all_changes == "off":
                return "off"
            if all_changes == "ask" and op not in NO_ASK:
                own = self._own_mode(scope, op, args)
                return own if own == "off" else "ask"
        return self._own_mode(scope, op, args)

    def _own_mode(self, scope: str, op: str, args: dict | None = None) -> str:
        kinds = {op.split(".", 1)[0]} | {"checkout.pay": {"payment"}, "checkout.flow": {"signin"},
                                         "checkout.fill": {"info"}}.get(op, set())
        if any(self.get(scope, f"{k}.*") == "off" for k in kinds):
            return "off"
        return min((self.get(scope, e) for e in effects(op, args or {})), key=RANK.__getitem__)

    def off(self, scope: str) -> list[str]:
        out = {op[:-2] for op, m in self.all(scope).items() if op.endswith(".*") and m == "off"}
        return sorted(out | (set(self.off("chat")) if kind(scope) == "topic" else set()))

    def restricted(self, scope: str) -> bool:
        """The owner set something for this very topic."""
        return kind(scope) == "topic" and bool(self.all(scope))

    def refuse_lost(self, ops: list[dict]) -> None:
        """For a process that belongs to no known run: what is off in any topic could be what it was meant to be kept from."""
        for r in self.db.execute("SELECT DISTINCT scope FROM settings WHERE scope LIKE 'topic:%'").fetchall():
            self.refuse(r["scope"], ops)

    def remember(self, key: str, value: list) -> None:
        self.db.execute("INSERT OR REPLACE INTO notes VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def remembered(self, key: str) -> list:
        r = self.db.execute("SELECT value FROM notes WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else []

    # --- topics

    def seen(self, scope: str, title: str) -> None:
        self.db.execute("INSERT INTO topics (scope, title, seen) VALUES (?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                        "seen=excluded.seen, title=CASE WHEN excluded.title='' THEN title ELSE excluded.title END",
                        (scope, title[:80], self.clock()))
        self.db.commit()

    def topics(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM topics ORDER BY seen DESC")]

    def role(self, scope: str) -> str:
        r = self.db.execute("SELECT role FROM topics WHERE scope=?", (scope,)).fetchone()
        return r["role"] if r else ""

    def set_role(self, scope: str, role: str, how: str) -> None:
        self.seen(scope, "")
        old = self.role(scope)
        with self.db:
            self.db.execute("UPDATE topics SET role=? WHERE scope=?", (role, scope))
            self.db.execute("INSERT INTO changes (at, scope, op, old, new, how) VALUES (?, ?, 'role', ?, ?, ?)",
                            (self.clock(), scope, old[:200] or "none", role[:200] or "none", how))

    def forget_topic(self, scope: str) -> None:
        """Only a topic that is gone or not limited: forgetting must not be a way around its limits."""
        self.forget(scope, "the topic was forgotten")
        with self.db:
            self.db.execute("DELETE FROM topics WHERE scope=?", (scope,))
            self.db.execute("DELETE FROM deliver WHERE topic=?", (scope,))

    def delivery(self, watcher: str) -> str:
        r = self.db.execute("SELECT topic FROM deliver WHERE watcher=?", (watcher,)).fetchone()
        return r["topic"] if r else ""

    def set_delivery(self, watcher: str, topic: str, how: str) -> None:
        old = self.delivery(watcher)
        with self.db:
            self.db.execute("DELETE FROM deliver WHERE watcher=?", (watcher,))
            if topic:
                self.db.execute("INSERT INTO deliver VALUES (?, ?)", (watcher, topic))
            self.db.execute("INSERT INTO changes (at, scope, op, old, new, how) VALUES (?, ?, 'messages go to', ?, ?, ?)",
                            (self.clock(), watcher, old or "main chat", topic or "main chat", how))

    def refuse(self, scope: str, ops: list[dict]) -> None:
        for o in ops:
            if self.mode(scope, o["op"], o["args"]) == "off":
                raise ToolError(f"the owner switched {o['op']} off" + {"watcher": " for this watcher", "topic": " here or everywhere"}.get(kind(scope), "")
                                + " (Ampi hub); don't try another way")

    def apply(self, scope: str, changes: dict[str, list[str]], how: str) -> None:
        """changes: op -> [old, new]. All of them or none; refused if any isn't `old` any more."""
        for op, (old, _) in changes.items():
            if self.get(scope, op) != old:
                raise ToolError(f"{op} was changed meanwhile; nothing was changed now")
        with self.db:
            for op, (old, new) in changes.items():
                if new == "default":
                    self.db.execute("DELETE FROM settings WHERE scope=? AND op=?", (scope, op))
                else:
                    self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?, ?)", (scope, op, new, self.clock()))
                self.db.execute("INSERT INTO changes (at, scope, op, old, new, how) VALUES (?, ?, ?, ?, ?, ?)",
                                (self.clock(), scope, op, old, new, how))

    def forget(self, scope: str, how: str = "the watcher was deleted") -> None:
        """A deleted watcher: a later one of the same name starts with nothing allowed."""
        self.apply(scope, {op: [self.get(scope, op), "default"] for op in self.all(scope)}, how)
        with self.db:
            self.db.execute("DELETE FROM deliver WHERE watcher=?", (scope,))

    def log(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,))]


def words(scope: str) -> str:
    if kind(scope) == "topic":
        _, place, thread = scope.split("/")
        where = "the private chat" if place == "private" else f"group {place}"
        return where + (f", topic {thread}" if thread != "0" else ", outside topics")
    return "your chats and groups" if scope == "chat" else f"watcher {scope.split('/', 1)[1]}"


async def email_items(services: Any, ids: list[str]) -> list[dict]:
    """Who and what, before anything happens to them (for the report and the checks)."""
    from .gmail import headers
    out = []
    for m in await services.gmail.metadata(ids):
        h = headers(m)
        out.append({"id": str(m.get("id", "")), "subject": h.get("subject") or "(no subject)", "from": h.get("from") or "?",
                    "labels": list(m.get("labelIds") or []), "at": int(m.get("internalDate") or 0) / 1000})
    return out


def check_hiding(op: str, items: list[dict], ids: list[str], now: float) -> None:
    """An AI step that read outside text can't make mail like this disappear unseen."""
    by = {i["id"]: i for i in items}
    kept = []
    for mid in ids:
        i = by.get(mid)
        why = None
        if i is None:
            why = "couldn't be checked"
        elif "STARRED" in i["labels"]:
            why = "starred"
        elif SECURITY_RE.search(i["subject"]):
            why = "about security, a code or an account"
        elif op == "gmail.trash" and "IMPORTANT" in i["labels"]:
            why = "marked important"
        elif op == "gmail.trash" and ("INBOX" not in i["labels"] or now - i["at"] > TRASH_MAX_AGE):
            why = "not new mail in the inbox"
        if why:
            kept.append(f"{mid} ({why})")
    if kept:
        raise ToolError("these stay in the inbox unless the owner moves them: " + ", ".join(kept[:MAX_ITEMS])
                        + ". Nothing was changed; repeat without them")
