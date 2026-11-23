"""Filling a login in: the checkout controller's part (bind_login, fill_login), against the fake engine."""

import json
import subprocess
import shutil
import urllib.parse

import pytest
from test_checkout_controller import Clock, FakeEngine, Frame, el

from ampi_tools import checkout as co

SITE = "https://www.club.example"
PW = "s3cret pa&ss"


class Engine(FakeEngine):
    facts: dict = {}

    def op(self, f, a):
        if a["op"] == "login":
            es = {k: f.els.get(a.get(k)) for k in ("user", "secret", "press")}
            if any(a.get(k) and not (e and e["connected"]) for k, e in es.items()):
                return {"connected": False}
            forms = {e.get("form") for e in es.values() if e}
            out = {"connected": True, "hasForm": forms != {None}, "sameForm": len(forms) == 1,
                   "passwords": sum(1 for e in f.els.values() if e["type"] == "password" and e["connected"] and not e.get("hidden")),
                   "shown": not any(e and e.get("hidden") for e in es.values()), "method": "post", "action": f"{SITE}/login",
                   "pressTag": es["press"]["tag"] if es["press"] else "", "pressMethod": "", "pressAction": "", "href": f.url}
            return {**out, **self.facts}
        if a["op"] == "set":
            self.keeps = [*getattr(self, "keeps", []), (a["n"], a.get("keep"))]
        return super().op(f, a)


def login_page():
    return [Frame("main", f"{SITE}/login", title="Log in", text="Welcome back", elements=[
        el("input", type="email", autocomplete="username", name="email", label="Email", form=1),     # 1
        el("input", type="password", autocomplete="current-password", name="pw", label="Password", form=1),  # 2
        el("button", label="Log in", form=1),                                                        # 3
        el("input", name="q", label="Search"),                                                       # 4
        el("button", label="Other", form=2),                                                         # 5
        el("input", name="comment", label="Your comment", form=1),                                   # 6
    ]), Frame("ads", "https://ads.example/x", parent="main", elements=[el("input", type="password", label="pw in a frame")])]


@pytest.fixture
def env():
    eng = Engine({f"{SITE}/login": login_page, f"{SITE}/": lambda: [Frame(
        "home", f"{SITE}/?token={urllib.parse.quote(PW)}", text=f"Hello! Your password {PW} (s3cret pa&amp;ss) was used", title=PW)]})
    clock = Clock()

    class CdpWrap:
        def __init__(self):
            self.call, self.close = eng.call, eng.close
    c = co.Controller(connect=CdpWrap, clock=clock, mem_mb=lambda: 2000, sleep=clock.sleep, check_url=lambda u: u)
    r = c.handle({"cmd": "open", "url": f"{SITE}/login"})
    refs = {e["label"]: e["ref"] for e in r["snapshot"]["elements"]}
    return c, eng, r["session"], refs


def si(c, **req):
    return c.handle(req, signin=True)


def bind(c, sid, refs, user="Email", password="Password", press="Log in"):
    return si(c, cmd="bind_login", session=sid, **{k: refs[v] for k, v in (("user", user), ("password", password), ("press", press)) if v})


def fill(c, sid, b, **kw):
    return si(c, **{"cmd": "fill_login", "session": sid, "bound": b["bound"], "host": b["host"], "user": "me@x.example",
                    "password": PW, **kw})


def test_signing_in(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    assert b["ok"] and b["host"] == "club.example" and PW not in json.dumps(b)
    eng.on_click[("main", 3)] = f"{SITE}/"
    r = fill(c, sid, b)
    assert r == {"ok": True, "session": sid, "pressed": True}
    assert eng.sets == [("main", 1, "me@x.example"), ("main", 2, PW)] and eng.keeps == [(1, False), (2, True)]
    assert eng.clicks == [("main", 3)]
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    said = json.dumps(snap, ensure_ascii=False)
    for form in (PW, urllib.parse.quote(PW), "s3cret"):
        assert form not in said
    assert "•••" in snap["text"] and "•••" in snap["url"] and "•••" in snap["title"]
    c.handle({"cmd": "close", "session": sid})
    assert c.retire  # the engine goes too


def test_only_the_sign_in_service_may_and_only_that(env):
    c, _, sid, refs = env
    for cmd in ("bind_login", "fill_login"):
        assert c.handle({"cmd": cmd, "session": sid, "user": refs["Email"]}) == {"ok": False, "error": "unknown command"}
    for cmd in ("snapshot", "open", "click", "type", "status", "fill", "pay", "close", "bind"):
        assert si(c, cmd=cmd, session=sid, url=f"{SITE}/", ref=refs["Log in"], text="x")["error"] == "unknown command"


def test_two_pages_user_name_first(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs, password=None)
    r = si(c, cmd="fill_login", session=sid, bound=b["bound"], host=b["host"], user="me")
    assert r["ok"] and eng.sets == [("main", 1, "me")]
    c.handle({"cmd": "close", "session": sid})
    assert c.retire


@pytest.mark.parametrize("kw,why", [
    ({"password": "Email"}, "password field"),
    ({"user": "Password"}, "user name"),
    ({"user": "Your comment"}, "doesn't look like"),
    ({"user": "Search", "password": None, "press": None}, "doesn't look like"),
    ({"press": "Other"}, "same form"),
    ({"press": "Email"}, "button"),
    ({"password": "pw in a frame"}, "frame"),
    ({"user": None, "password": None}, "give the user field"),
])
def test_wrong_fields(env, kw, why):
    c, eng, sid, refs = env
    r = bind(c, sid, refs, **kw)
    assert not r["ok"] and why in r["error"] and not eng.sets


@pytest.mark.parametrize("facts,why", [
    ({"passwords": 2}, "several password"), ({"passwords": 0}, "several"), ({"method": "get"}, "address"),
    ({"pressMethod": "get"}, "address"), ({"action": "https://evil.example/login"}, "another site"),
    ({"action": "http://www.club.example/login"}, "another site"), ({"pressAction": "https://evil.example/"}, "another site"),
    ({"action": "unreadable"}, "another site"), ({"pressTag": "a"}, "link"), ({"shown": False}, "visible"),
    ({"sameForm": False}, "same form"), ({"connected": False}, ""),
])
def test_forms_that_are_not_for_this(env, facts, why):
    c, eng, sid, refs = env
    eng.facts = facts
    r = bind(c, sid, refs)
    assert not r["ok"] and why in r["error"] and not eng.sets


def test_a_form_that_names_no_method_or_action_is_the_pages_own(env):
    c, eng, sid, refs = env
    eng.facts = {"method": "", "action": "", "hasForm": False}
    assert bind(c, sid, refs)["ok"]


def test_setting_a_new_password_is_the_owners(env):
    c, eng, sid, refs = env
    eng.frames[0].els[2]["autocomplete"] = "section-x New-Password"
    assert "new password" in bind(c, sid, refs)["error"]


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


@pytest.mark.parametrize("change", ["click", "element", "field", "url", "host", "facts", "reload", "ref", "session"])
def test_nothing_is_filled_when_something_changed(env, change):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    kw = {}
    if change == "click":
        c.handle({"cmd": "click", "session": sid, "ref": refs["Other"]})
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
    elif change == "reload":
        eng.frames[0].loader = "new"
    elif change == "ref":
        b["bound"]["elements"]["password"]["ref"] = refs["Email"]
    elif change == "session":
        sid = "0" * 16
    r = fill(c, sid, b, **kw)
    assert not r["ok"] and not eng.sets and PW not in json.dumps(r)


@pytest.mark.parametrize("kw", [{"password": None}, {"password": ""}, {"password": "x" * 201}, {"password": "a\nb"}, {"user": 5},
                                {"bound": None}, {"bound": {"elements": []}}])
def test_bad_values(env, kw):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    assert not fill(c, sid, b, **kw)["ok"] and not eng.sets


def test_a_disabled_button_is_not_pressed_but_may_wake_up(env):
    c, eng, sid, refs = env
    eng.frames[0].els[3]["disabled"] = True
    b = bind(c, sid, refs)
    assert b["ok"]
    r = fill(c, sid, b)
    assert not r["ok"] and "not pressed" in r["error"] and not eng.clicks and len(eng.sets) == 2


def test_the_engine_dying_says_nothing_of_the_password(env):
    c, eng, sid, refs = env
    b = bind(c, sid, refs)
    eng.die_after_click = True
    r = fill(c, sid, b)
    assert not r["ok"] and PW not in json.dumps(r) and c.retire


def test_hidden_cuts_every_form_and_leaves_the_rest():
    out = co.hidden({"a": [f"x{PW}y", urllib.parse.quote_plus(PW)], "n": 5, "b": None}, [PW, "", "abc"])
    assert out == {"a": ["x•••y", "•••"], "n": 5, "b": None}
    assert co.hidden({"a": "abc abcabc"}, ["abc"]) == {"a": "abc abcabc"}  # too short to cut out safely
    assert co.hidden({"a": "x"}, []) == {"a": "x"}


def test_login_host():
    assert co.login_host("https://WWW.Club.example:443/x?y") == "club.example"
    for bad in ("http://club.example", "https://club.example:444/", "https://user@1.2.3.4/", "about:blank", "", "https://[::1]/", "https://a..b/",
                "https://club.example:x/"):
        assert co.login_host(bad) == ""


# --- the page-side script itself, in Node (skipped without it) ------------------------------------------


NODE = shutil.which("node")
DOM = r"""
class El { constructor(tag, a, form) { this.tagName = tag.toUpperCase(); this.a = a || {}; this.form = form || null; this.nodeType = 1;
  this.type = this.a.type || (tag === 'button' ? 'submit' : 'text'); this.value = this.a.value || ''; this.isConnected = true; this.id = this.a.id || ''; this.disabled = false;
  this.parentElement = null; this.events = []; }
  getAttribute(k) { return k in this.a ? this.a[k] : null; } hasAttribute(k) { return k in this.a; }
  closest(sel) { if (sel === 'form') return this.form; if (sel.startsWith('button')) return this; return null; }
  querySelectorAll(sel) { return sel.includes('[type=password]') ? all.filter(e => e.form === this && e.type === 'password') : []; }
  dispatchEvent(e) { this.events.push(e.type); } }
class HTMLFormElement extends El {} class HTMLInputElement extends El {} class HTMLTextAreaElement extends El {}
for (const C of [HTMLInputElement, HTMLTextAreaElement]) Object.defineProperty(C.prototype, 'value', { get() { return this._v; }, set(v) { this._v = v; }, configurable: true });
for (const n of ['FocusEvent', 'InputEvent', 'Event']) globalThis[n] = class { constructor(t) { this.type = t; } };
Object.assign(globalThis, { HTMLFormElement, HTMLInputElement, HTMLTextAreaElement, location: { href: 'https://club.example/login' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) });
const form = new HTMLFormElement('form', { method: 'POST', action: '/do' }), other = new HTMLFormElement('form', {});
const user = new HTMLInputElement('input', { name: 'email', type: 'email' }, form), pw = new HTMLInputElement('input', { name: 'p', type: 'password' }, form);
const go = new HTMLInputElement('button', { formaction: 'https://evil.example/x' }, form), alien = new HTMLInputElement('button', {}, other);
const all = [user, pw, go, alien];
globalThis.document = { baseURI: 'https://club.example/login', readyState: 'complete', title: '', body: null, querySelector: () => null, getElementById: () => null,
  querySelectorAll: (s) => s === 'a[href]' ? [] : s === '*' ? [] : s.includes('[type=password]') ? all.filter(e => e.type === 'password') : s.includes('checkbox') && !s.includes('textarea') ? [] : all };
globalThis.CSS = { escape: String };
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_page_script(tmp_path):
    script = DOM + co.PAGE_JS + r"""
const run = (op) => JSON.parse(JSON.stringify(globalThis.__ampi.run(op)));
const nums = run({ op: 'enumerate', main: true }).elements.map(e => e.n);
const r = { facts: run({ op: 'login', user: nums[0], secret: nums[1], press: nums[2] }), alien: run({ op: 'login', user: nums[0], secret: nums[1], press: nums[3] }),
  gone: run({ op: 'login', secret: 99 }) };
run({ op: 'set', n: nums[1], value: 'hunter22', keep: true });
pw.type = 'text'; // "show password"
r.form = run({ op: 'form' }).fields; r.value = pw.value;
console.log(JSON.stringify(r));
"""
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    r = json.loads(subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert r["facts"] == {"connected": True, "hasForm": True, "sameForm": True, "passwords": 1, "shown": True, "method": "post",
                          "action": "https://club.example/do", "pressTag": "button", "pressMethod": "",
                          "pressAction": "https://evil.example/x", "href": "https://club.example/login"}
    assert r["alien"]["sameForm"] is False and r["gone"] == {"connected": False}
    assert r["value"] == "hunter22"
    assert [x["value"] for x in r["form"] if x["name"] == "p"] == ["(secret)"]
