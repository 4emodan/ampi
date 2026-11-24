"""A session the owner gave from Firefox: the extension's part in a real browser (opened again by the app's code), the controller, the
sign-in service and Ampi's side."""

import html
import json
import re
import subprocess
from pathlib import Path

import pytest
from test_checkout_controller import Clock, FakeEngine, Frame, el
from test_companion import JAVA, JAVAC, classes  # noqa: F401 (fixture)
from test_signin import Phone, Server, caller, certs, confs, fast  # noqa: F401 (fixtures)
from test_signin_fill import EDGE, Asked, run_op, svc

from ampi_phone import signin
from ampi_tools import checkout as co
from ampi_tools import client, ops_signin

FIREFOX = Path(__file__).resolve().parent.parent / "companion/firefox"
TOKEN = "Zx9" * 14
HOST = "www.club.example"


def ck(n, v=TOKEN, d=HOST, o=True, p="/", s=True, h=True, x="Lax"):
    return {"n": n, "v": v, "d": d, "o": o, "p": p, "s": s, "h": h, "x": x}


class Engine(FakeEngine):
    def __init__(self, pages):
        super().__init__(pages)
        self.cookies, self.calls, self.takes = [], [], lambda c: True

    def call(self, method, params=None, session=None, timeout=30):
        if method.startswith("Network."):
            self.calls.append((method, session))
            if method == "Network.setCookies":
                self.cookies = [c for c in params["cookies"] if self.takes(c)]
            return {"cookies": self.cookies} if method == "Network.getCookies" else {}
        return super().call(method, params, session, timeout)


@pytest.fixture
def env():
    eng = Engine({f"https://{HOST}/": lambda: [Frame("main", f"https://{HOST}/", title="Club", text=f"Hello Jane. Debug: {TOKEN}", elements=[
        el("button", label="Cancel membership"), el("input", name="otp", label="Code")])],
        f"https://{HOST}/members/my": lambda: [Frame("main", f"https://{HOST}/members/my", text="Hello")]})
    clock = Clock()

    class CdpWrap:
        def __init__(self):
            self.call, self.close = eng.call, eng.close
    return co.Controller(connect=CdpWrap, clock=clock, mem_mb=lambda: 2000, sleep=clock.sleep, check_url=lambda u: u, signin=True), eng


def give(c, cookies=None, host=HOST, path="/", **kw):
    return c.handle({"cmd": "fill_session", "host": host, "path": path, "cookies": [ck("sid")] if cookies is None else cookies, **kw}, signin=True)


# --- the controller ---------------------------------------------------------------------------------------


def test_a_session_opens_signed_in(env):
    c, eng = env
    assert c.handle({"cmd": "bind_session"}, signin=True) == {"ok": True}
    r = give(c, [ck("__Host-sid"), ck("csrf", "abc", o=False, d="club.example", h=False, x=""), ck("cf_clearance")], path="/")
    assert r == {"ok": True, "session": c.session.id, "kept": 2, "of": 2}
    assert eng.cookies == [
        {"name": "__Host-sid", "value": TOKEN, "path": "/", "secure": True, "httpOnly": True, "url": f"https://{HOST}/", "sameSite": "Lax"},
        {"name": "csrf", "value": "abc", "path": "/", "secure": True, "httpOnly": False, "url": "https://club.example/", "domain": ".club.example"}]
    assert eng.history == [f"https://{HOST}/"] and all(s == "s1" for _, s in eng.calls)
    snap = c.handle({"cmd": "snapshot", "session": r["session"]})["snapshot"]
    assert TOKEN not in json.dumps(snap) and "Hello Jane. Debug: •••" in snap["text"]
    # an account from the first moment
    g = c.handle({"cmd": "gate", "session": r["session"], "ref": snap["elements"][0]["ref"]})
    assert g["signed_in"] is True
    assert "signed in here" in c.handle({"cmd": "open", "session": r["session"], "url": f"https://{HOST}/x"})["error"]
    assert "right after" in c.handle({"cmd": "bind_code", "session": r["session"], "code": snap["elements"][1]["ref"]}, signin=True)["error"]
    c.handle({"cmd": "close", "session": r["session"]})
    assert c.retire  # the engine goes with it


def test_only_the_sign_in_service_may(env):
    c, _ = env
    for cmd in ("bind_session", "fill_session"):
        assert c.handle({"cmd": cmd, "host": HOST, "path": "/", "cookies": [ck("a")]})["error"] == "unknown command"


def test_never_into_a_session_that_is_open(env):
    c, eng = env
    c.handle({"cmd": "open", "url": f"https://{HOST}/"})
    for r in (give(c), c.handle({"cmd": "bind_session"}, signin=True)):
        assert "another checkout session" in r["error"]
    assert not eng.cookies
    c.handle({"cmd": "close", "session": c.session.id})
    assert "afresh" in give(c)["error"]  # this engine was used


@pytest.mark.parametrize("cookies", [
    [], [ck("sid")] * 81, "x", [5], [ck("a b")], [ck("sid", "x;y")], [ck("sid", "x\ny")], [ck("sid", "x" * 4097)], [ck("sid", d="evil.example")],
    [ck("sid", d="club.example")], [ck("sid", d="example", o=False)], [ck("sid", d="my.www.club.example", o=False)], [ck("sid", d=".club.example", o=False)], [ck("sid", p="x")],
    [ck("sid", p="/;x")], [ck("sid", x="lax")], [{**ck("sid"), "h": 1}], [{**ck("sid"), "v": None}], [ck("sid", d="ub.example", o=False)]])
def test_odd_cookies(env, cookies):
    c, eng = env
    r = give(c, cookies)
    assert not r["ok"] and not eng.cookies and c.session is None and TOKEN not in json.dumps(r)


@pytest.mark.parametrize("kw", [{"host": "localhost"}, {"host": "1.2.3.4"}, {"host": None}, {"path": "x"}, {"path": "//evil.example"}, {"path": "/a/../b"},
                                {"path": "/x y"}, {"path": None}, {"engine": "chrome"}, {"engine": 5}, {"engine": "lightpanda"}])
def test_odd_requests(env, kw):
    c, eng = env
    assert not give(c, **kw)["ok"] and not eng.cookies and c.session is None


@pytest.mark.parametrize("name", ["cf_clearance", "__cf_bm", "_abck", "bm_sz", "datadome", "_px3", "reese84", "incap_ses_1_2", "aws-waf-token", "CF_CLEARANCE"])
def test_nothing_that_vouches_for_a_person(env, name):
    c, eng = env
    r = give(c, [ck(name)])
    assert not r["ok"] and "nothing in that session" in r["error"] and not eng.cookies


def test_an_engine_that_takes_nothing(env):
    c, eng = env
    eng.takes = lambda x: False
    r = give(c)
    assert not r["ok"] and "took nothing" in r["error"] and c.session is None and eng.disposed == 1 and not eng.history
    c.retire = False
    eng.takes = lambda x: x["name"] == "a"
    assert give(c, [ck("a"), ck("b")])["kept"] == 1


def test_an_engine_that_fails(env):
    c, eng = env
    eng.fail["Page.navigate"] = 1
    r = give(c)
    assert not r["ok"] and c.session is None and TOKEN not in json.dumps(r)


# --- the extension ----------------------------------------------------------------------------------------


KEY = "ampi-key1-" + "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"
SCRIPT = r"""
(async () => {
  const c = (n, o) => Object.assign({ name: n, value: 'v', domain: 'www.club.example', hostOnly: true, path: '/', secure: true, httpOnly: true, sameSite: 'lax' }, o);
  const out = {};
  out.sites = ['https://www.club.example/a/b?x=1#y', 'http://www.club.example/', 'https://club.example:8443/', 'https://localhost/', 'https://1.2.3.4/', 'about:config',
    undefined, 'https://u@club.example/', 'https://club.example/a%20b/..%2f', 'https://club.example/a//b'].map(u => { const s = Ampi.site(u, false); return s.why ? null : [s.host, s.path]; });
  out.incognito = !!Ampi.site('https://club.example/', true).why;
  const all = [c('__Host-sid', { value: 'Zx9'.repeat(14) }), c('wide', { domain: '.club.example', hostOnly: false, sameSite: 'no_restriction' }), c('none', { sameSite: 'unspecified', httpOnly: false }),
    c('cf_clearance'), c('_ga_ABC'), c('part', { partitionKey: { topLevelSite: 'https://x.example' } }), c('other', { domain: 'evil.example' }), c('below', { domain: '.a.www.club.example', hostOnly: false }),
    c('bad name'), c('semi', { value: 'a;b' }), c('uni', { value: 'é' }), c('long', { value: 'x'.repeat(4097) }), c('__Host-sid', { value: 'again' }), c('hostonly', { domain: 'club.example' })];
  const p = Ampi.pick(all, 'www.club.example');
  out.picked = p.map(x => x.n); out.first = p[0]; out.third = p[2]; out.scope = [Ampi.scope(p), Ampi.scope([p[0]])];
  out.many = Ampi.pick(Array.from({ length: 100 }, (_, i) => c('n' + i)), 'www.club.example').length;
  const key = await Ampi.readKey('  pasted: """ + KEY + r""" \n');
  out.key = [key.extractable, key.usages.join()];
  out.noKey = await Ampi.readKey('ampi-key1-short');
  out.sealed = await Ampi.seal(key, 'www.club.example', '/my', p, 1700000000000);
  out.again = (await Ampi.seal(key, 'www.club.example', '/my', p, 1700000000000)) !== out.sealed;
  out.empty = await Ampi.seal(key, 'www.club.example', '/', [], 1);
  out.big = await Ampi.seal(key, 'www.club.example', '/', Ampi.pick(Array.from({ length: 20 }, (_, i) => c('n' + i, { value: 'x'.repeat(4000) })), 'www.club.example'), 1);
  document.querySelector('#out').textContent = 'RESULT' + JSON.stringify(out) + 'END';
})().catch(e => { document.querySelector('#out').textContent = 'RESULT' + JSON.stringify({ error: String(e && e.stack || e) }) + 'END'; });
"""


@pytest.mark.skipif(EDGE is None, reason="needs Edge or Chromium")
def test_the_extension_seals_what_the_app_opens(tmp_path, request):
    f = tmp_path / "t.html"
    f.write_text("<!doctype html><meta charset=utf-8><pre id=out></pre><script>" + (FIREFOX / "logic.js").read_text(encoding="utf-8") + "</script><script>" + SCRIPT
                 + "</script>", encoding="utf-8")
    r = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--virtual-time-budget=5000", f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom",
                        f.as_uri()], capture_output=True, timeout=90)
    dom = r.stdout.decode("utf-8", "replace")
    if "RESULT" not in dom:
        pytest.skip(f"{EDGE} gave no page: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    out = json.JSONDecoder().raw_decode(html.unescape(dom[dom.index("RESULT") + 6:]))[0]
    assert "error" not in out, out["error"]
    assert out["sites"] == [["www.club.example", "/a/b"], None, None, None, None, None, None, None, ["club.example", "/"], ["club.example", "/"]]
    assert out["incognito"] is True
    assert out["picked"] == ["__Host-sid", "wide", "none"]
    assert out["first"] == ck("__Host-sid") and out["third"]["x"] == "" and out["third"]["h"] is False
    assert out["scope"] == ["club.example", ""] and out["many"] == 80
    assert out["key"] == [False, "encrypt"] and out["noKey"] is None and out["again"] and out["empty"] is None and out["big"] is None
    assert re.fullmatch(r"ampi-session1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", out["sealed"]) and TOKEN not in out["sealed"]
    if not (JAVA and JAVAC):
        pytest.skip("needs a JDK for the rest")
    cp = request.getfixturevalue("classes")

    def opened(text, key=KEY):
        return subprocess.run([JAVA, "-cp", str(cp), "app.ampi.companion.LogicTest", "open", key, text, "1700000000000"], capture_output=True, text=True)
    r = opened(out["sealed"])
    head, body = r.stdout.splitlines()
    cookies = json.loads(body)
    assert head == "www.club.example /my 3 club.example" and [x["n"] for x in cookies] == out["picked"]
    # what the app sends is what the service and the controller take
    given = signin.given_of({"host": "www.club.example", "path": "/my", "cookies": cookies}, "club.example")
    assert given is not None and [co.Controller._cookie(x, given.host)["name"] for x in given.cookies] == out["picked"]
    # changed, or sealed with another key: not opened
    tag, h, body = out["sealed"].split(".")
    other = co.base64.urlsafe_b64encode(co.base64.urlsafe_b64decode(h + "==").replace(b"/my", b"/me")).decode().rstrip("=")
    assert opened(".".join([tag, other, body])).returncode != 0
    assert opened(out["sealed"], KEY[:-2] + "AA").returncode != 0


def test_the_extension_asks_for_little():
    m = json.loads((FIREFOX / "manifest.json").read_text(encoding="utf-8"))
    assert m["permissions"] == ["activeTab", "cookies", "clipboardWrite"] and m["optional_host_permissions"] == ["https://*/*"]
    assert not {"host_permissions", "background", "content_scripts", "externally_connectable", "web_accessible_resources"} & set(m)
    assert "default-src 'none'" in m["content_security_policy"]["extension_pages"]  # nothing leaves it by the network
    code = (FIREFOX / "popup.js").read_text(encoding="utf-8")
    assert "permissions.remove" in code and not re.search(r"fetch\(|XMLHttp|innerHTML|eval|onMessage|WebSocket", code)
    assert co.CLEARANCE.pattern.replace("(", "", 1) == re.search(r"CLEARANCE = /\^\((.*)\)\$/i", (FIREFOX / "logic.js").read_text(encoding="utf-8")).group(1) + ")"


# --- the sign-in service ----------------------------------------------------------------------------------


GIVEN = signin.Given(HOST, "/", [ck("sid")])


def ask(s, **kw):
    return s.handle({"cmd": "session", "host": "club.example", **kw}, None)


def test_from_end_to_end(env):
    c, eng = env
    s, phone = svc(c, Asked(("ok", GIVEN)))
    assert ask(s) == {"ok": True, "code": "ok", "kept": 1, "of": 1}
    assert phone.calls == [("club.example", "session")] and eng.cookies[0]["value"] == TOKEN and s.over(0)  # the process ends


@pytest.mark.parametrize("reply", [("denied", None), ("no_session", None), ("ok", None)])
def test_refused_on_the_phone(env, reply):
    c, eng = env
    s, _ = svc(c, Asked(reply))
    assert ask(s) == {"ok": False, "code": reply[0] if reply[0] != "ok" else "failed"} and not eng.cookies


def test_a_busy_browser_never_reaches_the_phone(env):
    c, eng = env
    c.handle({"cmd": "open", "url": f"https://{HOST}/"})
    s, phone = svc(c, Asked(("ok", GIVEN)))
    r = ask(s)
    assert r["code"] == "page" and "another checkout session" in r["error"] and not phone.calls


@pytest.mark.parametrize("req,code", [({"host": "accounts.google.com"}, "refused_site"), ({"host": "github.com"}, "refused_site"), ({"host": "Club.example"}, "bad_request"),
                                      ({"host": ["x"]}, "bad_request"), ({"engine": "x"}, "bad_request"), ({"engine": ["x"]}, "bad_request")])
def test_odd_requests_to_the_service(env, req, code):
    s, phone = svc(env[0], Asked(("ok", GIVEN)))
    assert ask(s, **req)["code"] == code and not phone.calls


SENT = {"ok": True, "host": HOST, "path": "/my", "cookies": [ck("sid"), ck("w", d="club.example", o=False)]}


@pytest.mark.parametrize("answer,ok", [
    (SENT, True), ({**SENT, "host": "club.example", "cookies": [ck("sid", d="club.example")]}, True), ({**SENT, "host": "evil.example"}, False),
    ({**SENT, "host": "a.club.example"}, False), ({**SENT, "path": "/a/../b"}, False), ({**SENT, "path": "https://evil.example"}, False),
    ({**SENT, "cookies": []}, False), ({**SENT, "cookies": [ck("sid", d="evil.example")]}, False), ({**SENT, "cookies": [ck("sid", d="com", o=False)]}, False),
    ({**SENT, "cookies": [ck("sid", v="a;b")]}, False), ({**SENT, "cookies": [ck("s")] * 81}, False), ({"ok": True}, False),
    ({**SENT, "cookies": [ck(f"s{i}", v="x" * 700) for i in range(40)]}, True)])
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
        code, got = signin.ask("club.example", mine, confs[1], s.vault, purpose="session")
        assert p.asked[0] == {"v": 1, "op": "session", "host": "club.example", "wait": signin.WAIT}
        assert (code, got is not None) == (("ok", True) if ok else ("failed", False))
        if ok:
            assert got.cookies == answer["cookies"] and TOKEN not in repr(got)


# --- Ampi's side ------------------------------------------------------------------------------------------


def run_session(env, tmp_path, origin="private", service=None, **args):
    import test_signin_fill as t

    real = t.validate
    t.validate = lambda ops: real([{"op": "signin.session", "args": ops[0]["args"]}])
    try:
        return run_op((env[0],), tmp_path, origin, service=service, **args)
    finally:
        t.validate = real


def test_ampi_opens_the_site_signed_in_without_seeing_the_session(env, tmp_path):
    c, eng = env
    s, _ = svc(c, Asked(("ok", GIVEN)))
    fake, r = run_session(env, tmp_path, service=s, site_host="https://www.club.example/x")
    assert r["ok"] and "1 of 1 cookies" in r["text"] and "Hello Jane" in r["text"] and TOKEN not in r["text"]
    assert fake.calls == [{"cmd": "session", "host": "club.example"}]
    fake, r = run_session(env, tmp_path, service=s, site_host="club.example")
    assert "close it first" in (r if isinstance(r, str) else r["error"]) and not fake.calls


@pytest.mark.parametrize("origin", ["group:family", "forwarded:private", "unknown", "watcher:mail"])
def test_only_the_owners_chat(env, tmp_path, origin):
    fake, r = run_session(env, tmp_path, origin, site_host="club.example")
    assert isinstance(r, str) and not fake.calls


def test_the_command():
    p = client.signin_parser()
    assert client.signin_ops(p.parse_args(["session", "club.example", "--engine", "lightpanda"])) == [
        {"op": "signin.session", "args": {"site_host": "club.example", "engine": "lightpanda"}}]
    assert "Firefox" in ops_signin.WORDS["no_session"]
