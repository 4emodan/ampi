"""Scenarios (ops_flow): contracts, the journal, and the run against a pretended booking site."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ampi_tools import ops_flow as of
from ampi_tools import watch_spec as ws
from ampi_tools.ops import Approvals, Policy, Services, validate
from ampi_tools.ops_checkout import CheckoutRefused, CheckoutSide
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError
from ampi_tools.vault import Vault

SITE = "https://www.club.example"
Z = ZoneInfo("Europe/Helsinki")
WHEN = (datetime.now(Z) + timedelta(days=9)).replace(hour=18, minute=30, second=0, microsecond=0)
DAY = WHEN.strftime("%Y-%m-%d")
CONTRACT = {
    "title": "Evening class", "place": "Example Studio", "site": SITE, "start": f"{SITE}/class", "engine": "lightpanda", "timezone": "Europe/Helsinki",
    "frames": ["https://chat.example"], "login": {"user": "Email", "button": ["Log In"], "done": "Hello"},
    "steps": [{"press": ["Decline All"], "optional": True}, {"press": ["Log In"]}, {"login": True}, {"press": ["Book Now"]},
              {"slot": {"name": "slots-{date}T00:00:00", "time": "12h", "next": ["Show next week"], "zone": "Time ({offset})"}}, {"press": ["Next"]}],
    "check": ["Evening class", "{when}"], "fields": {"Name": ".+", "Email": "@login"}, "book": ["Book Now"], "amounts": ["€80"],
    "confirm": {"text": ["Evening class", r"Total: ?0[.,]00", "{day} .{3,9} {year}, {hour12}:{minute}"], "order": r"Order number:(\d+)"}, "max_per_day": 2}


def run(coro):
    return asyncio.run(coro)


async def nosleep(_):
    return None


class Site:
    """The controller with a booking site behind it."""

    def __init__(self):
        self.session = None
        self.page, self.week, self.chosen, self.pressed, self.calls = "class", 0, None, [], []
        self.gen = 0
        self.booked = 0
        self.frames = [SITE]
        self.slot_label = "6:30 pm"
        self.totals, self.form = [], [{"label": "Name", "value": "x", "shown": True}, {"label": "Email", "value": "•••", "shown": True}]
        self.close_fails, self.empty_week, self.once, self.more_text, self.stale, self.zone = 0, False, None, "", 0, None
        self.around = None
        self.on_book = None   # an exception instead of booking
        self.lost = False     # the confirmation never comes
        self.break_on = None  # (cmd, exception)
        self.extra = []
        self.captcha = False
        self.texts = None
        self.dest = ""

    def el(self, n, label, kind="button", **kw):
        return {"ref": f"f0:{n}", "frame": "f0", "frame_origin": SITE, "kind": kind, "type": "", "name": "", "id": "", "label": label, "tag": "button",
                "disabled": False, "checked": False, "autocomplete": "", "role": "", "filled": False, **kw}

    def elements(self):
        if self.page == "class":
            return [self.el(1, "Decline All"), self.el(2, "Log In"), self.el(3, "Book Now"), self.el(4, "Book Now")]
        if self.page == "login":
            return [self.el(1, "Log In"), self.el(8, "Email", "input", type="email"), self.el(9, "Email", "input", type="email"),
                    self.el(10, "Password", "input", type="password"), self.el(11, "Log In")]
        if self.page == "in":
            return [self.el(3, "Book Now"), self.el(4, "Book Now")]
        if self.page == "calendar":
            base = WHEN + timedelta(days=7 * (self.week - 1) if self.week < 1 else 0)
            return [self.el(20, "Show next week"), self.el(23, "Next")] if self.empty_week and not self.week else [self.el(20, "Show next week"),
                    self.el(21, self.slot_label if self.week else "6:30 pm", "radio", tag="input", name=f"slots-{base:%Y-%m-%d}T00:00:00",
                            checked=self.chosen == self.week),
                    self.el(23, "Next")]
        if self.page == "form":
            return [self.el(30, "Add to Cart"), self.el(31, "Book Now"), *self.extra]
        return []

    def text(self):
        if self.page == "in":
            return "Hello Jane"
        if self.page == "calendar":
            return f"FREE SHIPPING OVER €80 week {self.week} Time ({self.zone or of.offset(WHEN)}) " + ("done" if self.chosen is not None else "")
        if self.page == "form":
            return f"{self.more_text} Booking Form Evening class {of.said(WHEN)} Add to Cart Book Now"
        if self.page == "thanks":
            return "Thank you" if self.lost else f"Evening class Order number:11 {WHEN.day} Νοε {WHEN.year}, {WHEN.hour % 12}:{WHEN.minute:02d} Subtotal 0,00€ Total: 0,00€"
        return "Evening class"

    def snap(self):
        return {"url": SITE, "origin": SITE, "title": "t", "text": self.text(), "elements": self.elements(), "frames": [{"id": "f0", "origin": o} for o in self.frames],
                "totals": self.totals if self.page == "form" else [], "gate_totals": [], "captcha": self.captcha, "risky_checked": []}

    def do(self, ref):
        e = next(x for x in self.elements() if x["ref"] == ref)
        self.gen += 1
        self.pressed.append((self.page, ref))
        n, page = e["ref"], self.page
        if n == "f0:2" and page == "class":
            self.page = "login"
        elif n == "f0:11":
            self.page = "in"
        elif n in ("f0:3", "f0:4") and page == "in":
            self.page = "calendar"
        elif n == "f0:20":
            self.week += 1
        elif n == "f0:21":
            self.chosen = self.week
        elif n == "f0:23":
            self.page = "form"
        elif n == "f0:31":
            self.booked += 1
            self.page = "thanks"

    async def call(self, req, timeout=None):
        self.calls.append(req)
        cmd = req["cmd"]
        if self.break_on and self.break_on[0] == cmd and self.break_on[2](self):
            raise self.break_on[1]
        if cmd == "status":
            return {"ok": True, "session": self.session, "state": "active"}
        if cmd == "open":
            self.session = "s1"
        elif cmd == "close":
            if self.close_fails:
                self.close_fails -= 1
                raise ToolError("the checkout browser didn't answer in time")
            self.session = None
        elif cmd == "click":
            self.do(req["ref"])
        elif cmd == "gate":
            e = next(x for x in self.elements() if x["ref"] == req["ref"])
            return {"ok": True, "kind": e["kind"], "tag": e["tag"], "texts": [e["label"]], "label": e["label"], "ident": "", "submits": False, "search": False,
                    "dest": "", "href": SITE, "frame_origin": SITE, "signed_in": self.page in ("in", "calendar", "form"), "filled": False, "priced": False,
                    "totals": [], "frames": self.frames}
        elif cmd == "bind_submit":
            e = next(x for x in self.elements() if x["ref"] == req["ref"])
            return {"ok": True, "session": self.session, "origin": SITE, "url": SITE, "button": e, "totals": self.totals, "risky_checked": [], "captcha": False,
                    "title": "t", "form": self.form, "digest": "d", "gen": self.gen, "seen": "s", "priced": True, "gate_totals": [], "frames": self.frames,
                    "press": {"tag": e["tag"], "texts": self.texts if self.texts and e["ref"] == "f0:31" else [e["label"]], "dest": self.dest, "method": "", "submits": False},
                    "around": self.around if self.around is not None else self.text(),
                    **({"box": e["kind"], "checked": e["checked"]} if e["kind"] == "radio" else {})}
        elif cmd == "submit":
            if req["button"]["ref"] == "f0:31" and self.on_book:
                raise self.on_book
            self.do(req["button"]["ref"])
            if self.once and req["button"]["ref"] == self.once[0]:
                e, self.once = self.once[1], None
                raise e
        elif cmd == "snapshot" and self.stale and self.page in ("in", "thanks"):
            self.stale -= 1
            raise ToolError("checkout: the page changed; take a new snapshot")
        return {"ok": True, "session": self.session, "snapshot": self.snap()}


class Phone:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    async def call(self, req, timeout=None):
        self.calls.append(req)
        return {"ok": True, "pressed": False, "password": True} if self.ok else {"ok": False, "code": "denied"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    site, phone = Site(), Phone()
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "class.json").write_text(json.dumps(CONTRACT))
    s = Services(checkout_side=CheckoutSide(site, Vault(tmp_path)), signin_client=phone,
                 flows=of.Flows(tmp_path / "flows", of.Journal(tmp_path / "flows.sqlite3")))
    d = Daemon(s, Approvals(tmp_path / "a.sqlite3"), Policy(), lambda pid: asyncio.sleep(0, True))
    real = of.flow_run
    monkeypatch.setitem(of.OPS, "checkout.flow", of.Op("checkout.flow", True, lambda s, a: real(s, a, nosleep), of.describe_flow, "BOOK"))
    return d, site, phone, tmp_path


def ask(d, origin="private", **kw):
    args = {"flow": "class", "flow_date": DAY, "flow_time": "18:30", **kw}
    return run(d.run(validate([{"op": "checkout.flow", "args": args}]), None, origin=origin))


def approve(d, res):
    row = d.approvals.get(res["approval"])
    return run(d.decide(res["approval"], True, row["nonce"], None))


def status(d):
    return [r["status"] for r in d.services.flows.journal.db.execute("SELECT * FROM runs ORDER BY id")]


def test_it_books_once_after_one_approval(env):
    d, site, phone, _ = env
    res = ask(d)
    summary = d.approvals.get(res["approval"])["summary"]
    assert "Evening class" in summary and of.said(WHEN) in summary and "fingerprint" in summary
    assert not site.calls[1:] and not phone.calls
    out = approve(d, res)
    assert out["status"] == "done" and "Order number 11" in out["text"], out
    assert site.booked == 1 and site.session is None and len(phone.calls) == 1 and phone.calls[0]["user"] == "f0:9"
    assert status(d) == ["done"] and not d.services.flows.busy
    with pytest.raises(ToolError, match="booked already"):
        ask(d)
    again = ask(d, again=True)
    assert "AGAIN" in d.approvals.get(again["approval"])["summary"]


@pytest.mark.parametrize("origin", ["group:x", "watcher:p/n@123456789012#s", "forwarded:x", "unknown"])
def test_only_from_the_owners_own_chat(env, origin):
    with pytest.raises(ToolError):
        ask(env[0], origin)
    with pytest.raises(ToolError):
        run(env[0].run(validate([{"op": "checkout.flows", "args": {}}]), None, origin=origin))
    assert {"checkout.flow", "checkout.flows"} <= ws.NOT_FOR_WATCHERS


@pytest.mark.parametrize("what,why", [
    ("waitlist", "isn't free"), ("frame", "other sites"), ("value", "as the contract says"), ("hidden", "can't be seen"), ("price", "about money"), ("card", "about money"),
    ("price2", "shows a price"), ("part", "next to the button"), ("zone", "doesn't say"), ("total", "total"), ("field", "isn't in the contract"), ("around", "next to the button"),
    ("captcha", "captcha"), ("caption", "says something else"), ("dest", "another site"), ("two", "exactly one"), ("denied", "refused on the phone"),
    ("refused", "nothing was pressed"), ("payframe", "other sites")])
def test_it_stops_and_nothing_is_booked(env, what, why):
    d, site, phone, _ = env
    res = ask(d)
    if what == "waitlist":
        site.slot_label = "6:30 pm Waitlist"
    elif what in ("frame", "payframe"):
        site.frames = [SITE, "https://evil.example" if what == "frame" else "https://js.stripe.com"]
    elif what == "total":
        site.totals = [{"amount": "25.00", "currency": "EUR", "label": "Total"}]
    elif what == "field":
        site.form = [*site.form, {"label": "Gift card", "value": "x", "shown": True}]
    elif what == "value":
        site.form[1]["value"] = "other@evil.example"
    elif what == "hidden":
        site.form[0]["shown"] = False
    elif what in ("price", "card", "part"):
        site.around = {"price": f"Evening class {of.said(WHEN)} 25,00 € Book Now", "card": f"Evening class {of.said(WHEN)} Visa ending in 4242 Book Now",
                       "part": f"Evening class 1{of.said(WHEN)} Book Now"}[what]
    elif what == "price2":
        site.more_text, site.around = "from €25", f"Evening class {of.said(WHEN)} Book Now"
    elif what == "zone":
        site.zone = "GMT+0" if of.offset(WHEN) != "GMT+0" else "GMT+9"
    elif what == "around":
        site.around = "Evening class 1 January 2027 at 6:30 pm Book Now"
    elif what == "captcha":
        site.captcha = True
    elif what == "caption":
        site.texts = ["Book Now", "and subscribe"]
    elif what == "dest":
        site.dest = "https://evil.example/x"
    elif what == "two":
        site.extra = [site.el(32, "Book Now")]
    elif what == "denied":
        phone.ok = False
    elif what == "refused":
        site.on_book = CheckoutRefused("the page changed; nothing was pressed")
    out = approve(d, res)
    assert out["status"] == "failed" and "nothing was booked" in out["text"] and why in out["text"], out
    assert site.booked == 0 and status(d) == ["failed"] and site.session is None
    assert ask(d)["approval"]  # it may be asked for again


@pytest.mark.parametrize("what", ["engine", "lost", "browser gone"])
def test_an_unclear_outcome_is_never_tried_again(env, what):
    d, site, _, _ = env
    res = ask(d)
    if what == "engine":
        site.on_book = ToolError("the browser failed")
    elif what == "lost":
        site.lost = True
    else:
        site.break_on = ("snapshot", CheckoutRefused("not running"), lambda s: s.page == "thanks")
    out = approve(d, res)
    assert out["status"] == "failed" and "MAY be booked" in out["text"], out
    assert status(d) == ["unknown"] and site.booked <= 1
    with pytest.raises(ToolError, match="MAY have gone through"):
        ask(d)


def test_a_crash_while_pressing_is_remembered(tmp_path):
    j = of.Journal(tmp_path / "j.sqlite3")
    a = j.reserve("f", SITE, "x", 5, False, None, 1000.0)
    b = j.reserve("f", SITE, "y", 5, False, None, 1000.0)
    j.set(b, "pressing", 1001.0)
    j.db.close()
    j = of.Journal(tmp_path / "j.sqlite3")
    assert [r["status"] for r in j.db.execute("SELECT status FROM runs WHERE id IN (?,?) ORDER BY id", (a, b))] == ["failed", "unknown"]
    with pytest.raises(sqlite3.IntegrityError):
        j.db.execute("INSERT INTO runs (site, slot, status) VALUES (?, 'y', 'done')", (SITE,))


def test_limit_and_again(tmp_path):
    j = of.Journal(tmp_path / "j.sqlite3")
    j.set(j.reserve("f", SITE, "x", 2, False, None, 1000.0), "done", 1000.0)
    with pytest.raises(ToolError, match="booked already"):
        j.reserve("g", SITE, "x", 2, False, None, 1001.0)  # another scenario for the same site and slot, too
    j.set(j.reserve("f", SITE, "x", 2, True, None, 1002.0), "done", 1002.0)
    with pytest.raises(ToolError, match="limit"):
        j.reserve("f", SITE, "y", 2, False, None, 1003.0)
    assert j.reserve("f", SITE, "y", 2, False, None, 1003.0 + 86400)


def test_a_changed_contract_needs_a_new_approval(env):
    d, site, _, tmp = env
    res = ask(d)
    (tmp / "flows" / "class.json").write_text(json.dumps({**CONTRACT, "title": "Other"}))
    out = approve(d, res)
    assert out["status"] == "failed" and "contract changed" in out["text"] and site.booked == 0


def test_nothing_else_uses_the_browser_meanwhile(env):
    d, _, _, _ = env
    d.services.flows.running = True
    for op in ("checkout.close", "checkout.snapshot", "signin.fill"):
        with pytest.raises(ToolError, match="scenario is using"):
            run(d.run([{"op": op, "args": {}}], None, origin="private"))
    assert "class" in run(d.run(validate([{"op": "checkout.flows", "args": {}}]), None, origin="private"))["text"]


def test_an_open_session_is_left_alone(env):
    d, site, _, _ = env
    res = ask(d)
    site.session = "mine"
    out = approve(d, res)
    assert out["status"] == "failed" and site.session == "mine" and site.booked == 0


@pytest.mark.parametrize("day,hour,why", [("2020-01-01", "10:00", "over"), ("2026-02-30", "10:00", "doesn't exist"), ("2030-01-01", "10:00", "days ahead"),
                                          ("2027-03-28", "03:30", "clocks change")])
def test_times(day, hour, why):
    with pytest.raises(ToolError, match=why):
        of.slot_time(CONTRACT, day, hour, datetime(2027, 3, 1, tzinfo=Z) if "2027" in day else datetime(2026, 1, 1, tzinfo=Z))


def test_words():
    t = datetime(2026, 11, 7, 0, 5)
    assert of.said(t) == "7 November 2026 at 12:05 am" and of.clock(t.replace(hour=12), "12h") == "12:05 pm" and of.clock(t, "24h") == "00:05"


@pytest.mark.parametrize("change", [{"site": "http://club.example"}, {"start": "https://evil.example/"}, {"engine": "x"}, {"timezone": "Nowhere"}, {"more": 1},
                                    {"steps": [{"press": ["x"]}]}, {"steps": [{"run": "x"}]}, {"check": ["x"]}, {"confirm": {"text": ["("], "order": "(x)"}},
                                    {"confirm": {"text": ["x"], "order": "x"}}, {"confirm": {"text": ["x"], "order": "(x)"}}, {"fields": ["x"]}, {"fields": {"x": "("}}, {"max_per_day": 50}, {"max_per_day": True}, {"frames": ["*"]}, {"book": []}])
def test_bad_contracts(change):
    of.parse(json.dumps(CONTRACT).encode())
    with pytest.raises(ToolError):
        of.parse(json.dumps({**CONTRACT, **change}).encode())


def test_a_failed_retry_keeps_what_was_known(env):
    d, site, phone, _ = env
    approve(d, ask(d))
    site.page, site.week, site.chosen, phone.ok = "class", 0, None, False
    assert approve(d, ask(d, again=True))["status"] == "failed"
    assert status(d) == ["done", "failed"]
    with pytest.raises(ToolError, match="booked already"):
        ask(d)


def test_a_crash_of_a_retry_keeps_what_was_known(tmp_path):
    j = of.Journal(tmp_path / "j.sqlite3")
    j.set(j.reserve("f", SITE, "x", 5, False, None, 1000.0), "done", 1000.0)
    j.reserve("f", SITE, "x", 5, True, None, 1001.0)
    j.db.close()
    assert of.Journal(tmp_path / "j.sqlite3").taken(SITE, "x")["status"] == "done"


@pytest.mark.parametrize("ref", ["f0:11", "f0:3", "f0:20", "f0:21", "f0:23", "f0:31"])
def test_pages_that_change_while_they_are_pressed(env, ref):
    d, site, _, _ = env
    site.once = (ref, ToolError("checkout: after pressing, the page changed"))
    site.stale = 2
    out = approve(d, ask(d))
    assert out["status"] == "done" and site.booked == 1 and status(d) == ["done"], out


def test_a_step_that_books_by_itself_is_unclear(env):
    d, site, _, _ = env
    do = site.do

    def early(ref):
        do(ref)
        if site.page == "calendar":
            site.page = "thanks"

    site.do = early
    out = approve(d, ask(d))
    assert "MAY be booked" in out["text"] and status(d) == ["unknown"], out


def test_a_session_that_does_not_close_blocks_the_browser(env):
    d, site, _, _ = env
    site.close_fails = 4
    assert approve(d, ask(d))["status"] == "done"
    assert site.session and d.services.flows.busy
    with pytest.raises(ToolError, match="scenario is using"):
        run(d.run([{"op": "checkout.snapshot", "args": {}}], None, origin="private"))
    assert of.Journal(d.services.flows.folder.parent / "flows.sqlite3").left_open() == "s1"
    run(d.run(validate([{"op": "checkout.snapshot", "args": {}}]), None, origin="private"))
    assert site.session is None and not d.services.flows.busy


def test_an_open_that_fails_half_way_is_closed(env):
    d, site, _, _ = env
    real = site.call

    async def call(req, timeout=None):
        r = await real(req, timeout)
        if req["cmd"] == "open":
            raise ToolError("the checkout browser didn't answer in time")
        return r

    site.call = call
    assert approve(d, ask(d))["status"] == "failed" and site.session is None and not d.services.flows.busy


def test_a_week_without_slots(env):
    d, site, _, _ = env
    site.empty_week = True
    assert approve(d, ask(d))["status"] == "done"


def test_different_buttons_with_the_same_words(env):
    d, site, _, _ = env
    els = site.elements
    site.elements = lambda: els() + ([site.el(40, "Book Now", "link", tag="a")] if site.page == "in" else [])
    out = approve(d, ask(d))
    assert out["status"] == "failed" and "different" in out["text"] and site.booked == 0


def test_groups_do_not_see_bookings(env):
    d, _, _, _ = env
    res = ask(d)
    assert "#%d" % res["approval"] not in d.list_text("group:x") and "#%d" % res["approval"] in d.list_text("private")


@pytest.mark.parametrize("text,found", [("FREE SHIPPING OVER €80\nLog In", {"€80"}), ("Total: 0,00€ Tax €0.00", set()), ("EUR 25", {"EUR25"}), ("25 CHF", {"25CHF"}), ("at 6:30 pm", set())])
def test_amounts(text, found):
    assert of.amounts(text) == found


def test_an_absent_optional_step_proves_nothing(env):
    d, site, _, tmp = env
    steps = [*CONTRACT["steps"][:4], {"press": ["Nope"], "optional": True}, {"press": ["Nope2"]}, *CONTRACT["steps"][4:]]
    (tmp / "flows" / "class.json").write_text(json.dumps({**CONTRACT, "steps": steps}))
    out = approve(d, ask(d))
    assert "MAY be booked" in out["text"] and status(d) == ["unknown"]


def test_the_heading_changes_before_the_slots(env):
    d, site, _, _ = env
    els, looks = site.elements, [0]

    def lagging():
        if site.page == "calendar" and site.week:
            looks[0] += 1
            if looks[0] < 6:
                site.week, w = 0, site.week
                try:
                    return els()
                finally:
                    site.week = w
        return els()

    site.elements = lagging
    out = approve(d, ask(d))
    assert out["status"] == "done" and [r for p, r in site.pressed if r == "f0:20"] == ["f0:20"], out
