"""One-time codes after a sign-in: the controller's rules (only where the sign-in itself led), the page script in a real browser, the
sign-in service between page and phone, and Ampi's side."""

import html
import json
import subprocess

import pytest
from test_checkout_controller import Frame, el
from test_signin import Phone, Server, caller, certs, confs, fast  # noqa: F401 (fixtures)
from test_signin_fill import EDGE, HOSTS, ME, PW, SITE, Asked, Engine, bind, fill, go, run_op, si, svc
from test_signin_fill import env as login_env  # noqa: F401

from ampi_phone import signin
from ampi_tools import checkout as co
from ampi_tools import client

CODE = "482913"
CODE_FACTS = {"connected": True, "hasForm": True, "count": 1, "max": 6, "type": "text", "extra": False, "boxes": 1, "shown": True,
              "method": "post", "action": f"{SITE}/verify", "boxed": False}


class CodeEngine(Engine):
    """Also knows the two page operations for codes."""
    code_facts: dict = {}
    words = "Two-step verification. Enter the code we sent to ••12"
    auto = None  # the page goes there with the last character

    def op(self, f, a):
        if a["op"] in ("code", "setcode"):
            e = f.els.get(a["n"])
            if not (e and e["connected"]):
                return {"connected": False, "ok": False, "set": False}
            p = f.els.get(a.get("press"))
            facts = {**CODE_FACTS, "press": "" if p is None else "form" if p.get("form") == e.get("form") is not None else "", **self.code_facts}
            if a["op"] == "code":
                return {**facts, "words": self.words}
            if a["i"] == 0 and {k: facts.get(k) for k in a["expect"]} != a["expect"]:
                return {"ok": False, "set": False}
            if e.get("vanish") == a["i"]:
                return {"ok": False, "set": a["i"] > 0}
            e["value"] += a["value"]
            self.sets.append((f.id, a["n"], a["value"]))
            if self.auto and a["i"] == facts["count"] - 1:
                self.navigate(self.auto)
            return {"ok": True}
        return super().op(f, a)


def code_page(url=f"{SITE}/verify"):
    return [Frame("main", url, title="Verification", text="Enter the code", elements=[
        el("input", autocomplete="one-time-code", name="otp", label="Code", form=1),   # 1
        el("button", label="Verify", form=1),                                           # 2
        el("link", label="Try another way"),                                            # 3
        el("button", label="Send a new code", form=2),                                  # 4
        el("button", label="Save", form=3),                                             # 5
        el("input", name="nickname", label="Nickname", form=3),                         # 6
        el("button", label="Accept all cookies"),                                       # 7
        el("checkbox", label="Remember this device", form=1)])]                         # 8


@pytest.fixture
def env(login_env):  # noqa: F811
    c, eng, sid, refs = login_env
    eng.__class__ = CodeEngine
    eng.auto, eng.code_facts = None, {}
    page = code_page()
    page[0].loader = "L-verify"
    eng.pages[f"{SITE}/verify"] = lambda: page
    eng.pages[f"{SITE}/in"] = lambda: [Frame("main", f"{SITE}/in", text="Welcome")]
    eng.on_click[("main", 3)] = f"{SITE}/verify"
    return c, eng, sid, refs


def signed_in(env):
    """After the login: the page that asks for a code, with its refs."""
    c, eng, sid, refs = env
    r = fill(c, sid, bind(c, sid, refs))
    assert r["ok"] and r["moved"]
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    return {e["label"]: e["ref"] for e in snap["elements"]}


def bind_code(c, sid, r, press="Verify", code="Code"):
    return si(c, cmd="bind_code", session=sid, code=r[code], **({"press": r[press]} if press else {}))


def fill_code(c, sid, b, code=CODE, **kw):
    return si(c, cmd="fill_code", session=sid, bound=b["bound"], host=b["host"], code=code, **kw)


def submit(c, sid, ref, **kw):
    b = c.handle({"cmd": "bind_submit", "session": sid, "ref": ref})
    assert b["ok"], b
    return c.handle({"cmd": "submit", "session": sid, **{k: b[k] for k in ("origin", "url", "button", "digest", "gen", "press", "seen", "frames")},
                     "expect_totals": b["totals"], **{k: b[k] for k in ("box", "checked") if k in b}, **kw})


# --- the controller ---------------------------------------------------------------------------------------


def test_a_code_after_signing_in(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    b = bind_code(c, sid, r)
    assert b["ok"] and b["host"] == "club.example" and b["targets"] == ["club.example"] and b["visited"] == ["club.example"]
    eng.on_click[("main", 2)] = f"{SITE}/in"
    sets = len(eng.sets)
    assert fill_code(c, sid, b) == {"ok": True, "session": sid, "pressed": True, "moved": True}
    assert eng.sets[sets:] == [("main", 1, CODE)] and eng.clicks[-1] == ("main", 2)
    assert "Welcome" in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["text"]


def test_only_after_signing_in(env):
    c, eng, sid, refs = env
    eng.navigate(f"{SITE}/verify")
    r = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    b = bind_code(c, sid, r)
    assert not b["ok"] and "right after" in b["error"]
    for cmd in ("bind_code", "fill_code"):  # and only the sign-in service may
        assert c.handle({"cmd": cmd, "session": sid, "code": r["Code"]})["error"] == "unknown command"


@pytest.mark.parametrize("what", ["click", "type", "back", "save", "box", "cookies in a form"])
def test_anything_else_in_between_ends_it(env, what):
    c, eng, sid, _ = env
    r = signed_in(env)
    if what == "click":
        c.handle({"cmd": "click", "session": sid, "ref": r["Try another way"]})
    elif what == "type":
        c.handle({"cmd": "type", "session": sid, "ref": r["Nickname"], "text": "x"})
    elif what == "back":
        c.handle({"cmd": "back", "session": sid})
    elif what == "save":
        assert submit(c, sid, r["Save"])["ok"]
    elif what == "box":
        assert submit(c, sid, r["Remember this device"])["ok"]
    else:
        eng.press = {"dialog": "cookies", "submits": True}
        c.handle({"cmd": "click", "session": sid, "ref": r["Accept all cookies"]})
        eng.press = {}
    b = bind_code(c, sid, r)
    assert not b["ok"] and "something else was done" in b["error"]


def test_steps_of_a_sign_in_do_not(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.press = {"dialog": "cookies", "submits": False}
    c.handle({"cmd": "click", "session": sid, "ref": r["Accept all cookies"]})
    eng.press = {}
    for k in ("Try another way", "Send a new code", "Send a new code"):
        assert bind_code(c, sid, r)["ok"]
        assert submit(c, sid, r[k])["ok"]
    assert bind_code(c, sid, r)["ok"]
    assert submit(c, sid, r["Send a new code"])["ok"]  # one too many
    assert not bind_code(c, sid, r)["ok"]


def test_the_sign_in_button_the_owner_approves(env):
    c, eng, sid, refs = env
    eng.frames[0].els[3]["label"] = "Enter the club"  # not a caption that is pressed unasked
    r = fill(c, sid, bind(c, sid, refs, press=None))
    assert r["ok"] and not r["pressed"]
    assert submit(c, sid, refs["Log in"])["ok"]
    r = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert bind_code(c, sid, r)["ok"]


def test_another_button_with_any_words_is_not_a_step(env):
    c, eng, sid, refs = env
    assert fill(c, sid, bind(c, sid, refs, press=None))["ok"]
    assert submit(c, sid, refs["Next"])["ok"]  # a step's caption, of another form: fine
    assert c.session.chain == c.session.gen
    eng.frames[0].els[9]["label"] = "Transfer"
    assert submit(c, sid, refs["Next"])["ok"]
    assert c.session.chain != c.session.gen


def test_what_to_pay_ends_it(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].text = "Σύνολο: 12,00 €"  # counts for the gate
    submit(c, sid, r["Send a new code"])
    assert c.session.chain != c.session.gen


@pytest.mark.parametrize("url", [f"{SITE}/login?next=/security/phone", f"{SITE}/login?x=https%253A%252F%252Fevil.example", f"{SITE}/settings/phone",
                                 f"{SITE}/transfer", f"{SITE}/login#a?next=/x", f"{SITE}/account/deletePhone"])
def test_an_address_that_leads_elsewhere(env, url):
    c, eng, sid, refs = env
    eng.pages[url] = eng.pages[f"{SITE}/login"]
    c.handle({"cmd": "open", "session": sid, "url": url})
    eng.navigate(f"{SITE}/login")
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert fill(c, sid, bind(c, sid, refs))["ok"]  # signing in works
    assert c.session.chain is None


@pytest.mark.parametrize("url,ok", [(f"{SITE}/login?next=%2Faccount", True), (f"{SITE}/login?next=%2Fsettings%2Fsecurity", False),
                                    (f"{SITE}/login?a=1#/x", True), (f"{SITE}/payment/login", False)])
def test_where_the_sites_own_sign_in_page_leads(env, url, ok):
    c, eng, sid, refs = env
    eng.frames[0].url = url
    assert fill(c, sid, bind(c, sid, refs))["ok"]
    assert (c.session.chain is not None) is ok


def test_not_for_long(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    c.clock.sleep(co.CODE_WINDOW + 1) if hasattr(c.clock, "sleep") else None
    c.session.last = c.clock()  # (the session itself lives on)
    assert "too long ago" in bind_code(c, sid, r)["error"]


@pytest.mark.parametrize("facts,why", [
    ({"extra": True}, "more than a code"), ({"boxes": 3}, "more than a code"), ({"boxes": None}, "more than a code"), ({"count": None}, "more than a code"),
    ({"boxed": True}, "sandboxed"), ({"boxed": None}, "sandboxed"), ({"shown": False}, "visible"), ({"method": "get"}, "address"),
    ({"action": "http://www.club.example/x"}, "https site"), ({"connected": False}, ""), ({"press": ""}, "button of that form")])
def test_forms_that_are_not_for_this(env, facts, why):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = facts
    b = bind_code(c, sid, r)
    assert not b["ok"] and why in b["error"]


@pytest.mark.parametrize("words", ["Confirm your payment of", "Enter the code to change your phone number", "New email address", "Transfer to", "Delete account?",
                                   "Verify your purchase", "3-D Secure", "Reset your password", "Vahvista maksu", "Подтвердите платёж", "Pay €5", "Total 12 EUR"])
def test_codes_for_something_else(env, words):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.words = words
    assert not bind_code(c, sid, r)["ok"]


@pytest.mark.parametrize("words", ["Two-factor authentication", "Verify your identity", "Enter the 6-digit code from your authenticator app",
                                   "We sent a code to •••@x.example. In order to continue, enter it", "Vahvistuskoodi", "Код подтверждения"])
def test_codes_for_signing_in(env, words):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.words = words
    assert bind_code(c, sid, r)["ok"]


@pytest.mark.parametrize("what", ["captcha", "total", "psp", "3ds", "url", "frame", "field", "disabled", "link", "caption"])
def test_pages_it_is_not_for(env, what):
    c, eng, sid, _ = env
    r, kw = signed_in(env), {}
    top = eng.frames[0]
    if what == "captcha":
        eng.frames.append(Frame("cap", "https://newassets.hcaptcha.com/captcha/v1/x#frame=challenge", parent="main"))
    elif what == "total":
        top.text = "Order total: 240,00 €"
    elif what == "psp":
        eng.frames.append(Frame("p", "https://js.stripe.com/v3/x", parent="main"))
    elif what == "3ds":
        eng.frames.append(Frame("p", "https://acs.bank.example/x", parent="main"))
    elif what == "url":
        top.url = f"{SITE}/payments/verify"
    elif what == "frame":
        eng.frames.append(Frame("g", "https://www.club.example/f", parent="main", elements=[el("input", name="otp", label="Inner")]))
        snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
        r["Inner"] = next(e["ref"] for e in snap["elements"] if e["label"] == "Inner")
        kw = {"code": "Inner", "press": None}
    elif what == "field":
        kw = {"code": "Nickname", "press": None}
    elif what == "disabled":
        top.els[1]["disabled"] = True
    elif what == "link":
        kw = {"press": "Try another way"}
    else:
        top.els[2]["label"] = "Pay now"
    b = bind_code(c, sid, r, **kw)
    assert not b["ok"] and not any(s[2] == CODE for s in eng.sets)


def test_three_tries_in_one_place(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    for _ in range(2):
        b = bind_code(c, sid, r, press=None)
        assert fill_code(c, sid, b)["ok"]
        eng.frames[0].els[1]["value"] = ""
    eng.frames[0].els[1]["name"] = "other"
    assert "first code went" in bind_code(c, sid, r, press=None)["error"]
    eng.frames[0].els[1]["name"] = "otp"
    assert fill_code(c, sid, bind_code(c, sid, r, press=None))["ok"]
    assert "three codes" in bind_code(c, sid, r, press=None)["error"]


def test_the_codes_own_button_approved_is_a_step(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    assert fill_code(c, sid, bind_code(c, sid, r, press=None))["ok"]
    assert submit(c, sid, r["Verify"])["ok"]
    assert bind_code(c, sid, r)["ok"]  # it was wrong: another try


def test_a_page_that_sends_it_by_itself(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = {"count": 6}
    eng.auto = f"{SITE}/in"
    clicks = len(eng.clicks)
    assert fill_code(c, sid, bind_code(c, sid, r)) == {"ok": True, "session": sid, "pressed": False, "moved": True}
    assert [x[2] for x in eng.sets[-6:]] == list(CODE) and len(eng.clicks) == clicks


@pytest.mark.parametrize("code,facts,put", [("12-34-56", {}, "123456"), ("1234567", {}, None), ("12345", {"count": 6}, None), ("12-3456", {"count": 6}, "123456"),
                                            ("G-123456", {"max": 0}, "G-123456"), ("abcdef", {"max": 0}, "abcdef")])
def test_codes_that_fit(env, code, facts, put):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts, before = facts, len(eng.sets)
    out = fill_code(c, sid, bind_code(c, sid, r, press=None), code=code)
    assert out["ok"] is (put is not None) and "".join(x[2] for x in eng.sets[before:]) == (put or "")
    if put is None:
        assert out["phase"] == "verify" and bind_code(c, sid, r, press=None)["ok"]


@pytest.mark.parametrize("code", ["", "12", "a b c d", "1" * 17, "123456\n", "-1234", 123456, None, "12;34"])
def test_odd_codes(env, code):
    c, eng, sid, _ = env
    r = signed_in(env)
    assert fill_code(c, sid, bind_code(c, sid, r), code=code)["error"] == "bad request"


@pytest.mark.parametrize("change", ["gen", "loader", "facts", "host", "press", "place", "words"])
def test_nothing_goes_in_when_something_changed(env, change):
    c, eng, sid, _ = env
    r = signed_in(env)
    b, kw = bind_code(c, sid, r), {}
    if change == "gen":
        c.session.gen += 1
    elif change == "loader":
        eng.frames[0].loader = "L-x"
    elif change == "facts":
        eng.code_facts = {"max": 8}
    elif change == "host":
        kw = {"host": "evil.example"}
    elif change == "press":
        eng.frames[0].els[2]["label"] = "Confirm"
    elif change == "place":
        eng.frames[0].url = f"{SITE}/other"
    else:
        eng.words = "Confirm your payment"
    out = fill_code(c, sid, {**b, **kw})
    assert not out["ok"] and out["phase"] == "verify" and not any(x[2] == CODE for x in eng.sets)


def test_the_page_changing_in_the_middle(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = {"count": 6}
    b = bind_code(c, sid, r)
    eng.frames[0].els[1]["vanish"] = 3
    out = fill_code(c, sid, b)
    assert not out["ok"] and out["phase"] == "sent" and CODE not in json.dumps(out)


def test_the_page_changed_just_before(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    b = bind_code(c, sid, r)
    eng.frames[0].els[1]["vanish"] = 0
    out = fill_code(c, sid, b)
    assert not out["ok"] and out["phase"] == "verify" and c.session.codes == 0 and c.session.code is None
    del eng.frames[0].els[1]["vanish"]
    assert bind_code(c, sid, r)["ok"]


def test_the_code_is_cut_out_until_the_next_page(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    assert fill_code(c, sid, bind_code(c, sid, r, press=None), code="2026")["ok"]
    eng.frames[0].text = "Wrong code 2026! Not 20261 or 1.2026"
    text = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["text"]
    assert text == "Wrong code •••! Not 20261 or 1.2026"
    eng.navigate(f"{SITE}/in")
    eng.frames[0].text = "Classes in 2026"
    assert c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["text"] == "Classes in 2026"


def test_password_and_code_in_one_form(env):
    c, eng, sid, refs = env
    eng.describe = lambda n, e, real=eng.describe: {**real(n, e), "max": e.get("max", 0)}
    eng.frames[0].els[10] = {**el("input", name="totp", label="Authentication code", form=1), "max": 6}
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    real = eng.login_facts

    def facts(f, a):
        out = real(f, a)
        return {**out, "extra": False} if a.get("code") else out
    eng.login_facts = facts
    assert "other fields" in bind(c, sid, refs)["error"]
    b = si(c, cmd="bind_login", session=sid, user=refs["Email"], password=refs["Password"], code=refs["Authentication code"], press=refs["Log in"])
    assert b["ok"] and set(b["bound"]["elements"]) == {"user", "password", "code", "press"}
    for bad in ({}, {"code": "1 2"}, {"code": "1234567"}):
        assert not fill(c, sid, b, **bad)["ok"] and not eng.sets
    r = fill(c, sid, b, code=CODE)
    assert r["ok"] and c.session.code[0] == CODE
    assert not si(c, cmd="bind_login", session=sid, user=refs["Email"], code=refs["Authentication code"])["ok"]


@pytest.mark.parametrize("kw", [{"name": "otp"}, {"name": "code"}, {"id": "verificationCode"}, {"name": "mfaCode"}, {"name": "token"}, {"autocomplete": "one-time-code"},
                                {"label": "Enter the code"}, {"label": "Digit 1", "max": 1}, {"label": "Koodi"}])
@pytest.mark.parametrize("how", ["type", "fill", "option"])
def test_ampi_never_types_a_code(env, kw, how):
    c, eng, sid, refs = env
    eng.describe = lambda n, e, real=eng.describe: {**real(n, e), "max": e.get("max", 0)}
    eng.frames[0].els[10] = el("input", **kw)
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    ref = next(e["ref"] for e in snap["elements"] if e["ref"].endswith(":10"))
    if how == "type":
        r = c.handle({"cmd": "type", "session": sid, "ref": ref, "text": CODE})
    elif how == "fill":
        b = c.handle({"cmd": "bind", "session": sid, "refs": [ref]})
        r = c.handle({"cmd": "fill", "session": sid, "origin": b["origin"], "fields": [{"ref": ref, "binding": b["bindings"][0], "value": CODE}]})
    else:
        c.session.signed_in = True
        r = c.handle({"cmd": "bind_submit", "session": sid, "ref": ref, "option": CODE})
    assert not r["ok"] and "code" in r["error"] and not eng.sets


@pytest.mark.parametrize("kw", [{"name": "promo_code"}, {"label": "Postal code"}, {"label": "Gift code"}, {"label": "Booking code"}, {"label": "Confirmation code"},
                                {"label": "Middle initial", "max": 1}, {"name": "comment"}, {"label": "Pinterest"}, {"label": "Country code"}])
def test_other_fields_are_as_before(env, kw):
    c, eng, sid, _ = env
    eng.describe = lambda n, e, real=eng.describe: {**real(n, e), "max": e.get("max", 0)}
    eng.frames[0].els[10] = el("input", **kw)
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    ref = next(e["ref"] for e in snap["elements"] if e["ref"].endswith(":10"))
    assert c.handle({"cmd": "type", "session": sid, "ref": ref, "text": "AB12"})["ok"]


@pytest.mark.parametrize("text,ok", [
    ("Send code", True), ("Resend", True), ("Text me", True), ("Email me a code", True), ("Try another way", True), ("Use authenticator app", True),
    ("Continue", True), ("Get a new verification code", True), ("Use a backup code", True), ("Use another method", True), ("Send a text message", True),
    ("Send", False), ("Request", False), ("Send money", False), ("Delete account", False), ("Confirm", False), ("Change number", False), ("Yes", False),
    ("Add phone number", False), ("Disable two-factor", False), ("Send code and pay", False), ("Continue to payment", False)])
def test_captions_of_steps(text, ok):
    assert bool(co.CODE_STEP.fullmatch(co.login_caption(text))) is ok


# --- the page script in a browser -------------------------------------------------------------------------


PAGE = """<!doctype html><meta charset=utf-8><title>Sign in</title><h1>Two-step verification</h1><h2 style="display:none">Pay now</h2>
<form id=one method=post action="/v"><p>Enter the code. <a href=/x>Change phone number</a></p><input name=otp maxlength=6 autocomplete=one-time-code>
 <input type=checkbox name=trust><input type=hidden name=t value=1><button id=b1>Verify</button><button type=button id=again>Send a new code</button></form>
<form id=six method=post><input class=d maxlength=1 name=d1><input class=d maxlength=1 name=d2><input class=d maxlength=1 name=d3><input class=d maxlength=1 name=d4>
 <input class=d maxlength=1 name=d5><input class=d maxlength=1 name=d6><input name=all style="display:none"><button id=b6>Confirm</button></form>
<div id=free><div><input type=tel inputmode=numeric name=f1 pattern="\\d{1}"><input type=tel name=f2 pattern="[0-9]"><input type=tel name=f3 maxlength=1><input type=tel name=f4 maxlength=1></div>
 <button id=bf>Continue</button></div>
<form id=more method=post><input name=m1><input name=phone><button>Save</button></form>
<form id=pw method=post><input name=p1><input type=password name=p2><button>Go</button></form>
<form id=ro method=post><input name=who readonly value=me><input name=r1><button>Go</button></form>
<form id=way method=post><input name=w1><input type=radio name=via value=sms><button>Go</button></form>
<form id=lg method=post><input type=email name=e><input type=password name=s><input name=totp maxlength=6><button id=bl>Log in</button></form>
<pre id=out></pre>"""
SCRIPT = r"""
const run = (op) => JSON.parse(JSON.stringify(globalThis.__ampi.run(op)));
const q = (s) => document.querySelector(s), out = {}, N = {}, I = {};
const all = run({ op: 'enumerate', main: true }).elements;
for (const e of all) { if (e.name) N[e.name] = e; if (e.id) I[e.id] = e; }
const facts = (n, p) => run({ op: 'code', n: N[n].n, press: p ? I[p].n : null });
out.max = [N.otp.max, N.who.max, N.d1.max];
out.one = facts('otp', 'b1'); out.other = facts('otp', 'b6').press; out.again = facts('otp', 'again').press;
out.six = facts('d1', 'b6'); out.second = facts('d2').count; out.free = facts('f1', 'bf'); out.wrongBox = facts('f1', 'b1').press;
out.extra = ['m1', 'p1', 'r1', 'w1'].map(n => facts(n).extra);
const expect = (f) => Object.fromEntries(['hasForm','count','max','type','extra','boxes','shown','method','action','boxed','press'].map(k => [k, f[k]]));
out.stale = run({ op: 'setcode', n: N.otp.n, i: 0, value: 'x', press: I.b1.n, expect: { ...expect(out.one), max: 8 } });
out.set = run({ op: 'setcode', n: N.otp.n, i: 0, value: '482913', press: I.b1.n, expect: expect(out.one) }); out.value = q('[name=otp]').value;
const seen = []; for (const d of document.querySelectorAll('.d')) d.addEventListener('input', (e) => seen.push(e.target.name + e.target.value));
out.boxes = [...'482913'].map((x, i) => run({ op: 'setcode', n: N.d1.n, i: i, value: x, press: I.b6.n, expect: expect(out.six) }).ok); out.seen = seen;
out.past = run({ op: 'setcode', n: N.d1.n, i: 6, value: 'x', press: I.b6.n, expect: {} });
q('[name=d4]').remove(); out.gone = run({ op: 'setcode', n: N.d1.n, i: 3, value: 'x', press: I.b6.n, expect: {} });
const press = (id, n) => { const g = run({ op: 'gate', n: I[id].n, with: N[n].n }); return Object.fromEntries(['tag','texts','to','submits','ident'].map(k => [k, g[k]])); };
q('#one').addEventListener('submit', (e) => { e.preventDefault(); out.sent = q('[name=otp]').value; });
out.clicked = run({ op: 'clickif', n: I.b1.n, expect: press('b1', 'otp'), filled: N.otp.n });
q('[name=otp]').value = ''; out.empty = run({ op: 'clickif', n: I.b1.n, expect: press('b1', 'otp'), filled: N.otp.n });
out.form = run({ op: 'form' }).fields.filter(f => /^d/.test(f.name)).map(f => f.value);
const lg = (c) => run({ op: 'login', user: N.e.n, secret: N.s.n, code: c ? N.totp.n : null });
out.lg = [lg(false).extra, lg(true).extra, lg(true).sameForm];
const le = Object.fromEntries(['hasForm','sameForm','passwords','extra','boxes','shown','method','action','boxed'].map(k => [k, lg(true)[k]]));
out.login = run({ op: 'signin', user: N.e.n, secret: N.s.n, code: N.totp.n, values: { user: 'me', password: 'hunter22', code: '482913' }, expect: le });
out.values = ['e', 's', 'totp'].map(n => q('[name=' + n + ']').value);
q('#out').textContent = 'RESULT' + JSON.stringify(out) + 'END';
"""


@pytest.mark.skipif(EDGE is None, reason="needs Edge or Chromium")
def test_the_page_script_in_a_browser(tmp_path):
    f = tmp_path / "t.html"
    f.write_text(PAGE + "<script>" + co.PAGE_JS + "</script><script>try{" + SCRIPT + "}catch(e){document.querySelector('#out').textContent='RESULT'+"
                 "JSON.stringify({error:String(e&&e.stack||e)})+'END'}</script>", encoding="utf-8")
    r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom",
                        f.as_uri()], capture_output=True, timeout=90)
    dom = r.stdout.decode("utf-8", "replace")
    if not dom.strip():
        pytest.skip(f"{EDGE} gave no page: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    out = json.loads(html.unescape(dom[dom.index("RESULT") + 6:dom.index("END", dom.index("RESULT"))]))
    assert "error" not in out, out["error"]
    assert out["max"] == [6, 0, 1]
    one = out["one"]
    assert {k: one[k] for k in ("hasForm", "count", "max", "extra", "boxes", "shown", "method", "press", "boxed")} == {
        "hasForm": True, "count": 1, "max": 6, "extra": False, "boxes": 1, "shown": True, "method": "post", "press": "form", "boxed": False}
    assert "Two-step verification" in one["words"] and "Enter the code." in one["words"]
    assert "Change phone" not in one["words"] and "Pay now" not in one["words"] and not co.CODE_DANGER.search(one["words"])
    assert out["other"] == "" and out["again"] == ""  # another form's button, one that doesn't send
    assert (out["six"]["count"], out["six"]["extra"], out["six"]["press"], out["second"]) == (6, False, "form", 1)
    assert (out["free"]["count"], out["free"]["hasForm"], out["free"]["press"], out["wrongBox"]) == (4, False, "box", "")
    assert out["extra"] == [True, True, False, True]
    assert out["stale"] == {"ok": False, "set": False} and out["set"] == {"ok": True} and out["value"] == CODE
    assert out["boxes"] == [True] * 6 and out["seen"] == [f"d{i + 1}{x}" for i, x in enumerate(CODE)]
    assert out["past"]["ok"] is False and out["gone"] == {"ok": False, "set": True}
    assert out["clicked"] == {"ok": True} and out["sent"] == CODE and out["empty"] == {"ok": False}
    assert set(out["form"]) == {"(secret)"}  # what went into them is never shown
    assert out["lg"] == [True, False, True] and out["login"] == {"ok": True} and out["values"] == ["me", "hunter22", CODE]


# --- the sign-in service ----------------------------------------------------------------------------------


GIVES = ("ok", signin.Login("", "", frozenset(HOSTS), CODE))


def after_login(env):
    """(service, its phone, the code page's refs)"""
    c, eng, sid, refs = env
    s, phone = svc(c)
    assert go(s, sid, refs)["ok"]
    phone.reply = GIVES
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    return s, phone, {e["label"]: e["ref"] for e in snap["elements"]}


def ask_code(s, sid, r, **kw):
    return s.handle({"cmd": "code", "session": sid, "code": r["Code"], "press": r["Verify"], **kw}, None)


def test_a_code_from_end_to_end(env):
    c, eng, sid, _ = env
    s, phone, r = after_login(env)
    eng.on_click[("main", 2)] = f"{SITE}/in"
    assert ask_code(s, sid, r) == {"ok": True, "code": "ok", "pressed": True, "moved": True}
    assert phone.calls == [("club.example", "fill"), ("club.example", "code")]
    assert s.held is None and s.over(0) and CODE not in json.dumps(s.seen[-1]["bound"])


@pytest.mark.parametrize("reply", [("denied", None), ("no_signin", None), ("ok", None), ("expired", None)])
def test_no_code_from_the_phone(env, reply):
    c, eng, sid, _ = env
    s, phone, r = after_login(env)
    phone.reply = reply
    out = ask_code(s, sid, r)
    assert out == {"ok": False, "code": reply[0] if reply[0] != "ok" else "failed"} and not any(x[2] == CODE for x in eng.sets)


def test_a_page_that_is_not_for_it_never_reaches_the_phone(env):
    c, eng, sid, _ = env
    s, phone, r = after_login(env)
    eng.words = "Confirm the payment"
    out = ask_code(s, sid, r)
    assert out["code"] == "page" and "something else than a sign-in" in out["error"] and len(phone.calls) == 1


@pytest.mark.parametrize("hosts,code", [(["other.example"], "other_sites"), (["other.example", "club.example"], "ok")])
def test_the_sites_of_the_login_count_again(env, hosts, code):
    c, eng, sid, _ = env
    s, phone, r = after_login(env)
    phone.reply = ("ok", signin.Login("", "", frozenset(hosts), CODE))
    assert ask_code(s, sid, r)["code"] == code


def test_a_form_that_sends_the_code_elsewhere(env):
    c, eng, sid, _ = env
    s, phone, r = after_login(env)
    eng.code_facts = {"action": "https://evil.example/"}
    assert ask_code(s, sid, r)["code"] == "other_target" and not any(x[2] == CODE for x in eng.sets)


@pytest.mark.parametrize("host", ["accounts.google.com", "github.com", "mybank.example", "web.telegram.org"])
def test_sites_no_login_is_used_on(env, host):
    c, eng, sid, refs = env
    eng.frames[0].url, eng.frames[0].origin = f"https://{host}/login", f"https://{host}"
    s, phone = svc(c, Asked(("ok", signin.Login(ME, PW, frozenset([host])))))
    assert go(s, sid, refs) == {"ok": False, "code": "refused_site"} and not phone.calls
    assert signin.refused(host)


def test_a_form_that_sends_the_login_to_one_of_them(env):
    c, eng, sid, refs = env
    eng.facts = {"action": "https://github.com/session"}
    s, _ = svc(c, Asked(("ok", signin.Login(ME, PW, frozenset(["club.example", "github.com"])))))
    assert go(s, sid, refs)["code"] == "other_target" and not eng.sets


def both(env):
    c, eng, sid, refs = env
    eng.frames[0].els[10] = {**el("input", name="totp", label="Authentication code", form=1), "max": 6}
    real = eng.login_facts
    eng.login_facts = lambda f, a: {**real(f, a), **({"extra": False} if a.get("code") else {})}
    refs.update({e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]})
    return refs


def test_login_and_code_together(env):
    c, eng, sid, refs = env
    refs = both(env)
    s, phone = svc(c, Asked(("ok", signin.Login(ME, PW, frozenset(HOSTS), CODE))))
    assert go(s, sid, refs, code=refs["Authentication code"])["ok"]
    assert phone.calls == [("club.example", "fill+code")] and s.seen[-1]["code"] == CODE
    assert s.held is None and s.over(0)


def test_a_held_login_asks_for_the_code_alone(env):
    c, eng, sid, refs = env
    refs = both(env)
    s, phone = svc(c, Asked(GIVES))
    s.held = signin.Held(sid, signin.Login(ME, PW, frozenset(HOSTS)), signin.time.monotonic() + 100)
    assert go(s, sid, refs, code=refs["Authentication code"])["ok"] and phone.calls == [("club.example", "code")]
    assert s.held is None


@pytest.mark.parametrize("req", [{}, {"code": "f0:1"}, {"session": "x", "code": "f0:1"}, {"session": "0" * 16}, {"session": "0" * 16, "code": "f0:x"},
                                 {"session": "0" * 16, "code": ["f0:1"]}, {"session": "0" * 16, "code": "f0:1", "press": 5}])
def test_bad_requests(env, req):
    s, phone = svc(env[0])
    assert s.handle({"cmd": "code", **req}, None) == {"ok": False, "code": "bad_request"} and not s.seen
    assert s.handle({"cmd": ["code"]}, None) == {"ok": False, "code": "bad_request"}


@pytest.mark.parametrize("purpose,answer,code", [
    ("code", {"ok": True, "code": CODE, "hosts": HOSTS}, "ok"), ("code", {"ok": True, "code": "1", "hosts": HOSTS}, "failed"),
    ("code", {"ok": True, "code": 482913, "hosts": HOSTS}, "failed"), ("code", {"ok": True, "code": CODE, "hosts": ["other.example"]}, "failed"),
    ("code", {"ok": True, "code": CODE}, "failed"), ("code", {"ok": False, "error": "no_signin"}, "no_signin"), ("code", {"ok": False, "error": "unsupported"}, "unsupported"),
    ("fill+code", {"ok": True, "user": ME, "secret": PW, "hosts": HOSTS, "code": CODE}, "ok"),
    ("fill+code", {"ok": True, "user": ME, "secret": PW, "hosts": HOSTS}, "unsupported"),  # an older app
    ("fill+code", {"ok": True, "hosts": HOSTS, "code": CODE}, "failed"),
])
def test_what_the_phone_is_asked_and_sends(confs, certs, purpose, answer, code):  # noqa: F811
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
        got, login = signin.ask("club.example", mine, confs[1], s.vault, purpose=purpose)
        asked = p.asked[0]
        assert asked["host"] == "club.example" and asked["wait"] == signin.CODE_WAIT and asked["op"] == ("code" if purpose == "code" else "get")
        assert purpose == "code" and "purpose" not in asked or asked.get("code") is True and asked["purpose"] == "fill"
        assert got == code and (login is not None) is (code == "ok")
        if code == "ok":
            assert login.code == CODE and login.hosts == frozenset(HOSTS) and CODE not in repr(login)


# --- Ampi's side ------------------------------------------------------------------------------------------


def run_code(env, tmp_path, origin="private", **args):
    import test_signin_fill as t

    real = t.validate
    t.validate = lambda ops: real([{"op": "signin.code", "args": ops[0]["args"]}])
    try:
        return run_op(env, tmp_path, origin, **args)
    finally:
        t.validate = real


def test_ampi_asks_for_the_code_and_never_sees_it(env, tmp_path):
    c, eng, sid, _ = env
    s, phone, r = after_login(env)
    eng.on_click[("main", 2)] = f"{SITE}/in"
    fake, out = run_code(env, tmp_path, service=s, code_ref=r["Code"], press_ref=r["Verify"])
    assert out["ok"] and out["text"].startswith("The code was filled in and its button pressed.") and "Welcome" in out["text"] and CODE not in out["text"]
    assert fake.calls == [{"cmd": "code", "session": sid, "code": r["Code"], "press": r["Verify"]}]


@pytest.mark.parametrize("origin", ["group:family", "forwarded:private", "unknown", "watcher:mail"])
def test_only_the_owners_chat(env, tmp_path, origin):
    fake, r = run_code(env, tmp_path, origin, code_ref="f0:1")
    assert isinstance(r, str) and not fake.calls


@pytest.mark.parametrize("answer,words", [({"ok": False, "code": "no_signin"}, ["no code was filled in", "signin fill"]),
                                          ({"ok": False, "code": "page", "error": "x", "phase": "sent"}, ["unclear", "code WAS sent"])])
def test_what_is_said(env, tmp_path, answer, words):
    class Odd:
        def handle(self, req, conn):
            return answer
    _, r = run_code(env, tmp_path, service=Odd(), code_ref="f0:1")
    text = r if isinstance(r, str) else r["error"]
    assert all(w in text for w in words)


def test_the_command():
    p = client.signin_parser()
    assert client.signin_ops(p.parse_args(["code", "--code", "f0:1"])) == [{"op": "signin.code", "args": {"code_ref": "f0:1"}}]
    assert client.signin_ops(p.parse_args(["fill", "--user", "f0:1", "--password", "f0:2", "--code", "f0:3"]))[0]["args"] == {
        "user_ref": "f0:1", "password_ref": "f0:2", "code_ref": "f0:3"}
    with pytest.raises(SystemExit):
        p.parse_args(["code"])
