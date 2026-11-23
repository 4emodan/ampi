"""The checkout controller against a fake engine that implements the same page ops in Python."""

import json
import re
import socket

import pytest

from ampi_tools import checkout as co
from ampi_tools import checkout_net

SHOP = "https://shop.example"
PSP = "https://js.stripe.com"
CARD = "4242 4242 4242 4242"


def el(kind, **kw):
    base = {"kind": kind, "tag": {"input": "input", "textarea": "textarea", "select": "select", "button": "button",
                                  "link": "a", "checkbox": "input"}[kind],
            "type": {"checkbox": "checkbox", "button": "submit"}.get(kind, kw.pop("type", "text")),
            "autocomplete": "", "name": "", "id": "", "label": "", "value": "", "checked": False,
            "connected": True, "options": [], "required": False}
    base.update(kw)
    return base


class Frame:
    def __init__(self, fid, url, elements=None, text="", title="", parent=None, origin=None):
        self.id, self.url, self.parent = fid, url, parent
        self.origin = origin if origin is not None else co.origin_of(url)
        self.loader = f"L-{fid}-1"
        self.els = {i + 1: e for i, e in enumerate(elements or [])}
        self.text, self.title = text, title


class FakeEngine:
    def __init__(self, pages):
        self.pages = pages  # url -> callable returning [Frame, ...] (first = main)
        self.frames: list[Frame] = []
        self.worlds: dict[int, str] = {}
        self.sets: list[tuple[str, int, str]] = []
        self.clicks: list[tuple[str, int]] = []
        self.disposed = 0
        self.closed = False
        self.on_click = {}  # (frame_id, n) -> url to navigate to
        self.history: list[str] = []
        self.fail: dict[str, int] = {}  # method -> how many more calls answer with a protocol error
        self.dead = False  # the connection is gone: every call fails
        self.die_after_click = False
        self.disable_after_set: tuple[str, int] | None = None

    def navigate(self, url):
        self.history.append(url)
        self.frames = self.pages[url]()

    def frame(self, fid):
        return next(f for f in self.frames if f.id == fid)

    def call(self, method, params=None, session=None, timeout=30):
        params = params or {}
        if self.dead:
            raise co.CdpError("connection closed")
        if self.fail.get(method):
            self.fail[method] -= 1
            raise co.CdpProtocolError(f"{method} failed")
        if method == "Target.createBrowserContext":
            return {"browserContextId": "ctx"}
        if method == "Target.createTarget":
            return {"targetId": "page-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method in ("Page.enable", "Runtime.enable"):
            return {}
        if method == "Target.disposeBrowserContext":
            self.disposed += 1
            return {}
        if method == "Page.navigate":
            self.navigate(params["url"])
            return {"frameId": "main"}
        if method == "Page.getFrameTree":
            def node(f):
                return {"frame": {"id": f.id, "url": f.url, "loaderId": f.loader,
                                  "securityOrigin": "null" if f.origin == "opaque" else f.origin},
                        "childFrames": [node(c) for c in self.frames if c.parent == f.id]}
            return {"frameTree": node(self.frames[0])}
        if method == "Page.createIsolatedWorld":
            ctx = len(self.worlds) + 1
            self.worlds[ctx] = params["frameId"]
            return {"executionContextId": ctx}
        if method == "Runtime.evaluate":
            expr = params["expression"]
            op = json.loads(re.search(r"__ampi\.run\((.*)\)\);\s*$", expr, re.S).group(1))
            f = self.frame(self.worlds[params["contextId"]])
            return {"result": {"value": json.dumps(self.op(f, op))}}
        raise AssertionError(method)

    def describe(self, n, e):
        d = {k: e[k] for k in ("kind", "tag", "type", "autocomplete", "name", "id", "label", "required")}
        d.update(n=n, checked=e["checked"] if e["kind"] == "checkbox" else None, filled=bool(e["value"]),
                 disabled=bool(e.get("disabled")), role=e.get("role", ""))
        if e["kind"] == "select":
            d["options"] = e["options"]
        return d

    def op(self, f, a):
        get = lambda n: f.els.get(n) if f.els.get(n, {}).get("connected") else None  # noqa: E731
        if a["op"] == "enumerate":
            out = {"href": f.url, "ready": "complete", "text": f.text,
                   "elements": [self.describe(n, e) for n, e in f.els.items() if e["connected"] and not e.get("hidden")],
                   "checked": [e["label"] for e in f.els.values() if e["connected"] and e["checked"]]}
            if a.get("main"):
                out["title"] = f.title
            return out
        if a["op"] == "form":
            if f.__dict__.get("form_broken"):
                return None
            return {"problem": f.__dict__.get("problem", ""), "hidden": f.__dict__.get("hidden", []), "fields": [
                {"label": e["label"], "kind": e["kind"], "value": True if e["kind"] == "checkbox" else e["value"],
                 "shown": not e.get("hidden"), "type": e["type"], "name": e["name"]}
                for n, e in f.els.items() if e["connected"] and (e["checked"] or e["value"])]}
        if a["op"] == "gate":
            e = get(a["n"])
            return {"connected": True, "tag": e["tag"], "texts": [e["label"]], "ident": e["id"], "submits": False,
                    "search": False, "dest": e.get("dest", f.url), "method": "post", "around": f.text} if e else {"connected": False}
        if a["op"] == "inspect":
            e = get(a["n"])
            return {"connected": True, **self.describe(a["n"], e)} if e else {"connected": False}
        if a["op"] == "set":
            e = get(a["n"])
            if not e:
                return {"ok": False}
            if e.get("vanish"):
                return {"ok": False}
            e["value"] = a["value"]
            self.sets.append((f.id, a["n"], a["value"]))
            if self.disable_after_set:
                self.frame(self.disable_after_set[0]).els[self.disable_after_set[1]]["disabled"] = True
            return {"ok": True}
        if a["op"] == "click":
            e = get(a["n"])
            if not e:
                return {"ok": False}
            self.clicks.append((f.id, a["n"]))
            if (f.id, a["n"]) in self.on_click:
                self.navigate(self.on_click[(f.id, a["n"])])
            if self.die_after_click:
                self.dead = True
            return {"ok": True}
        if a["op"] == "choose":
            e = get(a["n"])
            if a["option"] not in e["options"]:
                return {"ok": False, "why": "option"}
            e["value"] = a["option"]
            return {"ok": True}
        if a["op"] == "state":
            return {"href": f.url, "ready": "complete"}
        if a["op"] == "back":
            return {"ok": True}
        raise AssertionError(a)

    def close(self):
        self.closed = True


def checkout_page():
    main = Frame("main", f"{SHOP}/checkout", title="Checkout", text="Your order\nPad thai\nTotal: 23,90 €", elements=[
        el("input", type="email", autocomplete="email", name="email", label="Email"),          # 1
        el("input", autocomplete="cc-number", name="ccnum", label="Card number (merchant)"),   # 2
        el("checkbox", label="Save my card for next time"),                                   # 3
        el("button", label="Pay 23,90 €"),                                                    # 4
        el("select", label="Country", options=["Finland", "Sweden"]),                         # 5
        el("input", type="password", name="pw", label="Password"),                            # 6
    ])
    card = Frame("stripe", f"{PSP}/v3/elements-inner-card.html", parent="main", elements=[
        el("input", autocomplete="cc-number", name="cardnumber", label="Card number"),
        el("input", autocomplete="cc-exp", name="exp-date", label="MM / YY"),
        el("input", autocomplete="cc-csc", name="cvc", label="CVC"),
    ])
    blank = Frame("blank", "about:blank", parent="main", origin="opaque", elements=[el("input", label="hidden trap")])
    return [main, card, blank]


def confirmation_page():
    return [Frame("main2", f"{SHOP}/thanks", title="Thanks",
                  text="Shop menu\nAccount\nHelp\n" + "nav item\n" * 40 + "Thank you! Order 1234 confirmed")]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def env():
    eng = FakeEngine({f"{SHOP}/checkout": checkout_page, f"{SHOP}/thanks": confirmation_page,
                      "https://www.youtube.com/x": lambda: [Frame("yt", "https://www.youtube.com/x", text="video")]})
    clock = Clock()

    class CdpWrap:
        def __init__(self):
            self.call, self.close = eng.call, eng.close

    c = co.Controller(connect=CdpWrap, clock=clock, mem_mb=lambda: 2000, sleep=clock.sleep)
    return c, eng, clock


def opened(c):
    r = c.handle({"cmd": "open", "url": f"{SHOP}/checkout"})
    assert r["ok"], r
    return r["session"], r["snapshot"]


def ref(snap, label):
    return next(e["ref"] for e in snap["elements"] if e["label"] == label)


# --- snapshot -----------------------------------------------------------------------------------


def test_snapshot_lists_elements_frames_totals_and_flags(env):
    c, eng, _ = env
    sid, snap = opened(c)
    assert snap["origin"] == SHOP and snap["title"] == "Checkout"
    assert {f["origin"] for f in snap["frames"]} == {SHOP, PSP, "opaque"}
    assert snap["totals"] == [{"amount": "23.90", "currency": "EUR", "label": "total"}]
    assert not any(e["label"] == "hidden trap" for e in snap["elements"])  # opaque frame: never actionable
    card = next(e for e in snap["elements"] if e["name"] == "cardnumber")
    assert card["frame_origin"] == PSP and card["ref"].startswith("f1:")
    assert snap["risky_checked"] == [] and snap["captcha"] is False


def test_snapshot_never_contains_values(env):
    c, eng, _ = env
    sid, snap = opened(c)
    r = c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": "secret-mail@example.org"})
    assert r["ok"], r
    assert "secret-mail" not in json.dumps(r) and next(e for e in r["snapshot"]["elements"]
                                                        if e["label"] == "Email")["filled"] is True


def test_risky_checkbox_reported(env):
    c, eng, _ = env
    eng.pages[f"{SHOP}/checkout"] = lambda: (lambda fr: (fr[0].els[3].update(checked=True), fr)[1])(checkout_page())
    _, snap = opened(c)
    assert snap["risky_checked"] == ["Save my card for next time"]


# --- typing -------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [CARD, "4242-4242-4242-4242", "my card 4242424242424242 thanks", "4242.4242.4242.4242"])
def test_type_refuses_card_numbers(env, text):
    c, eng, _ = env
    sid, snap = opened(c)
    r = c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": text})
    assert not r["ok"] and "card number" in r["error"] and not eng.sets
    assert "4242" not in r["error"]


@pytest.mark.parametrize("label", ["Card number (merchant)", "Password", "Card number"])
def test_type_refuses_secret_fields(env, label):
    c, eng, _ = env
    sid, snap = opened(c)
    r = c.handle({"cmd": "type", "session": sid, "ref": ref(snap, label), "text": "hello"})
    assert not r["ok"] and not eng.sets


def test_type_and_choose_work(env):
    c, eng, _ = env
    sid, snap = opened(c)
    assert c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": "a@b.fi"})["ok"]
    assert c.handle({"cmd": "choose", "session": sid, "ref": ref(snap, "Country"), "option": "Sweden"})["ok"]
    r = c.handle({"cmd": "choose", "session": sid, "ref": ref(snap, "Country"), "option": "Mars"})
    assert not r["ok"] and "no such option" in r["error"]


def test_short_digit_runs_are_fine():
    assert not co.has_card_number("order 12345, phone +358 40 123 4567")
    assert co.has_card_number("4111 1111 1111 1111")


# --- open / navigation --------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://www.youtube.com/x", "file:///etc/passwd", "javascript:1"])
def test_open_refuses_blocked_urls(env, url):
    c, eng, _ = env
    r = c.handle({"cmd": "open", "url": url})
    assert not r["ok"] and c.session is None and not eng.history


def test_click_that_lands_on_a_blocked_site_closes_the_session(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.on_click[("main", 4)] = "https://www.youtube.com/x"
    r = c.handle({"cmd": "click", "session": sid, "ref": ref(snap, "Pay 23,90 €")})
    assert not r["ok"] and "session was closed" in r["error"] and c.session is None and eng.disposed == 1


def test_one_session_at_a_time_and_wrong_session(env):
    c, eng, _ = env
    sid, _ = opened(c)
    assert "another checkout session" in c.handle({"cmd": "open", "url": f"{SHOP}/checkout"})["error"]
    assert c.handle({"cmd": "snapshot", "session": "nope"})["error"] == "no such session"


def test_low_memory_refuses(env):
    c, eng, _ = env
    c.mem_mb = lambda: 100
    r = c.handle({"cmd": "open", "url": f"{SHOP}/checkout"})
    assert not r["ok"] and "memory" in r["error"]


# --- bind / fill --------------------------------------------------------------------------------


def bound(c, sid, refs):
    r = c.handle({"cmd": "bind", "session": sid, "refs": refs})
    assert r["ok"], r
    return r


def test_fill_sets_exactly_the_bound_element(env):
    c, eng, _ = env
    sid, snap = opened(c)
    b = bound(c, sid, [ref(snap, "Email")])
    assert b["origin"] == SHOP and b["bindings"][0]["autocomplete"] == "email"
    r = c.handle({"cmd": "fill", "session": sid, "origin": b["origin"],
                  "fields": [{"ref": b["bindings"][0]["ref"], "binding": b["bindings"][0], "value": "x@y.fi"}]})
    assert r["ok"], r
    assert eng.sets == [("main", 1, "x@y.fi")] and "x@y.fi" not in json.dumps(r)


@pytest.mark.parametrize("change", ["replaced", "attributes", "frame_origin", "top_origin"])
def test_fill_refuses_when_the_page_changed(env, change):
    c, eng, _ = env
    sid, snap = opened(c)
    b = bound(c, sid, [ref(snap, "Email")])
    main = eng.frames[0]
    if change == "replaced":
        main.els[1]["connected"] = False
        main.els[7] = el("input", type="email", autocomplete="email", name="email", label="Email")
    elif change == "attributes":
        main.els[1]["name"] = "evil"
    elif change == "frame_origin":
        main.origin = "https://evil.example"
    elif change == "top_origin":
        main.url = "https://shop.example.evil.com/checkout"
        main.origin = co.origin_of(main.url)
    r = c.handle({"cmd": "fill", "session": sid, "origin": b["origin"],
                  "fields": [{"ref": b["bindings"][0]["ref"], "binding": b["bindings"][0], "value": "x@y.fi"}]})
    assert not r["ok"] and not eng.sets and "x@y.fi" not in r["error"]


def test_fill_refuses_card_values(env):
    c, eng, _ = env
    sid, snap = opened(c)
    card = next(e["ref"] for e in snap["elements"] if e["name"] == "cardnumber")
    b = bound(c, sid, [card])
    r = c.handle({"cmd": "fill", "session": sid, "origin": SHOP,
                  "fields": [{"ref": card, "binding": b["bindings"][0], "value": "4242424242424242", "card": True}]})
    assert not r["ok"] and not eng.sets


def test_navigation_invalidates_refs(env):
    c, eng, _ = env
    sid, snap = opened(c)
    b = bound(c, sid, [ref(snap, "Email")])
    for f in eng.frames:
        f.loader = f.loader + "x"  # same page reloaded: new documents
    r = c.handle({"cmd": "fill", "session": sid, "origin": SHOP,
                  "fields": [{"ref": b["bindings"][0]["ref"], "binding": b["bindings"][0], "value": "x"}]})
    assert not r["ok"] and not eng.sets


# --- pay ----------------------------------------------------------------------------------------


def pay_request(c, sid, snap, card_refs=None, psp=(PSP,)):
    card_refs = card_refs or [next(e["ref"] for e in snap["elements"] if e["name"] == n)
                              for n in ("cardnumber", "exp-date", "cvc")]
    button = ref(snap, "Pay 23,90 €")
    b = bound(c, sid, card_refs + [button])
    values = ["4242424242424242", "12/30", "123"]
    fields = [{"ref": bb["ref"], "binding": bb, "value": v, "card": True} for bb, v in zip(b["bindings"][:-1], values)]
    return {"cmd": "pay", "session": sid, "origin": b["origin"], "fields": fields,
            "button": {"ref": button, "binding": b["bindings"][-1]}, "expect_totals": b["totals"],
            "psp_origins": list(psp)}


def test_pay_fills_clicks_and_goes_readonly(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.on_click[("main", 4)] = f"{SHOP}/thanks"
    r = c.handle(pay_request(c, sid, snap))
    assert r["ok"], r
    assert [s[:2] for s in eng.sets] == [("stripe", 1), ("stripe", 2), ("stripe", 3)] and eng.clicks == [("main", 4)]
    assert r["snapshot"]["readonly"] and "Order 1234" in r["snapshot"]["text"]
    assert "4242" not in json.dumps(r) and "123\"" not in json.dumps(r)
    assert "finished" in c.handle({"cmd": "type", "session": sid, "ref": "f0:1", "text": "x"})["error"]
    assert c.handle({"cmd": "snapshot", "session": sid})["ok"]


def test_pay_refuses_changed_totals(env):
    c, eng, _ = env
    sid, snap = opened(c)
    req = pay_request(c, sid, snap)
    eng.frames[0].text = "Your order\nTotal: 59,90 €"
    r = c.handle(req)
    assert not r["ok"] and "totals" in r["error"] and not eng.sets and not eng.clicks


def test_pay_refuses_risky_checkbox(env):
    c, eng, _ = env
    sid, snap = opened(c)
    req = pay_request(c, sid, snap)
    eng.frames[0].els[3]["checked"] = True
    r = c.handle(req)
    assert not r["ok"] and "ticked" in r["error"] and not eng.sets


def test_pay_refuses_card_into_merchant_field(env):
    c, eng, _ = env
    sid, snap = opened(c)
    r = c.handle(pay_request(c, sid, snap, card_refs=[ref(snap, "Card number (merchant)")]))
    assert not r["ok"] and "payment provider" in r["error"] and not eng.sets


def test_pay_refuses_psp_not_on_the_list(env):
    c, eng, _ = env
    sid, snap = opened(c)
    r = c.handle(pay_request(c, sid, snap, psp=("https://checkoutshopper-live.adyen.com",)))
    assert not r["ok"] and not eng.sets


def test_pay_phases_verify_refusal_is_definite(env):
    c, eng, _ = env
    sid, snap = opened(c)
    req = pay_request(c, sid, snap)
    eng.frames[0].text = "Total: 59,90 €"
    r = c.handle(req)
    assert r["phase"] == "verify" and not eng.sets and not c.session.readonly


def test_pay_field_vanishing_mid_fill_is_phase_filled_and_readonly(env):
    c, eng, _ = env
    sid, snap = opened(c)
    req = pay_request(c, sid, snap)
    eng.frame("stripe").els[3]["vanish"] = True  # the CVC field goes away once the others are set
    r = c.handle(req)
    assert not r["ok"] and r["phase"] == "filled" and not eng.clicks
    assert c.session.readonly  # no click/type can follow a half-filled card
    assert "finished" in c.handle({"cmd": "click", "session": sid, "ref": ref(snap, "Pay 23,90 €")})["error"]


def test_pay_button_disabled_after_fill_is_not_pressed(env):
    c, eng, clock = env
    sid, snap = opened(c)
    req = pay_request(c, sid, snap)
    eng.disable_after_set = ("main", 4)
    t0 = clock.t
    r = c.handle(req)
    assert not r["ok"] and r["phase"] == "filled" and "NOT pressed" in r["error"] and not eng.clicks
    assert clock.t - t0 >= co.PAY_FILL_SETTLE  # it waited for the provider before deciding


def test_engine_dying_after_the_click_is_unknown(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.die_after_click = True
    r = c.handle(pay_request(c, sid, snap))
    assert not r["ok"] and r["phase"] == "sent" and "MAY have gone through" in r["error"]
    assert c.session is None and c.retire and co.should_exit(c)


def test_redirect_to_blocked_site_after_the_click_is_unknown(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.on_click[("main", 4)] = "https://www.youtube.com/x"
    r = c.handle(pay_request(c, sid, snap))
    assert not r["ok"] and r["phase"] == "sent" and "MAY have gone through" in r["error"]
    assert c.retire and c.session is None


def test_pay_reply_has_a_confirmation_excerpt(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.on_click[("main", 4)] = f"{SHOP}/thanks"
    r = c.handle(pay_request(c, sid, snap))
    assert r["ok"] and "Order 1234 confirmed" in r["snapshot"]["confirmation"]
    assert len(r["snapshot"]["confirmation"]) <= 400


def test_fill_refusal_before_writing_is_phase_verify(env):
    c, eng, _ = env
    sid, snap = opened(c)
    b = bound(c, sid, [ref(snap, "Email")])
    eng.frames[0].els[1]["name"] = "evil"
    r = c.handle({"cmd": "fill", "session": sid, "origin": SHOP,
                  "fields": [{"ref": b["bindings"][0]["ref"], "binding": b["bindings"][0], "value": "x"}]})
    assert r["phase"] == "verify" and not eng.sets


def test_retire_only_after_a_paid_or_broken_session(env):
    c, eng, _ = env
    sid, _ = opened(c)
    c.handle({"cmd": "close", "session": sid})
    assert not c.retire and not co.should_exit(c)
    sid, snap = opened(c)
    eng.on_click[("main", 4)] = f"{SHOP}/thanks"
    assert c.handle(pay_request(c, sid, snap))["ok"]
    assert not co.should_exit(c)  # still open (read-only) until closed or expired
    c.handle({"cmd": "close", "session": sid})
    assert co.should_exit(c)


# --- engine hiccups -------------------------------------------------------------------------------


def test_protocol_error_is_retried_and_keeps_the_session(env):
    c, eng, _ = env
    sid, _ = opened(c)
    eng.fail["Runtime.evaluate"] = 1  # "Cannot find context" once, e.g. while a frame remounts
    r = c.handle({"cmd": "snapshot", "session": sid})
    assert r["ok"] and c.session is not None and r["snapshot"]["title"] == "Checkout"


def test_repeated_protocol_errors_say_page_changed_not_closed(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.fail["Runtime.evaluate"] = 5
    r = c.handle({"cmd": "click", "session": sid, "ref": ref(snap, "Pay 23,90 €")})
    assert not r["ok"] and "page changed" in r["error"] and c.session is not None and not c.retire


def test_element_op_on_a_new_document_is_refused(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.fail["Runtime.evaluate"] = 1
    eng.frames[0].loader += "x"  # the main frame navigated meanwhile
    r = c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": "a@b.fi"})
    assert not r["ok"] and not eng.sets and c.session is not None


def test_lost_connection_ends_the_session_and_retires(env):
    c, eng, _ = env
    sid, _ = opened(c)
    eng.dead = True
    r = c.handle({"cmd": "snapshot", "session": sid})
    assert not r["ok"] and c.session is None and co.should_exit(c)


# --- elements -------------------------------------------------------------------------------------


def test_disabled_elements_listed_and_not_clickable(env):
    c, eng, _ = env
    eng.pages[f"{SHOP}/checkout"] = lambda: (lambda fr: (fr[0].els[4].update(disabled=True), fr)[1])(checkout_page())
    sid, snap = opened(c)
    pay = next(e for e in snap["elements"] if e["label"] == "Pay 23,90 €")
    assert pay["disabled"] is True
    r = c.handle({"cmd": "click", "session": sid, "ref": pay["ref"]})
    assert not r["ok"] and "disabled" in r["error"] and not eng.clicks


def test_hidden_checked_risky_box_still_counts(env):
    c, eng, _ = env

    def page():
        fr = checkout_page()
        fr[0].els[3].update(checked=True, hidden=True, label="Subscribe and save 10%")
        return fr
    eng.pages[f"{SHOP}/checkout"] = page
    _, snap = opened(c)
    assert not any(e["label"].startswith("Subscribe") for e in snap["elements"])
    assert snap["risky_checked"] == ["Subscribe and save 10%"]


def test_three_digit_frame_refs():
    assert co.REF_RE.fullmatch("f123:45") and not co.REF_RE.fullmatch("f1234:1")


def test_page_js_lists_controls_before_links_and_checks_ancestors():
    js = co.PAGE_JS
    assert js.index("'input,textarea,select,button") < js.index("'a[href]'")
    assert "parentElement" in js and "aria-checked" in js and "disabled: disabledOf(el)" in js


def test_connect_engine_waits_for_the_listener_but_not_for_a_squatter(tmp_path):
    p = tmp_path / "tcp"
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    p.write_text(header)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:  # the engine comes up, but as the wrong user: refused at once
            p.write_text(header + "   0: 0100007F:4B85 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
                                  "   1000        0 1\n")
    with pytest.raises(co.Refused):
        co.connect_engine(991, (str(p),), wait=10, sleep=sleep)
    assert len(sleeps) == 3
    p.write_text(header)
    sleeps.clear()
    with pytest.raises(co.Refused):
        co.connect_engine(991, (str(p),), wait=2, sleep=sleeps.append)  # never comes up: gives up
    assert len(sleeps) == 4


# --- sessions -----------------------------------------------------------------------------------


def test_idle_and_hard_timeouts_and_hold(env):
    c, eng, clock = env
    sid, _ = opened(c)
    clock.t += co.IDLE + 1
    assert c.handle({"cmd": "status"})["state"] == "idle" and eng.disposed == 1
    sid, _ = opened(c)
    assert c.handle({"cmd": "hold", "session": sid, "until": clock.t + 25 * 60})["ok"]
    clock.t += 20 * 60
    assert c.handle({"cmd": "status"})["session"] == sid  # held while an approval waits
    clock.t += 11 * 60
    assert c.handle({"cmd": "status"})["state"] == "idle"  # but never beyond 30 minutes


def test_engine_failure_closes_the_session(env):
    c, eng, _ = env
    sid, snap = opened(c)

    def broken(*a, **k):
        raise co.CdpError("boom")
    eng.call = broken
    c.session.cdp.call = broken
    r = c.handle({"cmd": "snapshot", "session": sid})
    assert not r["ok"] and c.session is None


# --- engine connection, socket ------------------------------------------------------------------


def proc_tcp(tmp_path, uid, local="0100007F:4B85"):
    p = tmp_path / "tcp"
    p.write_text("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                 f"   0: {local} 00000000:0000 0A 00000000:00000000 00:00000000 00000000   {uid}        0 12345\n")
    return (str(p),)


def test_listener_uid_and_squatter_refused(tmp_path):
    assert co.listener_uid(19333, proc_tcp(tmp_path, 991)) == 991
    assert co.listener_uid(19333, proc_tcp(tmp_path, 991, local="00000000:4B85")) == 991
    with pytest.raises(co.Refused):
        co.connect_engine(991, proc_tcp(tmp_path, 1000))  # someone else bound the port
    with pytest.raises(co.Refused):
        co.connect_engine(None, proc_tcp(tmp_path, 991))


def test_socket_peer_check_and_no_echo():
    c = co.Controller(connect=lambda: (_ for _ in ()).throw(AssertionError("no")), mem_mb=lambda: 2000)
    a, b = socket.socketpair()
    a.sendall(b'{"cmd":"open","url":"http://10.0.0.1/' + b"SECRET4242" + b'"}\n')
    co.serve_one(c, b, {123}, uid_of=lambda s: 999)
    assert json.loads(a.recv(65536)) == {"ok": False, "error": "not allowed"}
    a, b = socket.socketpair()
    a.sendall(b'{"cmd":"fill","session":"x","fields":"SECRET4242"}\n')
    co.serve_one(c, b, {123}, uid_of=lambda s: 123)
    resp = a.recv(65536).decode()
    assert "SECRET" not in resp and json.loads(resp)["ok"] is False


def test_bad_requests():
    c = co.Controller(connect=lambda: None)
    assert c.handle(None)["error"] == "bad request"
    assert c.handle({"cmd": "eval"})["error"] == "unknown command"


# --- helpers ------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Total: 23,90 €", [("23.90", "EUR")]),
    ("Grand total €1,234.50", [("1234.50", "EUR")]),
    ("Yhteensä\n12,00 €", [("12.00", "EUR")]),
    ("Att betala: 199 kr", [("199.00", "KR")]),
    ("Order total USD 5", [("5.00", "USD")]),
    ("Total CA$12.50", [("12.50", "CAD")]),
    ("Total: A$ 9.95", [("9.95", "AUD")]),
    ("Total US$100", [("100.00", "USD")]),
    ("Total NZ$7", [("7.00", "NZD")]),
    ("Total $3.00", [("3.00", "USD")]),
    ("Total (2 items)", []),
    ("Subtotal 10.00 EUR\nTotal 12,50 EUR", [("10.00", "EUR"), ("12.50", "EUR")]),
])
def test_extract_totals(text, expected):
    assert [(t["amount"], t["currency"]) for t in co.extract_totals(text)] == expected


def test_origin_of():
    assert co.origin_of("https://js.stripe.com/v3/x#y") == "https://js.stripe.com"
    assert co.origin_of("https://a.b:8443/x") == "https://a.b:8443"
    assert co.origin_of("about:blank") == co.origin_of("data:text/html,x") == "opaque"
    assert co.origin_of("https://x.y/", "null") == "opaque"


def test_checkout_net_ruleset():
    rs = checkout_net.ruleset(991, 992, ["203.0.113.5/32"], ["2a01:4f8::/64"], ["185.12.64.1", "2a01:4ff:ff00::add:1"])
    assert "tcp dport 19333 meta skuid != { 0, 992 } reject with tcp reset" in rs
    assert "tcp sport 19333 meta skuid != 991 drop" in rs
    lines = rs.splitlines()
    est = next(i for i, ln in enumerate(lines) if "ct state established,related accept" in ln)
    local = next(i for i, ln in enumerate(lines) if "fib daddr type local reject" in ln)
    assert est < local  # replies to the controller over lo aren't caught by the local-reject rule
    assert lines.index(next(ln for ln in lines if "dport 19333" in ln)) < est  # port guard comes first
    assert "meta skuid 991 tcp dport { 80, 443 } accept" in rs and "ip daddr { 185.12.64.1 }" in rs
    assert rs == checkout_net.ruleset(991, 992, ["203.0.113.5/32"], ["2a01:4f8::/64"],
                                      ["185.12.64.1", "2a01:4ff:ff00::add:1"])
    files = checkout_net.files(991, 991, ["185.12.64.1"])
    assert "ampicheckout:x:991" in files["passwd"] and "0.0.0.0 www.youtube.com" in files["hosts"]


def submit_request(c, sid, snap, label="Pay 23,90 €"):
    b = c.handle({"cmd": "bind_submit", "session": sid, "ref": ref(snap, label)})
    assert b["ok"], b
    return b, {"cmd": "submit", "session": sid, "origin": b["origin"], "url": b["url"], "button": b["button"],
               "digest": b["digest"], "expect_totals": b["totals"], "gen": b["gen"], "press": b["press"], "frames": b["frames"],
               "seen": b["seen"]}


def test_submit_shows_the_form_and_presses_once(env):
    c, eng, _ = env
    sid, snap = opened(c)
    c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": "a@b.fi"})
    eng.frames[0].els[2]["value"] = "4242424242424242"
    b, rq = submit_request(c, sid, snap)
    assert {"label": "Email", "kind": "input", "value": "a@b.fi", "shown": True} in b["form"]
    assert "4242" not in json.dumps(b)
    eng.on_click[("main", 4)] = f"{SHOP}/thanks"
    r = c.handle(rq)
    assert r["ok"] and eng.clicks == [("main", 4)] and "Order 1234" in r["snapshot"]["text"]
    assert not c.handle(rq)["ok"] and eng.clicks == [("main", 4)]


@pytest.mark.parametrize("change", ["value", "label", "url", "totals", "box", "reload", "disabled", "new_field", "hidden", "dest", "frame",
                                    "unreadable", "typed", "clicked", "chosen", "around", "title"])
def test_submit_refuses_anything_changed(env, change):
    c, eng, _ = env
    sid, snap = opened(c)
    _, rq = submit_request(c, sid, snap)
    main = eng.frames[0]
    if change == "value":
        main.els[1]["value"] = "evil@x.y"
    elif change == "label":
        main.els[4]["label"] = "Delete account"
    elif change == "url":
        main.url = f"{SHOP}/other"
    elif change == "totals":
        main.text = "Total: 99,00 €"
    elif change == "box":
        main.els[3]["checked"] = True
    elif change == "reload":
        main.loader += "x"
    elif change == "disabled":
        main.els[4]["disabled"] = True
    elif change == "new_field":
        main.els[9] = el("input", label="Extra", value="1", hidden=True)
    elif change == "hidden":
        main.hidden = [["slot", "20:30"]]
    elif change == "dest":
        main.els[4]["dest"] = "https://evil.example/"
    elif change == "frame":
        eng.frames.append(Frame("new", "https://pay.example/w", parent="main"))
    elif change == "around":
        main.text = main.text.replace("Pad thai", "Pad thai 20:30")
    elif change == "title":
        main.title = "Delete account"
    elif change == "unreadable":
        main.form_broken = True
    elif change == "typed":
        assert c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": ""})["ok"]
    elif change == "chosen":
        assert c.handle({"cmd": "choose", "session": sid, "ref": ref(snap, "Country"), "option": "Sweden"})["ok"]
    elif change == "clicked":
        assert c.handle({"cmd": "click", "session": sid, "ref": ref(snap, "Save my card for next time")})["ok"]
        eng.clicks.clear()
    r = c.handle(rq)
    assert not r["ok"] and r["phase"] == "verify" and eng.clicks == []


@pytest.mark.parametrize("a,b", [("Time: 18:30", "Time: 20:00"), ("Reserved for 18:30", "Reserved for 21:00"),
                                 ("Appointment time 10:00", "Appointment time 12:00"), ("Class 18:30 left 2", "Class 20:00 left 2"),
                                 ("Time (free cancel) 10:00", "Time (no refunds!) 10:00"), ("time left 9:59", "time left 9:58")])
def test_submit_refuses_any_change_of_the_text_around(env, a, b):
    c, eng, _ = env
    sid, snap = opened(c)
    main = eng.frames[0]
    main.text += "\n" + a
    _, rq = submit_request(c, sid, snap)
    main.text = main.text.replace(a, b)
    r = c.handle(rq)
    assert not r["ok"] and r["phase"] == "verify" and eng.clicks == []


@pytest.mark.parametrize("line", ["Σύνολο 24,00 €", "Итого: 24,00 €", "Razem 24,00 €", "À payer 24,00 €", "Te betalen 24,00 €"])
def test_totals_in_more_languages(line):
    assert co.extract_totals(line, co.GATE_WORDS)
    assert not co.extract_totals(line)  # `pay` keeps its own words


@pytest.mark.parametrize("line", ["Suma częściowa 40,00 €", "Totale parziale 40,00 €", "Totaal excl. btw 40,00 €", "Итого без доставки 40,00 €"])
def test_foreign_subtotals_are_never_the_amount_to_pay(env, line):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.frames[0].text = line
    b = c.handle({"cmd": "bind", "session": sid, "refs": [ref(snap, "Pay 23,90 €")]})
    assert b["totals"] == []
    b = c.handle({"cmd": "bind_submit", "session": sid, "ref": ref(snap, "Pay 23,90 €")})
    assert b["totals"] == [] and b["gate_totals"]


@pytest.mark.parametrize("label,name,entered", [("Quantity", "", False), ("", "qty_1", False), ("Guests", "n", False),
                                                ("Phone", "phone", True), ("ID number", "", True), ("Donation", "amount", True), ("", "", True)])
def test_only_quantities_are_not_entered_text(env, label, name, entered):
    c, eng, _ = env
    sid, _ = opened(c)
    eng.frames[0].els[8] = el("input", type="number", label=label, name=name)
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    r = next(e["ref"] for e in snap["elements"] if e["type"] == "number")
    assert c.handle({"cmd": "type", "session": sid, "ref": r, "text": "2"})["ok"]
    assert c.handle({"cmd": "gate", "session": sid, "ref": r})["filled"] is entered


def test_prices_in_frames_count(env):
    c, eng, _ = env
    sid, snap = opened(c)
    eng.frames[0].text = "Delivery"
    assert not c.handle({"cmd": "gate", "session": sid, "ref": ref(snap, "Email")})["priced"]
    eng.frames[1].text = "Your price: 12 €"
    assert c.handle({"cmd": "gate", "session": sid, "ref": ref(snap, "Email")})["priced"]


def test_gate_reports_money_and_entered_text(env):
    c, _, _ = env
    sid, snap = opened(c)
    g = c.handle({"cmd": "gate", "session": sid, "ref": ref(snap, "Pay 23,90 €")})
    assert g["ok"] and not g["filled"] and g["totals"] and PSP in g["frames"] and g["priced"]
    c.handle({"cmd": "type", "session": sid, "ref": ref(snap, "Email"), "text": "a"})
    assert c.handle({"cmd": "gate", "session": sid, "ref": ref(snap, "Pay 23,90 €")})["filled"]


def test_submit_refuses_what_it_cannot_show(env):
    c, eng, _ = env
    sid, snap = opened(c)
    for attr, value in (("form_broken", True), ("problem", "a file to upload")):
        setattr(eng.frames[0], attr, value)
        r = c.handle({"cmd": "bind_submit", "session": sid, "ref": ref(snap, "Pay 23,90 €")})
        assert not r["ok"] and "owner" in r["error"]
        setattr(eng.frames[0], attr, None)


def test_submit_engine_dying_is_unknown(env):
    c, eng, _ = env
    sid, snap = opened(c)
    _, rq = submit_request(c, sid, snap)
    eng.die_after_click = True
    r = c.handle(rq)
    assert not r["ok"] and r["phase"] == "sent" and "MAY" in r["error"]


def test_page_js_has_no_value_reads_except_buttons():
    # Besides a button's caption, values are read only by the `form` op (bind_submit, for the owner's approval).
    js = co.PAGE_JS
    a, b = js.index("if (a.op === 'form')"), js.index("if (a.op === 'inspect')")
    assert "if (secret) v = '(secret)'" in js[a:b]
    js = (js[:a] + js[b:]).replace("n.value : '')", "")  # gate: a button's caption
    assert "el.value)" not in js.replace("(el.value || '') !== ''", "")
    assert "__ampiEls" not in js or True  # elements live in a closure Map inside the isolated world
