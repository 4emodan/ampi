"""Regression tests for the second round of adversarial review."""

import asyncio
import base64
import sqlite3
from dataclasses import replace
from types import SimpleNamespace as NS

import httpx
import pytest
from test_ampi_tools import BRIDGE_PID, decide, pending, tools, writes  # noqa: F401 (tools is a fixture)
from test_bot import OWNER, FakeRunner, setup  # noqa: F401 (setup is a fixture)
from test_tg_reader import FakeClient, dialog, message
from test_watchers import ButtonAPI, FakeTools, callback, gmail_watch

from ampi_tools.gmail import normalize_text, strict_addresses
from ampi_tools.ops import Approvals
from ampi_tools.todoist import ToolError
from assistant_bridge.approvals import ApprovalRelay
from assistant_bridge.bot import watcher_tools
from assistant_bridge.claude_runner import RunResult, build_args, scope_tools
from assistant_bridge.config import Watcher
from assistant_bridge.services import ServiceUnavailable
from assistant_bridge.telegram import TelegramError
from tg_reader.reader import Reader


def run(coro):
    return asyncio.run(coro)


# --- tg_reader ----------------------------------------------------------------------------

OTP_BOT = NS(id=999, first_name="Bank OTP", last_name=None, username="bankotp_bot", bot=True)


class ResolvingClient(FakeClient):
    def __init__(self, *a):
        super().__init__(*a)
        self.resolved = []

    async def get_peer_id(self, name):
        self.resolved.append(name)
        return {"@bankotp_bot": 999, "@publicnews": -1009000}[name]


def test_denied_chats_cant_be_reached_by_username():
    r = Reader(ResolvingClient([dialog(OTP_BOT, "Bank OTP", is_user=True)], {999: [message(1, "code 4242")]}),
               deny=frozenset({999}))
    for chat in ("@bankotp_bot", "999", "Bank OTP"):
        res = run(r.handle({"cmd": "history", "chat": chat}))
        assert not res["ok"], chat


def test_unknown_usernames_are_resolved_once_and_checked_against_the_deny_list():
    client = ResolvingClient([], {-1009000: []})
    r = Reader(client, deny=frozenset({999}))
    assert run(r.since("@publicnews"))["peer_id"] == -1009000
    run(r.since("@publicnews"))
    assert client.resolved == ["@publicnews"]  # cached: no daily lookup used up by every watcher poll
    assert not run(r.handle({"cmd": "history", "chat": "@bankotp_bot"}))["ok"]  # resolves to a denied id


def test_big_fetches_cost_more_and_watchers_have_their_own_budget():
    r = Reader(FakeClient([dialog(NS(id=11, first_name="A", last_name=None, username=None, bot=False), "A",
                                  is_user=True)], {11: []}))
    r.clock = lambda: 0.0
    assert run(r.handle({"cmd": "history", "chat": "A", "limit": 2000}))["ok"]  # 20 units: the whole minute
    assert "too many" in run(r.handle({"cmd": "chats"}))["error"]
    assert run(r.handle({"cmd": "since", "chat": "A"}))["ok"]  # watchers aren't starved


# --- ampi_tools ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["a@evil.com,", "a@evil.com, b@evil.com@", "a@b.com,,c@d.com", '"x@y.com" <e@evil.com>'])
def test_ambiguous_recipients_are_refused(value):
    with pytest.raises(ToolError):
        strict_addresses(value, "cc")


def test_recipients_are_rebuilt_from_what_was_parsed():
    assert strict_addresses('"Doe, J" <j@d.com>,x@y.org', "to") == '"Doe, J" <j@d.com>, x@y.org'


def test_hidden_characters_are_removed_from_what_is_sent():
    assert normalize_text("hi\t  there​ \nbye  \r\n") == "hi   there\nbye"


def test_a_cc_the_summary_doesnt_show_cant_be_sent(tools):
    daemon, _, gm_server = tools()
    resp = run(daemon.handle({"cmd": "run", "ops": [
        {"op": "gmail.send", "args": {"to": "boss@corp.com", "cc": "a@evil.com,", "body": "hi"}}]}, 1))
    assert not resp["ok"] and pending(daemon) == []


def test_sent_body_is_exactly_the_normalized_one(tools):
    daemon, _, gm_server = tools()
    run(daemon.handle({"cmd": "run", "ops": [
        {"op": "gmail.send", "args": {"to": "boss@corp.com", "body": "Thanks!​\t\nBest  "}}]}, 1))
    (item,) = pending(daemon)
    assert "│ Thanks!\n│ Best" in item["summary"]
    decide(daemon, item)
    (_, _, body), = [c for c in gm_server.calls if c[0] == "POST"]
    raw = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "Thanks!\nBest" in raw.replace("\r\n", "\n") and "​" not in raw


def test_adding_to_a_shared_project_needs_approval(tools):
    daemon, td_server, _ = tools()
    original = td_server.__call__

    def with_shared(request):
        if request.url.path.endswith("/projects") and request.method == "GET":
            return httpx.Response(200, json={"results": [{"id": "p_in", "name": "Inbox"},
                                                         {"id": "p_fam", "name": "Family", "is_shared": True}]})
        return original(request)

    daemon.services.todoist_client._client = httpx.AsyncClient(transport=httpx.MockTransport(with_shared))
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.add", "args": {"content": "x", "project": "Family"}}]}, 1))
    assert "approval" in resp and "SHARED project" in resp["text"]
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.add", "args": {"content": "y", "project": "Inbox"}}]}, 1))
    assert resp["ok"] and "approval" not in resp


def test_task_updates_show_the_whole_description(tools):
    daemon, _, _ = tools()
    long = "line one\n" + "word " * 300
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.update", "args": {"id": "1", "description": long}}]}, 1))
    assert resp["text"].count("word") == 300


def test_old_approval_databases_are_migrated(tmp_path):
    db = sqlite3.connect(tmp_path / "a.sqlite3")
    db.executescript("CREATE TABLE approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, "
                     "ops TEXT NOT NULL, summary TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'pending', "
                     "notified INTEGER NOT NULL DEFAULT 0, result TEXT, decided REAL);"
                     "INSERT INTO approvals (created, ops, summary) VALUES (9e9, '[]', 'old');")
    db.commit()
    db.close()
    a = Approvals(tmp_path / "a.sqlite3")
    assert a.get(1)["status"] == "expired"  # an old request without a nonce can't be approved
    new_id = a.add([], "new", None)
    assert len(a.get(new_id)["nonce"]) == 16


# --- bridge -------------------------------------------------------------------------------


def test_cancelled_watcher_runs_are_not_retried(setup):
    bot, api, _, _ = setup()
    tools_ = FakeTools()
    watch = gmail_watch(bot, tools_)
    run(watch.tick())
    tools_.ids = ["x"]
    calls = []

    async def cancelled_run(w, prompt, quiet_errors=False):
        calls.append(prompt)
        return RunResult(ok=False, text="", error="stopped by /cancel", cancelled=True)

    bot.run_watcher = cancelled_run
    run(watch.tick())
    run(watch.tick())
    assert len(calls) == 1 and api.sent == []


def test_slow_failures_are_not_repeated(setup):
    bot, api, _, _ = setup()
    tools_ = FakeTools()
    watch = gmail_watch(bot, tools_)
    run(watch.tick())
    tools_.ids = ["x"]
    calls = []

    async def slow_fail(w, prompt, quiet_errors=False):
        calls.append(prompt)
        return RunResult(ok=False, text="", error="stopped after 1800 s", timed_out=True, duration_ms=1_800_000)

    bot.run_watcher = slow_fail
    run(watch.tick())
    run(watch.tick())
    assert len(calls) == 1 and "Skipped those items" in api.sent[0]["text"]


def test_retried_runs_are_told_to_check_what_was_done(setup):
    bot, _, _, _ = setup()
    tools_ = FakeTools()
    watch = gmail_watch(bot, tools_)
    run(watch.tick())
    tools_.ids = ["x"]
    prompts = []

    async def quick_fail(w, prompt, quiet_errors=False):
        prompts.append(prompt)
        return RunResult(ok=False, text="", error="usage limit", duration_ms=500)

    bot.run_watcher = quick_fail
    run(watch.tick())
    run(watch.tick())
    assert "previous attempt" not in prompts[0] and "previous attempt" in prompts[1]


def test_unreachable_tools_service_keeps_the_buttons(setup):
    bot, api, _, _ = setup(api=ButtonAPI())

    async def down(path, payload, timeout=300):
        raise ServiceUnavailable("not reachable")

    run(ApprovalRelay(bot, "sock", down).handle_callback(callback()))
    assert bot.api.edits == [] and "still waiting" in api.sent[-1]["text"]


def test_rate_limited_sends_are_retried(setup):
    bot, _, _, _ = setup(api=ButtonAPI())
    bot.api.fail_buttons = TelegramError("sendMessage", "Too Many Requests", 429)
    t = FakeTools()
    t.pending = [{"id": 5, "summary": "x", "reason": None, "nonce": "n" * 16}]
    relay = ApprovalRelay(bot, "sock", t)
    run(relay.tick())
    bot.api.fail_buttons = None
    run(relay.tick())
    assert len(bot.api.buttons) == 1


def test_watchers_get_no_web_access_by_default(setup):
    bot, _, _, _ = setup()
    profile = replace(bot.profile, allowed_tools=("Read", "WebFetch", "WebSearch", "Bash(gmail:*)"))
    w = Watcher(name="w", kind="gmail", query="q", every_minutes=5)
    assert watcher_tools(w, profile) == ("Read", "Bash(gmail:*)")
    assert watcher_tools(replace(w, allowed_tools=("Read", "WebFetch")), profile) == ("Read", "WebFetch")


def test_runner_protects_workspace_settings_and_uses_the_settings_file():
    args = build_args(["claude"], session_id="s", resume=False, model=None, allowed_tools=["Write(//**)", "Read"],
                      append_system_prompt=None, settings_file="/etc/x.json")
    denied = args[args.index("--disallowedTools") + 1]
    assert "Edit(./.claude/**)" in denied and "Edit(./**/CLAUDE.md)" in denied
    assert args[args.index("--allowedTools") + 1] == "Edit(./**),Read"
    assert args[args.index("--settings") + 1] == "/etc/x.json"
    assert scope_tools(["Edit(./notes/**)"]) == ["Edit(./notes/**)"]


def test_tg_read_never_writes_claude_md(tmp_path, monkeypatch):
    from tg_reader import client

    monkeypatch.chdir(tmp_path)
    for name in ("CLAUDE.md", "claude.local.md"):
        with pytest.raises(SystemExit):
            client.deliver("x", name, "x")


# --- after round 3 --------------------------------------------------------------------------


def test_invisible_characters_cant_smuggle_data():
    from ampi_tools.ops import clean_block

    smuggled = "Hi" + "︀" * 16 + "\U000e0101" + " there" + "ㅤᅟ͏" + "x"
    assert normalize_text(smuggled) == "Hi therex"
    assert clean_block(smuggled) == "│ Hi therex"


def test_username_watchers_keep_using_the_name(setup):
    from assistant_bridge.config import Tools
    from assistant_bridge.watchers import Watch

    bot, _, _, _ = setup()
    t = FakeTools()
    t.tg = {"count": 0, "last_id": 10, "text": "", "chat": "@publicnews", "peer_id": -1009000}
    w = Watcher(name="news", kind="telegram", query="@publicnews", every_minutes=5)
    watch = Watch(bot, w, Tools(), t)
    run(watch.tick())
    run(watch.tick())
    assert [p["chat"] for _, p in t.requests] == ["@publicnews", "@publicnews"]


def test_a_broken_assistant_is_reported_once_across_batches(setup):
    bot, api, _, _ = setup()
    t = FakeTools()
    watch = gmail_watch(bot, t)
    run(watch.tick())

    async def quick_fail(w, prompt, quiet_errors=False):
        return RunResult(ok=False, text="", error="usage limit", duration_ms=500)

    bot.run_watcher = quick_fail
    for i in range(12):  # new items keep arriving while the assistant is broken
        t.ids = [f"x{i}"] + t.ids
        run(watch.tick())
    assert len(api.sent) == 1
