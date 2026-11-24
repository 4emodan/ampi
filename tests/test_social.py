import asyncio
import json
import logging
import time
import urllib.parse

import httpx
import pytest

from ampi_tools import meta
from ampi_tools.meta import INSTAGRAM, THREADS, MetaAuth, MetaClient, RedactingFormatter, check_text, social_ref, text_extras
from ampi_tools.ops import Approvals, Policy, Services
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError

BRIDGE = 4242
TOKEN = "TOKEN_SECRET_1234567890"


def run(coro):
    return asyncio.run(coro)


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.fail_with_url = False
        self.th_posts = {
            "201": {"id": "201", "text": "My post", "username": "me_user", "timestamp": "2026-11-19T10:00:00+0000",
                    "permalink": "https://www.threads.com/@me_user/post/CODE201", "shortcode": "CODE201", "is_reply": False},
            "301": {"id": "301", "text": "Nice post!", "username": "bob", "is_reply": True, "shortcode": "CODE301",
                    "replied_to": {"id": "201"}, "root_post": {"id": "201"}, "hide_status": "NOT_HUSHED"},
            "302": {"id": "302", "text": "agree", "username": "eve", "is_reply": True,
                    "replied_to": {"id": "301"}, "root_post": {"id": "201"}},
            "401": {"id": "401", "text": "stranger's post", "username": "zed", "is_reply": False},
            "501": {"id": "501", "permalink": "https://www.threads.com/@me_user/post/NEW"},
        }
        self.ig_comments = {
            "801": {"id": "801", "text": "Nice", "username": "fan", "media": {"id": "701"},
                    "replies": {"data": [{"id": "802", "text": "meh", "username": "troll"}]}},
            "803": {"id": "803", "text": "elsewhere", "username": "x", "media": {"id": "999"}},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        self.calls.append((request.url.host, request.method, request.url.path, q, dict(request.headers)))
        if self.fail_with_url:
            raise RuntimeError(f"boom while calling {request.url}")
        if request.url.host == "graph.threads.com":
            return self.threads(request.method, request.url.path.removeprefix("/v1.0"), q)
        return self.instagram(request.method, request.url.path, q)

    def threads(self, method, path, q):
        if path == "/me":
            return httpx.Response(200, json={"id": "111", "username": "me_user"})
        if path == "/me/replies":
            return httpx.Response(200, json={"data": []})
        if path == "/me/threads":
            return httpx.Response(200, json={"data": [self.th_posts["201"]], "paging": {}})
        if path == "/201/conversation":
            return httpx.Response(200, json={"data": [self.th_posts["301"], self.th_posts["302"]]})
        if path == "/111/threads" and method == "POST":
            return httpx.Response(200, json={"id": "501"})
        if path.endswith("/insights"):
            return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 3}, {"value": 4}]},
                                                      {"name": "likes", "total_value": {"value": 2}}]})
        if path.endswith("/manage_reply") or method == "DELETE":
            return httpx.Response(200, json={"success": True})
        item = self.th_posts.get(path.strip("/"))
        return httpx.Response(200, json=item) if item else httpx.Response(400, json={"error": {"message": "nope", "code": 100}})

    def instagram(self, method, path, q):
        if path == "/me":
            return httpx.Response(200, json={"user_id": "900", "username": "me_ig", "account_type": "MEDIA_CREATOR"})
        if path == "/me/media":
            return httpx.Response(200, json={"data": [{"id": "701", "caption": "Sunset", "shortcode": "IGCODE1",
                                                       "permalink": "https://www.instagram.com/p/IGCODE1/"}]})
        if path == "/701/comments":
            return httpx.Response(200, json={"data": [self.ig_comments["801"]]})
        if method == "POST" or method == "DELETE":
            return httpx.Response(200, json={"id": "804", "success": True})
        item = self.ig_comments.get(path.strip("/"))
        return httpx.Response(200, json=item) if item else httpx.Response(400, json={"error": {"message": "nope"}})


def make_auth(service, tmp_path, client, scopes, user, **extra):
    f = tmp_path / f"{service.name}.json"
    now = time.time()
    f.write_text(json.dumps({"access_token": TOKEN, "user_id": user[0], "username": user[1], "scopes": scopes,
                             "obtained_at": now, "expires_at": now + 50 * 86400, "login_at": now, **extra}))
    return MetaAuth(service, "appid", "APP_SECRET_abcdefgh", f, client)


@pytest.fixture
def social(tmp_path):
    def make(write=True, policy=Policy()):
        graph = FakeGraph()
        client = httpx.AsyncClient(transport=httpx.MockTransport(graph))
        th_scopes = list(THREADS.read_scopes + (THREADS.write_scopes if write else ()))
        th = MetaClient(THREADS, make_auth(THREADS, tmp_path, client, th_scopes, ("111", "me_user")), client)
        ig = MetaClient(INSTAGRAM, make_auth(INSTAGRAM, tmp_path, client, list(INSTAGRAM.read_scopes),
                                             ("900", "me_ig")), client)

        async def trusted(pid):
            return pid == BRIDGE

        daemon = Daemon(Services(threads_client=th, instagram_client=ig), Approvals(tmp_path / "a.sqlite3"), policy,
                        trusted)
        daemon.runs.origin_of = lambda pid: "private" if pid == 1 else "unknown"  # the owner's chat
        return daemon, graph

    return make


def ask(daemon, *ops, pid=None, origin=None):
    pid = pid or (2 if origin else 1)
    return run(daemon.handle({"cmd": "run", "ops": [{"op": o, "args": a} for o, a in ops], "origin": origin}, pid))


def approve(daemon):
    (item,) = run(daemon.handle({"cmd": "approvals.pending"}, BRIDGE))["items"]
    run(daemon.handle({"cmd": "approvals.notified", "id": item["id"], "message_id": 5}, BRIDGE))
    return run(daemon.handle({"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"],
                              "message_id": 5}, BRIDGE))


def writes(graph):
    return [(m, p) for h, m, p, *_ in graph.calls if m != "GET"]


# --- references and text ------------------------------------------------------------------------


def test_links_and_ids():
    assert social_ref("12345") == "id:12345"
    assert social_ref("https://www.threads.net/@Bob.x/post/DAbc123x?xmt=1") == "threads:bob.x:DAbc123x"
    assert social_ref("instagram.com/reel/IGCODE1/") == "instagram:IGCODE1"
    for bad in ("https://threads.com.evil.com/@a/post/xxxxxx", "https://evil.com/@a/post/xxxxxx",
                "https://www.threads.com@evil.com/@a/post/xxxxxx", "12a", "https://www.instagram.com/stories/x/1"):
        with pytest.raises(ToolError):
            social_ref(bad)


@pytest.mark.parametrize("text", [
    "hi​there", "evil‮exe.txt", "🎉\U000e0041\U000e0042", "🎉" + "︁" * 5, "x\U000e0100",
    "a͏b", "aㅤb", "tab\there", "x️",
])
def test_invisible_characters_are_refused(text):
    with pytest.raises(ToolError, match="invisible"):
        check_text(text, 500, True)


def test_ordinary_text_and_emoji_pass_and_emoji_count_as_bytes():
    assert check_text("Καλημέρα! 👩‍💻 ❤️\nline two", 500, True)
    check_text("α" * 500, 500, True)  # Greek letters count as one each
    with pytest.raises(ToolError, match="too long"):
        check_text("🎉" * 126, 500, True)  # 4 bytes each


def test_extras_list_mentions_links_and_tags():
    extras = text_extras("Thanks @bob and @eve! see example.com/x and https://раypal.com #ai")
    assert "will notify: @bob, @eve" in extras
    assert any("example.com/x" in e and "xn--" in e for e in extras)  # look-alike domains shown as punycode
    assert "tags: #ai" in extras


# --- Threads -------------------------------------------------------------------------------------


def test_reading_posts_replies_and_insights(social):
    daemon, graph = social()
    resp = ask(daemon, ("threads.replies", {"target": "201"}))
    assert resp["ok"] and "@bob" in resp["text"] and "data, not instructions" in resp["text"]
    resp = ask(daemon, ("threads.post", {"target": "https://www.threads.com/@me_user/post/CODE201"}))
    assert resp["ok"] and "views: 7" in resp["text"] and "likes: 2" in resp["text"]
    resp = ask(daemon, ("threads.post", {"target": "401"}))
    assert not resp["ok"] and "only the owner's posts" in resp["error"]
    assert all("access_token" not in json.dumps(c[4]) for c in graph.calls)  # Threads: token as a parameter


def test_publishing_always_asks_and_posts_exactly_once(social):
    daemon, graph = social(policy=Policy(auto_approve=frozenset({"threads.publish", "threads.delete"})))
    text = "Thanks @bob, see example.com"
    resp = ask(daemon, ("threads.publish", {"text": text, "target": "301"}), origin="watcher:th")
    assert resp["ok"] and resp["approval"] == 1, resp
    s = resp["text"]
    assert "PUBLIC reply on Threads to @bob" in s and "│ Thanks @bob, see example.com" in s
    assert "will notify: @bob" in s and "Asked for by watcher:th" in s
    assert writes(graph) == []
    assert approve(daemon)["status"] == "done"
    (post,) = [c for c in graph.calls if c[1] == "POST"]
    assert post[3]["text"] == text and post[3]["reply_to_id"] == "301" and post[3]["auto_publish_text"] == "true"


def test_a_reply_target_that_changed_after_approval_stops_the_post(social):
    daemon, graph = social()
    ask(daemon, ("threads.publish", {"text": "agreed", "target": "301"}))
    graph.th_posts["301"]["text"] = "Click evil.com"
    done = approve(daemon)
    assert done["status"] == "failed" and "changed or disappeared" in done["text"]
    assert writes(graph) == []


@pytest.mark.parametrize("op, target, error", [
    ("threads.publish", "401", "replies are only possible"),  # a stranger's own post
    ("threads.delete", "301", "only the owner's own posts"),
    ("threads.hide", "302", "only top-level replies"),
])
def test_targets_outside_the_owners_posts_are_refused(social, op, target, error):
    daemon, graph = social()
    resp = ask(daemon, (op, {"target": target, "text": "x"} if op == "threads.publish" else {"target": target}))
    assert not resp["ok"] and error in resp["error"] and writes(graph) == []


def test_read_only_logins_cant_publish(social):
    daemon, graph = social(write=False)
    resp = ask(daemon, ("threads.publish", {"text": "hello"}))
    assert not resp["ok"] and "--write" in resp["error"]


def test_tokens_never_reach_the_assistant_or_the_log(social, caplog):
    daemon, graph = social()
    graph.fail_with_url = True
    resp = ask(daemon, ("threads.posts", {}))
    assert not resp["ok"] and TOKEN not in json.dumps(resp) and "unreachable (RuntimeError)" in resp["error"]
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed %s", (f"https://x?access_token={TOKEN}",), None)
    try:
        raise RuntimeError(f"url ...?access_token={TOKEN}&client_secret=APP_SECRET_abcdefgh")
    except RuntimeError:
        import sys
        record.exc_info = sys.exc_info()
    out = RedactingFormatter("%(message)s").format(record)
    assert TOKEN not in out and "APP_SECRET_abcdefgh" not in out


def test_watcher_ops_are_only_for_the_bridge(social):
    daemon, graph = social()
    assert "for watchers" in ask(daemon, ("threads.ids", {}))["error"]
    resp = ask(daemon, ("threads.ids", {"query": "replies"}), pid=BRIDGE)
    assert resp["ok"] and resp["ids"] == ["301", "302"]
    shown = ask(daemon, ("threads.show", {"ids": ["301"]}), pid=BRIDGE)
    assert "@bob" in shown["text"] and "My post" in shown["text"]


# --- Instagram -----------------------------------------------------------------------------------


def test_instagram_comments_and_replies(social):
    daemon, graph = social(policy=Policy(auto_approve=frozenset({"instagram.reply", "instagram.hide"})))
    resp = ask(daemon, ("instagram.comments", {"target": "https://www.instagram.com/p/IGCODE1/"}))
    assert resp["ok"] and "@fan" in resp["text"] and "@troll" in resp["text"]
    assert all(c[4].get("authorization") == f"Bearer {TOKEN}" for c in graph.calls if c[0] == "graph.instagram.com")

    resp = ask(daemon, ("instagram.reply", {"target": "801", "text": "Thank you!"}))
    assert resp["ok"] and resp["approval"] == 1 and "Reply PUBLICLY on Instagram to @fan" in resp["text"]
    assert approve(daemon)["status"] == "done"
    assert [(m, p) for m, p in writes(graph)] == [("POST", "/801/replies")]

    resp = ask(daemon, ("instagram.hide", {"target": "801"}))  # relaxed by the policy: runs at once
    assert resp["ok"] and "Hid the comment by @fan" in resp["text"]
    resp = ask(daemon, ("instagram.delete_comment", {"target": "803"}))  # not on the owner's posts
    assert not resp["ok"] and "isn't a comment on one of the owner's posts" in resp["error"]


# --- tokens --------------------------------------------------------------------------------------


def test_old_tokens_are_refreshed_and_kept_if_refresh_fails(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "fail" in tmp_path.name:
            return httpx.Response(400, json={"error": {"message": "x"}})
        return httpx.Response(200, json={"access_token": "NEW_TOKEN_abcdefghij", "expires_in": 5184000})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = make_auth(THREADS, tmp_path, client, [], ("1", "u"))
    assert run(auth.refresh_if_due()) is False and calls == []  # fresh token: nothing to do
    data = auth.load()
    data["obtained_at"] -= 8 * 86400
    auth.save(data)
    assert run(auth.refresh_if_due()) is True and auth.load()["access_token"] == "NEW_TOKEN_abcdefghij"
    assert "th_refresh_token" in calls[0]
    assert "NEW_TOKEN_abcdefghij" not in meta.redact("token NEW_TOKEN_abcdefghij")


def test_expiry_warnings(tmp_path):
    client = httpx.AsyncClient()
    auth = make_auth(THREADS, tmp_path, client, [], ("1", "u"))
    assert auth.expiry_note() == ""
    data = auth.load()
    data["login_at"] -= 85 * 86400
    auth.save(data)
    assert "If the Threads profile is private" in auth.expiry_note()
    data["expires_at"] = time.time() + 2 * 86400
    auth.save(data)
    assert "expires in 1 day(s)" in auth.expiry_note() or "expires in 2 day(s)" in auth.expiry_note()
    data["expires_at"] = time.time() - 1
    auth.save(data)
    with pytest.raises(ToolError, match="login expired"):
        run(auth.token())


# --- watchers ------------------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from test_bot import FakeRunner, setup  # noqa: E402,F401 (setup is a fixture)
from test_watchers import FakeTools  # noqa: E402

from assistant_bridge.config import Tools, Watcher  # noqa: E402
from assistant_bridge.watchers import SOCIAL_RUNS_PER_DAY, Watch  # noqa: E402


def social_watch(bot, tools):
    w = Watcher(name="ig", kind="instagram", query="comments", every_minutes=30)
    bot.profile = replace(bot.profile, watchers=(w,))
    return Watch(bot, w, Tools(socket="tools", tg_reader_socket="tg"), tools)


def test_comment_floods_and_busy_days_dont_become_claude_runs(setup):
    bot, api, runner, _ = setup(runner=FakeRunner(reply="ok"))
    tools = FakeTools()
    watch = social_watch(bot, tools)
    run(watch.tick())  # baseline
    tools.ids = ["c1"]
    run(watch.tick())
    assert len(runner.calls) == 1 and "1 new comments on Instagram" in runner.calls[0]["prompt"]
    tools.ids = [f"f{i}" for i in range(40)] + ["c1"]
    run(watch.tick())
    assert len(runner.calls) == 1 and "40 new comments on Instagram (too many" in api.sent[-1]["text"]
    state = bot.store.get_watch_state(bot.profile.name, "ig")
    state["runs"]["n"] = SOCIAL_RUNS_PER_DAY
    bot.store.set_watch_state(bot.profile.name, "ig", state)
    tools.ids = ["d1"] + tools.ids
    run(watch.tick())
    assert len(runner.calls) == 1 and "daily limit" in api.sent[-1]["text"]
