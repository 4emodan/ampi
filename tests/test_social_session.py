"""Instagram/Threads through the owner's web session (meta_session.py, ops_social_session.py): the allow-list,
pauses, budgets, redaction, origin gating, approvals with re-checks, the DM watcher and social mode in the bridge.
Everything runs against fakes (httpx.MockTransport); nothing here can reach instagram.com or threads.com."""

import asyncio
import base64
import copy
import dataclasses
import json
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_bot import CHAT, FakeAPI, FakeRunner, msg  # noqa: F401
from test_watch_engine import FakeClaude, real_sandbox

from ampi_tools import client as cli
from ampi_tools import meta_session as msess
from ampi_tools import social_dm
from ampi_tools import watch_spec as ws
from ampi_tools.keeper import Keeper
from ampi_tools.meta import redact
from ampi_tools.meta_session import (
    DOCS, ENDPOINTS, IG, IG_DOCS, TH, TH_DOCS, Budget, SessionAuth, WebClient, allowed, shortcode_to_pk, store_session,
)
from ampi_tools.ops import Approvals, Policy, Services
from ampi_tools.ops_social_session import DM_REFUSAL, OTHER_REFUSAL, PRIVATE_REFUSAL, WRITE_REFUSAL, check_origin
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError
from assistant_bridge.bot import ProfileBot
from assistant_bridge.config import Profile
from assistant_bridge.store import Store
from assistant_bridge.watch.pipeline import Active, Pipeline, RunState
from assistant_bridge.watch.pipeline import Services as PipeServices

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = 4242
OWNER_ID = social_dm.FIXTURE_OWNER
IG_COOKIES = {"sessionid": "IGSESSION_secret_0001", "csrftoken": "IGCSRF_secret_00001", "ds_user_id": OWNER_ID}
TH_COOKIES = {"sessionid": "THSESSION_secret_001", "csrftoken": "THCSRF_secret_00001", "ds_user_id": OWNER_ID}
MEDIA_CODE, OWN_CODE = "DAbcdEFGhij", "DOwnPostABC"
MEDIA_PK, OWN_PK = shortcode_to_pk(MEDIA_CODE), shortcode_to_pk(OWN_CODE)
TH_CODE = "C1threadsXY"
TH_PK = shortcode_to_pk(TH_CODE)
DTSG, LSD = "DTSG_secret_token_01", "LSD_secret_tok_01"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def known_docs(monkeypatch):
    """The real tables hold placeholders that are never sent; the fakes answer to made-up "known" ids."""
    for i, (table, key) in enumerate([(TH_DOCS, k) for k in TH_DOCS] + [(IG_DOCS, k) for k in IG_DOCS]):
        monkeypatch.setitem(table, key, dataclasses.replace(table[key], doc_id=str(1000000000000001 + i),
                                                            status="known"))


def media(pk, code, owner, caption):
    return {"pk": pk, "id": f"{pk}_{owner[0]}", "code": code, "media_type": 1, "taken_at": 1763700000,
            "user": {"pk": owner[0], "username": owner[1]}, "caption": {"text": caption}, "like_count": 3,
            "comment_count": 2}


class FakeMeta:
    """instagram.com and threads.com as far as these tests need them."""

    def __init__(self):
        self.calls = []
        self.override = {}  # (method, path) or (host, method, path) -> httpx.Response (or a callable)
        self.inbox = copy.deepcopy(social_dm.FIXTURE_INBOX)
        self.more_comments = []  # a second page of comments
        self.children = {}  # comment id -> replies
        self.th_page = None  # what threads.com's page says instead of the logged-in page
        self.media = {MEDIA_PK: media(MEDIA_PK, MEDIA_CODE, ("2002", "bob"), "Sunset at the beach"),
                      OWN_PK: media(OWN_PK, OWN_CODE, (OWNER_ID, "me_ig"), "My own post")}
        self.comments = [{"pk": "5001", "text": "Lovely!", "user": {"pk": "2001", "username": "alice"},
                          "created_at": 1763700100},
                         {"pk": "5002", "text": "thanks", "user": {"pk": OWNER_ID, "username": "me_ig"},
                          "created_at": 1763700200}]
        self.th_post = {"pk": TH_PK, "code": TH_CODE, "user": {"pk": "3002", "username": "carol"},
                        "caption": {"text": "hello threads"}, "like_count": 5, "taken_at": 1763700000,
                        "text_post_app_info": {"direct_reply_count": 1}}
        self.th_reply = {"pk": "777", "code": "C1reply0001", "user": {"pk": "3003", "username": "dave"},
                         "caption": {"text": "nice one"}, "taken_at": 1763700500}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        form = dict(urllib.parse.parse_qsl(request.content.decode())) if request.content else {}
        self.calls.append(SimpleNamespace(method=request.method, host=request.url.host, path=request.url.path,
                                          query=q, headers=dict(request.headers), form=form))
        hit = self.override.get((request.url.host, request.method, request.url.path)) or \
            self.override.get((request.method, request.url.path))
        if hit is not None:
            return hit(request) if callable(hit) else hit
        if request.url.host == "www.instagram.com":
            return self.instagram(request.method, request.url.path, q, form)
        if request.url.host == "www.threads.com":
            return self.threads(request.method, request.url.path, form)
        return httpx.Response(599)

    def instagram(self, method, path, q, form):
        ok = {"status": "ok"}
        if path == f"/api/v1/users/{OWNER_ID}/info/":
            return httpx.Response(200, json={"user": {"pk": int(OWNER_ID), "username": "me_ig"}, **ok})
        if path == "/api/v1/users/web_profile_info/":
            if q.get("username") == "bob":
                return httpx.Response(200, json={"data": {"user": {
                    "id": "2002", "username": "bob", "full_name": "Bob B", "biography": "I love cats",
                    "edge_followed_by": {"count": 10}, "edge_follow": {"count": 5},
                    "edge_owner_to_timeline_media": {"count": 2}, "is_private": False,
                    "external_url": "https://bob.example"}}, **ok})
            return httpx.Response(404, json={"status": "fail", "message": "User not found"})
        if method == "GET" and path == "/":
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        if method == "POST" and path == "/graphql/query":
            ad = {"pk": "9", "code": "ADcode00001", "ad_id": "1", "user": {"pk": "1", "username": "brand"}}
            return httpx.Response(200, json={"data": {"xdt_api__v1__feed__timeline__connection": {"edges": [
                {"node": {"media": self.media[MEDIA_PK]}}, {"node": {"media": ad}}]}}})
        if path == "/api/v1/feed/user/2002/":
            return httpx.Response(200, json={"items": [self.media[MEDIA_PK]], **ok})
        for pk, m in self.media.items():
            if path == f"/api/v1/media/{pk}/info/":
                return httpx.Response(200, json={"items": [m], **ok})
            if path == f"/api/v1/media/{pk}/comments/":
                if q.get("min_id") == "page2":
                    return httpx.Response(200, json={"comments": self.more_comments, **ok})
                return httpx.Response(200, json={"comments": self.comments, "has_more_comments": bool(
                    self.more_comments), "next_min_id": "page2" if self.more_comments else None, **ok})
            for cid, kids in self.children.items():
                if path == f"/api/v1/media/{pk}/comments/{cid}/child_comments/":
                    return httpx.Response(200, json={"child_comments": kids, **ok})
        if path == "/api/v1/feed/reels_tray/":
            return httpx.Response(200, json={"tray": [{"id": 2002, "user": {"pk": 2002, "username": "bob"},
                                                       "media_count": 2, "latest_reel_media": 1763700000}], **ok})
        if path == "/api/v1/feed/reels_media/":
            return httpx.Response(200, json={"reels": {q.get("reel_ids"): {"items": [
                {"pk": "1", "taken_at": 1763700000, "media_type": 1, "accessibility_caption": "Photo of a cat",
                 "story_link_stickers": [{"story_link": {"url": "https://evil.example/x"}}]}]}}, **ok})
        if path == "/api/v1/direct_v2/inbox/":
            return httpx.Response(200, json=self.inbox)
        if path.startswith("/api/v1/direct_v2/threads/"):
            return httpx.Response(200, json=social_dm.FIXTURE_THREAD)
        if method == "POST" and path.endswith("/add/"):
            return httpx.Response(200, json={"id": "5003", "text": form.get("comment_text"), **ok})
        if method == "POST":
            return httpx.Response(200, json={"result": "following", **ok})
        return httpx.Response(404, json={"status": "fail", "message": "not found"})

    def threads(self, method, path, form):
        if method == "GET" and path == "/":
            return httpx.Response(200, text=self.th_page or PAGE, headers={"content-type": "text/html"})
        if method == "POST" and path == "/graphql/query":
            doc = next((k for k, d in TH_DOCS.items() if d.doc_id == form.get("doc_id")), None)
            v = json.loads(form.get("variables") or "{}")
            carol = {"pk": "3002", "username": "carol", "full_name": "Carol C", "biography": "threads fan",
                     "follower_count": 7}
            edges = {"edges": [{"node": {"thread_items": [{"post": self.th_post}, {"post": self.th_reply}]}}]}
            data = {"profile_by_name": {"user": carol if v.get("username") == "carol" else None},
                    "profile": {"user": {"pk": OWNER_ID, "username": "me_th"} if v.get("userID") == OWNER_ID
                                else carol},
                    "posts": {"mediaData": {"edges": [{"node": {"thread_items": [{"post": self.th_post}]}}]}},
                    "post": {"data": edges}, "feed": {"feedData": edges},
                    }.get(doc, {"xdt_mutation": {"status": "ok"}})
            return httpx.Response(200, text="for (;;);" + json.dumps({"data": data}))
        return httpx.Response(404)

    def paths(self):
        return [c.path for c in self.calls]


PAGE = ('<html><script>["DTSGInitialData",[],{"token":"%s"},258]["LSD",[],{"token":"%s"},323]'
        '</script></html>' % (DTSG, LSD))


async def trusted(pid):
    return pid == BRIDGE


class FakeVaultSide:
    """Just enough of ops_checkout.CheckoutSide for the vault warning."""

    def mask_any(self, obj, level="all"):
        return obj.replace("12 Secret Street", "[vault]") if isinstance(obj, str) else obj


def session_file(path, cookies, username, **extra):
    path.write_text(json.dumps({"cookies": cookies, "user_id": OWNER_ID, "username": username,
                                "saved_at": time.time(), "state": "ok", **extra}))


@pytest.fixture
def web(tmp_path):
    def make(origin="forwarded:social", policy=Policy(), vault=False):
        fake = FakeMeta()
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        notices = []

        def mk(service, cookies, username):
            path = tmp_path / f"{service.key}-session.json"
            session_file(path, cookies, username)
            return WebClient(service, SessionAuth(service, path, notices.append), client, gap=0)

        ig, th = mk(IG, IG_COOKIES, "me_ig"), mk(TH, TH_COOKIES, "me_th")
        services = Services(igweb_client=ig, thweb_client=th, checkout_side=FakeVaultSide() if vault else None)
        daemon = Daemon(services, Approvals(tmp_path / "a.sqlite3"), policy, trusted,
                        keeper=Keeper(tmp_path / "w.sqlite3"))
        daemon.runs.origin_of = lambda pid: pid == BRIDGE and "unknown" or w.origin
        w = SimpleNamespace(daemon=daemon, fake=fake, notices=notices, ig=ig, th=th, origin=origin)
        return w

    return make


def ask(w, *ops, pid=1):
    return run(w.daemon.handle({"cmd": "run", "ops": [{"op": o, "args": a} for o, a in ops]}, pid))


def approve(w, yes=True):
    (item,) = run(w.daemon.handle({"cmd": "approvals.pending"}, BRIDGE))["items"]
    run(w.daemon.handle({"cmd": "approvals.notified", "id": item["id"], "message_id": 5}, BRIDGE))
    return run(w.daemon.handle({"cmd": "approvals.decide", "id": item["id"], "approve": yes, "nonce": item["nonce"],
                                "message_id": 5}, BRIDGE))


# --- the allow-list -----------------------------------------------------------------------------------------


def test_every_listed_endpoint_is_allowed_exactly_as_listed():
    for name, ep in ENDPOINTS.items():
        path = ep.path.replace("{user}", "123").replace("{media}", "456").replace("{comment}", "789") \
            .replace("{thread}", "340282366841710300949128100000000001")
        if ep.kind == "graphql":
            for d in DOCS[ep.service].values():
                assert allowed(ep.method, ep.host, path, d.doc_id, d.name) == name
        else:
            assert allowed(ep.method, ep.host, path) == name
        assert not any(b in ep.path for b in ("/accounts/", "/challenge/", "seen"))


@pytest.mark.parametrize("method,host,path", [
    ("GET", "www.instagram.com", "/accounts/edit/"),
    ("GET", "www.instagram.com", "/api/v1/accounts/current_user/"),
    ("POST", "www.instagram.com", "/api/v1/web/accounts/edit/"),
    ("GET", "www.instagram.com", "/challenge/"),
    ("POST", "www.instagram.com", "/api/v1/challenge/123/"),
    ("POST", "www.instagram.com", "/api/v1/feed/timeline/"),           # the mobile app's feed
    ("GET", "www.instagram.com", "/api/v1/feed/timeline/"),
    ("POST", "www.instagram.com", "/api/v1/web/friendships/1/follow/"),  # not the endpoint in use
    ("GET", "www.instagram.com", "/api/v1/web/likes/1/like/"),         # wrong method
    ("GET", "i.instagram.com", "/api/v1/feed/timeline/"),              # the mobile API host
    ("GET", "www.instagram.com", "/api/v1/media/seen/"),
    ("POST", "www.instagram.com", "/api/v1/direct_v2/threads/1/items/2/seen/"),
    ("POST", "www.instagram.com", "/api/v1/direct_v2/threads/broadcast/text/"),  # DM sending: not in v1
    ("GET", "www.instagram.com", "/api/v1/media/abc/info/"),           # ids are numbers only
    ("GET", "www.threads.com", "/api/v1/feed/timeline/"),
])
def test_requests_outside_the_list_are_refused(method, host, path):
    assert allowed(method, host, path) is None


def test_graphql_needs_a_listed_doc_with_its_name():
    d = TH_DOCS["feed"]
    assert allowed("POST", "www.threads.com", "/graphql/query", d.doc_id, d.name) == "th.graphql"
    assert allowed("POST", "www.threads.com", "/graphql/query", "123456", d.name) is None
    assert allowed("POST", "www.threads.com", "/graphql/query", d.doc_id, "SomethingElseQuery") is None
    assert allowed("POST", "www.threads.com", "/graphql/query") is None
    assert allowed("GET", "www.instagram.com", "/api/v1/feed/timeline/", d.doc_id, d.name) is None


def test_the_client_refuses_unlisted_names_ids_and_queries(web):
    w = web()
    with pytest.raises(ToolError, match="unlisted"):
        run(w.ig.call("th.home"))
    with pytest.raises(ToolError, match="bad Instagram id"):
        run(w.ig.call("ig.like", ids={"media": "../../accounts/edit"}))
    with pytest.raises(ToolError, match="unlisted"):
        run(w.th.call("th.graphql", doc="change_password"))
    assert w.fake.calls == []


def test_headers_cookies_and_user_agent(web):
    w = web()
    assert ask(w, ("igweb.feed", {}))["ok"]
    assert ask(w, ("thweb.feed", {}))["ok"]
    for c in w.fake.calls:
        assert c.headers["user-agent"] == msess.USER_AGENT
        assert c.host in ("www.instagram.com", "www.threads.com")
        if c.host == "www.instagram.com":
            assert "sessionid=IGSESSION_secret_0001" in c.headers["cookie"] and "THSESSION" not in c.headers["cookie"]
            if c.path != "/":
                assert c.headers["x-ig-app-id"] == msess.IG_APP_ID and c.headers["x-csrftoken"] == "IGCSRF_secret_00001"
        else:
            assert "sessionid=THSESSION_secret_001" in c.headers["cookie"] and "IGSESSION" not in c.headers["cookie"]
    gql = next(c for c in w.fake.calls if c.path == "/graphql/query" and c.host == "www.threads.com")
    assert gql.form["doc_id"] == TH_DOCS["feed"].doc_id
    assert gql.form["fb_api_req_friendly_name"] == gql.headers["x-fb-friendly-name"] == TH_DOCS["feed"].name
    assert gql.form["fb_dtsg"] == DTSG and gql.form["lsd"] == gql.headers["x-fb-lsd"] == LSD
    assert gql.form["jazoest"] == "2" + str(sum(map(ord, DTSG)))


def test_page_tokens_are_fetched_once_then_reused(web):
    w = web()
    ask(w, ("thweb.feed", {}))
    ask(w, ("thweb.profile", {"user": "carol"}))
    assert w.fake.paths().count("/") == 1


def test_a_changed_graphql_doc_says_so_and_never_guesses(web):
    w = web()
    w.fake.override[("www.threads.com", "POST", "/graphql/query")] = httpx.Response(
        200, json={"errors": [{"message": "PersistedQueryNotFound"}]})
    resp = ask(w, ("thweb.feed", {}))
    assert not resp["ok"] and "Threads changed its web app; the `BarcelonaFeedTimelineQuery` query needs updating" \
        in resp["error"]


def test_fixed_gap_between_requests(tmp_path):
    fake = FakeMeta()
    waits = []

    async def sleep(s):
        waits.append(s)

    path = tmp_path / "ig.json"
    session_file(path, IG_COOKIES, "me_ig")
    c = WebClient(IG, SessionAuth(IG, path), httpx.AsyncClient(transport=httpx.MockTransport(fake)), gap=1.0,
                  sleep=sleep, clock=lambda: 100.0)
    run(c.call("ig.reels_tray"))
    run(c.call("ig.reels_tray"))
    assert waits == [1.0]  # exactly the fixed gap, no jitter


# --- pauses ----------------------------------------------------------------------------------------------------


def test_a_redirect_pauses_the_session_for_everyone_with_one_notice(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(
        302, headers={"location": "https://www.instagram.com/accounts/login/"})
    resp = ask(w, ("igweb.stories", {}))
    assert not resp["ok"] and IG.paused_text in resp["error"]
    n = len(w.fake.calls)
    resp = ask(w, ("igweb.feed", {}))
    assert not resp["ok"] and IG.paused_text in resp["error"]
    assert len(w.fake.calls) == n  # nothing more is sent
    assert len(w.notices) == 1 and "instagram-session --resume" in w.notices[0]
    assert json.loads(w.ig.auth.path.read_text())["state"] == "paused"
    # The DM watcher stops quietly (the owner already got the notice).
    out = run(msess_inbox(w))
    assert out.get("stop") is True and "paused" in out["text"]
    assert not any(c.path == "/api/v1/direct_v2/inbox/" for c in w.fake.calls)
    # Threads is a separate session and keeps working.
    assert ask(w, ("thweb.feed", {}))["ok"]
    w.ig.auth.resume()
    w.fake.override.clear()
    assert ask(w, ("igweb.feed", {}))["ok"]


async def msess_inbox(w):
    from ampi_tools.ops_social_session import ig_inbox
    return await ig_inbox(w.daemon.services, {})


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"message": "challenge_required", "challenge": {"url": "x"}, "status": "fail"}),
    httpx.Response(400, json={"message": "checkpoint_required", "checkpoint_url": "x", "status": "fail"}),
    httpx.Response(403, json={"message": "login_required", "status": "fail"}),
    httpx.Response(401, json={"status": "fail"}),
    httpx.Response(403, text="Forbidden"),
    httpx.Response(200, text="<html><a href='/accounts/login/'>Log in</a></html>"),
    httpx.Response(200, json={"items": []}, headers={"set-cookie": 'sessionid=""; Max-Age=0; Path=/'}),
])
def test_challenges_and_logouts_pause(web, response):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/user/2002/")] = response
    resp = ask(w, ("igweb.posts", {"user": "bob"}))
    assert not resp["ok"] and IG.paused_text in resp["error"]
    assert w.ig.auth.state() == "paused" and len(w.notices) == 1


def test_threads_login_errors_pause(web):
    w = web()
    w.fake.override[("www.threads.com", "POST", "/graphql/query")] = httpx.Response(
        200, text='for (;;);{"error":1357001,"errorSummary":"Not logged in"}')
    resp = ask(w, ("thweb.feed", {}))
    assert not resp["ok"] and TH.paused_text in resp["error"] and w.th.auth.state() == "paused"


def test_a_refused_user_agent_stops_instead_of_pretending(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(
        400, json={"message": "useragent mismatch", "status": "fail"})
    resp = ask(w, ("igweb.stories", {}))
    assert not resp["ok"] and "refuse non-browser clients" in resp["error"]
    assert w.ig.auth.load()["paused_reason"] == "user_agent"
    assert all(c.headers["user-agent"] == msess.USER_AGENT for c in w.fake.calls)


def test_429_on_a_read_cools_down_for_an_hour(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(429, json={"status": "fail"})
    resp = ask(w, ("igweb.stories", {}))
    assert not resp["ok"] and "slow down" in resp["error"]
    assert w.ig.auth.state() == "cooldown" and len(w.notices) == 1
    n = len(w.fake.calls)
    assert not ask(w, ("igweb.feed", {}))["ok"] and len(w.fake.calls) == n


def test_feedback_required_on_a_write_pauses_until_resumed(web):
    w = web()
    resp = ask(w, ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "text": "Nice!"}))
    assert resp["approval"]
    w.fake.override[("POST", f"/api/v1/web/comments/{MEDIA_PK}/add/")] = httpx.Response(
        400, json={"message": "feedback_required", "spam": True, "status": "fail"})
    out = approve(w)
    assert out["status"] == "failed" and IG.paused_text in out["text"]
    assert w.ig.auth.state() == "paused"


# --- secrets ---------------------------------------------------------------------------------------------------


def test_rotated_cookies_are_saved_registered_and_used(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(
        200, json={"tray": []}, headers={"set-cookie": "csrftoken=ROTATEDcsrf_value_77; Path=/; Secure"})
    ask(w, ("igweb.stories", {}))
    assert w.ig.auth.load()["cookies"]["csrftoken"] == "ROTATEDcsrf_value_77"
    assert "ROTATED" not in redact("token ROTATEDcsrf_value_77 here")
    w.fake.override.clear()
    ask(w, ("igweb.feed", {}))
    assert w.fake.calls[-1].headers["x-csrftoken"] == "ROTATEDcsrf_value_77"


def test_session_values_are_redacted_even_unregistered():
    for text in ("sessionid=abc123unregistered; csrftoken=zzzUNREG", "X-CSRFToken: zzzUNREG",
                 '{"fb_dtsg": "NAqUNREG:1", "lsd": "unregLSD"}', "jazoest=2999UNREG&ds_user_id=99887766",
                 "X-FB-LSD: unregLSD", "rur=\"NCG\\05499887766\""):
        out = redact(text)
        assert "UNREG" not in out and "unreg" not in out and "99887766" not in out, out


def test_no_cookie_or_token_reaches_replies_or_errors(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = lambda r: (_ for _ in ()).throw(
        RuntimeError(f"boom {r.headers['cookie']}"))
    resp = ask(w, ("igweb.stories", {}))
    assert resp["error"].endswith("Instagram unreachable (RuntimeError)")
    w.fake.override.clear()
    for op, a in (("igweb.feed", {}), ("thweb.feed", {}), ("igweb.profile", {"user": "bob"})):
        out = json.dumps(ask(w, (op, a)))
        for secret in (*IG_COOKIES.values(), *TH_COOKIES.values(), DTSG, LSD):
            if len(secret) >= 8:
                assert secret not in out


# --- budgets ---------------------------------------------------------------------------------------------------


def test_budgets_per_hour_and_day(tmp_path):
    now = [1_000_000.0]
    b = Budget(tmp_path / "usage.json", clock=lambda: now[0])
    for _ in range(120):
        b.take("read", False, "Instagram")
    with pytest.raises(ToolError, match="120 per hour"):
        b.take("read", False, "Instagram")
    for _ in range(30):
        b.take("read", True, "Instagram")  # watchers have their own hourly budget
    with pytest.raises(ToolError, match="watcher reads"):
        b.take("read", True, "Instagram")
    for _ in range(20):
        b.take("follow", False, "Instagram")
    with pytest.raises(ToolError, match="per hour"):
        b.take("write", False, "Instagram")
    now[0] += 3601
    with pytest.raises(ToolError, match="follows are used up|follows"):
        b.take("follow", False, "Instagram")  # 20 follows a day
    for _ in range(40):
        b.take("write", False, "Instagram")
        if len(b.hits["write"]) % 20 == 0:
            now[0] += 3601
    with pytest.raises(ToolError, match="60 per day"):
        b.take("write", False, "Instagram")
    # Kept across restarts.
    assert len(Budget(tmp_path / "usage.json", clock=lambda: now[0]).hits["write"]) == 60


def test_reads_per_day_include_watchers():
    now = 1_000_000.0
    b = Budget(None, clock=lambda: now)
    b.hits["read"] = [now - 7200] * 300  # older than an hour: the hourly caps are free
    b.hits["watch"] = [now - 7200] * 300
    for watch in (False, True):
        with pytest.raises(ToolError, match="600 a day"):
            b.take("read", watch, "Threads")
    b.take("write", False, "Threads")  # changes have their own budget


def test_quiet_hours():
    from datetime import datetime
    from ampi_tools.ops_social_session import quiet_at
    assert quiet_at("23:00-08:00", datetime(2026, 11, 22, 23, 30)) and quiet_at("23:00-08:00", datetime(2026, 11, 22, 7))
    assert not quiet_at("23:00-08:00", datetime(2026, 11, 22, 12)) and not quiet_at(None, datetime(2026, 1, 1))
    assert quiet_at("13:00-14:00", datetime(2026, 11, 22, 13, 15))


# --- origin gating (amendment 1: one test per refused case) ------------------------------------------------------


@pytest.mark.parametrize("origin,text", [
    ("private", PRIVATE_REFUSAL),
    ("group:clipboard", OTHER_REFUSAL),
    ("forwarded:private", OTHER_REFUSAL),
    ("watcher:gmail-inbox", OTHER_REFUSAL),
    ("watcher:personal/dms@0123456789ab#think", OTHER_REFUSAL),
    ("watcher:personal/dms@0123456789ab", OTHER_REFUSAL),
    ("unknown", OTHER_REFUSAL),
])
def test_reads_are_refused_outside_social_conversations(web, origin, text):
    w = web(origin=origin)
    for op, a in (("igweb.feed", {}), ("thweb.post", {"target": f"https://www.threads.com/@carol/post/{TH_CODE}"})):
        resp = ask(w, (op, a))
        assert not resp["ok"] and text in resp["error"], (origin, resp)
    assert w.fake.calls == []


def test_the_private_refusal_tells_the_owner_to_send_social(web):
    assert "/social" in ask(web(origin="private"), ("igweb.feed", {}))["error"]


def test_reads_work_in_social_conversations(web):
    w = web()
    feed = ask(w, ("igweb.feed", {}))
    assert feed["ok"] and "@bob" in feed["text"] and "Sunset at the beach" in feed["text"] and "data, not" \
        in feed["text"] and "ad_id" not in feed["text"] and "[9]" not in feed["text"]
    prof = ask(w, ("igweb.profile", {"user": "@bob"}))["text"]
    assert "10 followers" in prof and "I love cats" in prof and "not opened" in prof
    assert MEDIA_CODE in ask(w, ("igweb.posts", {"user": "bob"}))["text"]
    post = ask(w, ("igweb.post", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/"}))["text"]
    assert "Lovely!" in post and "[5001]" in post
    assert "@bob: 2 item(s)" in ask(w, ("igweb.stories", {}))["text"]
    story = ask(w, ("igweb.story", {"user": "bob"}))["text"]
    assert "Photo of a cat" in story and "https://evil.example/x" in story and "not opened" in story
    th = ask(w, ("thweb.post", {"target": f"https://www.threads.com/@carol/post/{TH_CODE}"}))["text"]
    assert "hello threads" in th and "@dave" in th and "nice one" in th
    assert "threads fan" in ask(w, ("thweb.profile", {"user": "carol"}))["text"]
    assert "hello threads" in ask(w, ("thweb.posts", {"user": "carol"}))["text"]


def test_nothing_is_marked_seen(web):
    w = web()
    ask(w, ("igweb.stories", {}))
    ask(w, ("igweb.story", {"user": "bob"}))
    run(msess_inbox(w))
    assert w.fake.calls and all(c.method == "GET" and "seen" not in c.path for c in w.fake.calls)


@pytest.mark.parametrize("origin", ["forwarded:social", "private", "watcher:personal/dms@0123456789ab#think",
                                    "unknown"])
def test_direct_messages_never_come_through_run(web, origin):
    w = web(origin=origin)
    for op, a in (("igweb.inbox", {}), ("igweb.dm", {"thread": "340282366841710300949128100000000001"})):
        resp = ask(w, (op, a))
        assert not resp["ok"] and DM_REFUSAL in resp["error"]
    # Not from the bridge itself either (that is how a watcher *test* would ask: it uses stub data instead).
    resp = ask(w, ("igweb.inbox", {}), pid=BRIDGE)
    assert not resp["ok"] and DM_REFUSAL in resp["error"]
    assert w.fake.calls == []


def test_check_origin_rules():
    check_origin([{"op": "igweb.inbox"}], "watcher:personal/dms@0123456789ab", "watch.run")
    check_origin([{"op": "igweb.feed"}], "forwarded:social", "run")
    with pytest.raises(ToolError):
        check_origin([{"op": "igweb.feed"}], "watcher:personal/dms@0123456789ab", "watch.run")
    with pytest.raises(ToolError):
        check_origin([{"op": "igweb.feed"}], "forwarded:social", "watch.run")


def test_watchers_can_declare_dm_reads_only():
    def spec(op):
        toml = (f'name = "w"\ndescription = "d"\n[[triggers]]\ntype = "manual"\n[[steps]]\nid = "a"\n'
                f'type = "adapter"\nop = "{op}"\n')
        return ws.parse({"watcher.toml": toml.encode()})
    assert spec("igweb.inbox")
    for op in ("igweb.feed", "thweb.post", "igweb.comment", "igweb.like", "thweb.follow"):
        with pytest.raises(ws.SpecError, match="isn't available to watchers"):
            spec(op)


# --- writes: always asked, exactly described, checked again ------------------------------------------------------


def test_comment_asks_with_exact_text_and_extras_then_posts(web):
    w = web(vault=True)
    text = "Great shot @alice! Come to 12 Secret Street"
    resp = ask(w, ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "text": text,
                                     "comment": "5001"}))
    assert resp["ok"] and resp["approval"]
    summary = resp["text"]
    assert "⚠ Asked for by a social-mode conversation" in summary
    assert f"│ {text}" in summary and "will notify: @alice" in summary
    assert "@bob's post “Sunset at the beach”" in summary and "reply to @alice's comment “Lovely!”" in summary
    assert "⚠ the text contains something stored in your personal info" in summary
    assert not any(c.method == "POST" for c in w.fake.calls)
    out = approve(w)
    assert out["status"] == "done", out
    post = next(c for c in w.fake.calls if c.method == "POST")
    assert post.path == f"/api/v1/web/comments/{MEDIA_PK}/add/"
    assert post.form == {"comment_text": text, "replied_to_comment_id": "5001"}
    assert post.headers["x-csrftoken"] == IG_COOKIES["csrftoken"] and post.headers["origin"] == \
        "https://www.instagram.com"


def test_comment_text_is_checked(web):
    resp = ask(web(), ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/",
                                         "text": "hi​there"}))
    assert not resp["ok"] and "invisible" in resp["error"]


def test_comment_is_not_posted_when_the_post_changed(web):
    w = web()
    assert ask(w, ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "text": "Nice"}))["approval"]
    w.fake.media[MEDIA_PK]["caption"]["text"] = "Now a scam link"
    out = approve(w)
    assert out["status"] == "failed" and "changed" in out["text"]
    assert not any(c.method == "POST" for c in w.fake.calls)


def test_reply_is_not_posted_when_the_parent_comment_changed(web):
    w = web()
    ask(w, ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "text": "Nice",
                              "comment": "5001"}))
    w.fake.comments[0]["text"] = "edited"
    out = approve(w)
    assert out["status"] == "failed" and "comment changed" in out["text"]
    assert not any(c.method == "POST" for c in w.fake.calls)


def test_likes_always_ask_even_if_the_config_relaxes_them(web):
    w = web(policy=Policy(auto_approve=frozenset({"igweb.like", "thweb.like"})))
    resp = ask(w, ("igweb.like", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/"}))
    assert resp["approval"] and "Like (publicly visible) @bob's Instagram post" in resp["text"]
    assert approve(w)["status"] == "done"
    assert w.fake.calls[-1].path == f"/api/v1/web/likes/{MEDIA_PK}/like/"
    resp = ask(w, ("thweb.like", {"target": f"https://www.threads.com/@carol/post/{TH_CODE}"}))
    assert resp["approval"]


def test_like_comment_and_unlike(web):
    w = web()
    resp = ask(w, ("igweb.like", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "comment": "5001"}))
    assert "@alice's comment “Lovely!”" in resp["text"]
    assert approve(w)["status"] == "done" and w.fake.calls[-1].path == "/api/v1/web/comments/like/5001/"
    ask(w, ("igweb.unlike", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/"}))
    assert approve(w)["status"] == "done" and w.fake.calls[-1].path == f"/api/v1/web/likes/{MEDIA_PK}/unlike/"


def test_follow_rechecks_the_account(web):
    w = web()
    resp = ask(w, ("igweb.follow", {"user": "bob"}))
    assert "FOLLOW @bob (Bob B) [2002] on Instagram" in resp["text"]
    real = w.fake.instagram

    def swapped(method, path, q, form):
        r = real(method, path, q, form)
        if path == "/api/v1/users/web_profile_info/":
            data = json.loads(r.content)
            data["data"]["user"]["id"] = "6666"
            return httpx.Response(200, json=data)
        return r

    w.fake.instagram = swapped
    out = approve(w)
    assert out["status"] == "failed" and "account changed" in out["text"]
    assert not any(c.method == "POST" for c in w.fake.calls)


def test_follow_threads(web):
    w = web()
    resp = ask(w, ("thweb.follow", {"user": "carol"}))
    assert "FOLLOW @carol (Carol C) [3002] on Threads" in resp["text"]
    assert approve(w)["status"] == "done"
    last = w.fake.calls[-1]
    assert last.form["doc_id"] == TH_DOCS["follow"].doc_id and json.loads(last.form["variables"]) == \
        {"target_user_id": "3002"}


def test_delete_comment_only_own_or_on_own_posts(web):
    w = web()
    resp = ask(w, ("igweb.delete_comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "comment": "5001"}))
    assert not resp["ok"] and "only the owner's own comments" in resp["error"]
    resp = ask(w, ("igweb.delete_comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "comment": "5002"}))
    assert resp["approval"] and "DELETE @me_ig's comment “thanks”" in resp["text"]
    assert approve(w)["status"] == "done"
    assert w.fake.calls[-1].path == f"/api/v1/web/comments/{MEDIA_PK}/delete/5002/"
    resp = ask(w, ("igweb.delete_comment", {"target": f"https://www.instagram.com/p/{OWN_CODE}/", "comment": "5001"}))
    assert resp["approval"]  # someone else's comment on the owner's own post


def test_rejected_writes_do_nothing(web):
    w = web()
    ask(w, ("thweb.unfollow", {"user": "carol"}))
    n = len(w.fake.calls)
    assert approve(w, yes=False)["status"] == "rejected"
    assert len(w.fake.calls) == n


def test_web_writes_are_not_for_watchers(web):
    w = web(origin="watcher:personal/dms@0123456789ab#think")
    resp = ask(w, ("igweb.like", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/"}))
    assert not resp["ok"] and WRITE_REFUSAL in resp["error"]
    assert w.fake.calls == []


@pytest.mark.parametrize("origin", ["private", "group:clipboard", "forwarded:private", "watcher:gmail-inbox",
                                    "unknown"])
def test_writes_are_refused_outside_social_conversations(web, origin):
    # Their approval text quotes captions, comments and names, and goes back to the calling run.
    w = web(origin=origin)
    for op, a in (("igweb.like", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/"}),
                  ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "text": "hi"}),
                  ("thweb.follow", {"user": "carol"})):
        resp = ask(w, (op, a))
        assert not resp["ok"] and WRITE_REFUSAL in resp["error"]
    assert w.fake.calls == [] and run(w.daemon.handle({"cmd": "approvals.pending"}, BRIDGE))["items"] == []


# --- the session commands ---------------------------------------------------------------------------------------


def test_store_session_verifies_whose_session_it_is(tmp_path):
    fake = FakeMeta()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    data = run(store_session(tmp_path, IG, dict(IG_COOKIES), client))
    assert data["username"] == "me_ig" and data["user_id"] == OWNER_ID
    saved = json.loads((tmp_path / "instagram-session.json").read_text())
    assert saved["cookies"] == IG_COOKIES and saved["state"] == "ok"
    assert fake.paths() == [f"/api/v1/users/{OWNER_ID}/info/"]  # never accounts/current_user
    assert not list(tmp_path.glob("*.new"))
    if hasattr(__import__("os"), "geteuid"):
        assert (tmp_path / "instagram-session.json").stat().st_mode & 0o777 == 0o600


def test_store_session_keeps_nothing_when_the_check_fails(tmp_path):
    fake = FakeMeta()
    fake.override[("GET", f"/api/v1/users/{OWNER_ID}/info/")] = httpx.Response(
        200, json={"user": {"pk": 42, "username": "someone_else"}})
    with pytest.raises(ToolError, match="didn't confirm"):
        run(store_session(tmp_path, IG, dict(IG_COOKIES), httpx.AsyncClient(transport=httpx.MockTransport(fake))))
    assert not (tmp_path / "instagram-session.json").exists() and not list(tmp_path.glob("*.new"))


def test_store_session_threads(tmp_path):
    fake = FakeMeta()
    data = run(store_session(tmp_path, TH, dict(TH_COOKIES), httpx.AsyncClient(transport=httpx.MockTransport(fake))))
    assert data["username"] == "me_th"


def test_resume_forget_and_status(tmp_path):
    path = tmp_path / "instagram-session.json"
    session_file(path, IG_COOKIES, "me_ig", state="paused", paused_reason="challenge")
    auth = SessionAuth(IG, path)
    assert "PAUSED" in auth.status() and "--resume" in auth.status()
    msess.session_command(tmp_path, IG, resume=True)
    assert auth.state() == "ok" and "on, @me_ig" in auth.status()
    msess.session_command(tmp_path, IG, forget=True)
    assert not path.exists() and "not set up" in auth.status()


def test_cookie_values_are_checked():
    assert msess.check_cookie("sessionid", ' "abc%3A123" ') == "abc%3A123"
    for name, value in (("sessionid", "a b"), ("sessionid", "a;b"), ("ds_user_id", "12a"), ("csrftoken", "")):
        with pytest.raises(ToolError):
            msess.check_cookie(name, value)


def test_social_sessions_command(web, tmp_path):
    w = web()
    assert run(w.daemon.handle({"cmd": "social.sessions"}, 1)) == {"ok": True, "instagram": True, "threads": True}
    w.th.auth.forget()
    assert run(w.daemon.handle({"cmd": "social.sessions"}, 1))["threads"] is False


def test_shortcodes():
    assert msess.pk_to_shortcode(shortcode_to_pk(MEDIA_CODE)) == MEDIA_CODE
    with pytest.raises(ToolError):
        shortcode_to_pk("bad/code")


# --- the DM watcher --------------------------------------------------------------------------------------------


def dm_template_files():
    base = ROOT / "templates" / "watchers-v2" / "instagram-dms"
    files = {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    return files


def test_dm_template_is_valid_low_risk_and_polls_within_budget():
    spec = ws.parse(dm_template_files(), expected_name="instagram-dms")
    assert ws.risk(ws.manifest(spec))[0] == "low"
    every = spec.triggers[0]["minutes"]
    assert 60 / every <= 30
    notify = next(s for s in spec.steps if s["type"] == "notify")
    assert notify["format"] == "markdown"  # so _defang always runs on the messages' text
    llm = next(s for s in spec.steps if s["type"] == "llm")
    assert llm["mode"] == "structured"
    assert next(s for s in spec.steps if s["type"] == "adapter")["args"]["quiet"]


def test_inbox_parsing_leaves_out_requests_strangers_groups_and_seen():
    threads = social_dm.inbox_threads(social_dm.FIXTURE_INBOX, OWNER_ID)
    assert [t["with"] for t in threads] == [["alice"]]
    assert [i["text"] for i in threads[0]["items"]] == ["(photo or video)", "Are we still on for Saturday?"]
    req = {"inbox": {"threads": [{**social_dm.FIXTURE_INBOX["inbox"]["threads"][0], "pending": True}]}}
    assert social_dm.inbox_threads(req, OWNER_ID) == []


def test_dm_watcher_end_to_end(web, tmp_path, monkeypatch):
    from ampi_tools import ops_social_session
    monkeypatch.setattr(ops_social_session, "_in_quiet", lambda s, quiet: False)  # whatever the time of day
    w = web()
    files = dm_template_files()
    resp = run(w.daemon.handle({"cmd": "watch.propose", "profile": "personal", "name": "instagram-dms",
                                "files": {p: base64.b64encode(d).decode() for p, d in files.items()},
                                "request": "tell me about new DMs", "requester": "Ampi"}, BRIDGE))
    assert resp["ok"], resp
    item = next(i for i in run(w.daemon.handle({"cmd": "approvals.pending"}, BRIDGE))["items"]
                if i["id"] == resp["approval"])
    assert run(w.daemon.handle({"cmd": "approvals.decide", "id": item["id"], "nonce": item["nonce"], "message_id": None,
                                "choice": "all", "approve": True}, BRIDGE))["status"] == "done"
    spec = ws.parse(files)
    act = Active("personal", spec, "all", "low", "active")
    sent = []

    async def notify(text, fmt, name=None):
        sent.append((text, fmt))

    async def tools(payload):
        return await w.daemon.handle(payload, BRIDGE)

    claude = FakeClaude(['{"worth": "yes", "summary": "@alice asks about [Saturday](https://x.example)"}'])
    pipe = Pipeline(PipeServices(tools=tools, sandbox=real_sandbox, claude=claude, notify=notify,
                                 browse=lambda u: None, workspace=tmp_path),
                    {"structured": asyncio.Semaphore(1), "agent": asyncio.Semaphore(1)})

    def state(space):
        return RunState(run_id="20261122-120000-abcdef", test=False, space=space,
                        ctx={"item": {"time": "now"}, "steps": {}, "run": {"id": "r", "trigger": "time.every"},
                             "watcher": {"name": "instagram-dms"}})

    st = state({})
    assert run(pipe.run(act, st)) == "ok", st.log
    prompt, kw = claude.calls[0]
    assert "Are we still on for Saturday?" in prompt and "untrusted-" in prompt
    assert "promo_bot" not in prompt and "already seen" not in prompt
    assert kw["tools"] == "" and kw["origin"].endswith("#summary")
    (text, fmt), = sent
    assert fmt == "markdown" and "[Saturday]" not in text and "(Saturday)" in text
    inbox_calls = [c for c in w.fake.calls if c.path == "/api/v1/direct_v2/inbox/"]
    assert len(inbox_calls) == 1 and w.ig.budget.hits["watch"]
    # The same messages again: reported once.
    st2 = state(dict(st.space))
    assert run(pipe.run(act, st2)) == "stopped" and len(claude.calls) == 1 and len(sent) == 1
    # A new message in the same (still unread) conversation: only the new one is summarized.
    w.fake.inbox["inbox"]["threads"][0]["items"].insert(0, {"item_id": "a4", "user_id": 2001, "item_type": "text",
                                                           "timestamp": 1763710000000000, "text": "Hello??"})
    claude.answers.append('{"worth": "yes", "summary": "@alice again"}')
    st3 = state(dict(st2.space))
    assert run(pipe.run(act, st3)) == "ok"
    prompt = claude.calls[1][0]
    assert "Hello??" in prompt and "Are we still on" not in prompt


def test_dm_watcher_test_mode_uses_stub_data(tmp_path):
    calls = []

    async def tools(payload):
        calls.append(payload)
        return {"ok": True, "text": "x"}

    async def notify(text, fmt, name=None):
        raise AssertionError("a test sends nothing")

    claude = FakeClaude(['{"worth": "yes", "summary": "@alice asks about Saturday"}'])
    pipe = Pipeline(PipeServices(tools=tools, sandbox=real_sandbox, claude=claude, notify=notify,
                                 browse=lambda u: None, workspace=tmp_path),
                    {"structured": asyncio.Semaphore(1), "agent": asyncio.Semaphore(1)})
    spec = ws.parse(dm_template_files())
    st = RunState(run_id="r1", test=True, space={}, ctx={"item": {}, "steps": {}, "run": {"id": "r"},
                                                         "watcher": {"name": "instagram-dms"}})
    assert run(pipe.run(Active("personal", spec, "ask", "low", "draft"), st)) == "ok"
    assert calls == []  # the daemon was never asked (it would refuse DMs from a test anyway)
    assert "test data" in claude.calls[0][0]
    assert any("test data instead of real messages" in p for p in st.previews)


# --- the client commands ---------------------------------------------------------------------------------------


def test_cli_picks_web_ops_when_a_session_exists(monkeypatch):
    sent = []
    sessions = {"instagram": True, "threads": False}

    def fake_request(payload):
        if payload["cmd"] == "social.sessions":
            return {"ok": True, **sessions}
        sent.append(payload["ops"][0])
        return {"ok": True, "text": "x"}

    monkeypatch.setattr(cli, "request", fake_request)
    monkeypatch.setattr(cli, "deliver", lambda text, *a, **k: text)
    monkeypatch.delenv("AMPI_ORIGIN", raising=False)
    cli.instagram_main(["post", f"https://www.instagram.com/p/{MEDIA_CODE}/"])  # not social: the official API
    monkeypatch.setenv("AMPI_ORIGIN", "forwarded:social")
    cli.instagram_main(["post", f"https://www.instagram.com/p/{MEDIA_CODE}/"])
    cli.threads_main(["post", f"https://www.threads.com/@carol/post/{TH_CODE}"])
    cli.instagram_main(["feed"])
    cli.instagram_main(["profile"])
    cli.instagram_main(["profile", "bob"])
    cli.instagram_main(["comment", "https://www.instagram.com/p/X1234/", "Nice", "--reply-to", "5001"])
    cli.instagram_main(["like", "https://www.instagram.com/p/X1234/", "--comment", "5001"])
    cli.instagram_main(["delete-comment", "5002", "--post", "https://www.instagram.com/p/X1234/"])
    cli.instagram_main(["delete-comment", "5002"])
    cli.threads_main(["posts"])
    cli.threads_main(["posts", "carol"])
    cli.threads_main(["follow", "carol"])
    assert [o["op"] for o in sent] == ["instagram.post", "igweb.post", "threads.post", "igweb.feed", "instagram.profile",
                                       "igweb.profile", "igweb.comment", "igweb.like", "igweb.delete_comment",
                                       "instagram.delete_comment", "threads.posts", "thweb.posts", "thweb.follow"]
    assert sent[6]["args"] == {"target": "https://www.instagram.com/p/X1234/", "text": "Nice", "comment": "5001"}


def test_cli_dms_only_triggers_the_watcher(monkeypatch, capsys):
    runs = []
    monkeypatch.setattr(cli, "_watcher_run", lambda name: runs.append(name) or {"ok": True, "text": "Queued a run."})
    monkeypatch.setattr(cli, "request", lambda payload: pytest.fail("no daemon request for dms"))
    monkeypatch.setattr(cli, "deliver", lambda text, *a, **k: text)
    assert cli.instagram_main(["dms"]) == 0
    assert runs == ["instagram-dms"] and "won't see the messages" in capsys.readouterr().out


# --- social mode in the bridge -----------------------------------------------------------------------------------


@pytest.fixture
def sbot(tmp_path):
    profile = Profile(name="personal", bot_token_env="TG", bot_token="t", owner_ids=frozenset({CHAT}),
                      workspace=tmp_path / "ws", model="sonnet",
                      allowed_tools=("Read", "Edit", "Write", "WebFetch", "WebSearch(*)", "Bash(browse:*)",
                                     "Bash(instagram:*)", "Bash(threads:*)", "Bash(env:*)"), extra_args=())
    profile.workspace.mkdir()
    api, runner = FakeAPI(), FakeRunner(reply="ok")
    store = Store(":memory:")
    return ProfileBot(profile, api, store, runner), api, runner, store


def test_social_flag_survives_resumed_turns_until_new(sbot):
    bot, api, runner, store = sbot
    run(bot.handle_message(msg("hi")))
    first = runner.calls[-1]
    assert first["origin"] is None and first["tag"][0] == "chat" and "WebFetch" in first["allowed_tools"]
    run(bot.handle_message(msg("/social")))
    assert "Social mode is on" in api.sent[-1]["text"]
    for text in ("show my instagram feed", "and the stories?"):
        run(bot.handle_message(msg(text)))
        call = runner.calls[-1]
        assert call["origin"] == "forwarded:social"
        assert call["tag"] == ("social", "personal")  # never a ("chat", …) tag: not the owner's words
        assert call["started"] is True and call["session_id"] == first["session_id"]  # --resume, a new process
        tools = call["allowed_tools"]
        assert "Bash(instagram:*)" in tools and "Read" in tools
        assert not any(t.startswith(("WebFetch", "WebSearch", "Edit", "Write")) or "browse" in t or "env" in t
                       for t in tools)
        assert {"WebFetch", "WebSearch", "Bash(browse:*)", "Edit", "Write", "NotebookEdit"} <= set(call["extra_denied"])
        assert "social mode" in call["append_system_prompt"]
    run(bot.handle_message(msg("/status")))
    assert "Social mode: on" in api.sent[-1]["text"]
    run(bot.handle_message(msg("/new")))  # a new topic: a new conversation, not social
    run(bot.handle_message(msg("hello", thread=555)))
    assert runner.calls[-1]["origin"] is None and runner.calls[-1]["tag"] is not None
    run(bot.handle_message(msg("still social here")))  # the old topic stays social
    assert runner.calls[-1]["origin"] == "forwarded:social"
    run(bot.handle_message(msg("/reset")))
    run(bot.handle_message(msg("fresh")))
    assert runner.calls[-1]["origin"] is None


def test_new_without_topics_ends_social_mode(sbot):
    bot, api, runner, store = sbot
    bot.api.topics = False
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("/new")))
    run(bot.handle_message(msg("x")))
    assert runner.calls[-1]["origin"] is None


def test_only_the_owner_command_sets_social_mode(sbot):
    bot, api, runner, store = sbot
    run(bot.handle_message(msg("/social", user=999)))
    run(bot.handle_message(msg("/social", forward_origin={"type": "hidden_user", "sender_user_name": "X"})))
    assert not store.get_or_create("personal", CHAT, 0).social
    group = {"message_id": 1, "chat": {"id": -100, "type": "supergroup"}, "from": {"id": CHAT}, "text": "/social"}
    run(bot.handle_message(group))
    assert not store.get_or_create("personal", -100, 0).social


def test_social_flag_survives_a_restart(tmp_path):
    db = tmp_path / "b.sqlite3"
    s = Store(db)
    s.get_or_create("personal", 1, 0)
    s.set_social("personal", 1, 0)
    s.close()
    assert Store(db).get_conversation("personal", 1, 0).social is True


# --- review fixes ------------------------------------------------------------------------------------------------


def test_placeholder_doc_ids_are_never_sent(web, monkeypatch):
    w = web()
    monkeypatch.setitem(TH_DOCS, "feed", dataclasses.replace(TH_DOCS["feed"], doc_id="0", status="placeholder"))
    monkeypatch.setitem(IG_DOCS, "feed", dataclasses.replace(IG_DOCS["feed"], doc_id="0", status="placeholder"))
    for op in ("thweb.feed", "igweb.feed"):
        resp = ask(w, (op, {}))
        assert not resp["ok"] and "isn't known yet: copy its doc_id from DevTools" in resp["error"]
    assert w.fake.calls == []


def test_the_shipped_tables_mark_placeholders_and_stale_ids():
    src = Path(msess.__file__)  # the autouse fixture patched the dicts, not the source
    src = src.read_text(encoding="utf-8")
    assert src.count('"placeholder")') >= 7 and src.count('"stale")') == 3
    assert "/api/v1/feed/timeline/" not in "".join(ep.path for ep in ENDPOINTS.values())


def test_page_tokens_belong_to_the_session(web):
    w = web()
    ask(w, ("thweb.feed", {}))
    ask(w, ("thweb.feed", {}))
    w.th.auth.resume()  # e.g. after a checkpoint: the old page tokens aren't reused
    ask(w, ("thweb.feed", {}))
    assert [c.path for c in w.fake.calls if c.host == "www.threads.com"].count("/") == 2


def test_refused_page_tokens_are_fetched_again_once_not_paused(web):
    w = web()
    stale = httpx.Response(200, text='for (;;);{"error":1357004,"errorSummary":"Sorry, something went wrong"}')
    real = w.fake.threads
    hits = []

    def once(method, path, form):
        if path == "/graphql/query" and not hits:
            hits.append(1)
            return stale
        return real(method, path, form)

    w.fake.threads = once
    assert ask(w, ("thweb.feed", {}))["ok"]
    assert [c.path for c in w.fake.calls].count("/") == 2 and w.th.auth.state() == "ok"
    w.fake.threads = lambda m, p, f: httpx.Response(200, text=stale.text) if p == "/graphql/query" else real(m, p, f)
    resp = ask(w, ("thweb.feed", {}))
    assert not resp["ok"] and "refused the page tokens twice" in resp["error"] and w.th.auth.state() == "ok"


@pytest.mark.parametrize("page,reason,text", [
    ('<html><a href="/accounts/login/">Log in</a></html>', "login_required", "confirm it's you"),
    ("<html>Please use a supported browser</html>", "user_agent", "refuse non-browser clients"),
])
def test_a_page_without_tokens_pauses_once(web, page, reason, text):
    w = web()
    w.fake.th_page = page
    resp = ask(w, ("thweb.feed", {}))
    assert not resp["ok"] and text in resp["error"]
    assert w.th.auth.load()["paused_reason"] == reason and len(w.notices) == 1
    ask(w, ("thweb.feed", {}))
    assert [c.path for c in w.fake.calls].count("/") == 1  # never fetched again in a loop


def test_comments_on_later_pages_and_replies_are_found(web):
    w = web()
    w.fake.more_comments = [{"pk": "5009", "text": "old one", "user": {"pk": "2009", "username": "zoe"}}]
    w.fake.comments[0]["child_comment_count"] = 1
    w.fake.children = {"5001": [{"pk": "5010", "text": "a reply", "user": {"pk": "2010", "username": "yan"}}]}
    resp = ask(w, ("igweb.like", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "comment": "5009"}))
    assert resp["approval"] and "@zoe's comment “old one”" in resp["text"]
    assert any(c.query.get("min_id") == "page2" for c in w.fake.calls)
    approve(w)
    resp = ask(w, ("igweb.comment", {"target": f"https://www.instagram.com/p/{MEDIA_CODE}/", "text": "yes",
                                     "comment": "5010"}))
    assert resp["approval"] and "@yan's comment “a reply”" in resp["text"]


def test_quoted_posts_stay_inside_their_post(web):
    w = web()
    quoted = {"pk": "888", "code": "C1quoted001", "user": {"pk": "3004", "username": "erin"},
              "caption": {"text": "the original"}}
    w.fake.th_post["text_post_app_info"]["share_info"] = {"quoted_post": quoted}
    text = ask(w, ("thweb.post", {"target": f"https://www.threads.com/@carol/post/{TH_CODE}"}))["text"]
    assert "(quoting @erin: the original)" in text
    replies = text.split("Replies")[1]
    assert "erin" not in replies and "@dave" in replies
    assert "[888]" not in ask(w, ("thweb.feed", {}))["text"]


def _spec(extra):
    toml = ('name = "w"\ndescription = "d"\n[[triggers]]\ntype = "manual"\n[[steps]]\nid = "dms"\n'
            'type = "adapter"\nop = "igweb.inbox"\n' + extra)
    files = {"watcher.toml": toml.encode(), "prompts/p.md": b"x", "templates/t.txt": b"x"}
    return ws.parse({k: v for k, v in files.items() if k == "watcher.toml" or k in toml})


@pytest.mark.parametrize("extra,match", [
    ('[[steps]]\nid = "a"\ntype = "llm"\nmode = "agent"\nprompt = "prompts/p.md"\ntools = ["Read"]\n', "agent or web"),
    ('[[steps]]\nid = "a"\ntype = "web"\nurl = "https://example.com/x"\n', "agent or web"),
    ('[[steps]]\nid = "a"\ntype = "notify"\ntext = "{{steps.dms.text}}"\n', "markdown"),
    ('[[steps]]\nid = "a"\ntype = "adapter"\nop = "telegram.send"\nargs = { chat = "me", text = "x" }\n',
     "can't change anything"),
])
def test_dm_watchers_may_only_summarize(extra, match):
    with pytest.raises(ws.SpecError, match=match):
        _spec(extra)
    assert _spec('[[steps]]\nid = "a"\ntype = "notify"\nformat = "markdown"\ntext = "{{steps.dms.text}}"\n')


def test_defanged_text_has_no_clickable_addresses():
    from assistant_bridge.watch.pipeline import defang_text
    out = defang_text("go to https://evil.example/x or t.me/abc, www.site.com, [x](y) `code` tg:resolve")
    assert "https://" not in out and "hxxps://" in out and "t(.)me" in out and "site(.)com" in out
    assert "[" not in out and "`" not in out and "tg:" not in out


def test_social_runs_get_no_permission_tool(sbot):
    bot, api, runner, store = sbot

    class Broker:
        used = []

        def run(self, label, outside_content):
            self.used.append(label)
            raise AssertionError("no permission tool for social runs")

    bot.permissions = Broker()
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("read the feed")))
    assert Broker.used == [] and runner.calls[-1]["origin"] == "forwarded:social"
    assert "mcp__ampi__install_system_packages" not in runner.calls[-1]["allowed_tools"]


def test_instagram_dms_works_from_social_mode_only_for_the_dm_watcher():
    from assistant_bridge.watch.control import Control
    from assistant_bridge.watch.engine import Caller
    started, busy = [], [0]
    engine = SimpleNamespace(profile=SimpleNamespace(name="personal"),
                             db=SimpleNamespace(busy_count=lambda profile, name: busy[0]),
                             manual=lambda name, by, owner=False: started.append(owner) or f"Queued {name} ({by}).")
    control = Control({"personal": engine}, "unused")
    who = control.who(Caller("forwarded:social", ("social", "personal")))
    assert who is not None and who.kind == "social"
    out = run(control.dispatch({"cmd": "run", "name": "instagram-dms"}, who))
    assert out["ok"] and "social mode" in out["text"] and started == [False]  # quiet hours apply
    assert "less than 10 minutes" in run(control.dispatch({"cmd": "run", "name": "instagram-dms"}, who))["text"]
    control._social_runs.clear()
    busy[0] = 1
    assert "already queued" in run(control.dispatch({"cmd": "run", "name": "instagram-dms"}, who))["text"]
    assert started == [False]
    for req in ({"cmd": "run", "name": "other"}, {"cmd": "list"}, {"cmd": "build", "name": "x", "text": "y"},
                {"cmd": "logs", "run": "r"}):
        assert not run(control.dispatch(req, who))["ok"]
    assert control.who(Caller("forwarded:private", ("social", "personal"))) is None
    assert control.who(Caller("forwarded:social", None)) is None


def test_a_manual_dm_check_ignores_quiet_hours(web, monkeypatch):
    from ampi_tools import ops_social_session
    monkeypatch.setattr(ops_social_session, "_in_quiet", lambda s, quiet: True)
    w = web()
    out = run(ops_social_session.ig_inbox(w.daemon.services, {"quiet": "00:00-23:59"}))
    assert out["stop"] and "Quiet hours" in out["text"] and w.fake.calls == []
    out = run(ops_social_session.ig_inbox(w.daemon.services, {"quiet": "00:00-23:59", "manual": True}))
    assert "Are we still on" in out["text"] and out["threads"]


def test_login_required_summaries_pause(web):
    w = web()
    w.fake.override[("www.threads.com", "POST", "/graphql/query")] = httpx.Response(
        200, text='for (;;);{"error":1357053,"errorSummary":"Login required"}')
    assert not ask(w, ("thweb.feed", {}))["ok"] and w.th.auth.state() == "paused"


def test_refused_page_tokens_never_resend_a_change(web):
    w = web()
    assert ask(w, ("thweb.like", {"target": f"https://www.threads.com/@carol/post/{TH_CODE}"}))["approval"]
    real = w.fake.threads

    def refuse_like(method, path, form):
        if path == "/graphql/query" and form.get("doc_id") == TH_DOCS["like"].doc_id:
            return httpx.Response(200, text='for (;;);{"error":1357004,"errorSummary":"Sorry"}')
        return real(method, path, form)

    w.fake.threads = refuse_like
    out = approve(w)
    assert out["status"] == "failed" and "nothing was changed" in out["text"]
    assert sum(c.form.get("doc_id") == TH_DOCS["like"].doc_id for c in w.fake.calls) == 1
    assert w.th.budget.hits["write"] == [] and w.th.auth.state() == "ok"


def test_social_runs_keep_only_allow_listed_tools():
    from assistant_bridge.bot import without_web
    tools = ("Read", "Read(./**)", "Glob", "Grep", "Skill", "Bash(instagram:*)", "Bash(threads:*)", "Bash(curl:*)",
             "Bash(todoist:*)", "mcp__web__fetch", "WebFetch", "Edit", "Bash", "Bash(info:*)", "Bash(instagram *)")
    assert without_web(tools) == ("Read", "Read(./**)", "Glob", "Grep", "Skill", "Bash(instagram:*)",
                                  "Bash(threads:*)")
