"""Signing in on a page: the checkout controller's part (bind_login, fill_login) against the fake engine, the page script in a
real browser (Edge, where there is one), the sign-in service between page and phone, and Ampi's side."""

import asyncio
import html
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest
from test_checkout_controller import Clock, FakeEngine, Frame, el
from test_signin import Phone, Server, caller, certs, confs, fast, service  # noqa: F401 (fixtures)

from ampi_phone import signin
from ampi_tools import checkout as co
from ampi_tools import ops_checkout as oc
from ampi_tools.ops import Approvals, Policy, Services, validate
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError

SITE = "https://www.club.example"
PW = "s3cret pa&ss"
ME = "me@x.example"
FACTS = ("hasForm", "sameForm", "passwords", "extra", "boxes", "shown", "method", "action", "boxed")


class Engine(FakeEngine):
    def __init__(self, pages):
        super().__init__(pages)
        self.facts, self.press, self.on_signin, self.after_click, self.keeps = {}, {}, None, None, []
        self.moved = False     # the page isn't what it was, just before it is filled in
        self.lose = None       # op: the page goes away while it runs

    def login_facts(self, f, a):
        es = [f.els.get(a.get(k)) for k in ("user", "secret") if a.get(k)]
        if any(not (e and e["connected"]) for e in es):
            return {"connected": False}
        forms = {e.get("form") for e in es}
        form = es[0].get("form")
        mine = [e for e in f.els.values() if e.get("form") == form and e["connected"]]
        out = {"connected": True, "hasForm": form is not None, "sameForm": len(forms) == 1,
               "passwords": sum(1 for e in mine if e["type"] == "password" and not e.get("hidden")),
               "extra": any(e not in es and e["kind"] in ("input", "textarea", "select") for e in mine),
               "boxes": sum(1 for e in mine if e["kind"] == "checkbox"),
               "shown": not any(e.get("hidden") for e in es), "method": "post", "action": f"{SITE}/login", "boxed": False}
        return {**out, **self.facts}

    def gate(self, f, a):
        e = f.els.get(a["n"])
        if not (e and e["connected"]):
            return {"connected": False}
        other = f.els.get(a.get("with")) or {}
        return {"connected": True, "tag": e["tag"], "texts": [e["label"]], "ident": e["id"], "submits": e.get("form") is not None,
                "search": False, "dest": f"{SITE}/login", "to": f"{SITE}/login", "method": "post", "ownMethod": True, "inForm": True,
                "plain": False, "formButton": e.get("form") is not None, "dialog": False, "leaks": False,
                "withForm": e.get("form") is not None and e.get("form") == other.get("form"), "around": f.text, **self.press}

    def op(self, f, a):
        if a["op"] == "login":
            return self.login_facts(f, a)
        if a["op"] == "gate":
            return self.gate(f, a)
        if a["op"] == "signin":
            facts = self.login_facts(f, a)
            if {k: facts.get(k) for k in a["expect"]} != a["expect"] or self.moved:
                return {"ok": False, "set": False}
            for k, name in (("user", "user"), ("secret", "password")):
                if a.get(k):
                    if f.els[a[k]].get("vanish"):
                        return {"ok": False}
                    f.els[a[k]]["value"] = a["values"][name]
                    self.sets.append((f.id, a[k], a["values"][name]))
            if self.on_signin:
                self.on_signin(f)
            return {"ok": True}
        if a["op"] == self.lose:
            self.lose = None
            super().op(f, {"op": "click", "n": a["n"]})
            self.frames = [Frame("main", f"{SITE}/in", text="Welcome")]
            self.frames[0].loader = "L-main-2"
            raise co.CdpProtocolError("Cannot find context")
        if a["op"] == "clickif":
            g = self.gate(f, a)
            if any(json.dumps(g.get(k)) != json.dumps(v) for k, v in a["expect"].items()):
                return {"ok": False}
            r = super().op(f, {"op": "click", "n": a["n"]})
            if self.after_click:
                self.after_click(f)
            return r
        return super().op(f, a)


def login_page():
    return [Frame("main", f"{SITE}/login", title="Log in", text="Welcome back", elements=[
        el("input", type="email", autocomplete="username", name="email", label="Email", form=1),     # 1
        el("input", type="password", autocomplete="current-password", name="pw", label="Password", form=1),  # 2
        el("button", label="Log in", form=1),                                                        # 3
        el("input", name="q", label="Search"),                                                       # 4
        el("button", label="Place order", form=2),                                                   # 5
        el("input", name="comment", label="Your comment", form=3),                                   # 6
        el("checkbox", label="Remember me", form=1),                                                 # 7
        el("input", name="username", label="User name", form=4),                                     # 8
        el("button", label="Next", form=4),                                                          # 9
    ]), Frame("ads", "https://ads.example/x", parent="main", elements=[el("input", type="password", label="pw in a frame")])]


@pytest.fixture
def env():
    eng = Engine({f"{SITE}/login": login_page, f"{SITE}/": lambda: [Frame(
        "home", f"{SITE}/?token={urllib.parse.quote(PW)}", text=f"Hello {ME.upper()}! Your password {PW} (s3cret pa&amp;ss) was used", title=PW)],
        "https://other.example/": lambda: [Frame("o", "https://other.example/", text="hi")]})
    clock = Clock()

    class CdpWrap:
        def __init__(self):
            self.call, self.close = eng.call, eng.close
    c = co.Controller(connect=CdpWrap, clock=clock, mem_mb=lambda: 2000, sleep=clock.sleep, check_url=lambda u: u, signin=True)
    r = c.handle({"cmd": "open", "url": f"{SITE}/login"})
    refs = {e["label"]: e["ref"] for e in r["snapshot"]["elements"]}
    return c, eng, r["session"], refs


def si(c, **req):
    return c.handle(req, signin=True)


def bind(c, sid, refs, user="Email", password="Password", press="Log in"):
    return si(c, cmd="bind_login", session=sid, **{k: refs[v] for k, v in (("user", user), ("password", password), ("press", press)) if v})


def fill(c, sid, b, **kw):
    req = {"cmd": "fill_login", "session": sid, "bound": b["bound"], "host": b["host"], "name": ME, "user": ME, "password": PW, **kw}
    return si(c, **req)


def test_signing_in(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    assert b["ok"] and b["host"] == "club.example" and b["visited"] == ["club.example"] and b["targets"] == ["club.example"]
    eng.on_click[("main", 3)] = f"{SITE}/"
    r = fill(c, sid, b)
    assert r == {"ok": True, "session": sid, "pressed": True, "moved": True}
    assert eng.sets == [("main", 1, ME), ("main", 2, PW)] and eng.clicks == [("main", 3)]
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    said = json.dumps(snap, ensure_ascii=False)
    for form in (PW, urllib.parse.quote(PW), "s3cret", ME, ME.upper()):
        assert form not in said
    assert "•••" in snap["text"] and snap["url"].startswith(f"{SITE}/?") and "•••" in snap["title"] and snap["origin"] == SITE
    c.handle({"cmd": "close", "session": sid})
    assert c.retire  # the engine goes too


def test_only_the_sign_in_service_may_and_only_that(env):
    c, _, sid, refs = env
    for cmd in ("bind_login", "fill_login"):
        assert c.handle({"cmd": cmd, "session": sid, "user": refs["Email"]}) == {"ok": False, "error": "unknown command"}
    for cmd in ("snapshot", "open", "click", "type", "status", "fill", "pay", "close", "bind", "submit", "gate", "hold"):
        assert si(c, cmd=cmd, session=sid, url=f"{SITE}/", ref=refs["Log in"], text="x")["error"] == "unknown command"


def test_the_socket_tells_the_two_callers_apart():
    import socket

    seen = []

    class C:
        def handle(self, req, signin=False):
            seen.append((req["cmd"], signin))
            return {"ok": True}

    def call(uid):
        a, b = socket.socketpair()
        with a:
            a.sendall(b'{"cmd": "x"}\n')
            co.serve_one(C(), b, {7}, uid_of=lambda conn: uid, signin_uids={8})
            return json.loads(a.makefile().readline())
    assert call(7)["ok"] and call(8)["ok"] and call(9) == {"ok": False, "error": "not allowed"}
    assert seen == [("x", False), ("x", True)]


def test_a_slow_caller_does_not_hold_the_controller(monkeypatch):
    import socket

    t = [0.0]
    monkeypatch.setattr(co.time, "monotonic", lambda: t[0])
    a, b = socket.socketpair()
    with a, b:
        for _ in range(20):
            a.sendall(b"x")
        real = b.recv

        class Slow:
            def settimeout(self, s):
                pass

            def recv(self, n):
                t[0] += 3
                return real(1)
        with pytest.raises(co.Refused):
            co.read_line(Slow())


# --- which form, which fields -----------------------------------------------------------------------------


@pytest.mark.parametrize("kw,why", [
    ({"password": "Email"}, "password field"),
    ({"user": "Password"}, "user name"),
    ({"user": "Your comment"}, "doesn't look like"),
    ({"user": "Search", "password": None, "press": None}, "doesn't look like"),
    ({"password": "pw in a frame"}, "frame"),
    ({"user": None, "password": None}, "give the user field"),
    ({"user": None}, "alone isn't a sign-in"),       # how sites ask "is it really you?" before something important
    ({"user": "User name"}, "same form"),
])
def test_wrong_fields(env, kw, why):
    c, eng, sid, refs = env
    r = bind(c, sid, refs, **kw)
    assert not r["ok"] and why in r["error"] and not eng.sets


@pytest.mark.parametrize("facts,why", [
    ({"passwords": 2}, "several password"), ({"passwords": 0}, "isn't usable"), ({"extra": True}, "other fields"), ({"boxes": 3}, "other fields"),
    ({"boxes": None}, "other fields"), ({"method": "get"}, "address"), ({"method": "dialog"}, "address"), ({"method": "foo"}, "address"),
    ({"action": "http://www.club.example/login"}, "https site"), ({"action": "unreadable"}, "https site"),
    ({"boxed": True}, "sandboxed"), ({"boxed": None}, "sandboxed"),
    ({"shown": False}, "visible"), ({"sameForm": False}, "same form"), ({"connected": False}, ""),
])
def test_forms_that_are_not_for_this(env, facts, why):
    c, eng, sid, refs = env
    eng.facts = facts
    r = bind(c, sid, refs)
    assert not r["ok"] and why in r["error"] and not eng.sets


def test_where_the_form_sends_it_is_told_to_the_service(env):
    c, eng, sid, refs = env
    eng.facts = {"action": "https://auth.example/x"}
    eng.press = {"to": "https://third.example/y"}
    assert bind(c, sid, refs)["targets"] == ["auth.example", "club.example", "third.example"]
    eng.facts, eng.press = {"method": "", "action": "", "hasForm": False}, {"to": "", "method": "get", "ownMethod": False}
    assert bind(c, sid, refs)["targets"] == ["club.example"]


def test_setting_a_new_password_is_the_owners(env):
    c, eng, sid, refs = env
    eng.frames[0].els[2]["autocomplete"] = "section-x New-Password"
    assert "new password" in bind(c, sid, refs)["error"]


def test_nothing_ampi_typed_is_in_that_page(env):
    c, eng, sid, refs = env
    assert c.handle({"cmd": "type", "session": sid, "ref": refs["Your comment"], "text": "attacker@evil.example"})["ok"]
    assert "was typed into this page" in bind(c, sid, refs)["error"]


@pytest.mark.parametrize("url", ["http://www.club.example/login", "https://www.club.example:8443/login", "https://1.2.3.4/login",
                                 "https://localhost/login", "https://xn--/login"])
def test_only_sites_a_login_can_be_for(env, url):
    c, eng, sid, refs = env
    eng.frames[0].url, eng.frames[0].origin = url, co.origin_of(url)
    assert not bind(c, sid, refs)["ok"]


def test_an_origin_that_is_not_the_addresses(env):
    c, eng, sid, refs = env
    eng.frames[0].origin = "https://evil.example"
    assert "https page" in bind(c, sid, refs)["error"]


def test_a_captcha_is_the_owners(env):
    c, eng, sid, refs = env
    eng.frames.append(Frame("cap", "https://newassets.hcaptcha.com/captcha/v1/x#frame=challenge", parent="main"))
    assert "captcha" in bind(c, sid, refs)["error"]


# --- the button -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["Place order", "Buy now", "Pay", "Delete my account", "Change e-mail", "Confirm", "Submit", "Send reset link",
                                   "Subscribe", "Log in and pay", "Continue to payment", "Sign up", "Create account", "OK", "Yes", "", "Log out",
                                   "Log in €5"])
def test_only_a_button_that_says_it_signs_in_is_pressed(env, label):
    c, eng, sid, refs = env
    eng.frames[0].els[3]["label"] = label
    r = bind(c, sid, refs)
    assert not r["ok"] and "checkout submit" in r["error"]
    assert bind(c, sid, refs, press=None)["ok"]  # filling is fine: the button then asks the owner


@pytest.mark.parametrize("label", ["Log in", "LOGIN", "Sign in →", "Sign\u00a0in", "Continue", "Next", "Kirjaudu sisään", "Войти", "Σύνδεση",
                                   "Continue with email", "Anmelden"])
def test_sign_in_buttons(env, label):
    c, eng, sid, refs = env
    eng.frames[0].els[3]["label"] = label
    assert bind(c, sid, refs)["ok"]


@pytest.mark.parametrize("press", [{"submits": False}, {"withForm": False}, {"tag": "a"}, {"tag": "div"}, {"texts": ["Log in", "Place order"]},
                                   {"texts": []}, {"method": "get", "ownMethod": True}, {"method": "x", "ownMethod": True}, {"connected": False}])
def test_buttons_that_are_not_the_forms_own(env, press):
    c, eng, sid, refs = env
    eng.press = press
    assert not bind(c, sid, refs)["ok"]


def test_the_button_of_another_form(env):
    c, eng, sid, refs = env
    assert "button of that form" in bind(c, sid, refs, press="Place order")["error"]
    assert "button" in bind(c, sid, refs, press="Email")["error"]


@pytest.mark.parametrize("what", ["total", "price", "frame", "box"])
def test_no_button_where_there_is_something_to_pay(env, what):
    c, eng, sid, refs = env
    if what == "total":
        eng.frames[0].text = "Order total: 240,00 €"
    elif what == "price":
        eng.frames[0].text = "From €12"
    elif what == "frame":
        eng.frames.append(Frame("p", "https://js.stripe.com/v3/x", parent="main"))
    else:
        eng.frames[0].els[7].update(label="Save my card", checked=True)
    r = bind(c, sid, refs, user="User name", password=None, press="Next")
    assert not r["ok"] and "prices or a payment form" in r["error"] and not eng.clicks


@pytest.mark.parametrize("change", ["label", "to", "gone"])
def test_the_button_is_checked_again_when_it_is_pressed(env, change):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)

    def swap(f):
        if change == "label":
            f.els[3]["label"] = "Place order"
        elif change == "to":
            eng.press = {"to": "https://evil.example/"}
        else:
            f.els[3]["connected"] = False
    eng.on_signin = swap
    r = fill(c, sid, b)
    assert not eng.clicks and (r.get("pressed") is False or "not pressed" in r["error"])


def test_a_press_that_takes_the_page_away(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    eng.lose = "clickif"
    r = fill(c, sid, b)
    assert not r["ok"] and r["phase"] == "sent" and "look at it" in r["error"] and eng.clicks == [("main", 3)]


def test_a_press_that_is_refused_on_a_page_that_moved_on(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)

    def swap(f):
        f.els[3]["label"], f.url = "Place order", f"{SITE}/in"
    eng.on_signin = swap
    r = fill(c, sid, b)
    assert not r["ok"] and r["phase"] == "sent" and not eng.clicks


def test_nothing_was_filled_in_after_all(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    eng.moved = True
    r = fill(c, sid, b)
    assert not r["ok"] and r["phase"] == "verify" and not eng.sets and c.session.secrets  # (cut out all the same)
    assert not c.session.signed_in and not c.session.user_step
    eng.moved = False
    c.session.user_step = True
    second = lambda: fill(c, sid, bind(c, sid, refs))
    eng.moved = True
    assert second()["phase"] == "verify" and c.session.user_step and c.session.account  # (as it was before)
    eng.moved = False
    assert fill(c, sid, bind(c, sid, refs))["ok"] and c.session.signed_in


def test_nothing_was_filled_in_on_the_second_page(env):
    c, eng, sid, refs = env
    fill(c, sid, bind(c, sid, refs, user="User name", password=None, press=None), password=None)
    second_page(eng)
    b = bind(c, sid, refs, user=None)
    eng.moved = True
    assert fill(c, sid, b, user=None)["phase"] == "verify" and c.session.user_step
    eng.moved = False
    assert fill(c, sid, bind(c, sid, refs, user=None), user=None)["ok"]


def test_a_user_name_in_the_button(env):
    c, eng, sid, refs = env
    eng.frames[0].els[3]["id"] = f"go-{ME}"
    fill(c, sid, bind(c, sid, refs, user="User name", password=None, press=None), password=None)
    second_page(eng)
    r = fill(c, sid, bind(c, sid, refs, user=None), user=None)
    assert r["ok"] and r["pressed"] and eng.clicks == [("main", 3)]


def test_a_field_set_with_the_owners_approval(env):
    c, eng, sid, refs = env
    eng.frames[0].els[10] = el("select", label="People", options=["1", "2"])
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    ask = lambda ref, option: c.handle({"cmd": "bind_submit", "session": sid, "ref": refs[ref], "option": option})
    assert "checkout choose" in ask("People", "2")["error"]  # (not in an account: no approval needed)
    c.session.typed.clear()
    fill(c, sid, bind(c, sid, refs, press=None))
    sets = len(eng.sets)
    for ref, option in (("People", ""), ("People", 5), ("Search", "4242 4242 4242 4242"), ("Password", "x"), ("Remember me", "x"), ("Log in", "x")):
        assert not ask(ref, option)["ok"]
    b = ask("People", "2")
    assert b["ok"] and b["set"] == "select" and b["option"] == "2"
    send = lambda b, **kw: c.handle({"cmd": "submit", "session": sid, "expect_totals": b["totals"],
                                     **{k: b.get(k) for k in ("origin", "url", "button", "gen", "press", "seen", "frames", "digest", "set", "option")}, **kw})
    assert not send(b, option=None)["ok"] and not send(b, set="input")["ok"]
    assert send(b)["ok"] and eng.frames[0].els[10].get("value") == "2" and not eng.clicks
    assert not send(b)["ok"]  # an approval is good for one
    r = send(ask("People", "7"))
    assert not r["ok"] and "no such option" in r["error"] and r["phase"] == "verify"
    assert send(ask("Search", "chess"))["ok"] and eng.sets[sets:] == [("main", 4, "chess")]


def test_a_box_the_owner_approves(env):
    c, eng, sid, refs = env
    b = c.handle({"cmd": "bind_submit", "session": sid, "ref": refs["Remember me"]})
    assert b["ok"] and b["box"] == "checkbox" and b["checked"] is False
    req = {"cmd": "submit", "session": sid, "origin": b["origin"], "url": b["url"], "button": b["button"], "gen": b["gen"], "press": b["press"],
           "seen": b["seen"], "expect_totals": b["totals"], "frames": b["frames"], "digest": b["digest"], "box": b["box"], "checked": b["checked"]}
    for wrong in ({"checked": True}, {"box": "radio"}, {"box": None}):
        assert "changed" in c.handle({**req, **wrong})["error"] and not eng.clicks
    b2 = c.handle({"cmd": "bind_submit", "session": sid, "ref": refs["Remember me"]})
    assert c.handle({**req, "gen": b2["gen"]})["ok"] and eng.clicks == [("main", 7)]
    eng.frames[0].els[7].update(label="Auto-renew my membership", checked=False)
    assert "subscription" in c.handle({"cmd": "bind_submit", "session": sid, "ref": refs["Remember me"]})["error"]
    assert "button" in c.handle({"cmd": "bind_submit", "session": sid, "ref": refs["Email"]})["error"]


def test_a_button_that_wakes_up_late(env):
    c, eng, sid, refs = env
    eng.frames[0].els[3]["disabled"] = True
    b = bind(c, sid, refs)
    assert b["ok"]
    r = fill(c, sid, b)
    assert not r["ok"] and "not pressed" in r["error"] and r["phase"] == "filled" and not eng.clicks and len(eng.sets) == 2


# --- once, in order, unchanged -------------------------------------------------------------------------------


def second_page(eng):
    """The page after the user name: only the password (and who it is for, read-only, which the script doesn't count)."""
    eng.frames[0].els[1]["connected"] = False


def test_two_pages(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs, user="User name", password=None, press="Next")
    r = fill(c, sid, b, password=None)
    assert r["ok"] and r["pressed"] and r["moved"] is False and eng.sets == [("main", 8, ME)]  # (the page didn't react)
    assert "now the password" in bind(c, sid, refs, user="User name", password=None, press=None)["error"]  # only one such step
    assert "other fields" in bind(c, sid, refs, user=None)["error"]  # (a form that also asks for an e-mail isn't the second page)
    second_page(eng)
    b = bind(c, sid, refs, user=None)
    assert b["ok"]
    assert fill(c, sid, b, user=None)["ok"] and eng.sets[-1] == ("main", 2, PW)
    assert "already" in bind(c, sid, refs)["error"] and "already" in bind(c, sid, refs, user=None)["error"]  # one sign-in per session


def test_in_a_signed_in_session_no_other_address_is_opened(env):
    c, eng, sid, refs = env
    assert c.handle({"cmd": "open", "url": f"{SITE}/login", "session": sid})["ok"]
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    fill(c, sid, bind(c, sid, refs))
    assert "signed in here" in c.handle({"cmd": "open", "url": f"{SITE}/account/cancel?yes=1", "session": sid})["error"]
    g = c.handle({"cmd": "gate", "session": sid, "ref": refs["Log in"]})
    assert g["signed_in"] is True and g["form_button"] is True


def test_the_sites_a_session_was_on(env):
    c, eng, sid, refs = env
    c.handle({"cmd": "open", "url": "https://other.example/", "session": sid})
    c.handle({"cmd": "open", "url": f"{SITE}/login", "session": sid})
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert bind(c, sid, refs)["visited"] == ["club.example", "other.example"]


def test_a_site_that_forwards_at_once_counts_too(env):
    c, eng, sid, refs = env
    eng.pages["https://evil.example/"] = login_page  # (it forwards before anyone looks)
    c.handle({"cmd": "open", "url": "https://evil.example/", "session": sid})
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert bind(c, sid, refs)["visited"] == ["club.example", "evil.example"]
    for i in range(60):
        c._visit(c.session, f"https://s{i}.example/")
    assert "too many sites" in bind(c, sid, refs)["error"]


def test_after_the_user_name_it_counts_as_an_account(env):
    c, eng, sid, refs = env
    fill(c, sid, bind(c, sid, refs, user="User name", password=None, press=None), password=None)
    assert c.handle({"cmd": "gate", "session": sid, "ref": refs["Log in"]})["signed_in"] is True
    assert "signed in here" in c.handle({"cmd": "open", "url": f"{SITE}/x", "session": sid})["error"]


def test_fields_that_act_at_once_in_an_account(env):
    c, eng, sid, refs = env
    eng.frames[0].els[10] = el("select", label="Plan", options=["Gold", "Free"])
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert c.handle({"cmd": "choose", "session": sid, "ref": refs["Plan"], "option": "Free"})["ok"]
    c.session.typed.clear()
    fill(c, sid, bind(c, sid, refs, press=None))
    for req in ({"cmd": "choose", "ref": refs["Plan"], "option": "Gold"}, {"cmd": "type", "ref": refs["Search"], "text": "x"}):
        assert "--option" in c.handle({**req, "session": sid})["error"]
    assert c.handle({"cmd": "type", "session": sid, "ref": refs["Your comment"], "text": "hello"})["ok"]  # part of a form


def test_the_login_never_goes_into_an_address(env):
    c, eng, sid, refs = env
    fill(c, sid, bind(c, sid, refs, press=None))
    eng.press = {"leaks": True}
    assert "into an address" in c.handle({"cmd": "click", "session": sid, "ref": refs["Log in"]})["error"] and not eng.clicks


def test_what_is_to_pay_is_seen_as_it_is(env):
    c, eng, sid, refs = env
    fill(c, sid, bind(c, sid, refs, press=None), name="1234567", user="1234567")
    eng.frames[0].text = "no 1234567. Total: 1234567 €"
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    assert snap["priced"] and snap["totals"] == [{"amount": "1234567.00", "currency": "EUR", "label": "total"}] and "1234567" not in snap["text"]


def test_a_press_the_owner_approves_after_signing_in(env):
    c, eng, sid, refs = env
    fill(c, sid, bind(c, sid, refs, press=None))
    eng.frames[0].url = f"{SITE}/book?who={urllib.parse.quote(ME)}"
    eng.frames[0].els[5]["label"] = f"Book as {ME}"
    b = c.handle({"cmd": "bind_submit", "session": sid, "ref": refs["Place order"]})
    assert b["ok"] and ME not in json.dumps(b)
    req = {"cmd": "submit", "session": sid, "origin": b["origin"], "url": b["url"], "button": b["button"], "gen": b["gen"], "press": b["press"],
           "seen": b["seen"], "expect_totals": b["totals"], "frames": b["frames"], "digest": b["digest"]}
    r = c.handle(req)
    assert r["ok"] and r["phase"] == "sent" and eng.clicks[-1] == ("main", 5)


def test_an_engine_is_not_used_twice_where_there_are_sign_ins(env):
    c, _, sid, _ = env
    c.handle({"cmd": "close", "session": sid})
    assert c.retire and "starting afresh" in c.handle({"cmd": "open", "url": f"{SITE}/login"})["error"]
    d = co.Controller(connect=lambda: None)
    assert d.fresh is False


@pytest.mark.parametrize("change", ["click", "element", "field", "url", "host", "facts", "reload", "ref", "session", "press"])
def test_nothing_is_filled_when_something_changed(env, change):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    kw = {}
    if change == "click":
        c.handle({"cmd": "click", "session": sid, "ref": refs["Place order"]})
    elif change == "element":
        eng.frames[0].els[2]["connected"] = False
    elif change == "field":
        eng.frames[0].els[2]["name"] = "other"
    elif change == "url":
        eng.frames[0].url, eng.frames[0].origin = "https://evil.example/login", "https://evil.example"
    elif change == "host":
        kw["host"] = "evil.example"
    elif change == "facts":
        eng.facts = {"action": f"{SITE}/elsewhere"}
    elif change == "press":
        eng.press = {"to": f"{SITE}/elsewhere"}
    elif change == "reload":
        eng.frames[0].loader = "new"
    elif change == "ref":
        b["bound"]["elements"]["password"]["ref"] = refs["Email"]
    elif change == "session":
        sid = "0" * 16
    r = fill(c, sid, b, **kw)
    assert not r["ok"] and not eng.sets and PW not in json.dumps(r) and (r.get("phase") == "verify" or change == "session")


@pytest.mark.parametrize("what", ["frame_id", "origin", "loader", "gen"])
def test_every_part_of_the_binding_counts(env, what):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    b["bound"][what] = "x"
    assert not fill(c, sid, b)["ok"] and not eng.sets


@pytest.mark.parametrize("kw", [{"password": None}, {"password": ""}, {"password": "x" * 201}, {"password": "a\nb"}, {"user": 5}, {"name": None},
                                {"password": "\ud800abcd"}, {"bound": None}, {"bound": {"elements": []}}])
def test_bad_values(env, kw):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    assert not fill(c, sid, b, **kw)["ok"] and not eng.sets and c.session is not None


def test_the_page_changing_while_it_is_filled(env):
    c, eng, sid, refs = env
    eng.frames[0].els[2]["vanish"] = True
    r = fill(c, sid, bind(c, sid, refs))
    assert not r["ok"] and r["phase"] == "filled" and not eng.clicks


def test_the_engine_dying_says_nothing_of_the_password(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    eng.die_after_click = True
    r = fill(c, sid, b)
    assert not r["ok"] and PW not in json.dumps(r) and c.retire


def test_a_page_that_stays_the_same(env):
    c, eng, sid, refs = env
    assert fill(c, sid, bind(c, sid, refs))["moved"] is False
    eng2 = Engine({f"{SITE}/login": login_page})
    c2 = co.Controller(connect=lambda: type("W", (), {"call": eng2.call, "close": eng2.close})(), clock=Clock(), mem_mb=lambda: 2000,
                       sleep=lambda s: None, check_url=lambda u: u)
    c2.sleep = c2.clock.sleep
    r = c2.handle({"cmd": "open", "url": f"{SITE}/login"})
    refs = {e["label"]: e["ref"] for e in r["snapshot"]["elements"]}
    eng2.after_click = lambda f: f.els[2].update(connected=False)  # the form is gone: a single-page site took it
    assert fill(c2, r["session"], bind(c2, r["session"], refs))["moved"] is True


# --- cutting the login out of replies ------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [PW, "Tr0ub(4)dor&3", "pa ss!1", "abc*def~1", "p%ss word*", "ä-ö-ü-ß", "a'b\"c<d>&e", "back\\slash"])
def test_it_is_cut_in_every_way_a_page_would_write_it(secret):
    js = [urllib.parse.quote(secret, safe="!'()*~-_."), urllib.parse.quote_plus(secret, safe="*-._"), urllib.parse.quote(secret, safe=""),
          urllib.parse.quote(secret, safe="").lower() if "%" in urllib.parse.quote(secret, safe="") else secret,
          urllib.parse.quote(urllib.parse.quote(secret, safe=""), safe=""), html.escape(secret), json.dumps(secret)[1:-1], secret, *([secret.upper()] if len(secret.upper()) == len(secret) else [])]
    for f in js:
        out = co.hidden({"text": f"x {f} y", "url": f"https://a.example/p?q=1&t={f}&z=2", "label": f}, [secret])
        said = json.dumps(out, ensure_ascii=False)
        assert f not in said and "•••" in out["text"] and "•••" in out["url"] and out["url"].startswith("https://a.example"), f


def test_only_the_pages_words_are_touched():
    out = co.hidden({"kind": "button", "origin": "https://www.club.example", "ref": "f0:1", "session": "club", "text": "a button", "n": 5,
                     "url": "https://www.club.example/club", "elements": [{"kind": "button", "label": "Button"}]}, ["button", "club"])
    assert out == {"kind": "button", "origin": "https://www.club.example", "ref": "f0:1", "session": "club", "text": "a •••", "n": 5,
                   "url": "https://www.club.example/•••", "elements": [{"kind": "button", "label": "•••"}]}
    assert co.hidden({"text": "abc abcabc"}, ["abc"]) == {"text": "abc abcabc"}  # too short to cut out safely
    assert co.hidden({"text": "x"}, []) == {"text": "x"}


def test_the_beginning_of_one_at_the_end_of_a_cut_text():
    long = "a" * 40 + " "
    assert co.hidden({"text": long + "s3cret p"}, [PW]) == {"text": long + "•••"}
    assert co.hidden({"text": long + "s3c"}, [PW]) == {"text": long + "s3c"} and co.hidden({"text": "s3cret p"}, [PW]) == {"text": "s3cret p"}


def test_user_names_that_are_words_or_numbers():
    h = lambda text, name: co.hidden({"text": text}, [co.Name(name), PW])["text"]
    assert h("Information for info, INFO!", "info") == "Information for •••, •••!"
    assert h("savanna anna", "anna") == "savanna •••" and h("Order total: 2400,00 EUR 2400", "2400") == "Order total: 2400,00 EUR 2400"
    assert h("240000 but 240000,00 and 1240000 or 1.240000", "240000") == "••• but 240000,00 and 1240000 or 1.240000"
    assert h("?e=me%40x.example&x=%22me%40x.example%22&y=%2522me%2540x.example", ME) == "?e=•••&x=%22•••%22&y=%2522•••"
    assert h("(me@x.example) x-me@x.example", ME) == "(•••) x-•••" and h("some@x.example", ME) == "some@x.example"
    assert co.hidden({"text": "1 2+2 is a+b here"}, ["a bc"])["text"] == "1 2+2 is a+b here"
    assert co.hidden({"text": "one s3cret%2520pa%26ss two"}, [PW])["text"] == "one ••• two"


def test_what_the_service_gets_back_is_as_it_is():
    assert co.hidden({"bound": {"press": {"texts": [ME]}}, "title": ME}, [co.Name(ME)]) == {"bound": {"press": {"texts": [ME]}}, "title": "•••"}


def test_a_piece_is_never_left_over(env):
    c, eng, sid, refs = env
    fill(c, sid, bind(c, sid, refs))
    eng.frames[0].title = "t" * 295 + PW
    eng.frames[0].els[4]["label"] = "l" * 115 + PW
    said = json.dumps(c.handle({"cmd": "snapshot", "session": sid}), ensure_ascii=False)
    assert "s3c" not in said


def test_login_host():
    assert co.login_host("https://WWW.Club.example:443/x?y") == "club.example"
    for bad in ("http://club.example", "https://club.example:444/", "https://user@1.2.3.4/", "about:blank", "", "https://[::1]/", "https://a..b/",
                "https://club.example:x/"):
        assert co.login_host(bad) == ""


# --- what Ampi may press in a signed-in account --------------------------------------------------------------


def g(text, kind="button", **kw):
    base = {"kind": kind, "texts": [text] if text else [], "tag": "a" if kind == "link" else "input" if kind in ("checkbox", "radio") else "button",
            "submits": False, "filled": True, "signed_in": True, "frame_origin": SITE, "href": f"{SITE}/a", "dest": f"{SITE}/classes/5" if kind == "link" else "",
            "totals": [], "frames": [SITE], "priced": False, "role": "", "form_button": True, "plain": kind == "link", "dialog": False, "search": False,
            "ident": "", "method": ""}
    return {**base, **kw}


ACTS = ["Renew", "Upgrade plan", "Top up", "Start free trial", "Redeem", "Use credit", "Transfer", "Withdraw", "Cancel membership", "Close account",
        "Deactivate account", "Turn off 2-step verification", "Create API key", "Show recovery codes", "Remove", "Accept", "Save", "Change", "Edit", "Choose",
        "Continue", "Apply", "Yes", "OK", "Confirm", "Book", "Book now", "Use plan", "Log out", "", "Add", "Join waitlist", "Accept all", "Checkout",
        "Use this address", "Go premium", "Try it free", "Switch plan", "Change plan", "Reorder", "Rebook", "Autopay", "Use points", "Claim", "Extend", "Give",
        "Tip", "Yes, I'm sure", "Proceed", "I understand", "Allow", "Authorize", "Grant access", "Connect app", "Verify", "Trust this device", "Forget device",
        "Signout", "Log off", "Opt out", "Leave club", "Pause membership", "Freeze", "Stop", "End membership", "Terminate", "Suspend", "Quit", "Unlink",
        "Disconnect", "Drop class", "Swap", "Set as default", "Share", "Invite", "Block", "Archive", "Undo", "Close", "Decline", "Reject", "Dismiss"]


@pytest.mark.parametrize("text", ACTS + ["12 months", "1 month", "10 credits", "50 euros", "5 tickets", "30 days", "Money transfer", "Monthly plan",
                                         "Monthly billing", "Wedding booking", "Wednesday off", "Mayor", "Saturn", "May I"])
def test_in_an_account_almost_every_button_asks(text):
    assert oc.commits(g(text)) in ("order", "commit")
    assert oc.commits(g(text, priced=True)) in ("order", "commit")
    assert oc.commits(g(text, kind="checkbox", role="switch")) in ("order", "commit")


@pytest.mark.parametrize("text", ["Search", "Filter", "Back", "Menu", "Next month", "Previous week", "Show more", "Add to cart", "View cart", "10:30",
                                  "9:00 - 10:00", "10.30 am", "Fri 27", "14", "27 Nov", "Friday, 27 November 2026", "Nov 27", "May 5", "2026-11-27", "27.11."])
def test_looking_around_does_not(text):
    assert oc.commits(g(text)) is None
    assert oc.commits(g(text, submits=True)) in ("order", "commit")  # unless it is a form's button


def test_a_search_forms_own_button():
    assert oc.commits(g("Search", submits=True, search=True)) is None and oc.commits(g("Pay", submits=True, search=True))


@pytest.mark.parametrize("text", ["Close", "×", "Decline", "Reject all", "Accept all cookies", "Necessary only"])
def test_closing_a_dialog(text):
    assert oc.commits(g(text, dialog="cookies")) is None
    assert (oc.commits(g(text, dialog="dialog")) is None) is (text in ("Close", "×"))  # (declining may be an invitation)
    assert oc.commits(g(text)) and oc.commits(g(text, dialog="cookies", submits=True))


def test_with_money_around_only_dates_and_moving_about():
    for text in ("10:30", "Next month", "Back", "Fri 27", "Add to cart"):
        assert oc.commits(g(text, priced=True)) is None
    for text in ("Next", "Show more", "Search", "View cart", "More", "Filter"):
        assert oc.commits(g(text, priced=True)) in ("order", "commit")


@pytest.mark.parametrize("dest", [f"{SITE}/classes/drawing-for-beginners", f"{SITE}/schedule?date=2026-11-27", f"{SITE}/", f"{SITE}/my-bookings",
                                  f"{SITE}/bookings/5f3a9c2d", f"{SITE}/en/events/5", f"{SITE}/orders/12/details", f"{SITE}/booking-calendar/life-drawing?referral=x",
                                  f"{SITE}/account", f"{SITE}/products/blue-pen?page=2",
                                  f"{SITE}/book-online", f"{SITE}/events/book-club-friday", f"{SITE}/start"])
def test_links_to_places(dest):
    assert oc.commits(g("Drawing, Friday", kind="link", dest=dest)) is None


@pytest.mark.parametrize("dest", [
    f"{SITE}/a#", f"{SITE}/a", f"{SITE}/a?x=1", "javascript:void(0)", "https://other.example/classes/5", f"{SITE}.evil.example/classes", f"{SITE}/signout",
    f"{SITE}/sign-out", f"{SITE}/logoff", f"{SITE}/leave", f"{SITE}/membership/end", f"{SITE}/pause", f"{SITE}/terminate", f"{SITE}/drop?id=5",
    f"{SITE}/reorder?id=5", f"{SITE}/rebook/5", f"{SITE}/autopay/on", f"{SITE}/oneclick?sku=5", f"{SITE}/1-click", f"{SITE}/trial/start",
    f"{SITE}/plan/switch?to=gold", f"{SITE}/oauth/authorize?x=1", f"{SITE}/device?code=5", f"{SITE}/allow/1", f"{SITE}/set-default/2", f"{SITE}/opt-out",
    f"{SITE}/undo/7", f"{SITE}/mark-paid", f"{SITE}/enroll/5", f"{SITE}/waitlist/join/5", f"{SITE}/claim/5", f"{SITE}/use-credit/5", f"{SITE}/%2564elete/5",
    f"{SITE}/%63ancel", f"{SITE}/account/close", f"{SITE}/account/cancel?id=5", f"{SITE}/bookings/5/cancel", f"{SITE}/classes/5?action=book",
    f"{SITE}/classes/../logout", f"{SITE}/classes//x", f"{SITE}/classes/5/cancel_booking", f"{SITE}/profile/x7",
    *(f"{SITE}/{x}" for x in ("classes/5/book", "classes/book/5", "events/5/register", "class/5/signup", "class/5/sign-up", "events/5/rsvp", "classes/5/attend",
                              "classes/5/apply", "services/5/use", "product/5/order", "class/5/unregister", "classes/5/checkin", "classes/5/refund",
                              "classes/5/book-now", "classes/5/rebook", "events/book_spot/5", "blog/5/like", "news/5/vote", "shop/add/5")), f"{SITE}/anything-else", f"{SITE}/me/do-it"])
def test_links_that_may_do_something(dest):
    assert oc.commits(g("Drawing, Friday", kind="link", dest=dest)) in ("order", "commit")


@pytest.mark.parametrize("text", [x for x in ACTS if x not in (
    # (words that a plain link to a place may have; the place decides)
    "Add", "Apply", "Choose", "Book", "Show recovery codes", "OK", "Close", "Dismiss", "Decline", "Reject", "Use this address")])
def test_links_that_say_they_do_something(text):
    assert oc.commits(g(text, kind="link")) in ("order", "commit")


def test_a_price_next_to_a_plain_link():
    for text in ("Life drawing, Fri 18:00 - €45", "Pen €20", "Drawing"):
        assert oc.commits(g(text, kind="link", priced=True)) is None
        assert oc.commits(g(text, kind="link", priced=True, dest=f"{SITE}/cart/add?id=5"))


def test_arrows():
    for signed_in in (True, False):
        assert oc.commits(g("›", signed_in=signed_in, filled=False)) is None
        assert oc.commits(g("›", signed_in=signed_in, submits=True)) == "commit"
    assert oc.commits(g("→", priced=True)) == "commit" and oc.commits(g("✕")) == "commit"


def test_a_time_with_a_price():
    assert oc.caption("18:00 €45") == "18:00" and oc.caption("18:00 45 €") == "18:00"
    assert oc.commits(g("18:00 €45", signed_in=False, filled=False)) is None


def test_links_that_are_more_than_links():
    assert oc.commits(g("Drawing", kind="link", plain=False)) and oc.commits(g("", kind="link"))
    assert oc.commits(g("Gold", kind="button", tag="a", dest=f"{SITE}/delete/5"))  # an option inside a link


def test_boxes_in_an_account():
    assert oc.commits(g("I agree to the house rules", kind="checkbox"))  # ("agree": the owner's)
    assert oc.commits(g("Vegetarian", kind="checkbox")) is None  # part of a form: the owner sees it when it is sent
    for text in ("Auto-renew my plan", "Autopay", "Save card", "2FA", "MFA", "Public profile", "Share my data", "Use my points"):
        assert oc.commits(g(text, kind="checkbox")), text
        assert oc.commits(g("", kind="checkbox", label=text)), text  # (a <label> around it isn't its caption)
    assert oc.commits(g("Vegetarian", kind="checkbox", form_button=False)) and oc.commits(g("x", kind="checkbox", tag="div"))
    assert oc.commits(g("Morning", kind="radio")) is None


def test_outside_an_account_nothing_changed():
    assert oc.commits(g("Continue", signed_in=False, filled=False)) is None and oc.commits(g("Save", signed_in=False, filled=False)) is None


# --- the page-side script in a real browser (Edge on the owner's PC, Chromium elsewhere; skipped without one) ------


EDGE = next((str(p) for p in (Path(os.environ.get("ProgramFiles(x86)", "x")) / "Microsoft/Edge/Application/msedge.exe",
                              Path(os.environ.get("ProgramFiles", "x")) / "Microsoft/Edge/Application/msedge.exe",
                              *filter(None, map(shutil.which, ("chromium", "google-chrome", "chrome", "msedge", "microsoft-edge")))) if Path(p).exists()), None)
PAGE = """<!doctype html><meta charset=utf-8><title>t</title><base href="https://base.example/">
<form id=login method=POST action="/do"><input name=email type=email><input name=p type=password><input type=checkbox name=remember>
 <input type=hidden name=csrf value=x><input name=url style="display:none" value=""><input type=password name=trap tabindex=-1><button>Log in</button>
 <button formaction="https://evil.example/x" id=evil>Log in</button><button formmethod=get id=get>Log in</button></form>
<form id=plain><input name=user2><input type=password name=p2><button id=b2>Sign in</button></form>
<form id=traps method=post><input type=email name=e10><input type=password name=p10><input name=website tabindex=-1><input name=hp style="opacity:0">
 <div style="position:absolute;left:-9999px"><input name=off></div><select name=language><option>en</select><button>Log in</button></form>
<form id=sel method=post><input type=email name=e11><input type=password name=p11><select name=plan><option>gold</select><button>Log in</button></form>
<form id=seen method=post><input type=email name=e12><input type=password name=p12><input name=hp2 style="opacity:0" value=x><button>Log in</button></form>
<form id=reauth><input type=password name=p3><button>Delete my account</button></form>
<form id=mail method=post><input type=email name=new><input type=password name=p4><input name=other><button>Change</button></form>
<form id=far method=post></form><input form=far type=email name=e5><input form=far type=password name=p5><input form=far type=password name=p5b><button form=far id=b5>Log in</button>
<form id=js action="javascript:void(0)"><input type=email name=e6><input type=password name=p6><button>Log in</button></form>
<form id=getf method=get><input type=email name=e7><input type=password name=p7><button>Log in</button></form>
<div id=free><div><input type=email name=e8></div><div><input type=password name=p8></div><button id=b8>Log in</button></div><input name=newsletter>
<a href="/classes/5">Plain</a><a href="/x" onclick="1">Onclick</a><a href="/x" data-method=delete>Method</a><a href="/x" role=button>Role</a>
<label><input type=checkbox>Loose</label><form><label><input type=checkbox>In form</label><button>Go</button></form>
<div role=dialog><button type=button>Bye</button></div><div class="my-Cookie-bar"><button type=button>Bye2</button></div>
<div class="cookies-not-set"><p>LONG</p><button type=button>Bye3</button></div>
<label id=lab><input type=email name=e9><button form=login>x</button></label><pre id=out></pre>"""
SCRIPT = r"""
const run = (op) => JSON.parse(JSON.stringify(globalThis.__ampi.run(op)));
const q = (s) => document.querySelector(s);
const all = run({ op: 'enumerate', main: true }).elements, byName = {}, byId = {};
for (const e of all) { if (e.name) byName[e.name] = e.n; if (e.id) byId[e.id] = e.n; }
const out = {}; const login = (u, s) => run({ op: 'login', user: u ? byName[u] : null, secret: s ? byName[s] : null });
out.login = login('email', 'p'); out.plain = login('user2', 'p2'); out.reauth = login(null, 'p3'); out.mail = login('new', 'p4');
out.far = login('e5', 'p5'); out.js = login('e6', 'p6'); out.getf = login('e7', 'p7'); out.free = login('e8', 'p8'); out.mixed = login('email', 'p2');
out.extras = [['e10', 'p10'], ['e11', 'p11'], ['e12', 'p12']].map(x => login(...x).extra);
const btn = (sel) => all.find(e => e.tag === 'button' && run({ op: 'gate', n: e.n }).connected && q(sel) && e.id === (q(sel).id)).n;
const gate = (sel, withName) => { const g = run({ op: 'gate', n: byId[sel], with: byName[withName] }); return [g.submits, g.withForm, g.method, g.ownMethod, g.to]; };
const framed = (h) => { const d = document.createElement('div'); d.innerHTML = h; document.body.append(d); const r = login('email', 'p').boxed; d.remove(); return r; };
out.boxed = ['', '<iframe src="https://other.example/x" sandbox></iframe>', '<iframe srcdoc="x"></iframe>', '<iframe></iframe>',
  '<div><template shadowrootmode=open></template></div>', '<iframe srcdoc="x" sandbox="allow-scripts"></iframe>', '<iframe sandbox></iframe>', '<iframe sandbox src="data:text/html,x"></iframe>', '<iframe sandbox src="' + location.href + '"></iframe>'].map(framed);
const host = document.createElement('div'); document.body.append(host); host.attachShadow({ mode: 'open' }).innerHTML = '<iframe sandbox srcdoc="x"></iframe>';
out.shadow = login('email', 'p').boxed; host.remove();
out.evil = gate('evil', 'p'); out.get = gate('get', 'p'); out.b2 = gate('b2', 'p2'); out.b5 = gate('b5', 'p5'); out.b8 = gate('b8', 'p8'); out.wrong = gate('b2', 'p');
const look = (sel) => { const e = all.find(x => x.label === sel); const g = run({ op: 'gate', n: e.n }); return [g.tag, g.plain, g.formButton, g.dialog]; };
out.links = ['Plain', 'Onclick', 'Method', 'Role'].map(look); out.boxes = ['Loose', 'In form'].map(look); out.dialogs = ['Bye', 'Bye2', 'Bye3', 'Go'].map(look);
const expect = (f) => Object.fromEntries(['hasForm','sameForm','passwords','extra','boxes','shown','method','action','boxed'].map(k => [k, f[k]]));
// a page that swaps the password field for a text field while the user name is being filled in
q('[name=e6]').addEventListener('input', () => { q('[name=p6]').type = 'text'; });
out.swap = [run({ op: 'signin', user: byName.e6, secret: byName.p6, values: { user: 'me', password: 'hunter22' }, expect: expect(out.js) }), q('[name=p6]').value];
out.stale = run({ op: 'signin', user: byName.email, secret: byName.p, values: { user: 'me', password: 'hunter22' }, expect: { ...expect(out.login), action: 'https://x/' } });
const seen = []; q('#plain').addEventListener('submit', (e) => { e.preventDefault(); seen.push([q('#plain').method, e.submitter && e.submitter.id]); });
q('[name=p2]').addEventListener('input', (e) => seen.push(['input', e.target.value.length]));
out.fill = run({ op: 'signin', user: byName.user2, secret: byName.p2, values: { user: 'me', password: 'hunter22' }, expect: expect(out.plain) });
out.values = [q('[name=user2]').value, q('[name=p2]').value, q('#plain').getAttribute('method')];
const g2 = run({ op: 'gate', n: byId.b2, with: byName.p2 }); const press = {}; for (const k of ['tag','texts','to','submits','ident']) press[k] = g2[k];
out.clickWrong = run({ op: 'clickif', n: byId.b2, expect: { ...press, texts: ['Other'] } });
q('#plain').removeAttribute('method'); out.click = run({ op: 'clickif', n: byId.b2, expect: press }); out.seen = seen;
out.clickGet = run({ op: 'clickif', n: byId.get, expect: {} });
const lg = run({ op: 'login', user: byName.email, secret: byName.p }); run({ op: 'signin', user: byName.email, secret: byName.p, values: { user: 'me', password: 'hunter22' }, expect: expect(lg) });
out.leaks = [run({ op: 'gate', n: byId.get }).leaks, run({ op: 'gate', n: byId.evil }).leaks];
q('[name=p2]').type = 'text'; out.form = run({ op: 'form' }).fields.filter(f => f.name === 'p2').map(f => f.value);
q('#out').textContent = 'RESULT' + JSON.stringify(out) + 'END';
"""


@pytest.mark.skipif(EDGE is None, reason="needs Edge or Chromium")
def test_the_page_script_in_a_browser(tmp_path):
    f = tmp_path / "t.html"
    f.write_text(PAGE.replace("LONG", "x " * 2500).replace("<meta charset=utf-8>", "<meta charset=utf-8><body class=cookies-not-set>") + "<script>" + co.PAGE_JS + "</script><script>try{" + SCRIPT + "}catch(e){document.querySelector('#out').textContent='RESULT'+"
                 "JSON.stringify({error:String(e&&e.stack||e)})+'END'}</script>", encoding="utf-8")
    r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom",
                        f.as_uri()], capture_output=True, timeout=90)
    dom = r.stdout.decode("utf-8", "replace")
    if not dom.strip():
        pytest.skip(f"{EDGE} gave no page: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    assert "RESULT" in dom, (dom[-1000:], r.stderr.decode("utf-8", "replace")[-1000:])
    out = json.loads(html.unescape(dom[dom.index("RESULT") + 6:dom.index("END", dom.index("RESULT"))]))
    assert "error" not in out, out["error"]
    ok = {"connected": True, "hasForm": True, "sameForm": True, "passwords": 1, "extra": False, "shown": True}
    assert {k: out["login"][k] for k in ok} == ok and out["login"]["boxes"] == 1 and out["login"]["method"] == "post"
    assert out["login"]["action"] == "https://base.example/do"
    assert {k: out["plain"][k] for k in ok} == ok and out["plain"]["method"] == "" and out["plain"]["action"] == ""  # not its <base>
    assert out["reauth"]["passwords"] == 1 and out["reauth"]["extra"] is False  # (alone: the controller refuses that)
    assert out["mail"]["extra"] is True
    assert out["far"]["passwords"] == 2 and out["far"]["sameForm"] is True  # fields that belong to a form from elsewhere
    assert out["js"]["action"] == "" and out["getf"]["method"] == "get" and out["mixed"]["sameForm"] is False
    assert out["free"]["hasForm"] is False and out["free"]["passwords"] == 1 and out["free"]["extra"] is False and out["free"]["action"] == ""
    assert out["evil"] == [True, True, "post", True, "https://evil.example/x"] and out["get"][2:4] == ["get", True]
    assert out["b2"] == [True, True, "get", False, ""] and out["b5"][:2] == [True, True] and out["b8"][:2] == [False, False]
    assert out["wrong"][:2] == [True, False]
    assert [x[:2] for x in out["links"]] == [["a", True], ["a", False], ["a", False], ["a", False]]
    assert [x[2] for x in out["boxes"]] == [False, True] and [x[3] for x in out["dialogs"]] == ["dialog", "cookies", "", ""]
    assert out["shadow"] is True
    assert out["boxed"][:5] == [False] * 5 and out["boxed"][5:8] == [True] * 3  # (a file: page is of no origin: the last is not tested)
    assert out["extras"] == [False, True, True]  # traps for robots and a language; a plan; a hidden field with something in it
    assert out["swap"] == [{"ok": False}, ""] and out["stale"] == {"ok": False, "set": False}
    assert out["fill"] == {"ok": True} and out["values"] == ["me", "hunter22", "post"]
    assert out["clickWrong"] == {"ok": False} and out["click"] == {"ok": True} and out["clickGet"] == {"ok": False}
    assert out["leaks"] == [True, False]  # a button of the form that would send the password in an address
    assert out["seen"] == [["input", 8], ["post", "b2"]]
    assert out["form"] == ["(secret)"]  # also when the page shows it


# --- the sign-in service: page -> phone -> page ---------------------------------------------------------


HOSTS = ["club.example", "auth.club.example"]
LOGIN = {"ok": True, "user": "me@x.example", "secret": PW, "hosts": HOSTS}


class Asked:
    """Stands in for the phone."""

    def __init__(self, reply=("ok", signin.Login("me@x.example", PW, frozenset(HOSTS)))):
        self.reply, self.calls = reply, []

    def __call__(self, host, conn, purpose="test"):
        self.calls.append((host, purpose))
        return self.reply


def svc(c, phone=None):
    phone = phone or Asked()
    seen = []

    def controller(req, timeout=60):
        seen.append(req)
        return json.loads(json.dumps(c.handle(req, signin=True)))
    s = signin.Service(frozenset({9}), phone, controller)
    s.seen = seen
    return s, phone


def user_page(s, sid, refs, **kw):
    return go(s, sid, refs, user=refs["User name"], password=None, press=refs["Next"], **kw)


def go(s, sid, refs, **kw):
    req = {"cmd": "fill", "session": sid, "user": refs["Email"], "password": refs["Password"], "press": refs["Log in"], **kw}
    return s.handle({k: v for k, v in req.items() if v is not None}, None)


def test_a_sign_in_from_end_to_end(env):
    c, eng, sid, refs = env
    s, phone = svc(c)
    r = go(s, sid, refs)
    assert r == {"ok": True, "code": "ok", "pressed": True, "moved": False, "password": True}
    assert phone.calls == [("club.example", "fill")]  # the browser's site, with nothing from the caller in it
    assert eng.sets == [("main", 1, "me@x.example"), ("main", 2, PW)] and eng.clicks == [("main", 3)]
    assert s.held is None and s.over(0)  # the process ends at once


def test_the_caller_cannot_choose_the_site(env):
    c, eng, sid, refs = env
    s, phone = svc(c)
    assert s.handle({"cmd": "fill", "session": sid, "user": refs["Email"], "password": refs["Password"], "host": "bank.example"}, None)["ok"]
    assert phone.calls == [("club.example", "fill")]


@pytest.mark.parametrize("reply", [("denied", None), ("no_login", None), ("ok", None)])
def test_refused_on_the_phone(env, reply):
    c, eng, sid, refs = env
    s, _ = svc(c, Asked(reply))
    r = go(s, sid, refs)
    assert r == {"ok": False, "code": reply[0] if reply[0] != "ok" else "failed"} and not eng.sets and s.held is None


def test_a_bad_form_never_reaches_the_phone(env):
    c, eng, sid, refs = env
    eng.facts = {"extra": True}
    s, phone = svc(c)
    r = go(s, sid, refs)
    assert r["code"] == "page" and "other fields" in r["error"] and not phone.calls


def test_a_form_that_sends_it_elsewhere(env):
    c, eng, sid, refs = env
    eng.facts = {"action": "https://evil.example/"}
    s, phone = svc(c)
    assert go(s, sid, refs) == {"ok": False, "code": "other_target"} and not eng.sets and s.held is None
    eng.facts = {"action": "https://auth.club.example/"}  # an address the owner saved with this login
    assert go(s, sid, refs)["ok"]


def test_a_session_that_was_elsewhere(env):
    c, eng, sid, refs = env
    c.handle({"cmd": "open", "url": "https://other.example/", "session": sid})
    c.handle({"cmd": "open", "url": f"{SITE}/login", "session": sid})
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    s, phone = svc(c)
    assert go(s, sid, refs) == {"ok": False, "code": "other_sites"} and not eng.sets and s.held is None and s.spent


def test_related_sites():
    assert signin.related("b.example", "a.b.example") and signin.related("x.y", "x.y")
    for site, host in (("evilb.example", "b.example"), ("a.example", "b.example"), ("evil.blogs.example", "blogs.example"), ("example", "b.example")):
        assert not signin.related(site, host)


def test_two_pages_one_fingerprint(env, monkeypatch):
    c, eng, sid, refs = env
    s, phone = svc(c)
    assert user_page(s, sid, refs) == {"ok": True, "code": "ok", "pressed": True, "moved": False, "password": False}
    until = s.held.until
    assert not s.over(1e9)
    second_page(eng)
    r = go(s, sid, refs, user=None)
    assert r["ok"] and len(phone.calls) == 1 and s.held is None and s.over(0)
    r = go(s, sid, refs)
    assert r["code"] == "page" and "already" in r["error"] and len(phone.calls) == 1 and until  # one sign-in per session


def test_what_is_kept_is_not_kept_longer_by_trying(env, monkeypatch):
    c, eng, sid, refs = env
    s, phone = svc(c)
    user_page(s, sid, refs)
    until = s.held.until
    eng.facts = {"extra": True}
    go(s, sid, refs, user=None)
    eng.facts = {}
    assert s.held.until == until
    assert user_page(s, sid, refs)["code"] == "page" and s.held.until == until


@pytest.mark.parametrize("other", ["session", "host", "late"])
def test_a_kept_login_is_for_that_session_and_site_and_not_long(env, other, monkeypatch):
    c, eng, sid, refs = env
    s, phone = svc(c)
    user_page(s, sid, refs)
    second_page(eng)
    if other == "session":
        s.held.session = "f" * 16
    elif other == "host":
        eng.frames[0].url, eng.frames[0].origin = "https://other.example/login", "https://other.example"
        eng.facts, eng.press = {"action": "https://other.example/login"}, {"to": ""}
    else:
        s.held.until = 0
        assert s.over(0) and s.held is None and s.spent
    phone.reply = ("denied", None)
    assert go(s, sid, refs, user=None)["code"] == "denied" and len(phone.calls) == 2 and not any(v[2] == PW for v in eng.sets)


def test_another_address_of_the_same_login_needs_no_second_fingerprint(env):
    c, eng, sid, refs = env
    s, phone = svc(c)
    user_page(s, sid, refs)
    second_page(eng)
    eng.frames[0].url, eng.frames[0].origin = "https://auth.club.example/pw", "https://auth.club.example"
    eng.facts, eng.press = {"action": "https://auth.club.example/pw"}, {"to": ""}
    assert go(s, sid, refs, user=None)["ok"] and len(phone.calls) == 1


def test_a_page_that_changes_while_the_owner_decides(env):
    c, eng, sid, refs = env

    class Slow(Asked):
        def __call__(self, host, conn, purpose="test"):
            c.handle({"cmd": "click", "session": sid, "ref": refs["Place order"]})
            return super().__call__(host, conn, purpose)
    s, phone = svc(c, Slow())
    r = go(s, sid, refs)
    assert r["code"] == "page" and "changed" in r["error"] and not eng.sets
    phone.reply = ("denied", None)
    assert go(s, sid, refs)["ok"] and len(phone.calls) == 1  # unlocked for this page a moment ago, and not used


def test_a_half_filled_form_uses_the_login_up(env):
    c, eng, sid, refs = env
    s, _ = svc(c)
    eng.frames[0].els[2]["vanish"] = True
    r = go(s, sid, refs)
    assert not r["ok"] and s.held is None and s.spent


def test_the_browser_gone(env):
    c, eng, sid, refs = env
    s, phone = svc(c)
    for error, code in ((FileNotFoundError(), "no_browser"), (signin.Busy(), "browser_busy")):
        s.controller = lambda req, timeout, error=error: (_ for _ in ()).throw(error)
        assert go(s, sid, refs) == {"ok": False, "code": code} and not phone.calls
    real = svc(c)[0].controller

    def dies(req, timeout):
        if req["cmd"] == "fill_login":
            raise ConnectionResetError
        return real(req)
    s.controller = dies
    assert go(s, sid, refs) == {"ok": False, "code": "unknown"} and s.held is None


def test_a_session_that_is_gone_keeps_nothing(env):
    c, eng, sid, refs = env
    s, phone = svc(c)
    real = s.controller

    def closes(req, timeout=60):
        if req["cmd"] == "fill_login":
            c.drop()
        return real(req)
    s.controller = closes
    r = user_page(s, sid, refs)
    assert not r["ok"] and s.held is None and s.spent


def test_nobody_is_waiting_any_more(env):
    import socket

    c, eng, sid, refs = env
    a, b = socket.socketpair()
    with a, b:
        class Left(Asked):
            def __call__(self, host, conn, purpose="test"):
                a.close()
                return super().__call__(host, conn, purpose)
        s, phone = svc(c, Left())
        r = s.handle({"cmd": "fill", "session": sid, "user": refs["Email"], "password": refs["Password"]}, b)
    assert r == {"ok": False, "code": "lost"} and not eng.sets and s.held is None


def test_the_controllers_socket(monkeypatch):
    import socket as so

    srv = so.create_server(("127.0.0.1", 0))

    class Fake(so.socket):
        def connect(self, path):
            assert path == signin.CHECKOUT
            super().connect(srv.getsockname())
    real = so.socket
    monkeypatch.setattr(signin.socket, "socket", lambda *a, **kw: real(*a, **kw) if kw else Fake(so.AF_INET))
    monkeypatch.setattr(signin.socket, "AF_UNIX", so.AF_INET, raising=False)

    def answer(reply):
        def go():
            conn, _ = srv.accept()
            with conn:
                assert conn.makefile().readline() == '{"cmd": "bind_login"}\n'
                if reply is not None:
                    conn.sendall(reply)
                else:
                    threading.Event().wait(1.5)
        threading.Thread(target=go, daemon=True).start()
    answer(b'{"ok": true}\n')
    assert signin.controller({"cmd": "bind_login"}) == {"ok": True}
    for bad in (b"", b"[1]\n"):
        answer(bad)
        with pytest.raises(signin.Lost):
            signin.controller({"cmd": "bind_login"})
    answer(None)
    with pytest.raises(signin.Busy):
        signin.controller({"cmd": "bind_login"}, timeout=0.5)
    srv.close()


@pytest.mark.parametrize("req", [{}, {"session": "x"}, {"session": "0" * 16}, {"session": "0" * 16, "user": "f0:x"},
                                 {"session": "0" * 16, "press": "f0:1"}, {"session": "0" * 16, "user": 5},
                                 {"session": ["0" * 16], "user": "f0:1"}])
def test_bad_requests(env, req):
    s, phone = svc(env[0])
    assert s.handle({"cmd": "fill", **req}, None) == {"ok": False, "code": "bad_request"} and not s.seen


@pytest.mark.parametrize("answer,ok", [
    (LOGIN, True), ({**LOGIN, "user": ""}, False), ({**LOGIN, "secret": "a\nb"}, False), ({**LOGIN, "secret": "x" * 300}, False),
    ({**LOGIN, "hosts": ["other.example"]}, False), ({**LOGIN, "hosts": "club.example"}, False), ({"ok": True}, False),
    ({**LOGIN, "hosts": ["club.example", 5]}, False)])
def test_what_the_phone_sends(confs, certs, answer, ok):  # noqa: F811
    def inside(phone, inner):
        phone.asked.append(inner.read())
        inner.send({"asking": True})
        inner.send(answer)
        try:
            inner.read()
        except OSError:
            pass
    with Server(confs[0]) as s, caller() as (mine, _):
        p = Phone(certs, s, inside)
        code, login = signin.ask("club.example", mine, confs[1], s.vault, purpose="fill")
        assert p.asked[0]["purpose"] == "fill" and p.asked[0]["host"] == "club.example"
        if ok:
            assert code == "ok" and login == signin.Login("me@x.example", PW, frozenset(HOSTS))
        else:
            assert (code, login) == ("failed", None)
        assert PW not in repr(login)


def test_the_process_ends_at_once_after_a_login_was_used(monkeypatch, env):
    c, eng, sid, refs = env
    monkeypatch.setattr(signin, "peer_uid", lambda conn: 9)
    import socket as so

    listener = so.create_server(("127.0.0.1", 0))
    s, _ = svc(c)
    t = threading.Thread(target=s.run, args=(listener,), daemon=True)
    t.start()

    def call(req):
        with so.create_connection(listener.getsockname(), timeout=10) as conn:
            conn.sendall(json.dumps(req).encode() + b"\n")
            return json.loads(conn.makefile().readline())
    base = {"cmd": "fill", "session": sid}
    assert call({**base, "user": refs["User name"], "press": refs["Next"]})["ok"]
    t.join(1.5)
    assert t.is_alive()  # it keeps the login for the second page
    second_page(eng)
    assert call({**base, "password": refs["Password"], "press": refs["Log in"]})["ok"]
    t.join(5)
    assert not t.is_alive()
    listener.close()


def test_when_the_process_ends(monkeypatch):
    s = signin.Service(frozenset())
    s.held = signin.Held("0" * 16, signin.Login("u", "p", frozenset()), signin.time.monotonic() + 100)
    assert not s.over(1e9)
    s.calls = 1
    s.held = None
    s.spent = True
    assert not s.over(1e9)  # someone is waiting for an answer
    s.calls = 0
    assert s.over(0)


# --- ampi-tools -------------------------------------------------------------------------------------------


class Checkout:
    """ops_checkout's client, over the controller."""

    def __init__(self, c):
        self.c = c

    async def call(self, req, timeout=60):
        r = self.c.handle(req)
        if not r.get("ok"):  # as the real one
            raise ToolError(r["error"])
        return r


class SigninSide:
    def __init__(self, s):
        self.s, self.calls = s, []

    async def call(self, req, timeout=None):
        self.calls.append(req)
        return self.s.handle(req, None)


def run_op(env, tmp_path, origin="private", service=None, **args):
    from ampi_tools.ops_checkout import CheckoutSide, PaymentRules
    from ampi_tools.vault import Vault

    side = CheckoutSide(Checkout(env[0]), Vault(tmp_path), PaymentRules())
    fake = SigninSide(service or svc(env[0])[0])

    async def trusted(pid):
        return False
    d = Daemon(Services(signin_client=fake, checkout_side=side), Approvals(tmp_path / "a.sqlite3"), Policy(), trusted)
    try:
        r = asyncio.run(d.run(validate([{"op": "signin.fill", "args": args}]), None, None, origin))
        return fake, d._masked(r, [{"op": "signin.fill", "args": args}])
    except ToolError as e:
        return fake, str(e)


def test_ampi_signs_in_and_sees_the_next_page_without_the_password(env, tmp_path):
    c, eng, sid, refs = env
    eng.on_click[("main", 3)] = f"{SITE}/"
    fake, r = run_op(env, tmp_path, user_ref=refs["Email"], password_ref=refs["Password"], press_ref=refs["Log in"])
    assert r["ok"] and r["text"].startswith("The login was filled in and sent.") and "Hello •••!" in r["text"]
    assert PW not in r["text"] and "s3cret" not in r["text"] and ME.upper() not in r["text"]
    assert fake.calls == [{"cmd": "fill", "session": sid, "user": refs["Email"], "password": refs["Password"], "press": refs["Log in"]}]


@pytest.mark.parametrize("origin", ["group:family", "forwarded:private", "unknown", "watcher:mail"])
def test_only_the_owners_chat(env, tmp_path, origin):
    fake, r = run_op(env, tmp_path, origin, user_ref="f0:1")
    assert isinstance(r, str) and not fake.calls


def test_ampi_hears_only_known_words(env, tmp_path):
    class Odd:
        def handle(self, req, conn):
            return {"ok": False, "code": "page", "error": "x\u202e" + "y" * 900}
    _, r = run_op(env, tmp_path, service=Odd(), password_ref="f0:2")
    text = r if isinstance(r, str) else r["error"]
    assert "not signed in" in text and "\u202e" not in text and len(text) < 300
    _, r = run_op(env, tmp_path, press_ref="f0:3")
    assert "--user" in (r if isinstance(r, str) else r["error"])


@pytest.mark.parametrize("answer,words", [
    ({"ok": False, "code": "page", "error": "after the button was pressed, the page went away", "phase": "sent"}, ["unclear", "WAS sent", "snapshot"]),
    ({"ok": False, "code": "page", "error": "x", "phase": "filled"}, ["not signed in", "filled in already"]),
    ({"ok": False, "code": "unknown"}, ["unclear", "MAY have been sent"])])
def test_what_is_said_when_it_may_have_worked(env, tmp_path, answer, words):
    class Odd:
        def handle(self, req, conn):
            return answer
    _, r = run_op(env, tmp_path, service=Odd(), password_ref="f0:2")
    text = r if isinstance(r, str) else r["error"]
    assert all(w in text for w in words)


def test_the_page_cannot_be_read_afterwards(env, tmp_path):
    c, eng, sid, refs = env

    class Odd:
        def handle(self, req, conn):
            c.drop()
            return {"ok": True, "code": "ok", "pressed": True, "moved": False, "password": True}
    _, r = run_op(env, tmp_path, service=Odd(), password_ref="f0:2")
    assert r["ok"] and "filled in and sent" in r["text"] and "can't be read" in r["text"] and "hasn't changed yet" in r["text"]


def test_odd_refs(env, tmp_path):
    fake, r = run_op(env, tmp_path, password_ref="f0:2; rm")
    assert isinstance(r, str) and not fake.calls


def test_a_controller_that_is_to_end_takes_no_more_requests(monkeypatch):
    monkeypatch.setenv("AMPI_CHECKOUT_CLIENT_UIDS", "7")
    monkeypatch.setenv("AMPI_CHECKOUT_ENGINE_UID", "9")
    monkeypatch.setattr(co.sys, "argv", ["checkout.py", "serve"])

    class Ctl:
        retire, session = False, object()

        def __init__(self, **kw):
            pass

        def expire(self):  # the session ran out; its engine isn't used again
            self.retire, self.session = True, None

    class Srv:
        def accept(self):
            raise AssertionError("accepted a request")
    monkeypatch.setattr(co, "Controller", Ctl)
    monkeypatch.setattr(co, "listening_socket", Srv)
    monkeypatch.setattr(co.select, "select", lambda r, w, x, t: (r, [], []))
    assert co.main() == 0
