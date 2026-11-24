"""Scenarios: bookings a fixed script carries out (docs/FLOWS.md; registered into ops.OPS).

Ampi only names a scenario, a date and a time. What is pressed, and what has to be on the page before and after, is in a contract the
owner's server keeps (flows/*.json in the daemon's home: outside the repository). One approval covers the whole task, and one
press that books: never a second one for the same slot without the owner saying so.

Nothing here presses anything by itself. A press goes the way Ampi's own presses go (ops_checkout: what `click` would refuse is bound and
checked as for `submit`, and pressed only if nothing changed since); instead of the owner, the contract says whether it is the
expected one. Whatever those refuse stays refused: prices on the button, totals, payment frames, captchas, disguised captions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import ops as _ops
from .ops import FIELDS, OPS, PRE_RESOLVE, Op, Result, Services, _bool, _date, clean, need
from .ops_checkout import AMOUNT_RE, PRIVATE_ONLY, CheckoutRefused, _check_live, _money, _resolve_submit, checkout_submit, commits, cs
from .todoist import ToolError

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
SITE_RE = re.compile(r"https://(?:[a-z0-9-]{1,63}\.)+[a-z]{2,63}")
TTL = 1800            # a request is good for half an hour
AHEAD_DAYS = 90
WAIT, EVERY, CONFIRM = 24, 2, 60   # seconds for a page to become what is expected; between two looks
TOTAL = 420           # seconds for the whole task
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


# Next to the button that books: something about money that no total and no price may say.
MONEY_WORDS = re.compile(r"\b(visa|master ?card|amex|american express|ending (in|with)|credits?|charged?|pay(ment|able)?|paid|fees?|deposit|cost|price|balance|plan|subscri(be|ption)|will be used)\b", re.I)


OTHER_AMOUNT = re.compile(r"\b(?:eur|usd|gbp|chf|sek|nok|dkk|pln)\s?\d[\d.,]*|(?<![\d.,])\d[\d.,]*\s?(?:chf|sek|nok|dkk|pln|euros?|dollars?|pounds?)\b", re.I)


def has(text: str, part: str) -> bool:
    """`part` as it is, not inside a longer word or number (7 December in 27 December)."""
    return bool(re.search(r"(?<![\w.,:])" + re.escape(norm(part)) + r"(?![\w:])(?![.,]\d)", norm(text)))


def amounts(text: Any) -> set[str]:
    """The prices in a text that aren't nothing."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    found = {re.sub(r"\s", "", m.group()) for x in (AMOUNT_RE, OTHER_AMOUNT) for m in x.finditer(t)}
    return {x.strip(".,") for x in found if re.search(r"[1-9]", x)}


def norm(text: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).split()).casefold()


def flow_name(v: Any) -> str:
    if not isinstance(v, str) or not NAME_RE.fullmatch(v):
        raise ToolError("give the scenario's name as `checkout flows` shows it")
    return v


def flow_time(v: Any) -> str:
    if not isinstance(v, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        raise ToolError("times must be HH:MM (24 hours)")
    return v


FIELDS.update({"flow": flow_name, "flow_date": _date, "flow_time": flow_time, "again": _bool})


# --- contracts -----------------------------------------------------------------------------------

KEYS = {"title", "place", "site", "start", "engine", "timezone", "frames", "login", "steps", "check", "fields", "book", "amounts", "confirm", "max_per_day"}


def parse(raw: bytes) -> dict:
    """A contract, or ToolError: every key is known and every pattern compiles."""
    try:
        c = json.loads(raw)
    except ValueError:
        raise ToolError("the contract isn't JSON") from None

    def bad(what: str) -> ToolError:
        return ToolError(f"the contract's {what} isn't right")

    def strs(v: Any, what: str, n: int = 8, least: int = 1) -> list[str]:
        if not isinstance(v, list) or not least <= len(v) <= n or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in v):
            raise bad(what)
        return v

    def patterns(v: Any, what: str) -> None:
        for x in strs(v, what):
            try:
                re.compile(x)
            except re.error:
                raise bad(what) from None

    if not isinstance(c, dict) or set(c) != KEYS:
        raise bad("list of keys")
    for k in ("title", "place"):
        strs([c[k]], k)
    if not isinstance(c["site"], str) or not SITE_RE.fullmatch(c["site"]):
        raise bad("site")
    if not isinstance(c["start"], str) or not c["start"].startswith(c["site"] + "/"):
        raise bad("start")
    if c["engine"] not in ("obscura", "lightpanda"):
        raise bad("engine")
    try:
        ZoneInfo(c["timezone"])
    except Exception:
        raise bad("timezone") from None
    if any(not SITE_RE.fullmatch(x) for x in strs(c["frames"], "frames", least=0)):
        raise bad("frames")
    g = c["login"]
    if not isinstance(g, dict) or set(g) != {"user", "button", "done"}:
        raise bad("login")
    strs([g["user"]], "login")
    strs(g["button"], "login", 3)
    patterns([g["done"]], "login")
    if not isinstance(c["steps"], list) or not 1 <= len(c["steps"]) <= 20:
        raise bad("steps")
    kinds = []
    for s in c["steps"]:
        if not isinstance(s, dict):
            raise bad("steps")
        if "press" in s and set(s) <= {"press", "optional"}:
            strs(s["press"], "steps", 3)
            if s.get("optional", True) is not True:
                raise bad("steps")
            kinds.append("press")
        elif s == {"login": True}:
            kinds.append("login")
        elif set(s) == {"slot"}:
            t = s["slot"]
            if not isinstance(t, dict) or set(t) != {"name", "time", "next", "zone"}:
                raise bad("steps")
            strs([t["name"], t["zone"]], "steps")
            if t["name"].count("{date}") != 1 or t["time"] not in ("12h", "24h"):
                raise bad("steps")
            strs(t["next"], "steps", 3)
            kinds.append("slot")
        else:
            raise bad("steps")
    if kinds.count("login") != 1 or kinds.count("slot") != 1 or kinds.index("login") > kinds.index("slot"):
        raise bad("steps")
    strs(c["check"], "check")
    if not any("{when}" in x for x in c["check"]):
        raise bad("check")
    if not isinstance(c["fields"], dict) or len(c["fields"]) > 12:
        raise bad("fields")
    for k, v in c["fields"].items():
        strs([k], "fields")
        if not isinstance(v, str) or not re.fullmatch(r"@login|@me\.[a-z_.]{1,30}", v):
            patterns([v], "fields")
    strs(c["amounts"], "amounts", 20, 0)
    strs(c["book"], "book", 3)
    f = c["confirm"]
    if not isinstance(f, dict) or set(f) != {"text", "order"}:
        raise bad("confirm")
    patterns([x for x in f["text"]] if isinstance(f["text"], list) else None, "confirm")
    if not all(any(k in x for x in f["text"]) for k in ("{day}", "{minute}")):
        raise bad("confirm")
    patterns([f["order"]], "confirm")
    if re.compile(f["order"]).groups != 1:
        raise bad("confirm")
    if isinstance(c["max_per_day"], bool) or not isinstance(c["max_per_day"], int) or not 1 <= c["max_per_day"] <= 5:
        raise bad("max_per_day")
    return c


def load(folder: Path, name: str) -> tuple[dict, str]:
    path = folder / f"{flow_name(name)}.json"
    try:
        raw = path.read_bytes()
    except OSError:
        raise ToolError("no such scenario; `checkout flows` lists them") from None
    if len(raw) > 64_000:
        raise ToolError("that contract is too big")
    return parse(raw), hashlib.sha256(raw).hexdigest()


def said(when: datetime) -> str:
    """As pages in English say it: 27 November 2026 at 6:30 pm."""
    return f"{when.day} {MONTHS[when.month - 1]} {when.year} at {clock(when, '12h')}"


def clock(when: datetime, how: str) -> str:
    return when.strftime("%H:%M") if how == "24h" else f"{when.hour % 12 or 12}:{when.minute:02d} {'am' if when.hour < 12 else 'pm'}"


def slot_time(c: dict, day: str, hour: str, now: datetime) -> datetime:
    zone = ZoneInfo(c["timezone"])
    try:
        naive = datetime.fromisoformat(f"{day}T{hour}")
    except ValueError:
        raise ToolError("that date doesn't exist") from None
    when = naive.replace(tzinfo=zone)
    if when.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
        raise ToolError("the clocks change at that time; the owner books it")
    if when <= now:
        raise ToolError("that time is over")
    if when > now + timedelta(days=AHEAD_DAYS):
        raise ToolError(f"at most {AHEAD_DAYS} days ahead")
    return when


# --- the journal ---------------------------------------------------------------------------------


class Journal:
    """What was booked, and what may have been. Written before the press: after a crash it is known that it may have happened."""

    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, flow TEXT, site TEXT, slot TEXT, status TEXT, created REAL,
                                             changed REAL, approval INTEGER, note TEXT, old INTEGER, was TEXT, unclear INTEGER);
            CREATE UNIQUE INDEX IF NOT EXISTS one_per_slot ON runs (site, slot) WHERE status IN ('reserved','pressing','done','unknown');
            CREATE TABLE IF NOT EXISTS open (session TEXT);
        """)
        for r in self.db.execute("SELECT id, unclear FROM runs WHERE status='reserved'").fetchall():
            self.set(r["id"], "unknown" if r["unclear"] else "failed", time.time(), "the service stopped before it was finished")
        self.db.execute("UPDATE runs SET status='unknown', note='the service stopped while booking' WHERE status='pressing'")

    def taken(self, site: str, slot: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE site=? AND slot=? AND status IN ('reserved','pressing','done','unknown')",
                               (site, slot)).fetchone()

    def today(self, site: str, now: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE site=? AND created>? AND status IN "
                               "('reserved','pressing','done','unknown','replaced')", (site, now - 86400)).fetchone()[0]

    def reserve(self, flow: str, site: str, slot: str, limit: int, again: bool, approval: int | None, now: float) -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.taken(site, slot)
            if old is not None and (old["status"] in ("reserved", "pressing") or not again):
                raise ToolError(already(old))
            if self.today(site, now) >= limit:
                raise ToolError(f"the limit of this scenario ({limit} in 24 hours) is reached")
            if old is not None:
                self.db.execute("UPDATE runs SET status='replaced', changed=? WHERE id=?", (now, old["id"]))
            row = self.db.execute("INSERT INTO runs (flow, site, slot, status, created, changed, approval, old, was) VALUES (?,?,?,'reserved',?,?,?,?,?)",
                                  (flow, site, slot, now, now, approval, *((old["id"], old["status"]) if old is not None else (None, None)))).lastrowid
            self.db.execute("COMMIT")
            return int(row or 0)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def set(self, row: int, status: str, now: float, note: str = "") -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE runs SET status=?, changed=?, note=? WHERE id=?", (status, now, note[:300], row))
            r = self.db.execute("SELECT * FROM runs WHERE id=?", (row,)).fetchone()
            if status == "failed" and r["old"] is not None:  # nothing came of it: what was known of the slot before still counts
                self.db.execute("UPDATE runs SET status=? WHERE id=? AND status='replaced'", (r["was"], r["old"]))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def unclear(self, row: int, is_: bool) -> None:
        """A press before the last one may have done more than it should have."""
        self.db.execute("UPDATE runs SET unclear=? WHERE id=?", (int(is_), row))

    def opened(self, session: str) -> None:
        self.db.execute("DELETE FROM open")
        if session:
            self.db.execute("INSERT INTO open VALUES (?)", (session,))

    def left_open(self) -> str:
        r = self.db.execute("SELECT session FROM open").fetchone()
        return r["session"] if r else ""


def already(old: sqlite3.Row) -> str:
    if old["status"] == "unknown":
        return ("a booking of that slot was started before, and it MAY have gone through: the owner looks at the site or their email first. Only "
                "if they then say it isn't booked: --again")
    if old["status"] == "done":
        return "that slot was booked already; only if the owner says it is to be booked once more (they cancelled it, say): --again"
    return "that slot is being booked right now"


class Flows:
    def __init__(self, folder: Path, journal: Journal):
        self.folder, self.journal = folder, journal
        self.running = False
        self.stuck = journal.left_open()  # a session of a scenario that wasn't closed ("?": which one isn't known)

    @property
    def busy(self) -> bool:
        """A scenario uses the checkout browser, or left a session open there."""
        return self.running or bool(self.stuck)


def flows(s: Services) -> Flows:
    if s.flows is None:
        raise ToolError("scenarios aren't set up")
    return s.flows


# --- the run -------------------------------------------------------------------------------------


class Run:
    def __init__(self, s: Services, c: dict, when: datetime, sleep: Any = asyncio.sleep):
        self.s, self.c, self.when, self.sleep = s, c, when, sleep
        self.note: Any = lambda unclear: None
        self.opened: Any = lambda session: None
        self.unclear = self.signed_in = self.opening = False
        self.side = cs(s)
        self.session = ""
        self.at = "opening the site"

    def stop(self, why: str) -> ToolError:
        return ToolError(self.side.scrub(f"stopped while {self.at}: {why}"))

    def safe(self, snap: dict) -> None:
        if snap.get("captcha"):
            raise self.stop("a captcha / bot check is showing; the owner does this one")
        if snap.get("origin") != self.c["site"]:
            raise self.stop("the browser is on another site")
        others = {str(f.get("origin")) for f in snap.get("frames") or []} - {self.c["site"], "opaque", *self.c["frames"]}
        if others:
            raise self.stop(f"the page has parts of other sites ({clean(', '.join(sorted(others)), 120)})")
        if any(_money(t) for t in [*(snap.get("totals") or []), *(snap.get("gate_totals") or [])]):
            raise self.stop("the page shows a total to pay")

    async def look(self) -> dict | None:
        """The page; None while it is changing."""
        try:
            snap = (await self.side.client.call({"cmd": "snapshot", "session": self.session}, timeout=40)).get("snapshot") or {}
        except CheckoutRefused:
            raise
        except ToolError as e:
            if "page changed" in str(e) or "didn't answer" in str(e):
                return None
            raise
        if snap.get("origin") == "opaque" and not snap.get("captcha"):
            return None
        self.safe(snap)
        if re.search(self.c["confirm"]["order"], str(snap.get("text") or "")):
            self.mark(True)
            raise self.stop("the page shows an order already")
        return snap

    def mark(self, unclear: bool) -> None:
        """Whether a press after signing in may have done more than lead on (until the next step's page is there)."""
        if self.unclear != unclear:
            self.unclear = unclear
            self.note(unclear)

    def own(self, snap: dict) -> list[dict]:
        top = str(((snap.get("frames") or [{}])[0]).get("id") or "f0")
        return [e for e in snap.get("elements") or [] if e.get("frame") == top and e.get("frame_origin") == self.c["site"]]

    def buttons(self, snap: dict, captions: list[str]) -> list[dict]:
        want = {norm(x) for x in captions}
        return [e for e in self.own(snap) if e.get("kind") in ("button", "link") and norm(e.get("label")) in want]

    async def until(self, ready: Any, seconds: float = WAIT) -> Any:
        """What `ready(snapshot)` finds, once it finds something; None if it doesn't in time."""
        for i in range(max(1, int(seconds // EVERY))):
            if i:
                await self.sleep(EVERY)
            snap = await self.look()
            found = ready(snap) if snap is not None else None
            if found:
                return found
        return None

    @staticmethod
    def shape(snap: dict) -> tuple:
        return (snap.get("url"), str(snap.get("text") or "")[:20000], tuple((e.get("ref"), e.get("label"), e.get("disabled")) for e in snap.get("elements") or []))

    async def settled(self, captions: list[str], optional: bool = False) -> dict | None:
        """The button, once the page has stopped changing (a page may show its buttons before they work). Several with that caption
        only where pressing any of them is the same."""
        last: list = [None]

        def ready(snap: dict) -> Any:
            bs = [e for e in self.buttons(snap, captions) if not e.get("disabled")]
            now = (snap.get("url"), len(self.own(snap)), [(e.get("ref"), e.get("label"), e.get("disabled")) for e in bs])
            same, last[0] = last[0] == now, now
            return bs if bs and same else None

        found = await self.until(ready, 10 if optional else WAIT + 6)
        if not found:
            if optional:
                return None
            raise self.stop(f"there is no “{clean(captions[0], 60)}”")
        if len(found) > 1:
            facts = [await self.side.client.call({"cmd": "gate", "session": self.session, "ref": e["ref"]}) for e in found[:6]]
            if len(found) > 6 or any([g.get(k) for k in ("texts", "dest", "method", "submits", "tag")] != [facts[0].get(k) for k in ("texts", "dest", "method", "submits", "tag")]
                                     for g in facts):
                raise self.stop(f"there are different “{clean(captions[0], 60)}”")
        return found[0]

    async def bind(self, e: dict, captions: list[str]) -> dict:
        """What pressing it would really do, checked as a `checkout submit`, and then against the contract."""
        r = (await _resolve_submit(self.s, {"ref": e["ref"], "what": self.c["title"][:100]}))["resolved"]
        b, press = r["button"], r["press"]
        if r["session"] != self.session or r["origin"] != self.c["site"] or b.get("frame_origin") != self.c["site"] or not b.get("main", True):
            raise self.stop("that isn't the site's own button")
        want = {norm(x) for x in captions}
        if not {norm(x) for x in press.get("texts") or [] if norm(x)} <= want or norm(b.get("label")) not in want:
            raise self.stop("the button says something else, too")
        if r.get("dest_origin") not in ("", self.c["site"]):
            raise self.stop("the button sends to another site")
        if set(r["frames"]) - {self.c["site"], "opaque", "null", *self.c["frames"]}:
            raise self.stop("the page has parts of other sites")
        if any(_money(t) for t in r["totals"]):
            raise self.stop("the page shows a total to pay")
        return r

    async def press(self, find: Any, captions: list[str]) -> None:
        """Presses what `find()` finds: before the sign-in as `checkout click` would, if that is harmless; afterwards always bound to
        what was checked. A refusal (the page changed just then) is tried again; what may have been pressed is not."""
        for turn in range(3):
            e = await find()
            if not e:
                return
            if not self.signed_in:
                g = await self.side.client.call({"cmd": "gate", "session": self.session, "ref": e["ref"]})
                if commits(g, self.side) is None and {norm(x) for x in g.get("texts") or [] if norm(x)} <= {norm(x) for x in captions}:
                    await self.side.client.call({"cmd": "click", "session": self.session, "ref": e["ref"]}, timeout=90)
                    return
            try:
                bound = await self.bind(e, captions)
                self.mark(self.signed_in)
                await checkout_submit(self.s, {"resolved": bound})
                return
            except CheckoutRefused:  # not pressed
                self.mark(False)
                if turn == 2:
                    raise
                await self.sleep(EVERY)
            except ToolError as err:
                if "MAY have gone through" in str(err):
                    return  # (the page went on while it was pressed) the next step's page tells
                if "page changed" in str(err) and turn < 2 and not self.unclear:
                    await self.sleep(EVERY)
                    continue
                raise

    async def login(self) -> None:
        g = self.c["login"]
        done = re.compile(g["done"])

        def form(snap: dict) -> Any:
            es = self.own(snap)
            secrets = [i for i, e in enumerate(es) if e.get("kind") == "input" and e.get("type") == "password"]
            if len(secrets) != 1:
                return None
            users = [e for e in es[:secrets[0]] if e.get("kind") == "input" and e.get("type") in ("email", "text")
                     and norm(e.get("label")) == norm(g["user"])]
            return (users[-1], es[secrets[0]]) if users else None

        self.at = "signing in"
        found = await self.until(form)
        if not found:
            raise self.stop("the sign-in form isn't there")
        user, secret = found
        await OPS["signin.fill"].run(self.s, {"user_ref": user["ref"], "password_ref": secret["ref"]})

        self.signed_in = True

        def button(snap: dict) -> Any:  # the first one after the password, once it can be pressed
            es = self.own(snap)
            at = next((i for i, e in enumerate(es) if e.get("ref") == secret["ref"]), None)
            first = [] if at is None else [e for e in es[at + 1:] if e in self.buttons(snap, g["button"])][:1]
            return first[0] if first and not first[0].get("disabled") else None

        async def find() -> dict:
            b = await self.until(button)
            if not b:
                raise self.stop("the sign-in button isn't there")
            return b

        await self.press(find, g["button"])

        def ready(snap: dict) -> bool:
            return bool(done.search(str(snap.get("text") or ""))) and not any(e.get("type") == "password" for e in self.own(snap))

        self.mark(False)  # (it only signs in)
        if not await self.until(ready, 40):
            raise self.stop("the site didn't accept the sign-in (or asks for something more)")

    async def slot(self, t: dict) -> None:
        self.at = "choosing the slot"
        name = re.compile(re.escape(t["name"]).replace(re.escape("{date}"), r"(\d{4}-\d{2}-\d{2})"))
        day, label = self.when.date(), norm(clock(self.when, t["time"]))

        def slots(snap: dict) -> list[tuple[date, dict]]:
            out = []
            for e in self.own(snap):
                m = name.fullmatch(str(e.get("name") or "")) if e.get("kind") in ("radio", "checkbox") else None
                if m:
                    with contextlib.suppress(ValueError):
                        out.append((date.fromisoformat(m.group(1)), e))
            return out

        zone = t["zone"].replace("{offset}", offset(self.when))
        if not await self.until(lambda snap: self.buttons(snap, t["next"])):
            raise self.stop("the calendar isn't there")
        self.mark(False)
        if not await self.until(lambda snap: has(str(snap.get("text") or ""), zone)):
            raise self.stop(f"the calendar doesn't say “{clean(zone, 60)}”")
        now = datetime.now(self.when.tzinfo).date()
        weeks = (day - timedelta(days=day.weekday()) - (now - timedelta(days=now.weekday()))).days // 7

        async def at() -> list[tuple[date, dict]]:
            return (await self.until(slots, 12)) or []

        def mine(snap: dict) -> list[dict]:
            return [e for d, e in slots(snap) if d == day and norm(e.get("label")) == label and not e.get("disabled")]

        for i in range(weeks + 2):  # (one more: which day a week begins with)
            seen = await at()
            here = [e for d, e in seen if d == day and norm(e.get("label")) == label and not e.get("disabled")]
            if here:
                break
            if any(d >= day for d, _ in seen):
                free = [clean(e.get("label"), 30) for d, e in seen if d == day]
                raise self.stop("that slot isn't free" + (f" (that day: {', '.join(free[:6])})" if free else " (nothing that day)"))
            if i == weeks + 1:
                raise self.stop("that slot isn't there")
            shape = self.shape(await self.look() or {})[1]
            first = min((d for d, _ in seen), default=None)
            await self.press(lambda: self.settled(t["next"]), t["next"])

            def moved(snap: dict) -> bool:  # (the heading may change before the slots do)
                days = [d for d, _ in slots(snap)]
                return bool(days) and (min(days) > first if first else str(snap.get("text") or "")[:20000] != shape)

            if not await self.until(moved):  # a week without any?
                snap = await self.look()
                if snap is None or slots(snap) or str(snap.get("text") or "")[:20000] == shape or not self.buttons(snap, t["next"]):
                    raise self.stop("the calendar doesn't move on")
            self.mark(False)
        if len(here) != 1:
            raise self.stop("the slot is there more than once")

        async def find() -> dict | None:
            e = (await self.until(mine) or [None])[0]
            if e is None:
                raise self.stop("the slot went away")
            return None if e.get("checked") else e

        await self.press(find, [str(here[0].get("label"))])
        if here[0].get("checked") is not None and not await self.until(lambda snap: [e for e in mine(snap) if e.get("checked")], 10):
            raise self.stop("the slot wasn't chosen")
        self.mark(False)

    def check(self, around: str, form: list[dict], page: str) -> None:
        """The last look: what stands around the button, which is what the press is bound to, and what is filled in."""
        for x in self.c["check"]:
            x = x.replace("{when}", said(self.when))
            if not has(around, x):
                raise self.stop(f"the page doesn't say “{clean(x, 80)}” next to the button")
        if MONEY_WORDS.search(unicodedata.normalize("NFKC", around)) or amounts(around):
            raise self.stop("there is something about money next to the button")
        other = amounts(page) - {re.sub(r"\s", "", x) for x in self.c["amounts"]}
        if other:
            raise self.stop(f"the page shows a price ({clean(', '.join(sorted(other)), 60)})")
        want = {norm(k): v for k, v in self.c["fields"].items()}
        for f in form:
            k, v = norm(f.get("label")), f.get("value")
            rule = want.pop(k, None)
            if rule is None:
                raise self.stop(f"something is filled in that isn't in the contract (or twice): “{clean(f.get('label'), 60)}”")
            if f.get("shown") is not True:
                raise self.stop(f"“{clean(f.get('label'), 60)}” is filled in, but can't be seen on the page")
            v = "on" if v is True else str(v)
            if rule == "@login":
                good = v == "•••"
            elif rule.startswith("@"):
                own = self.side.vault.value(rule[1:])
                good = norm(v) == norm(own) != ""
                if rule == "@me.phone" and not good:  # with or without the country, spaces
                    a, b = sorted((re.sub(r"\D", "", v).lstrip("0"), re.sub(r"\D", "", own).lstrip("0")), key=len)
                    good = len(a) >= 8 and b.endswith(a) and not re.search(r"[^\d\s()+.-]", v)
            else:
                good = bool(re.fullmatch(rule, v))
            if not good:
                raise self.stop(f"“{clean(f.get('label'), 60)}” isn't filled in as the contract says")

    async def go(self, before: Any) -> str:
        """The order number. `before` is called right before the press that books."""
        client = self.side.client
        if (await client.call({"cmd": "status"})).get("session"):
            raise self.stop("the checkout browser is in use")
        self.opening = True
        self.opened("?")
        r = await client.call({"cmd": "open", "url": self.c["start"], "engine": self.c["engine"]}, timeout=90)
        self.session = str(r.get("session") or "")
        self.opened(self.session)
        for step in self.c["steps"]:
            if "press" in step:
                self.at = f"looking for “{clean(step['press'][0], 60)}”"
                if self.signed_in and {norm(x) for x in step["press"]} & {norm(x) for x in self.c["book"]}:
                    # The same words as the button that books: only where no slot is chosen yet.
                    if not await self.until(lambda snap: not has(str(snap.get("text") or ""), said(self.when)) and self.buttons(snap, step["press"])):
                        raise self.stop("a slot is chosen there already")

                async def find(step: dict = step) -> dict | None:
                    e = await self.settled(step["press"], bool(step.get("optional")))
                    if e:
                        self.mark(False)
                    return e

                await self.press(find, step["press"])
            elif "login" in step:
                await self.login()
            else:
                await self.slot(step["slot"])
        self.at = "checking the booking"
        if not await self.until(lambda snap: has(str(snap.get("text") or ""), said(self.when)) and self.buttons(snap, self.c["book"])):
            raise self.stop("the booking page doesn't show that date and time")
        self.mark(False)
        button = await self.settled(self.c["book"])
        snap = await self.look()
        found = self.buttons(snap or {}, self.c["book"])
        if snap is None or len(found) != 1 or found[0]["ref"] != (button or {}).get("ref"):
            raise self.stop("there isn't exactly one button that books")
        bound = await self.bind(found[0], self.c["book"])
        raw = await client.call({"cmd": "bind_submit", "session": self.session, "ref": found[0]["ref"]})
        page = await self.look()
        if page is None or not bound.get("digest") or (raw.get("digest"), raw.get("gen"), raw.get("seen")) != (bound["digest"], bound["gen"], bound["seen"]):
            raise self.stop("the page keeps changing")
        self.check(str(raw.get("around") or ""), raw.get("form") or [], str(page.get("text") or ""))
        if self.when <= datetime.now(self.when.tzinfo):
            raise self.stop("that time is over")
        await _check_live(self.side, bound, "booked")
        before()
        why = "the site shows no confirmation"
        try:
            await checkout_submit(self.s, {"resolved": bound})
        except CheckoutRefused:
            raise
        except ToolError as e:  # it may have been pressed: what the page says now decides
            why = str(e)
        self.at = "reading the confirmation"
        return await self.confirmed(why)

    async def confirmed(self, why: str) -> str:
        f = self.c["confirm"]
        words = {"{day}": str(self.when.day), "{year}": str(self.when.year), "{minute}": f"{self.when.minute:02d}",
                 "{hour12}": str(self.when.hour % 12 or 12), "{hour}": f"{self.when.hour:02d}"}
        texts = []
        for x in f["text"]:
            for k, v in words.items():
                x = x.replace(k, v)
            texts.append(x)
        for i in range(CONFIRM // EVERY):
            if i:
                await self.sleep(EVERY)
            # (A total of nothing is part of the confirmation: not `look`.)
            try:
                snap = (await self.side.client.call({"cmd": "snapshot", "session": self.session}, timeout=40)).get("snapshot") or {}
            except CheckoutRefused:
                break
            except ToolError:
                continue
            text = str(snap.get("text") or "")
            m = re.search(f["order"], text)
            if snap.get("origin") == self.c["site"] and m and all(re.search(x, text) for x in texts):
                return clean(m.group(1), 40)
        raise ToolError(why)


def offset(when: datetime) -> str:
    """As in GMT+2 or GMT+5:30."""
    m = int((when.utcoffset() or timedelta()).total_seconds() // 60)
    return f"GMT{'-' if m < 0 else '+'}{abs(m) // 60}" + (f":{abs(m) % 60:02d}" if m % 60 else "")


# --- operations ----------------------------------------------------------------------------------


async def flow_list(s: Services, a: dict) -> Result:
    fl = flows(s)
    lines = []
    for p in sorted(fl.folder.glob("*.json")):
        try:
            c, _ = load(fl.folder, p.stem)
            lines.append(f"{p.stem}: {clean(c['title'], 100)}, {clean(c['place'], 100)} (times in {c['timezone']})")
        except ToolError as e:
            lines.append(f"{clean(p.stem, 40)}: can't be used ({e})")
    rows = fl.journal.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10").fetchall()
    if rows:
        lines += ["Lately:", *(f"  {clean(r['flow'], 40)} {r['slot']}: {r['status']} {clean(r['note'], 100)}" for r in rows)]
    return "\n".join(lines) or "No scenarios yet."


async def _resolve_flow(s: Services, a: dict) -> dict:
    name, day, hour = need(a, "flow", "flow_date", "flow_time")
    fl = flows(s)
    side = cs(s)
    if s.signin_client is None:
        raise ToolError("saved logins aren't set up on the server")
    c, digest = load(fl.folder, name)
    when = slot_time(c, day, hour, datetime.now(ZoneInfo(c["timezone"])))
    old = fl.journal.taken(c["site"], f"{day} {hour}")
    if old is not None and (not a.get("again") or old["status"] in ("reserved", "pressing")):
        raise ToolError(already(old))
    if fl.journal.today(c["site"], side.clock()) >= c["max_per_day"]:
        raise ToolError(f"the limit of this scenario ({c['max_per_day']} in 24 hours) is reached")
    await free(s)
    if fl.busy:
        raise ToolError("another scenario is running; try again in a few minutes")
    if (await side.client.call({"cmd": "status"})).get("session"):
        raise ToolError("a checkout session is open: close it first (`checkout close`)")
    return {"resolved": {"flow": name, "day": day, "hour": hour, "digest": digest, "again": bool(a.get("again")) and old is not None,
                         "old": old["id"] if old is not None else None, "state": old["status"] if old is not None else "",
                         "expires": side.clock() + TTL, "when": when.isoformat()}}


async def describe_flow(s: Services, a: dict) -> str:
    r = a.get("resolved") or {}
    c, digest = load(flows(s).folder, r["flow"])
    if digest != r["digest"]:
        raise ToolError("the contract changed meanwhile; ask again")
    when = datetime.fromisoformat(r["when"])
    lines = [f"Book: {clean(c['title'], 120)}", f"When: {when.strftime('%A')} {said(when)} ({clean(c['timezone'], 40)})",
             f"Where: {clean(c['place'], 120)}, on {c['site']}",
             "Signs in with your saved login: your phone asks for a fingerprint right after this.",
             "Books exactly this slot, once. Stops if it isn't free, or if the page shows a total, a payment form or anything unexpected."]
    if r.get("again"):
        lines.append("⚠ AGAIN: " + ("this slot was booked already" if r["state"] == "done" else "an earlier try MAY have gone through: look first"))
    return cs(s).scrub("\n".join(lines))


async def flow_run(s: Services, a: dict, sleep: Any = asyncio.sleep) -> Result:
    r = a.get("resolved")
    if not isinstance(r, dict):
        raise ToolError("internal: not checked first")
    fl, side = flows(s), cs(s)
    if side.clock() > r["expires"]:
        raise ToolError("this request expired; nothing was booked. Ask Ampi again")
    c, digest = load(fl.folder, r["flow"])
    if digest != r["digest"]:
        raise ToolError("the contract changed since you approved; nothing was booked")
    when = slot_time(c, r["day"], r["hour"], datetime.now(ZoneInfo(c["timezone"])))
    slot = f"{r['day']} {r['hour']}"
    if r["again"]:  # only past the try the owner was told about
        old = fl.journal.taken(c["site"], slot)
        if old is not None and old["id"] != r["old"]:
            raise ToolError(already(old))
    await free(s)
    if fl.busy:
        raise ToolError("another scenario is running; nothing was booked")
    fl.running = True
    run, row, pressing, ended = Run(s, c, when, sleep), 0, False, False

    def before() -> None:
        nonlocal pressing
        fl.journal.set(row, "pressing", side.clock())
        pressing = True

    try:
        row = fl.journal.reserve(r["flow"], c["site"], slot, c["max_per_day"], r["again"], a.get("approval_id"), side.clock())
        run.note = lambda unclear: fl.journal.unclear(row, unclear)
        run.opened = fl.journal.opened
        try:
            async with asyncio.timeout(TOTAL):
                order = await run.go(before)
        except CheckoutRefused:
            if pressing and run.at != "reading the confirmation":
                pressing = False  # the controller refused before pressing: definitely not booked
            raise
        fl.journal.set(row, "done", side.clock(), f"order {order}")
        ended = True
        return side.scrub(f"Booked: {clean(c['title'], 120)}, {said(when)}, {clean(c['place'], 120)}. Order number {order}.")
    except BaseException as e:
        why = side.scrub(clean(str(e), 300)) or type(e).__name__
        if isinstance(e, TimeoutError):
            why = f"it took too long (while {run.at})"
        if row and not ended:
            fl.journal.set(row, "unknown" if pressing or run.unclear else "failed", side.clock(), why)
        if not isinstance(e, Exception):  # cancelled, shutting down
            raise
        if pressing or run.unclear:
            raise ToolError(f"{'the button that books was pressed' if pressing else 'a button was pressed'}, but: {why}. It MAY be booked. Look at the site or your email before trying again; "
                            "it isn't tried again by itself") from None
        raise ToolError(f"nothing was booked: {why}") from None
    finally:
        if run.opening:
            fl.stuck = run.session or "?"
        fl.running = False
        with contextlib.suppress(Exception):
            await free(s, 3, sleep)


async def free(s: Services, tries: int = 1, sleep: Any = asyncio.sleep) -> None:
    """Closes a session a scenario left open. Until that is sure, nothing else may use the browser (it is signed in)."""
    fl = s.flows
    if fl is None or fl.running or not fl.stuck:
        return
    client = cs(s).client
    for i in range(tries):
        if i:
            await sleep(EVERY)
        try:
            stuck = fl.stuck
            now = (await client.call({"cmd": "status"})).get("session")
            # (Nothing else could open one meanwhile.)
            if now and stuck in ("?", now):
                await client.call({"cmd": "close", "session": now})
                now = (await client.call({"cmd": "status"})).get("session")
            if fl.running or fl.stuck != stuck:  # somebody else did it meanwhile
                return
            if not now or stuck not in ("?", now):
                fl.stuck = ""
                fl.journal.opened("")
                return
        except ToolError:
            continue


def check_busy(s: Services, ops: list[dict]) -> None:
    """While a scenario uses the browser (or a session of one may still be open there), nothing else does."""
    if s.flows is not None and s.flows.busy and any(o["op"].startswith(("checkout.", "signin.")) and o["op"] != "checkout.flows" for o in ops):
        raise ToolError("a scenario is using the checkout browser right now; wait until it is done")


OPS["checkout.flows"] = Op("checkout.flows", False, flow_list)
OPS["checkout.flow"] = Op("checkout.flow", True, flow_run, describe_flow, "BOOK with a fixed script")
PRE_RESOLVE["checkout.flow"] = _resolve_flow
PRIVATE_ONLY.update({"checkout.flows", "checkout.flow"})
_ops.ALWAYS_CRITICAL = _ops.ALWAYS_CRITICAL | {"checkout.flow"}
