"""Before connecting a real session: a stop holds whatever happens to the file. Fakes only."""

import asyncio
import contextlib
import json
import threading
import time

import httpx
import pytest
from test_social_fixes import dm_prompts, engine_for, run_once, thread
from test_social_round5 import Failing
from test_social_session import IG_COOKIES, ask, known_docs, run, session_file, web  # noqa: F401

from ampi_tools import meta_session as msess
from ampi_tools.meta_session import IG, SessionAuth, WebClient
from ampi_tools.todoist import ToolError

TRAY = "/api/v1/feed/reels_tray/"
KINDS = ["pause", "cooldown"]


def stop_reply(w, kind):
    w.fake.override[("GET", TRAY)] = lambda _: (
        httpx.Response(302, headers={"location": "/accounts/login/"}) if kind == "pause" else httpx.Response(429, json={}))


def break_save(monkeypatch, how):
    """Saving the session file fails; reading still works."""
    broken = {"on": True}
    real_save, real_lock = SessionAuth.save, msess.file_lock

    def save(self, data):
        if broken["on"] and how == "oserror":
            raise OSError("disk full")
        return real_save(self, data)

    @contextlib.contextmanager
    def lock(path):
        if broken["on"] and how == "busy" and path.name.endswith("session.json"):
            raise ToolError("the session file is busy; try again in a moment")
        with real_lock(path):
            yield

    monkeypatch.setattr(SessionAuth, "save", save)
    monkeypatch.setattr(msess, "file_lock", lock)
    return broken


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("how", ["busy", "oserror"])
def test_a_stop_holds_when_it_cannot_be_saved(web, monkeypatch, kind, how):
    w = web()
    broken = break_save(monkeypatch, how)
    stop_reply(w, kind)
    assert not ask(w, ("igweb.stories", {}))["ok"]
    n = len(w.fake.calls)
    for _ in range(2):
        assert not ask(w, ("igweb.feed", {}))["ok"]
    assert len(w.fake.calls) == n and len(w.notices) == 1
    assert w.ig.auth.state() in ("paused", "cooldown")
    broken["on"] = False
    assert not ask(w, ("igweb.feed", {}))["ok"] and len(w.fake.calls) == n
    d = json.loads(w.ig.auth.path.read_text())  # saved at the next request
    assert d["state"] == "paused" if kind == "pause" else d["cooldown_until"] > time.time()
    assert len(w.notices) == 1


def test_an_unsaved_cooldown_is_saved_before_anything_is_sent_again(web, monkeypatch):
    w = web()
    broken = break_save(monkeypatch, "oserror")
    stop_reply(w, "cooldown")
    ask(w, ("igweb.stories", {}))
    w.fake.override.clear()
    n = len(w.fake.calls)
    w.ig.auth.clock = lambda: time.time() + 4000  # over, but never saved
    assert not ask(w, ("igweb.stories", {}))["ok"] and len(w.fake.calls) == n
    broken["on"] = False
    assert ask(w, ("igweb.stories", {}))["ok"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_stop_that_could_not_be_saved_survives_a_restart(web, monkeypatch, kind):
    w = web()
    broken = break_save(monkeypatch, "oserror")
    stop_reply(w, kind)
    ask(w, ("igweb.stories", {}))
    broken["on"] = False
    auth = SessionAuth(IG, w.ig.auth.path)
    again = WebClient(IG, auth, w.ig._client, gap=0)
    n = len(w.fake.calls)
    assert auth.state() in ("paused", "cooldown")
    with pytest.raises(ToolError):
        run(again.call("ig.reels_tray"))
    assert len(w.fake.calls) == n
    assert "PAUSED" in auth.status() or "cooling" in auth.status()


def test_an_empty_marker_counts_as_paused(web):
    w = web()
    w.ig.auth.marker().touch()
    assert w.ig.auth.state() == "paused" and not ask(w, ("igweb.feed", {}))["ok"] and w.fake.calls == []


def test_resume_clears_an_unsaved_stop(web, monkeypatch):
    w = web()
    broken = break_save(monkeypatch, "oserror")
    stop_reply(w, "pause")
    ask(w, ("igweb.stories", {}))
    broken["on"] = False
    w.fake.override.clear()
    SessionAuth(IG, w.ig.auth.path).resume()  # the owner's command, another process
    assert ask(w, ("igweb.feed", {}))["ok"] and not w.ig.auth.marker().exists()
    assert w.ig.auth.state() == "ok"


def test_a_stop_never_reaches_a_new_session(web, monkeypatch):
    w = web()
    broken = break_save(monkeypatch, "oserror")
    stop_reply(w, "pause")
    ask(w, ("igweb.stories", {}))
    broken["on"] = False
    w.fake.override.clear()
    session_file(w.ig.auth.path, {**IG_COOKIES, "sessionid": "NEWSESSION_secret_9"}, "me_ig",
                 saved_at=time.time() + 5, sid="new")
    assert SessionAuth(IG, w.ig.auth.path).state() == "ok"
    assert ask(w, ("igweb.feed", {}))["ok"]


def test_cancelling_the_caller_keeps_the_stop_and_its_notice(web, monkeypatch):
    w = web()
    stop_reply(w, "pause")
    entered, release = threading.Event(), threading.Event()
    real = SessionAuth.save

    def slow(self, data):
        entered.set()
        release.wait(5)
        real(self, data)

    monkeypatch.setattr(SessionAuth, "save", slow)

    async def go():
        t = asyncio.create_task(w.ig.call("ig.reels_tray"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        before = (len(w.notices), w.ig.auth.state())
        release.set()
        for _ in range(100):
            if json.loads(w.ig.auth.path.read_text())["state"] == "paused":
                break
            await asyncio.sleep(0.02)
        assert before == (1, "paused")

    run(go())
    assert json.loads(w.ig.auth.path.read_text())["state"] == "paused" and len(w.notices) == 1


def test_a_failing_notice_is_sent_later_once(web):
    w = web()
    got, fail = [], [True]

    def notify(text):
        if fail[0]:
            raise RuntimeError("db locked")
        got.append(text)

    w.ig.auth.notify = notify
    stop_reply(w, "pause")
    ask(w, ("igweb.stories", {}))
    fail[0] = False
    ask(w, ("igweb.feed", {}))
    ask(w, ("igweb.feed", {}))
    assert len(got) == 1


def test_a_waiting_request_is_not_sent_after_a_challenge_in_a_body(web):
    w = web()
    w.fake.override[("GET", TRAY)] = lambda _: httpx.Response(400, json={"message": "challenge_required"})

    async def go():
        return await asyncio.gather(*(w.ig.call("ig.reels_tray") for _ in range(3)), return_exceptions=True)

    run(go())
    assert len(w.fake.calls) == 1


@pytest.mark.parametrize("html", [
    '<html><script>window.location.href = "/accounts/login/?next=x"</script></html>',
    "<script>location.replace('https://www.instagram.com/challenge/abc/')</script>",
    '<script>location="\\/accounts\\/login\\/"</script>', "<script>top.location.assign('/accounts/login')</script>",
    '<meta http-equiv="refresh" content="0;url=/challenge/?x">',
    "<META content='0; URL=/accounts/login/' HTTP-EQUIV=refresh>",
])
@pytest.mark.parametrize("status", [400, 404])
def test_pages_that_send_the_browser_to_login_pause(web, html, status):
    w = web()
    w.fake.override[("GET", TRAY)] = httpx.Response(status, text=html)
    ask(w, ("igweb.stories", {}))
    assert w.ig.auth.state() == "paused"


@pytest.mark.parametrize("html", ["<html>Not found</html>", '<a href="/accounts/login_activity/">x</a>',
                                  '<script>location.href="/accounts/login_activity/"</script>',
                                  '<script>location.href="/explore/"</script>', "a word about /challenge/ pages"])
def test_other_not_found_pages_do_not(web, html):
    w = web()
    w.fake.override[("GET", TRAY)] = httpx.Response(404, text=html)
    assert not ask(w, ("igweb.stories", {}))["ok"] and w.ig.auth.state() == "ok"


@pytest.mark.parametrize("error", ["You've hit your limit · resets 3pm", "Not logged in · Please run /login",
                                   "OAuth token has expired", "exit code 1", "something new"])
def test_unknown_ai_failures_wait(tmp_path, error):
    claude = Failing([])
    claude.error = error
    e, w, _, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "hello-there"))])
    for _ in range(4):
        run_once(e, w)
    claude.error = None
    assert run_once(e, w) == "ok" and "hello-there" in dm_prompts(claude)[-1]


@pytest.mark.parametrize("error", [
    "API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy",
    "stop_reason: refusal", "Output blocked by content filtering policy"])
def test_content_refusals_count(tmp_path, error):
    claude = Failing([])
    claude.error = error
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "x"))])
    assert [run_once(e, w) for _ in range(3)] == ["failed", "failed", "ok"]


@pytest.mark.parametrize("error", ["exit code 1", "usage limit reached"])
def test_a_call_that_never_works_ends_in_the_failing_notice(tmp_path, error):
    claude = Failing([])
    claude.error = error
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "x"))])
    asked = []
    tools = e.tools

    async def spy(payload, timeout=300):
        asked.append(payload)
        return await tools(payload)

    e.tools = spy
    for _ in range(70):  # each time a newer item replaces the one waiting for its retry, like a 15-minute timer does
        e.db.enqueue("personal", w.name, "x", "time.every", [{"time": "now"}], 60, True)
        run(e.run_item(w, e.db.next_item("personal", w.name)))
    assert any(p.get("status") == "failing" for p in asked) and any("in a row" in t for t in bot.sent)
