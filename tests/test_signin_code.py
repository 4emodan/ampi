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
                return {**facts, "words": self.words, "heads": self.words}
            if a["i"] == 0 and {k: facts.get(k) for k in a["expect"]} != a["expect"]:
                return {"ok": False, "set": False}
            if a.get("look"):
                return {"ok": True, "full": sorted(e.get("boxes", {}))}
            if e.get("vanish") == a["i"]:
                return {"ok": False, "set": a["i"] > 0}
            if facts["count"] > 1:
                e.setdefault("boxes", {})[a["i"]] = a["value"]
                if not a["value"]:
                    del e["boxes"][a["i"]]
                self.posts += [x for x in ["".join(e["boxes"].get(i, "") for i in range(facts["count"]))] if len(x) == facts["count"]]
            e["value"] += a["value"]
            self.sets.append((f.id, a["n"], a["value"]))
            if self.auto and a["i"] == facts["count"] - 1 and a["value"]:
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
    eng.auto, eng.code_facts, eng.posts = None, {}, []
    real = eng.describe
    eng.describe = lambda n, e: {**real(n, e), "visible": not e.get("hidden")}
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
        assert c.handle({"cmd": "click", "session": sid, "ref": r["Try another way"]})["ok"]
    elif what == "type":
        eng.words = "Your profile"
        assert c.handle({"cmd": "type", "session": sid, "ref": r["Nickname"], "text": "x"})["ok"]
        eng.words = CodeEngine.words
    elif what == "back":
        assert c.handle({"cmd": "back", "session": sid})["ok"]
    elif what == "save":
        assert submit(c, sid, r["Save"])["ok"]
    elif what == "box":
        assert submit(c, sid, r["Remember this device"])["ok"]
    else:
        eng.press = {"dialog": "cookies", "submits": True}
        assert c.handle({"cmd": "click", "session": sid, "ref": r["Accept all cookies"]})["ok"]
        eng.press = {}
    b = bind_code(c, sid, r)
    assert not b["ok"] and "where a sign-in itself led" in b["error"]


def test_steps_of_a_sign_in_do_not(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.press = {"dialog": "cookies", "submits": False}
    c.handle({"cmd": "click", "session": sid, "ref": r["Accept all cookies"]})
    eng.press = {}
    for k in ("Try another way", "Send a new code", "Send a new code", "Send a new code"):
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


@pytest.mark.parametrize("url,ok", [(f"{SITE}/login?next=%2Faccount", True), (f"{SITE}/login?next=%2Fsettings%2Fsecurity", False),
                                    (f"{SITE}/login?a=1#/x", True), (f"{SITE}/payment/login", False), (f"{SITE}/login?to=security-settings", False),
                                    (f"{SITE}/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3Dweb%26scope%3Dopenid%2520profile%2520email%26state%3Dx", True),
                                    (f"{SITE}/auth/realms/x?client_id=web&response_type=code&scope=openid+profile+email&redirect_uri=https%3A%2F%2Fwww.club.example%2Fsignin-oidc", True),
                                    (f"{SITE}/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%3Fredirect_uri%3D%2Fwallet%2Fsend", False)])
def test_where_the_sites_own_sign_in_page_leads(env, url, ok):
    c, eng, sid, refs = env
    eng.frames[0].url = url
    assert fill(c, sid, bind(c, sid, refs))["ok"]
    assert (c.session.chain is not None) is ok


@pytest.mark.parametrize("url,ok", [
    (f"{SITE}/", True), (f"{SITE}/en", True), (f"{SITE}/login", True), (f"{SITE}/account/login", True), (f"{SITE}/wp-login.php", True), (f"{SITE}/users/sign_in", True),
    (f"{SITE}/#/login", True), (f"{SITE}/en/log-in?lang=en", True), (f"{SITE}/?utm_source=x", True), (f"{SITE}/en-gb/", True), (f"{SITE}/me", False), (f"{SITE}/my", False), (f"{SITE}/go?x=1", False),
    (f"{SITE}/tx?to=evil&amount=500", False), (f"{SITE}/?do=sendmoney&to=evil", False), (f"{SITE}/login?next=L3NlY3VyaXR5", False), (f"{SITE}/login?returnTo=billing", False),
    (f"{SITE}/login?user_code=ABCD-EFGH", False), (f"{SITE}/login?token=MAGIC", False), (f"{SITE}/#send?to=evil", False), (f"{SITE}/#x", False), (f"{SITE}/index.html", True), (f"{SITE}/account", True), (f"{SITE}/my-account/", True), (f"{SITE}/session/new", True), (f"{SITE}/member-login", True),
    (f"{SITE}/shop/login", True), (f"{SITE}/user-login", True), (f"{SITE}/index.php?route=account/login", True), (f"{SITE}/login?next=/", True), (f"{SITE}/login/email", True),
    (f"{SITE}/login?callbackUrl={SITE}/", True), (f"{SITE}/login?callbackUrl=https://evil.example/", False), (f"{SITE}/login?next=//evil.example/", False),
    (f"{SITE}/login?next=/account?do=x", False), (f"{SITE}/login?go=/", False), (f"{SITE}/wallet/account", False), (f"{SITE}/transfer-login", False), (f"{SITE}/x9/login", False),
    (f"{SITE}/settings/security", False),
    (f"{SITE}/login?next=/x", False), (f"{SITE}/login/return/security", False), (f"{SITE}/wallet", False), (f"{SITE}/login?return=security-settings", False),
    (f"{SITE}/#/wallet/send", False), (f"{SITE}/login#x?next=/y", False), (f"{SITE}/oauth/authorize?client_id=1", False), ("http://www.club.example/login", False),
    (f"{SITE}/classes", False), (f"{SITE}/login/confirm", False)])
def test_where_a_session_begins(env, url, ok):
    assert co.plain_entry(url) is ok


def started(env, url=f"{SITE}/"):
    """A new session at the site's start page, which has links and things on it."""
    c, eng, sid, _ = env
    c.handle({"cmd": "close", "session": sid})
    c.retire = False
    eng.pages[url] = lambda: [Frame("home", url, title="Club", text="Hello", elements=[
        el("link", label="Log in", dest=f"{SITE}/login"), el("link", label="here", dest=f"{SITE}/settings/security"), el("button", label="Log In"),
        el("link", label="Log in", id="l2", dest=f"{SITE}/login?next=/transfer/confirm"), el("button", label="Book now"), el("input", name="q", label="Search", type="search"),
        el("link", label="Join", dest="https://evil.example/login"), el("button", label="Accept all cookies"), el("link", label="Accept", id="l3", dest=f"{SITE}/wallet")])]
    r = c.handle({"cmd": "open", "url": url})
    return r["session"], {i + 1: e["ref"] for i, e in enumerate(r["snapshot"]["elements"])}


@pytest.mark.parametrize("n,press,ok", [
    (1, {"plain": True, "dest": f"{SITE}/login", "submits": False}, True), (2, {"dest": f"{SITE}/settings/security", "submits": False}, False),
    (3, {"submits": False}, True), (3, {"submits": True}, False), (3, {"submits": False, "texts": ["Open login dialog", "☰"]}, True), (3, {"submits": False, "texts": ["Sign in / Register"]}, True),
    (1, {"dest": "javascript:void(0)", "submits": False}, True), (2, {"dest": "javascript:void(0)", "submits": False}, False), (8, {"dialog": "cookies", "submits": False, "texts": ["Got it"]}, True), (4, {"dest": f"{SITE}/login?next=/transfer/confirm", "submits": False}, False),
    (5, {"submits": False}, False), (7, {"dest": "https://evil.example/login", "submits": False}, False),
    (1, {"plain": True, "dest": f"{SITE}/login?state=abc", "submits": False}, True), (2, {"plain": True, "dest": f"{SITE}/login", "submits": False}, True),
    (2, {"plain": False, "dest": f"{SITE}/login", "submits": False}, False), (2, {"plain": True, "dest": f"{SITE}/login?state=abc", "submits": False}, False),
    (8, {"dialog": "cookies", "submits": False}, True), (9, {"dialog": "cookies", "submits": False, "dest": f"{SITE}/wallet"}, False)])
def test_what_was_done_before_the_sign_in_counts(env, n, press, ok):
    c, eng, _, _ = env
    sid, r = started(env)
    eng.press = press
    c.handle({"cmd": "click", "session": sid, "ref": r[n]})
    eng.press = {}
    assert (c.session.chain == c.session.gen) is ok
    eng.navigate(f"{SITE}/login")
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert fill(c, sid, bind(c, sid, refs))["ok"]  # signing in itself works
    assert (c.session.chain == c.session.gen) is ok


@pytest.mark.parametrize("what", ["type", "back", "open elsewhere", "open here"])
def test_other_things_before_the_sign_in(env, what):
    c, eng, _, _ = env
    sid, r = started(env)
    if what == "type":
        c.handle({"cmd": "type", "session": sid, "ref": r[6], "text": "x"})
    elif what == "back":
        c.handle({"cmd": "back", "session": sid})
    else:
        url = f"{SITE}/login" if what == "open here" else "https://other.example/"
        c.handle({"cmd": "open", "session": sid, "url": url})
    assert (c.session.chain == c.session.gen) is (what == "open here")


def test_a_second_page_revives_nothing(env):
    c, eng, sid, refs = env
    eng.frames[0].url = f"{SITE}/login?next=/settings/security"
    assert fill(c, sid, bind(c, sid, refs, user="User name", password=None, press=None))["ok"] and c.session.chain is None
    page = [Frame("main", f"{SITE}/login/password", elements=[el("input", type="password", name="pw", label="Password", form=1), el("button", label="Log in", form=1)])]
    page[0].loader = "L-2"
    eng.frames = page
    refs = {e["label"]: e["ref"] for e in c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["elements"]}
    assert fill(c, sid, si(c, cmd="bind_login", session=sid, password=refs["Password"], press=None))["ok"]
    assert c.session.chain is None


@pytest.mark.parametrize("press,ok", [
    ({"dest": f"{SITE}/verify"}, True), ({"dest": f"{SITE}/verify/sms?method=sms"}, True), ({"dest": "javascript:void(0)"}, True), ({"dest": f"{SITE}/challenge?action=send"}, False),
    ({"dest": "https://evil.example/verify"}, False), ({"dest": f"{SITE}/transfer"}, False), ({"dest": f"{SITE}/settings/security"}, False), ({"dest": f"{SITE}/account/email"}, False),
    ({"dest": f"{SITE}/verify#/wallet/send"}, False), ({"dest": f"{SITE}/verify#top"}, True), ({"dest": f"{SITE}/verify?method=/x"}, False)])
def test_where_a_step_leads(env, press, ok):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.press = {"submits": False, **press}
    assert submit(c, sid, r["Try another way"])["ok"]
    assert (c.session.chain == c.session.gen) is ok


@pytest.mark.parametrize("kind,press,ok", [("button", {"submits": True, "method": "post"}, True), ("button", {"submits": False}, False),
                                           ("button", {"submits": True, "method": "get"}, False), ("link", {"submits": False}, False)])
def test_continue_is_a_step_only_as_a_forms_button(env, kind, press, ok):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].els[4 if kind == "button" else 3]["label"] = "Continue"
    eng.press = {"dest": f"{SITE}/verify", **press}
    assert submit(c, sid, r["Send a new code" if kind == "button" else "Try another way"])["ok"]
    assert (c.session.chain == c.session.gen) is ok


def test_after_a_code_only_a_new_one_or_its_own_button(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    assert fill_code(c, sid, bind_code(c, sid, r, press=None))["ok"]
    assert submit(c, sid, r["Send a new code"])["ok"] and c.session.chain == c.session.gen
    assert submit(c, sid, r["Try another way"])["ok"] and c.session.chain != c.session.gen


def test_another_route_of_the_same_page_is_another_place(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].url = f"{SITE}/app?a=1#/login/verify"
    assert fill_code(c, sid, bind_code(c, sid, r, press=None))["ok"]
    for url in (f"{SITE}/app?a=1#/wallet/send/verify", f"{SITE}/app?a=1&b=2#/login/verify"):
        eng.frames[0].url = url
        assert "first code went" in bind_code(c, sid, r, press=None)["error"]
    for url in (f"{SITE}/app?a=2#/login/verify", f"{SITE}/app?a=1&error=invalid_code#/login/verify"):
        eng.frames[0].url = url
        assert bind_code(c, sid, r, press=None)["ok"]


@pytest.mark.parametrize("facts", [{"captions": ["Pay now"]}, {"captions": ["Verify https://www.club.example/transfer"]}, {"action": f"{SITE}/transfer/confirm"}])
def test_what_would_send_it_counts_too(env, facts):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = facts
    assert not bind_code(c, sid, r, press=None)["ok"]


def test_the_fields_own_name_counts_too(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].els[1]["name"] = "transferOtp"
    assert not bind_code(c, sid, r)["ok"]


@pytest.mark.parametrize("how,ok", [("link", True), ("button", True), ("text", False), ("both", False)])
def test_a_page_that_offers_to_sign_out_is_an_account(env, how, ok):
    c, eng, sid, refs = env
    if how == "text":
        eng.frames[0].text = "Log out"
    else:
        eng.frames[0].els[20] = el("link" if how == "both" else how, label="Log out")
        eng.frames[0].els[3]["label"] = "Log in" if how == "both" else "Enter"
    c.handle({"cmd": "snapshot", "session": sid})
    assert c.session.account is ok


@pytest.mark.parametrize("words,kw,ok", [
    ("Enter the code we sent to your email", {"label": "Enter it here"}, False), ("Check your inbox for a code", {"label": "", "name": "x1"}, False), ("Enter the code from your email", {"label": "Email"}, True),
    ("Please check your email address", {"label": "Member number"}, True), ("Your confirmation will be sent to your email. Phone number", {"label": "Emergency contact"}, True),
    ("Security code", {"label": "Apartment"}, True), ("Código de descuento", {"label": "Cupón"}, True),
    ("Verification code", {"name": "search"}, True), ("Find a class", {"label": "Enter it here"}, True), ("We have emailed you a code", {"label": "Promo code"}, True),
    ("Código de verificación", {"label": "Aquí"}, False)])
def test_where_the_page_asks_for_a_code_ampi_types_only_plain_fields(env, words, kw, ok):
    c, eng, sid, _ = env
    eng.words = words
    eng.frames[0].els[10] = el("input", **kw)
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    ref = next(e["ref"] for e in snap["elements"] if e["ref"].endswith(":10"))
    assert c.handle({"cmd": "type", "session": sid, "ref": ref, "text": "482913"})["ok"] is ok


def test_in_an_account_looser_names_count(env):
    c, eng, sid, _ = env
    eng.words = "Settings"
    eng.frames[0].els[10] = el("input", label="Confirmation code")
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    ref = next(e["ref"] for e in snap["elements"] if e["ref"].endswith(":10"))
    c.session.signed_in = True
    assert "one-time code" in c.handle({"cmd": "type", "session": sid, "ref": ref, "text": "482913"})["error"]


def test_fingerprints_of_the_page_say_nothing_about_it(env):
    import hashlib

    c, eng, sid, _ = env
    r = signed_in(env)
    b = c.handle({"cmd": "bind_submit", "session": sid, "ref": r["Save"]})
    title, around = eng.frames[0].title, eng.frames[0].text
    assert b["seen"] != hashlib.sha256(f"{title}\n{around}".encode()).hexdigest() and len(b["seen"]) == len(b["digest"]) == 64


def test_what_may_have_been_done_is_not_done_again(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = {"count": 6}
    b = bind_code(c, sid, r)
    real, n = eng.op, [0]

    def op(f, a):
        if a["op"] == "setcode" and not a.get("look"):
            n[0] += 1
            real(f, a)
            raise co.CdpProtocolError("Cannot find context")
        return real(f, a)
    eng.op = op
    out = fill_code(c, sid, b)
    assert not out["ok"] and out["phase"] == "sent" and n[0] == 1 and c.session.code and c.session.codes == 1


@pytest.mark.parametrize("change", ["words", "new-password", "price", "signup", "chain", "pin"])
def test_a_code_next_to_a_password_follows_the_same_rules(env, change):
    c, eng, sid, refs = env
    refs = both(env)
    if change == "words":
        eng.words = "Confirm the transfer"
    elif change == "signup":
        eng.words = "Create your account"
    elif change == "price":
        eng.frames[0].text = "Order total: 500,00 €"
    elif change == "chain":
        c.session.gen += 1
    elif change == "pin":
        eng.frames[0].els[10].update(name="p3", label="PIN code again", type="password")
    else:
        eng.frames[0].els[10]["autocomplete"] = "new-password"
    b = si(c, cmd="bind_login", session=sid, user=refs["Email"], password=refs["Password"], code=refs["Authentication code"])
    assert not b["ok"]


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
    real = eng.op

    def op(f, a):
        return {"ok": False, "set": False} if a["op"] == "setcode" else real(f, a)
    eng.op = op
    out = fill_code(c, sid, b)
    assert not out["ok"] and out["phase"] == "verify" and c.session.codes == 0 and c.session.code == [] and c.session.code_place is None
    eng.op = real
    assert bind_code(c, sid, r)["ok"]


def test_another_try_in_boxes_never_mixes_codes(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = {"count": 6}
    for code in ("482913", "777777"):
        assert fill_code(c, sid, bind_code(c, sid, r, press=None), code=code)["ok"]
    assert eng.posts == ["482913", "777777"]


def test_a_page_that_takes_its_time_gets_it_once(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = {"count": 6}
    t, real = c.clock(), eng.op

    def op(f, a):
        if c.clock() > t + 3 and a["op"] == "inspect" and f.els.get(a["n"], {}).get("name") == "otp" and eng.posts:
            f.els[1]["connected"] = False  # the answer came
        return real(f, a)
    eng.op = op
    clicks = len(eng.clicks)
    assert fill_code(c, sid, bind_code(c, sid, r)) == {"ok": True, "session": sid, "pressed": False, "moved": True}
    assert len(eng.clicks) == clicks


def test_a_button_that_stays_disabled_was_not_pressed(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].els[2]["disabled"] = True
    real = eng.op
    eng.op = lambda f, a: {"ok": False} if a["op"] == "clickif" else real(f, a)
    out = fill_code(c, sid, bind_code(c, sid, r))
    assert out["ok"] and out["pressed"] is False


@pytest.mark.parametrize("what", ["type", "open", "click"])
def test_what_was_refused_was_not_done(env, what):
    c, eng, sid, _ = env
    r = signed_in(env)
    if what == "click":
        eng.frames[0].els[5]["disabled"] = True
    out = c.handle({"cmd": what, "session": sid, "ref": r["Save"] if what == "click" else r["Code"], "text": CODE, "url": f"{SITE}/x"})
    assert not out["ok"] and bind_code(c, sid, r)["ok"]


def test_which_way_the_code_should_come(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    for how in ("click", "submit"):
        eng.frames[0].els[9] = {**el("checkbox", label="Text message to ••12", form=1), "kind": "radio", "type": "radio"}
        snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
        ref = next(e["ref"] for e in snap["elements"] if e["label"].startswith("Text message"))
        eng.press = {"texts": []}  # a radio's own words are its label's
        out = c.handle({"cmd": "click", "session": sid, "ref": ref}) if how == "click" else submit(c, sid, ref)
        eng.press = {}
        assert out["ok"] and bind_code(c, sid, r)["ok"]
    eng.frames[0].els[9]["label"] = "Delete my phone"
    c.handle({"cmd": "click", "session": sid, "ref": ref})
    assert not bind_code(c, sid, r)["ok"]


def test_words_go_into_any_field_there(env):
    c, eng, sid, refs = env
    eng.words = "Enter the code we sent to your email"
    eng.frames[0].els[10] = el("input", label="Allergies")
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    ref = next(e["ref"] for e in snap["elements"] if e["ref"].endswith(":10"))
    assert c.handle({"cmd": "type", "session": sid, "ref": ref, "text": "nuts and shellfish"})["ok"]
    assert not c.handle({"cmd": "type", "session": sid, "ref": ref, "text": "482 913"})["ok"]
    b = c.handle({"cmd": "bind", "session": sid, "refs": [ref]})
    assert c.handle({"cmd": "fill", "session": sid, "origin": b["origin"], "fields": [{"ref": ref, "binding": b["bindings"][0], "value": "00100"}]})["ok"]


def test_why_no_code_can_be_filled_in(env):
    c, eng, sid, _ = env
    assert c.handle({"cmd": "status"})["codes"] is True
    r = signed_in(env)
    c.handle({"cmd": "click", "session": sid, "ref": r["Try another way"]})
    assert c.handle({"cmd": "status"})["codes"] is False and "`checkout click` did something" in bind_code(c, sid, r)["error"]
    c.handle({"cmd": "close", "session": sid})
    c.retire = False
    eng.pages[f"{SITE}/classes"] = eng.pages[f"{SITE}/login"]
    c.handle({"cmd": "open", "url": f"{SITE}/classes"})
    assert c.session.broke.startswith("the session began at") and c.handle({"cmd": "status"})["codes"] is False


@pytest.mark.parametrize("label,ok", [("Send code to •••12", True), ("Send money to •••12", False), ("Text message", True), ("Email", True), ("Email j•••@x.example", True), ("Authenticator app", True), ("SMS", True), ("Backup code", True),
                                      ("Email me offers", False), ("Text editor", False), ("Phone settings", False)])
def test_ways_as_buttons(env, label, ok):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].els[4].update(label=label)
    eng.press = {"submits": False, "dest": ""}
    assert submit(c, sid, r["Send a new code"])["ok"]
    assert (c.session.chain == c.session.gen) is ok


def test_ways_only_as_buttons(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.frames[0].els[3]["label"] = "Email"
    eng.press = {"submits": False, "dest": f"{SITE}/verify/email"}
    assert submit(c, sid, r["Try another way"])["ok"] and c.session.chain != c.session.gen


def test_nothing_is_a_step_before_the_login_was_used(env):
    c, eng, sid, refs = env
    eng.frames[0].els[20] = el("link", label="Log out")
    eng.frames[0].els[3]["label"] = "Enter"
    c.handle({"cmd": "snapshot", "session": sid})
    assert c.session.account and c.session.chain == c.session.gen
    eng.frames[0].els[9]["label"] = "Continue"
    eng.press = {"submits": True, "method": "post", "dest": f"{SITE}/login"}
    submit(c, sid, refs["Next"])
    assert c.session.chain != c.session.gen


@pytest.mark.parametrize("words,yes", [("500,00 €", True), ("500.00 EUR", True), ("10 000 €", True), ("1 000 EUR", True), ("€0.50", True), ("5 000,00 kr", True), ("Cart 0,00 €", False),
                                       ("€0", False), ("0.00 EUR", False), ("Room 5", False), ("$ 12", True), ("2026", False)])
def test_prices(words, yes):
    assert co.priced(words) is yes


def test_the_mask_stays_while_it_is_not_sure(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    assert fill_code(c, sid, bind_code(c, sid, r, press=None))["ok"]
    eng.frames[0].text = f"x {CODE}"
    show = lambda: c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["text"]  # noqa: E731
    eng.frames[0].els[1]["connected"] = False
    assert show() == "x •••"  # gone, but just now
    c.clock.sleep(61)
    assert show() == f"x {CODE}"


@pytest.mark.parametrize("label,box,ok", [("Log in", True, True), ("Log in", False, False), ("Transfer", True, False), ("Verify", True, True)])
def test_a_sign_in_without_a_form(env, label, box, ok):
    c, eng, sid, refs = env
    assert fill(c, sid, bind(c, sid, refs, press=None))["ok"]
    eng.frames[0].els[3]["label"] = label
    eng.press = {"submits": False, "withForm": False, "withBox": box}
    assert submit(c, sid, refs["Log in"])["ok"]
    assert (c.session.chain == c.session.gen) is ok


def test_a_wrong_code_shown_where_the_form_went(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    eng.code_facts = {"action": f"{SITE}/verify/check"}
    assert fill_code(c, sid, bind_code(c, sid, r, press=None))["ok"]
    eng.frames[0].url = f"{SITE}/verify/check"
    eng.frames[0].els[1]["id"] = "mat-input-7"
    assert bind_code(c, sid, r, press=None)["ok"]
    eng.frames[0].url = f"{SITE}/other"
    assert not bind_code(c, sid, r, press=None)["ok"]


@pytest.mark.parametrize("what,ok", [("zero", True), ("stripe", True), ("short", True), ("robot", False), ("total", False), ("advice", True), ("orders", True), ("heading", False), ("failed", True)])
def test_ordinary_things_on_such_pages(env, what, ok):
    c, eng, sid, _ = env
    r = signed_in(env)
    if what == "zero":
        eng.frames[0].text = "Cart total: 0,00 €"
    elif what == "stripe":
        eng.frames.append(Frame("m", "https://js.stripe.com/v3/m-outer-abc.html", parent="main"))
    elif what == "short":
        eng.frames[0].text = "Please verify it's you"
    elif what == "robot":
        eng.frames[0].text = "Verify you are human"
    elif what == "total":
        eng.frames[0].text = "Order total: 10,00 €"
    elif what == "failed":
        eng.words = "Не удалось войти. Введите код. Maksuton tekstiviesti"
    else:
        eng.words = "Track your order and checkout faster" if what == "orders" else "If this wasn't you, change your password immediately"
        if what == "heading":
            real = eng.op
            eng.op = lambda f, a: {**real(f, a), "heads": eng.words} if a["op"] == "code" else real(f, a)
        else:
            real = eng.op
            eng.op = lambda f, a: {**real(f, a), "heads": "Sign in"} if a["op"] == "code" else real(f, a)
    assert bind_code(c, sid, r)["ok"] is ok


@pytest.mark.parametrize("kw,strict,loose", [
    ({"label": "Quantity", "max": 1}, False, False), ({"name": "timezone", "id": "timezone"}, False, False), ({"label": "One-time purchase"}, False, False),
    ({"name": "phoneCode", "label": "Code"}, True, True), ({"id": "ContentArea_txtCode", "label": "Code"}, True, True), ({"id": "code-group", "name": "code"}, True, True),
    ({"label": "Código"}, True, True), ({"name": "SMSCode"}, True, True), ({"label": "Enter the 6 digits"}, False, True), ({"name": "authcode"}, True, True),
    ({"label": "Country", "name": "countryCode"}, False, False), ({"label": "Group code"}, False, False), ({"name": "orderCode"}, False, False)])
def test_fields(kw, strict, loose):
    d = {"type": "text", "autocomplete": "", "name": "", "id": "", "label": "", "max": 0, **kw}
    assert (co.code_field(d), co.code_field(d, loose=True)) == (strict, loose)


def test_the_code_is_cut_out_until_the_next_page(env):
    c, eng, sid, _ = env
    r = signed_in(env)
    assert fill_code(c, sid, bind_code(c, sid, r, press=None), code="2026")["ok"]
    eng.frames[0].text = "Wrong code 2026! Not 20261, but 2 0 2 6, 20-26 and x2026"
    text = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["text"]
    assert text == "Wrong code •••! Not 20261, but •••, ••• and x•••"
    eng.frames[0].els[1]["value"] = ""
    assert fill_code(c, sid, bind_code(c, sid, r, press=None), code="3344")["ok"]
    eng.frames[0].text = "2026 and 3\n3\n4\n4"
    assert c.handle({"cmd": "snapshot", "session": sid})["snapshot"]["text"] == "••• and •••"  # the first one, too
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
    for bad in ({"code": None}, {"code": "1 2"}, {"code": "1234567"}):
        assert not fill(c, sid, b, **bad)["ok"] and not eng.sets
    r = fill(c, sid, b, code=CODE)
    assert r["ok"] and c.session.code[0][0] == CODE and c.session.codes == 1
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
    eng.words = "Find your booking"
    eng.describe = lambda n, e, real=eng.describe: {**real(n, e), "max": e.get("max", 0)}
    eng.frames[0].els[10] = el("input", **kw)
    snap = c.handle({"cmd": "snapshot", "session": sid})["snapshot"]
    ref = next(e["ref"] for e in snap["elements"] if e["ref"].endswith(":10"))
    assert c.handle({"cmd": "type", "session": sid, "ref": ref, "text": "AB12"})["ok"]


@pytest.mark.parametrize("text,ok", [
    ("Send code", True), ("Send a code to j***@x.example", True), ("Resend", True), ("Text me", True), ("Email me a code", True), ("Try another way", True), ("Use authenticator app", True),
    ("Continue", False), ("Get a new verification code", True), ("Use a backup code", True), ("Use another method", True), ("Send a text message", True),
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
<main><p>Confirm your transfer</p><p>500 EUR</p><div><div>Deep</div><div><label>Payee</label><form id=z method=post><input name=z1><button>Go</button></form></div></div></main>
<div><p>Above it</p><section role=dialog><header><p>Send money</p></header><form id=y method=post><input name=y1><button>Go</button></form></section></div>
<footer>Footer offer</footer>
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
out.near = ['Confirm your transfer', '500 EUR', 'Deep', 'Payee', 'Footer offer'].map(x => facts('z1').words.includes(x));
out.dialog = ['Send money', 'Above it'].map(x => facts('y1').words.includes(x));
const expect = (f) => Object.fromEntries(['hasForm','count','max','type','extra','boxes','shown','method','action','boxed','press'].map(k => [k, f[k]]));
out.stale = run({ op: 'setcode', n: N.otp.n, i: 0, value: 'x', press: I.b1.n, expect: { ...expect(out.one), max: 8 } });
out.look = run({ op: 'setcode', n: N.d1.n, i: 0, look: true, value: '', press: I.b6.n, expect: expect(out.six) });
out.box = [['b1', 'otp'], ['bf', 'f1'], ['b1', 'f1']].map(x => run({ op: 'gate', n: I[x[0]].n, with: N[x[1]].n }).withBox);
out.set = run({ op: 'setcode', n: N.otp.n, i: 0, value: '482913', press: I.b1.n, expect: expect(out.one) }); out.value = q('[name=otp]').value;
const seen = []; for (const d of document.querySelectorAll('.d')) d.addEventListener('input', (e) => seen.push(e.target.name + e.target.value));
out.boxes = [...'482913'].map((x, i) => run({ op: 'setcode', n: N.d1.n, i: i, value: x, press: I.b6.n, expect: expect(out.six) }).ok); out.seen = seen;
out.full = run({ op: 'setcode', n: N.d1.n, i: 0, look: true, value: '', press: I.b6.n, expect: {} }).full; out.cleared = [5, 4].map(i => run({ op: 'setcode', n: N.d1.n, i: i, value: '', press: I.b6.n, expect: {} }).ok);
out.left = q('[name=d6]').value + q('[name=d5]').value + q('[name=d4]').value; seen.length = 6;
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
    assert [x.strip() for x in one["captions"]] == ["Verify"] and [x.strip() for x in out["free"]["captions"]] == ["Continue"]
    assert out["near"] == [True, True, True, True, False] and out["dialog"] == [True, True]
    assert out["other"] == "" and out["again"] == ""  # another form's button, one that doesn't send
    assert (out["six"]["count"], out["six"]["extra"], out["six"]["press"], out["second"]) == (6, False, "form", 1)
    assert (out["free"]["count"], out["free"]["hasForm"], out["free"]["press"], out["wrongBox"]) == (4, False, "box", "")
    assert out["extra"] == [True, True, False, True]
    assert out["stale"] == {"ok": False, "set": False} and out["set"] == {"ok": True} and out["value"] == CODE
    assert out["boxes"] == [True] * 6 and out["seen"] == [f"d{i + 1}{x}" for i, x in enumerate(CODE)]
    assert out["look"] == {"ok": True, "full": []} and out["full"] == [0, 1, 2, 3, 4, 5] and out["cleared"] == [True, True] and out["left"] == "9"
    assert out["box"] == [True, True, False]
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
