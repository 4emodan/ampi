"""Fifth review of the social session. Fakes only."""

import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import httpx
import pytest
from test_bot import CHAT, msg
from test_social_fixes import SchemaClaude, dm_prompts, engine_for, run_once, thread
from test_social_round4 import LINK, TLINK, like_reply, reader
from test_social_session import (  # noqa: F401 (fixtures)
    BRIDGE, IG_COOKIES, MEDIA_CODE, approve, ask, known_docs, run, sbot, session_file, web,
)

from ampi_tools import meta_session as msess
from ampi_tools.meta_session import IG, SessionAuth
from ampi_tools.ops_telegram import TelegramStore
from ampi_tools.todoist import ToolError
from assistant_bridge.approvals import ApprovalRelay
from assistant_bridge.services import ServiceError
from assistant_bridge.telegram import TelegramError

LIKE = f"/api/v1/web/likes/{msess.shortcode_to_pk(MEDIA_CODE)}/like/"


# --- 1: a hung tg-reader never holds up approvals -----------------------------------------------------------------


def test_telegram_protect_does_not_wait_for_tg_reader(web, tmp_path):
    w = web()

    async def hang(social):
        await asyncio.sleep(30)

    w.daemon.services.telegram_side = NS(store=TelegramStore(tmp_path / "t.sqlite3"), client=NS(tell_own_bots=hang))

    async def go():
        return await asyncio.wait_for(
            w.daemon.handle({"cmd": "telegram.protect", "ids": [5], "bots": [5]}, BRIDGE), 2)

    assert run(go())["ok"]


def test_a_failing_protect_does_not_stop_approval_requests(sbot):
    asked = []

    async def ask_fn(socket, payload, timeout=60):
        asked.append(payload["cmd"])
        if payload["cmd"] == "telegram.protect":
            raise ServiceError("no answer")
        return {"ok": True, "items": [], "notices": []}

    relay = ApprovalRelay(sbot[0], "x", ask_fn, protected=lambda: ([1], True))
    run(relay.tick())
    assert "approvals.pending" in asked


def test_nothing_is_read_before_the_first_push(tmp_path):
    r = reader(deny=False)
    r.guard(tmp_path / "b.json", required=True)
    for req in ({"cmd": "chats"}, {"cmd": "history", "chat": "Anna K"}, {"cmd": "search", "query": "flat"},
                {"cmd": "since", "chat": "Anna K"}):
        assert not run(r.handle(req))["ok"] and r.client.calls == []


def test_saving_still_needs_an_active_deny(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pwd", NS(getpwnam=lambda n: NS(pw_uid=1)))
    monkeypatch.setenv("TG_WRITER_SOCKET", str(tmp_path / "none.sock"))
    if not hasattr(msess.socket, "AF_UNIX"):
        monkeypatch.setattr(msess.socket, "AF_UNIX", 1, raising=False)
    with pytest.raises(ToolError, match="doesn't confirm"):
        msess.reader_guard(tmp_path)

    class Sock:
        reply = b"{}"

        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def settimeout(self, t): pass
        def connect(self, p): pass
        def sendall(self, b): pass

        def makefile(self, mode):
            return NS(readline=lambda n: self.reply)

    monkeypatch.setattr(msess.socket, "socket", Sock)
    Sock.reply = b'{"ok": true, "active": false}'
    with pytest.raises(ToolError):
        msess.reader_guard(tmp_path)
    Sock.reply = b'{"ok": true, "active": true}'
    msess.reader_guard(tmp_path)


# --- 2, 12: what counts against a batch ----------------------------------------------------------------------------


class Failing(SchemaClaude):
    error = "usage limit reached"

    async def __call__(self, prompt, **kw):
        if self.error:
            self.calls.append((prompt, kw))
            return NS(ok=False, text="", error=self.error)
        return await super().__call__(prompt, **kw)


@pytest.mark.parametrize("error", ["usage limit reached", "stopped after 180 s", "API Error: 529 overloaded",
                                   "network error", "API Error: Connection error."])
def test_transient_ai_failures_never_end_in_a_count(tmp_path, error):
    claude = Failing([])
    claude.error = error
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "hello-there"))])
    for _ in range(5):
        assert run_once(e, w) == "failed"
    claude.error = None
    assert run_once(e, w) == "ok" and "hello-there" in dm_prompts(claude)[-1] and "summary" in bot.sent[-1]


@pytest.mark.parametrize("answer", ["nonsense timeout network", None])
def test_invalid_answers_and_refusals_count(tmp_path, answer):
    claude = SchemaClaude([answer] * 8) if answer else Failing([])
    claude.error = "the model refused" if not answer else None
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "x"))])
    assert [run_once(e, w) for _ in range(3)] == ["failed", "failed", "ok"]
    assert "couldn't be summarized" in bot.sent[-1]


def test_telegram_outages_do_not_count_but_refusals_do_before_the_watcher_is_paused(tmp_path):
    from assistant_bridge.watch.engine import FAILS_BEFORE_FAILING
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, "x"))])
    send, code = bot._send_text, [502]

    async def flaky(chat_id, thread_id, text, **kw):
        if code[0] and "in a summary" in text:
            raise TelegramError("sendMessage", "x", code[0])
        await send(chat_id, thread_id, text, **kw)

    bot._send_text = flaky
    for _ in range(6):
        run_once(e, w)
    code[0] = 400
    runs = [run_once(e, w) for _ in range(4)]
    assert runs.index("ok") < FAILS_BEFORE_FAILING - 1 and runs[0] == "failed"


@pytest.mark.parametrize("raw", [b"[]", b'{"ids": 5}', b'{"ids": [["1", "a"]], "ai": "x", "send": []}',
                                 b'{"ids": [[["z"], {}]], "ai": null}'])
def test_a_broken_pending_file_is_reset(tmp_path, raw):
    e, w, _, space = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, "x"))])
    space["pending.json"] = raw
    space["failure.json"] = raw
    assert run_once(e, w) == "ok"


# --- 3: replies that aren't JSON -------------------------------------------------------------------------------------


@pytest.mark.parametrize("status,text,paused", [
    (404, "<html>Page not found <a href='/accounts/login_activity/'>x</a></html>", False), (400, "Bad request", False),
    (404, '<html><form action="/challenge/x/"></form></html>', True),
    (404, '<html><a href="/accounts/login/">Log in</a></html>', True), (200, "<html>hi</html>", True),
])
def test_ordinary_not_found_pages_do_not_pause(web, status, text, paused):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(status, text=text)
    assert not ask(w, ("igweb.stories", {}))["ok"]
    assert (w.ig.auth.state() == "paused") is paused


def test_one_empty_reply_is_an_error_and_a_second_one_pauses(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = lambda r: httpx.Response(200)
    ask(w, ("igweb.stories", {}))
    assert w.ig.auth.state() == "ok"
    ask(w, ("igweb.stories", {}))
    assert w.ig.auth.state() == "paused"


# --- 4: the gap note ----------------------------------------------------------------------------------------------------


def test_a_gap_after_earlier_reports_is_said(tmp_path):
    items = [("a", 10, "x")]
    t = [thread(*items)]
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: t)
    run_once(e, w)
    t[0] = thread(("z", 90, "much later"), has_older=True)  # nothing loaded overlaps what was reported
    run_once(e, w)
    assert "earlier" in bot.sent[-1]
    t[0] = thread(("y", 95, "next"), ("z", 90, "much later"), has_older=True)
    run_once(e, w)
    assert "earlier" not in bot.sent[-1]


# --- 5, 7: wording -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [{"message": "rate limit exceeded"}, {"error_type": "RATE-LIMITED"},
                                  {"errors": [{"message": "Too  many_requests"}]}, {"message": "rate_limit"},
                                  {"errors": [{"extensions": {"code": "ratelimit"}}]}])
def test_rate_limit_wordings(data):
    assert msess._problem(200, data, "", "graphql") == "slow_down"


@pytest.mark.parametrize("data", [
    {"errors": [{"extensions": {"original_error": {"message": "login_required"}}}]},
    {"payload": {"message": "checkpoint_required"}}, {"error": {"originalError": {"errorSummary": "Please log in"}}},
])
def test_wrapped_errors_are_seen(data):
    assert msess._problem(200, data, "", "graphql") == "challenge"


@pytest.mark.parametrize("data", [
    {"errors": [{"message": "Unknown user agent field"}]}, {"errors": [{"message": "field denied", "code": 403}]},
    {"errors": [{"message": "x", "path": ["errors", "data"]}]},
])
def test_harmless_errors_do_not_pause(data):
    assert msess._problem(200, data, "", "graphql") is None


def test_wrapping_is_followed_only_so_far():
    d = {"message": "login_required"}
    for _ in range(200):
        d = {"payload": d}
    assert msess._problem(200, d, "") is None


def test_a_logged_in_page_may_link_to_login_activity(web):
    from test_social_session import PAGE
    w = web()
    w.fake.th_page = PAGE + '<a href="/accounts/login_activity/">Login activity</a>'
    assert ask(w, ("thweb.feed", {}))["ok"]


# --- 6: results of changes ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("body,paused", [
    ({"data": {"like": {"media": {"status": "fail"}, "status": "ok"}}}, False),
    ({"data": {"like": {"status": "ok", "errors": [{"message": "login_required"}]}}}, True),
    ({"data": {"like": {"status": "ok", "error": {"message": "feedback_required"}}}}, True),
    ({"data": {"like": {"success": False, "status": "ok"}}}, False),
    ({"data": {"like": {"id": "5"}}}, False),  # no success field
    ({"data": {"like": "ok"}}, False),
])
def test_nested_failures_of_a_change(web, body, paused):
    w = web()
    ask(w, ("thweb.like", {"target": TLINK}))
    like_reply(w, body)
    out = approve(w)
    assert out["status"] == "failed" and "Liked" not in out["text"]
    assert (w.th.auth.state() == "paused") is paused


def test_nested_rest_failure(web):
    w = web()
    ask(w, ("igweb.like", {"target": LINK}))
    w.fake.override[("POST", LIKE)] = httpx.Response(200, json={"status": "ok", "data": {"status": "fail"}})
    assert approve(w)["status"] == "failed"


def test_placeholder_changes_stay_refused(web, monkeypatch):
    import dataclasses
    w = web()
    for k in ("like", "unlike", "follow", "unfollow"):
        monkeypatch.setitem(msess.TH_DOCS, k, dataclasses.replace(msess.TH_DOCS[k], doc_id="0", status="placeholder"))
    ask(w, ("thweb.like", {"target": TLINK}))
    n = len(w.fake.calls)
    assert "isn't known yet" in approve(w)["text"]
    assert all(c.form.get("doc_id") != "0" for c in w.fake.calls[n:])


# --- 8-15 ------------------------------------------------------------------------------------------------------------------


def test_the_bot_list_is_saved_before_it_counts(tmp_path, monkeypatch):
    import os
    r = reader(deny=False)
    r.guard(tmp_path / "b.json", required=True)
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        r.set_own_bots([777111], True)
    assert not r.own_bots_active() and not run(r.handle({"cmd": "chats"}))["ok"]
    monkeypatch.setattr(os, "replace", real)
    r.set_own_bots([777111], True)
    assert r.own_bots_active() and json.loads((tmp_path / "b.json").read_text())["bots"] == [777111]


def test_an_empty_bot_list_is_not_an_answer(tmp_path):
    r = reader(deny=False)
    r.guard(tmp_path / "b.json", required=True)
    r.set_own_bots([], False)
    assert not run(r.handle({"cmd": "chats"}))["ok"]


def test_the_session_flag_stays_after_the_session_is_forgotten(tmp_path):
    r = reader(deny=False)
    r.guard(tmp_path / "b.json", required=True)
    r.set_own_bots([777111], True)
    r.set_own_bots([777111], False)
    again = reader(deny=False)
    again.guard(tmp_path / "b.json", required=True)
    assert again.social and 777111 in again.deny


def test_a_run_keeps_its_own_social_flag(tmp_path):
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, "x"))])
    flags = []

    async def send(chat_id, thread_id, text, markdown=True, social=False, **kw):
        flags.append(social)

    bot._send_text = send
    e.watchers.clear()  # deleted or replaced while this run goes on
    assert run_once(e, w) == "ok" and flags == [True]


def test_giving_up_on_a_changing_conversation_is_said(sbot):
    bot, api, runner, store = sbot
    build = bot._build_prompt

    async def change(m, social=False):
        store.reset_session("personal", CHAT, 0)
        return await build(m, social)

    bot._build_prompt = change
    run(bot.handle_message(msg("hi")))
    assert runner.calls == [] and "again" in api.sent[-1]["text"]


@pytest.mark.parametrize("reply", [httpx.Response(302, headers={"location": "/x"}), httpx.Response(403),
                                   httpx.Response(400, json={"message": "feedback_required"}),
                                   httpx.Response(200, text="<html>")])
def test_a_pause_during_a_change_keeps_the_warning(web, reply):
    w = web()
    ask(w, ("igweb.like", {"target": LINK}))
    w.fake.override[("POST", LIKE)] = reply
    out = approve(w)["text"]
    assert w.ig.auth.state() == "paused" and "MAY have" in out


def test_waiting_for_the_file_lock_does_not_stop_the_loop(web, tmp_path):
    w = web()
    path = w.ig.auth.path
    code = ("import sys,time,pathlib\nfrom ampi_tools.meta_session import file_lock\n"
            "with file_lock(pathlib.Path(sys.argv[1])):\n print('locked', flush=True)\n time.sleep(1.5)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(
        200, json={"tray": []}, headers={"set-cookie": "csrftoken=ROTATED_value_2; Path=/"})

    async def go():
        gaps, last = [], time.monotonic()

        async def tick():
            nonlocal last
            while True:
                await asyncio.sleep(0.05)
                gaps.append(time.monotonic() - last)
                last = time.monotonic()

        t = asyncio.create_task(tick())
        await w.ig.call("ig.reels_tray")
        t.cancel()
        return max(gaps)

    assert run(go()) < 0.8
    p.wait()
    assert w.ig.auth.load()["cookies"]["csrftoken"] == "ROTATED_value_2"


def test_the_file_lock_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(msess, "LOCK_WAIT", 0.3)
    path = tmp_path / "instagram-session.json"
    session_file(path, IG_COOKIES, "me_ig")
    code = ("import sys,time,pathlib\nfrom ampi_tools.meta_session import file_lock\n"
            "with file_lock(pathlib.Path(sys.argv[1])):\n print('locked', flush=True)\n time.sleep(3)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    with pytest.raises(ToolError, match="busy"):
        SessionAuth(IG, path).resume()
    p.kill()


def test_short_texts_are_not_remembered(sbot):
    store = sbot[3]
    store.add_social_message(CHAT, 1, "Done.")
    store.add_social_message(CHAT, 2, "a longer text from a social reply")
    assert not store.is_social_text("Done.") and store.is_social_text("a longer text from a  social reply")
