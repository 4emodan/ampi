"""The dashboard on the web: one-time links, sessions, what its service may ask, and its HTTP checks."""

import asyncio
import http.client
import json
import socketserver
import threading
from pathlib import Path

import pytest
from test_abilities import SCOPE, agent, env  # noqa: F401 (fixture)
from test_ampi_tools import BRIDGE_PID
from test_hub import hubmod

from ampi_dash import dash
from ampi_tools import server as srv
from ampi_tools.todoist import ToolError
from ampi_tools.web_sessions import IDLE, LIFE, WebSessions, load_url, watch_cert

URL = "https://192.0.2.1"
DASH = 4711


def run(c):
    return asyncio.run(c)


def key(url):
    assert url.startswith(URL + "/ampi/#k=")
    return url.split("#k=")[1]


def test_a_link_works_once_and_only_for_a_while():
    now = [0.0]
    w = WebSessions(URL, clock=lambda: now[0])
    k = key(w.new_link())
    for bad in (None, 1, "", "x" * 43, k + "a"):
        with pytest.raises(ToolError):
            w.login(bad)
    w.check(w.login(k))
    with pytest.raises(ToolError):
        w.login(k)
    k = key(w.new_link())
    now[0] += 301
    with pytest.raises(ToolError):
        w.login(k)
    a, b = key(w.new_link()), key(w.new_link())
    with pytest.raises(ToolError):
        w.login(a)
    w.login(b)


def test_sessions_end():
    now = [0.0]
    w = WebSessions(URL, clock=lambda: now[0])

    def new():
        return w.login(key(w.new_link()))

    s = new()
    now[0] += IDLE + 1
    with pytest.raises(ToolError):
        w.check(s)
    s = new()
    for _ in range(int(LIFE / 1000) + 1):
        now[0] += 1000
        try:
            w.check(s)
        except ToolError:
            break
    else:
        raise AssertionError
    first, *rest = [new() for _ in range(4) if not now.__setitem__(0, now[0] + 1)]
    with pytest.raises(ToolError):
        w.check(first)
    for r in rest:
        w.check(r)
    w.logout(rest[0])
    with pytest.raises(ToolError):
        w.check(rest[0])
    w.check(rest[1])
    w.logout()
    with pytest.raises(ToolError):
        w.check(rest[1])


def test_off_without_an_address():
    with pytest.raises(ToolError):
        WebSessions().new_link()
    assert load_url(None) == "" and load_url({"url": URL}) == URL
    for bad in ("http://192.0.2.1", URL + "/x", 5, "https://a b"):
        with pytest.raises(ValueError):
            load_url({"url": bad})


@pytest.fixture
def webbed(env, monkeypatch):  # noqa: F811
    d, gm, store = env
    d.web = WebSessions(URL)
    monkeypatch.setattr(srv, "dash_uid", lambda: DASH)

    def ask(req, uid=DASH, pid=1):
        return run(d.handle(req, pid, uid))

    return d, store, ask


def test_only_the_bridge_makes_links(webbed):
    d, _, ask = webbed
    assert not ask({"cmd": "web.link"})["ok"] and not ask({"cmd": "web.link"}, uid=1000)["ok"]
    assert not ask({"cmd": "web.link"}, uid=DASH, pid=BRIDGE_PID)["ok"]
    assert ask({"cmd": "web.link"}, uid=1000, pid=BRIDGE_PID)["ok"]


def test_the_service_may_do_nothing_without_a_session_and_nothing_else_with_one(webbed):
    d, store, ask = webbed
    cmds = [{"cmd": c, "scope": SCOPE} for c in srv.HUB] + [
        {"cmd": "run", "ops": [{"op": "gmail.labels", "args": {}}]}, {"cmd": "approvals.list"}, {"cmd": "social.sessions"},
        {"cmd": "approvals.status", "id": 1}, {"cmd": None}] + [{"cmd": c} for c in srv.ADMIN]
    for c in cmds:
        for session in ({}, {"session": "x" * 43}):
            assert not ask({**c, **session})["ok"], c
    s = ask({"cmd": "web.login", "key": key(d.web.new_link())})["session"]
    assert "signed in" in store.unsent_notices()[-1]["text"]
    for c in cmds:
        assert ask({**c, "session": s, "changes": {}})["ok"] == (c["cmd"] in ("hub.activity", "hub.abilities", "hub.watchers")) \
            or c["cmd"] == "hub.overview", c
    assert ask({"cmd": "hub.set", "scope": SCOPE, "session": s, "changes": {"gmail.star": ["default", "off"], "gmail.archive": ["default", "report"]}})["approval"]
    assert d.permissions.all(SCOPE) == {"gmail.star": "off"} and "stricter" in store.unsent_notices()[-1]["text"]
    for cmd in ("web.login", "web.logout", "hub.overview"):
        assert not ask({"cmd": cmd, "session": s, "key": s}, uid=1000)["ok"]
    assert ask({"cmd": "web.logout", "session": s})["ok"] and not ask({"cmd": "hub.watchers", "session": s})["ok"]


@pytest.fixture
def served():
    asked, answers = [], {}

    def ask(r):
        asked.append(r)
        return answers.get(r["cmd"], {"ok": True, "session": "s" * 43})

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), dash.make_handler(URL, ask, Path(hubmod.STATIC)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def req(method, path, body=None, headers=None, cookie="s" * 43, **drop):
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        h = {"Host": "192.0.2.1", "X-Ampi-Hub": "web", "Origin": URL, "Sec-Fetch-Site": "same-origin",
             "Cookie": f"__Host-ampi={cookie}", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
        h = {k: v for k, v in h.items() if v is not None}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        return r.status, r.read().decode(), r

    yield req, asked, answers
    server.shutdown()
    server.server_close()


def test_pages_hold_no_data(served):
    req, asked, _ = served
    for p in ("/", "/abilities", "/watchers", "/activity", "/hub.css", "/hub.js", "/overview.js"):
        status, body, r = req("GET", p, cookie="")
        assert status == 200 and "default-src 'none'" in r.getheader("Content-Security-Policy")
        assert 'href="/' not in body and 'src="/' not in body
    for p in ("/address-book", "/address-book.js", "/../dash.py", "/api", "/x", "/api/places"):
        assert req("GET", p)[0] in (404, 403)
    assert not asked


def test_login_sets_a_strict_cookie(served):
    req, asked, answers = served
    status, body, r = req("POST", "/api/login", {"k": "k" * 43}, cookie="")
    c = r.getheader("Set-Cookie")
    assert status == 200 and "s" * 43 not in body
    assert c.startswith("__Host-ampi=" + "s" * 43) and all(x in c for x in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"))
    answers["web.login"] = {"ok": False, "error": "this link was used"}
    status, _, r = req("POST", "/api/login", {"k": "x"}, cookie="")
    assert status == 401 and r.getheader("Set-Cookie") is None


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": None},
                                     {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"X-Ampi-Hub": None},
                                     {"Content-Type": "text/plain"}, {"Origin": "http://192.0.2.1"}])
def test_other_sites_cant_send(served, headers):
    req, asked, _ = served
    for p, b in (("/api/set", {"scope": "chat", "changes": {}}), ("/api/login", {"k": "k" * 43}), ("/api/logout", {})):
        assert req("POST", p, b, headers=headers)[0] in (403, 415, 421)
    assert not asked


@pytest.mark.parametrize("cookie", ["", "short", "s" * 44, "s" * 42 + "!", "a" * 43 + "; __Host-ampi=" + "b" * 43])
def test_bad_cookies(served, cookie):
    req, asked, _ = served
    assert req("GET", "/api/overview", cookie=cookie)[0] == 401 and not asked


def test_requests_carry_the_session_and_nothing_else_of_the_caller(served):
    req, asked, answers = served
    assert req("POST", "/api/set", {"scope": "chat", "changes": {"a": ["b", "c"]}, "cmd": "run", "session": "z"})[0] == 200
    assert asked == [{"cmd": "hub.set", "session": "s" * 43, "scope": "chat", "changes": {"a": ["b", "c"]}}]
    assert req("GET", "/api/abilities?scope=watcher%3Ap%2Fn")[0] == 200 and asked[-1]["scope"] == "watcher:p/n"
    answers["hub.overview"] = {"ok": False, "error": "signed out: x"}
    assert req("GET", "/api/overview")[0] == 401
    assert req("POST", "/api/set", {"x": "y" * 20000})[0] == 413
    assert "Max-Age=0" in req("POST", "/api/logout", {})[2].getheader("Set-Cookie")


def test_the_owner_hears_of_a_certificate_that_isnt_renewed(monkeypatch):
    told, n = [], [0]

    async def sleep(_):
        n[0] += 1
        if n[0] > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        run(watch_cert(URL, told.append, left=lambda h: 3600))
    assert len(told) == 1 and "1 hours" in told[0]
