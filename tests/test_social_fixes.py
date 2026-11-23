"""Third review of the social session: pauses, containment of strangers' text, the DM watcher. Each test failed
before its fix (fakes only: no requests to Meta, no real Claude)."""

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_bot import CHAT, FakeAPI, FakeRunner, msg
from test_social_session import (  # noqa: F401 (fixtures)
    BRIDGE, MEDIA_CODE, PAGE, TH_CODE, ask, approve, dm_template_files, known_docs, run, sbot, web,
)
from test_watch_engine import FakeClaude, real_sandbox

from ampi_tools import client as cli
from ampi_tools import meta_session as msess
from ampi_tools import ops_social_session as oss
from ampi_tools import social_dm
from ampi_tools import watch_spec as ws
from ampi_tools.meta_session import TH_DOCS
from assistant_bridge.approvals import ApprovalRelay
from assistant_bridge.claude_runner import build_args
from assistant_bridge.watch.control import Control, Who
from assistant_bridge.watch.db import WatchDB
from assistant_bridge.watch.engine import Caller, WatchEngine
from assistant_bridge.watch.pipeline import Active, Pipeline, RunState, _defang
from assistant_bridge.watch.pipeline import Services as PipeServices

SECRET = "SECRET-DM-TEXT"
LINK = f"https://www.instagram.com/p/{MEDIA_CODE}/"
STALE = {"error": 1357004, "errorSummary": "Sorry, something went wrong"}


# --- 1-3: pauses -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status,data,kind,want", [
    (400, {**STALE, "message": "challenge_required"}, "graphql", "challenge"),
    (200, {**STALE, "require_login": True}, "graphql", "challenge"),
    (401, STALE, "graphql", "challenge"),
    (403, STALE, "graphql", "challenge"),
    (429, STALE, "graphql", "slow_down"),
    (200, STALE, "graphql", "stale_tokens"),
    (200, STALE, "json", None),
])
def test_stale_tokens_never_hide_a_stop_signal(status, data, kind, want):
    assert msess._problem(status, data, "", kind) == want


@pytest.mark.parametrize("summary,pauses", [
    ("Not Logged In", True), ("Login required", True), ("Please log in", True), ("Login expired", True),
    ("Your session expired", True), ("Session has expired", True),
    ("This link has expired", False), ("This story has expired", False), ("Blog in maintenance", False),
])
def test_login_phrases_are_matched_whole(summary, pauses):
    assert msess._problem(200, {"errorSummary": summary}, "") == ("challenge" if pauses else None)


def test_an_empty_challenge_field_is_not_a_challenge():
    assert msess._problem(200, {"challenge": None, "checkpoint_url": None, "status": "ok"}, "") is None
    assert msess._problem(200, {"challenge": {"url": "/x"}}, "") == "challenge"


def stale_reply(*_):
    return httpx.Response(200, text="for (;;);" + json.dumps(STALE))


def test_repeated_stale_replies_pause_and_every_request_counts(web):
    w = web()
    w.fake.override[("www.threads.com", "POST", "/graphql/query")] = stale_reply
    assert not ask(w, ("thweb.feed", {}))["ok"]
    assert w.th.auth.state() == "paused" and len(w.notices) == 1
    sent = len([c for c in w.fake.calls if c.host == "www.threads.com"])
    assert sent == 4 and len(w.th.budget.hits["read"]) == sent  # page, query, page, query


def test_a_refused_write_counts_and_is_not_resent(web):
    w = web()
    assert ask(w, ("thweb.like", {"target": f"https://www.threads.com/@carol/post/{TH_CODE}"}))["approval"]
    real = w.fake.threads
    w.fake.threads = lambda m, p, f: (stale_reply() if f.get("doc_id") == TH_DOCS["like"].doc_id else real(m, p, f))
    assert approve(w)["status"] == "failed"
    assert sum(c.form.get("doc_id") == TH_DOCS["like"].doc_id for c in w.fake.calls) == 1
    assert len(w.th.budget.hits["write"]) == 1


def test_a_tokenless_page_pauses_with_neutral_text(web):
    w = web()
    w.fake.th_page = "<html>Something new</html>"
    err = ask(w, ("thweb.feed", {}))["error"]
    assert w.th.auth.state() == "paused" and len(w.notices) == 1
    for text in (err, w.notices[0], ask(w, ("thweb.feed", {}))["error"]):
        assert "user agent" not in text and "non-browser" not in text and "confirm it's you" not in text, text


def test_an_explicit_user_agent_refusal_on_the_page_says_so(web):
    w = web()
    w.fake.th_page = "<html>useragent mismatch</html>"
    assert "non-browser" in ask(w, ("thweb.feed", {}))["error"]


def test_concurrent_callers_share_one_token_fetch_and_one_refresh(web):
    w = web()
    c = w.th
    tokens = iter(["DTSG_first_token", "DTSG_second_token"])
    bad = []

    async def handler(request):
        await asyncio.sleep(0.01)
        if request.url.path == "/":
            return httpx.Response(200, text=PAGE.replace("DTSG_secret_token_01", next(tokens)))
        if b"DTSG_first_token" in request.content:
            bad.append(1)
            return stale_reply()
        return httpx.Response(200, json={"data": {"feedData": {"edges": []}}})

    seen = []

    async def spy(request):
        seen.append(request.url.path)
        return await handler(request)

    c._client = httpx.AsyncClient(transport=httpx.MockTransport(spy))

    async def go():
        return await asyncio.gather(*(c.call("th.graphql", doc="feed") for _ in range(4)), return_exceptions=True)

    out = run(go())
    assert [o for o in out if isinstance(o, Exception)] == []
    assert seen.count("/") == 2 and len(bad) >= 1 and c.auth.state() == "ok"


# --- 4: DM text stays out of run files, logs and transcripts -----------------------------------------------------


class Bot:
    def __init__(self, tmp_path, runner=None):
        self.profile = SimpleNamespace(name="personal", owner_chat_id=CHAT, workspace=tmp_path / "ws", timezone="UTC")
        self.profile.workspace.mkdir(exist_ok=True)
        self.runner = runner or SimpleNamespace(run_turn=None)
        self.sent = []

    async def _send_text(self, chat_id, thread_id, text, markdown=True, links=True, **kw):
        if getattr(self, "fail", False):
            raise RuntimeError("telegram down")
        self.sent.append(text)

    async def notify(self, text):
        self.sent.append(text)


def engine_for(tmp_path, claude, threads, spec_files=None):
    bot = Bot(tmp_path, SimpleNamespace(run_turn=claude))
    db = WatchDB(":memory:")
    e = WatchEngine(bot, db, tools_socket="x", tg_socket="y", data_dir=tmp_path / "data", sandbox=real_sandbox)
    space = {}

    async def tools(payload, timeout=300):
        import base64
        cmd = payload.get("cmd")
        if cmd == "watch.run":
            t = social_dm.inbox_threads({"inbox": {"threads": threads()}}, "1001")
            return {"ok": True, "text": social_dm.inbox_text(t), "threads": social_dm.redact_threads(t)}
        if cmd == "watch.space_get":
            return {"ok": True, "files": {k: base64.b64encode(v).decode() for k, v in space.items()}}
        if cmd == "watch.space_put":
            for p, d in payload["changes"].items():
                space[p] = base64.b64decode(d)
            return {"ok": True}
        return {"ok": True}

    e.tools = tools
    e.pipeline.svc.tools = tools
    spec = ws.parse(spec_files or dm_template_files())
    w = Active("personal", spec, "all", "low", "active")
    e.watchers[w.name] = w
    return e, w, bot, space


class SchemaClaude(FakeClaude):
    """Answers in whatever form the watcher's schema asks for."""

    async def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if self.answers:
            text = self.answers.pop(0)
        else:
            text = json.dumps({k: "yes" if k == "worth" else f"{SECRET} in a summary"
                               for k in json.loads(kw["json_schema"])["properties"]})
        return SimpleNamespace(ok=True, text=text, error=None)


def thread(*items, tid="1", **kw):
    return {"thread_id": tid, "thread_title": "alice", "is_group": False, "users": [{"pk": "2001", "username": "alice"}],
            "items": [{"item_id": i, "user_id": 2001, "timestamp": ts, "item_type": "text", "text": t}
                      for i, ts, t in items], **kw}


def run_once(e, w):
    e.db.enqueue("personal", w.name, "x", "time.every", [{"time": "now"}], 60, False)
    row = e.db.next_item("personal", w.name)
    return run(e.run_item(w, row))


def test_dm_run_files_logs_and_space_hold_no_dm_text(tmp_path):
    e, w, bot, space = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, SECRET))])
    assert run_once(e, w) == "ok"
    assert SECRET in bot.sent[-1]
    blobs = [p.read_text(encoding="utf-8", errors="replace") for p in (tmp_path / "data").rglob("*") if p.is_file()]
    blobs += [p.read_text(encoding="utf-8", errors="replace") for p in (tmp_path / "ws").rglob("*") if p.is_file()]
    blobs += [v.decode() for v in space.values()]
    blobs += [str([tuple(r) for r in e.db.db.execute(f"SELECT * FROM {t}")]) for t in
              ("wt_items", "wt_runs", "wt_parked", "wt_triggers")]
    assert blobs and all(SECRET not in b for b in blobs)
    run_id = e.db.runs("personal", w.name)[0]["id"]
    who = Who("private", "personal")
    for cmd in ({"cmd": "logs", "run": run_id}, {"cmd": "runs"}, {"cmd": "show"}, {"cmd": "space"}, {"cmd": "audit"}):
        out = run(Control({"personal": e}, "x").dispatch({**cmd, "name": w.name}, who))
        assert SECRET not in json.dumps(out)


def test_errors_of_a_dm_run_keep_no_dm_text(tmp_path, caplog):
    bad = '{"summary": 5, "%s": 1}' % SECRET
    e, w, bot, _ = engine_for(tmp_path, FakeClaude([bad, bad]), lambda: [thread(("a", 10, SECRET))])
    assert run_once(e, w) == "failed"
    row = e.db.runs("personal", w.name)[0]
    files = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "data").rglob("*.json"))
    assert SECRET not in json.dumps(row) and SECRET not in files and SECRET not in caplog.text
    assert "step summary failed" in row["error"]


def test_dm_ai_calls_leave_no_transcript(tmp_path):
    claude = SchemaClaude([])
    e, w, _, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, SECRET))])
    run_once(e, w)
    assert "--no-session-persistence" in (claude.calls[0][1].get("extra_args") or ())


def test_dm_watchers_may_use_no_other_operation():
    # A read of another service with DM text in its arguments would send that text out.
    toml = ('name = "w"\ndescription = "d"\n[[triggers]]\ntype = "manual"\n[[steps]]\nid = "dms"\ntype = "adapter"\n'
            'op = "igweb.inbox"\n[[steps]]\nid = "x"\ntype = "adapter"\nop = "youtube.search"\n'
            'args = { query = "{{steps.dms.text}}" }\n')
    with pytest.raises(ws.SpecError):
        ws.parse({"watcher.toml": toml.encode()})


# --- 5: social text stays out of later runs -----------------------------------------------------------------------


def test_social_conversations_are_not_written_to_the_shared_chat_logs(sbot):
    bot, api, runner, store = sbot
    runner.reply = SECRET
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("MY-SOCIAL-QUESTION")))
    text = "".join(p.read_text(encoding="utf-8") for p in bot.profile.workspace.rglob("*.md"))
    assert SECRET not in text and "MY-SOCIAL-QUESTION" not in text
    rows = [tuple(r) for r in store._db.execute("SELECT * FROM runs")]
    assert "MY-SOCIAL-QUESTION" not in str(rows)


def test_quoting_a_social_reply_in_another_turn_does_not_bring_its_text_back(sbot):
    bot, api, runner, store = sbot

    ids = iter(range(100, 200))

    async def send(chat_id, text, thread_id=None, parse_mode=None):
        api.sent.append({"chat_id": chat_id, "text": text, "thread_id": thread_id, "parse_mode": parse_mode})
        return {"message_id": next(ids)}

    api.send_message = send
    run(bot.handle_message(msg("/social")))
    runner.reply = SECRET
    run(bot.handle_message(msg("feed")))
    mid = 100 + [s["text"] for s in api.sent].index(SECRET)
    run(bot.handle_message(msg("/reset")))
    run(bot.handle_message(msg("and this?", reply_to_message={"message_id": mid, "text": SECRET})))
    assert runner.calls[-1]["origin"] is None and SECRET not in runner.calls[-1]["prompt"]
    run(bot.handle_message(msg("ok?", reply_to_message={"message_id": 7, "text": "ordinary"})))
    assert "ordinary" in runner.calls[-1]["prompt"]


def test_social_output_is_never_saved_in_the_workspace(web, tmp_path, monkeypatch, capsys):
    w = web()
    w.fake.media[list(w.fake.media)[0]]["caption"]["text"] = "x" * 400
    w.fake.comments = [{"pk": str(6000 + i), "text": f"{i} " + "y" * 590, "user": {"pk": "1", "username": "u"}}
                       for i in range(100)]
    resp = ask(w, ("igweb.post", {"target": LINK, "limit": 1000}))
    assert len(resp["text"]) < 16_000 and "-n" in resp["text"]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AMPI_ORIGIN", "forwarded:social")
    monkeypatch.setattr(cli, "request", lambda p, **k: {"ok": True, "text": "z" * 60_000})
    cli.instagram_main(["feed"])
    assert not [p for p in tmp_path.rglob("*") if p.suffix == ".md"]
    assert len(capsys.readouterr().out) < 21_000


def test_social_approvals_are_visible_only_to_social_runs(web):
    w = web()
    resp = ask(w, ("igweb.like", {"target": LINK}))
    n = resp["approval"]

    def look(origin):
        w.origin = origin
        lst = run(w.daemon.handle({"cmd": "approvals.list"}, 1))
        one = run(w.daemon.handle({"cmd": "approvals.status", "id": n}, 1))
        return json.dumps(lst) + json.dumps(one)

    for origin in ("private", "group:x", "forwarded:private", "unknown", "watcher:gmail"):
        assert "Sunset" not in look(origin) and f"#{n}" not in look(origin), origin
    assert f"#{n}" in look("forwarded:social")
    approve(w)
    for origin in ("private", "unknown"):
        assert "Liked" not in look(origin)
    # The bridge itself still sees the outcome.
    assert run(w.daemon.handle({"cmd": "approvals.status", "id": n}, BRIDGE))["status"] == "done"


def test_social_runs_do_not_see_other_requests(web):
    w = web(origin="private")
    n = w.daemon.approvals.add([{"op": "gmail.send", "args": {}}], "PRIVATE-MAIL summary", None)
    w.origin = "forwarded:social"
    assert "PRIVATE-MAIL" not in json.dumps(run(w.daemon.handle({"cmd": "approvals.list"}, 1)))
    assert not run(w.daemon.handle({"cmd": "approvals.status", "id": n}, 1))["ok"]


def test_social_approval_results_are_not_logged_for_later_turns(sbot):
    bot, api, runner, store = sbot

    async def ask_fn(socket, payload, timeout=60):
        return {"ok": True, "status": "done", "text": "Liked @bob's post", "social": True}

    async def edit(*a):
        return {}

    api.edit_message_text = edit
    api.answer_callback_query = lambda *a: edit()
    relay = ApprovalRelay(bot, "x", ask_fn)
    run(relay.handle_callback({"id": "1", "from": {"id": CHAT}, "data": "ap:5:abc:y",
                               "message": {"message_id": 9, "text": f"🔐 {SECRET}",
                                           "chat": {"id": CHAT, "type": "private"}}}))
    text = "".join(p.read_text(encoding="utf-8") for p in bot.profile.workspace.rglob("*.md"))
    assert SECRET not in text and "Liked" not in text


def test_decide_marks_social_requests_for_the_bridge(web):
    w = web()
    ask(w, ("igweb.like", {"target": LINK}))
    (item,) = run(w.daemon.handle({"cmd": "approvals.pending"}, BRIDGE))["items"]
    assert item["social"] is True
    assert approve(w)["social"] is True


# --- 6: social runs can only use Instagram and Threads ----------------------------------------------------------


@pytest.mark.parametrize("op", ["info.fields", "gmail.search", "todoist.tasks", "telegram.send", "maps.search",
                                "wolt.status", "youtube.feed", "payment.log", "checkout.open"])
def test_other_services_are_refused_in_social_mode(web, op):
    w = web()
    resp = ask(w, (op, {}))
    assert not resp["ok"] and "social mode" in resp["error"]


def test_batches_are_refused_in_social_mode(web):
    w = web()
    assert not ask(w, ("igweb.feed", {}), ("igweb.stories", {}))["ok"]
    assert w.fake.calls == []


def test_no_relaxed_changes_from_social_mode(web):
    from ampi_tools.ops import Policy
    w = web(policy=Policy(auto_approve=frozenset({"threads.hide"})))
    ran = []

    async def prep(s, a):
        return {"id": "1", "username": "x", "text": "t"}

    import ampi_tools.ops as ops
    old = ops.PREPARE["threads.hide"]
    ops.PREPARE["threads.hide"] = prep
    try:
        resp = ask(w, ("threads.hide", {"target": "1"}))
    finally:
        ops.PREPARE["threads.hide"] = old
    assert resp.get("approval") and ran == []


@pytest.mark.parametrize("main,argv", [(cli.instagram_main, ["apply", "p.json"]), (cli.threads_main, ["apply", "p.json"]),
                                       (cli.threads_main, ["publish", "--text-file", "f.txt"]),
                                       (cli.instagram_main, ["comment", LINK, "--text-file", "f.txt"])])
def test_plans_and_text_files_are_gone_in_social_mode(monkeypatch, main, argv, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.json").write_text('[{"op": "gmail.search", "args": {"query": "x"}}]')
    (tmp_path / "f.txt").write_text("private notes")
    monkeypatch.setenv("AMPI_ORIGIN", "forwarded:social")
    sent = []
    monkeypatch.setattr(cli, "request", lambda p, **k: sent.append(p) or {"ok": True, "text": ""})
    with pytest.raises(SystemExit):
        main(argv)
    assert sent == []


# --- 7: the tools of a social run --------------------------------------------------------------------------------


def test_social_runs_have_no_built_in_tools_and_no_profile_settings(sbot):
    bot, api, runner, store = sbot
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("feed")))
    call = runner.calls[-1]
    assert call["tools"] == ""
    assert set(call["allowed_tools"]) == {"mcp__social__instagram", "mcp__social__threads"}
    assert call["setting_sources"] == ""
    assert "social_mcp" in " ".join(call["extra_args"])
    # Without this the run can start before its only tools are there (seen with the real CLI, 2.1.133).
    assert call["extra_env"]["MCP_CONNECTION_NONBLOCKING"] == "0"
    args = build_args(["claude"], session_id="s", resume=True, model=None, allowed_tools=call["allowed_tools"],
                      append_system_prompt=None, extra_args=call["extra_args"], tools="", setting_sources="")
    assert args[args.index("--tools") + 1] == "" and args[args.index("--setting-sources") + 1] == ""


def test_profile_path_rules_and_extra_args_do_not_reach_social_runs(sbot):
    import dataclasses
    bot, api, runner, store = sbot
    bot.profile = dataclasses.replace(bot.profile, allowed_tools=("Read(//**)", "Grep", "Bash(instagram:*)"),
                                      extra_args=("--add-dir", "/"))
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("feed")))
    call = runner.calls[-1]
    assert call["allowed_tools"] == ("mcp__social__instagram",)
    assert "--add-dir" not in call["extra_args"]


def test_the_social_mcp_server_runs_only_the_two_commands():
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "instagram", "arguments": {"args": ["apply", "x.json"]}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "gmail", "arguments": {"args": ["search", "x"]}}}]
    import os
    p = subprocess.run([sys.executable, "-m", "assistant_bridge.social_mcp"], input="\n".join(map(json.dumps, reqs)) + "\n",
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "AMPI_SOCIAL_TOOLS": "instagram"})
    out = {m["id"]: m["result"] for m in map(json.loads, p.stdout.splitlines())}
    assert [t["name"] for t in out[2]["tools"]] == ["instagram"]
    assert out[3]["isError"] and out[4]["isError"], p.stderr


# --- 8, 10: starting the DM watcher ------------------------------------------------------------------------------


def test_a_retry_is_not_an_owner_run(tmp_path):
    e, w, _, _ = engine_for(tmp_path, FakeClaude([]), lambda: [])
    e.manual(w.name, "the owner (button)", owner=True)
    row = e.db.next_item("personal", w.name)
    assert row["item"]["owner"] is True
    run_id = e.db.start_run("personal", w.name, "h", row["id"], "manual")
    e.db.update_run(run_id, status="failed")
    e.db.set_item(row["id"], "failed", run_id)
    run(Control({"personal": e}, "x").dispatch({"cmd": "retry", "run": run_id}, Who("private", "personal")))
    assert not e.db.next_item("personal", w.name)["item"].get("owner")


def test_the_first_social_start_works_right_after_boot_and_only_success_is_stamped(monkeypatch):
    from assistant_bridge.watch import control as cm
    import time
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)  # a machine that just booted
    replies = ["instagram-dms isn't an active watcher.", "Queued a run of instagram-dms.", "Queued a run."]
    e = SimpleNamespace(profile=SimpleNamespace(name="personal"), db=SimpleNamespace(busy_count=lambda p, n: 0),
                        manual=lambda n, by, owner=False: replies.pop(0))
    c = Control({"personal": e}, "x")
    who = c.who(Caller("forwarded:social", ("social", "personal")))
    req = {"cmd": "run", "name": "instagram-dms"}
    assert "isn't an active" in run(c.dispatch(req, who))["text"]
    assert "Queued" in run(c.dispatch(req, who))["text"]
    assert "10 minutes" in run(c.dispatch(req, who))["text"]
    assert cm.SOCIAL_RUN_GAP == 600


# --- 9: no lost messages --------------------------------------------------------------------------------------------


def dm_prompts(claude):
    return [c[0] for c in claude.calls]


def test_more_than_five_messages_are_all_reported(tmp_path):
    items = [(f"m{i:02}", 100 + i, f"msg-{i:02}") for i in range(12)]
    claude = SchemaClaude([])
    e, w, _, _ = engine_for(tmp_path, claude, lambda: [thread(*reversed(items))])
    run_once(e, w)
    assert all(f"msg-{i:02}" in "".join(dm_prompts(claude)) for i in range(12))


def test_big_backlogs_come_in_bounded_batches_without_loss(tmp_path):
    items = [(f"m{i:03}", 100 + i, f"msg-{i:03}-" + "x" * 300) for i in range(90)]
    claude = SchemaClaude([])
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(*reversed(items))])
    while run_once(e, w) == "ok":
        pass
    prompts = dm_prompts(claude)
    assert all(len(p) < 16_000 for p in prompts) and len(prompts) > 1
    assert all(sum(f"msg-{i:03}-" in p for p in prompts) == 1 for i in range(90))
    assert any("more" in t for t in bot.sent[:1])


def test_the_v2_dm_message_is_not_written_to_the_chat_logs(sbot, tmp_path):
    # (passed before the fixes too: bot.run_watcher, which logs reports, is the old watchers' path)
    bot = sbot[0]
    e = WatchEngine(bot, WatchDB(":memory:"), tools_socket="x", tg_socket="y", data_dir=tmp_path / "data")
    spec = ws.parse(dm_template_files())
    e.watchers[spec.name] = Active("personal", spec, "all", "low", "active")
    run(e._notify_text(SECRET, "markdown", spec.name))
    assert SECRET in bot.api.sent[-1]["text"]
    assert not any(SECRET in p.read_text(encoding="utf-8") for p in bot.profile.workspace.rglob("*.md"))


@pytest.mark.parametrize("what", ["notify", "llm"])
def test_a_failed_delivery_is_repeated_not_lost(tmp_path, what):
    claude = SchemaClaude([] if what == "notify" else ["nonsense", "nonsense"])
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "hello-once"))])
    bot.fail = what == "notify"
    assert run_once(e, w) == "failed"
    bot.fail = False
    assert run_once(e, w) == "ok" and "hello-once" in dm_prompts(claude)[-1]
    assert run_once(e, w) == "stopped"


def test_messages_with_the_same_timestamp_are_not_dropped(tmp_path):
    items = [("a", 10, "first-one")]
    claude = SchemaClaude([])
    e, w, _, _ = engine_for(tmp_path, claude, lambda: [thread(*items)])
    run_once(e, w)
    items.append(("b", 10, "second-one"))
    assert run_once(e, w) == "ok"
    assert "second-one" in dm_prompts(claude)[-1] and "first-one" not in dm_prompts(claude)[-1]


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"x"', b'{"1": "junk"}', b'{"1": {"ts": "z", "ids": 4}}',
                                 b"\xff\xfe"])
def test_a_corrupt_cursor_file_is_tolerated(tmp_path, raw):
    claude = SchemaClaude([])
    e, w, _, space = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "hello"))])
    space["cursors.json"] = raw
    assert run_once(e, w) == "ok"


def test_an_unloaded_gap_is_said(tmp_path):
    claude = SchemaClaude([])
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "hello"), has_older=True)])
    run_once(e, w)
    assert "earlier" in bot.sent[-1]


def test_an_answer_cannot_suppress_the_report(tmp_path):
    claude = SchemaClaude(['{"worth": "no", "summary": "only spam"}'])
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "hello"))])
    assert run_once(e, w) == "ok" and bot.sent
    assert "worth" not in (Path(__file__).parents[1] / "templates/watchers-v2/instagram-dms/watcher.toml").read_text(
        encoding="utf-8")


# --- 11: defang -----------------------------------------------------------------------------------------------------


def test_defang_is_unchanged_for_other_watchers():
    assert _defang("see https://a.com/x [l](y) `c`") == "see https://a.com/x (l)(y) 'c'"


@pytest.mark.parametrize("text,gone", [
    ("https://evil.zip/x", "https://"), ("evil.zip", "evil.zip"), ("a.museum", "a.museum"), ("пример.рф", "пример.рф"),
    ("t.me/abc", "t.me"), ("1.2.3.4", "1.2.3.4"), ("evil。com", "evil。com"), ("@someone", "@someone"),
    ("tg://resolve?x", "tg://"), ("/reset", "/reset"),
])
def test_defang_for_social_watchers_covers_every_address(text, gone):
    assert gone not in _defang({"a": [text]}, True)["a"][0]


def test_social_defang_keeps_ordinary_text():
    assert _defang("Costs 3.50, ok. See you!", True) == "Costs 3.50, ok. See you!"


# --- 12: finding comments ------------------------------------------------------------------------------------------


class Pages:
    def __init__(self, pages, children=None):
        self.pages, self.children, self.calls = pages, children or {}, []

    async def call(self, name, ids=None, params=None):
        self.calls.append((name, ids, params))
        if name == "ig.child_comments":
            return {"child_comments": self.children.get(ids["comment"], [])}
        return self.pages[min(sum(c[0] == "ig.media_comments" for c in self.calls), len(self.pages)) - 1]


def cm(pk, **kw):
    return {"pk": pk, "text": "t", "user": {"pk": "1", "username": "u"}, **kw}


def test_headload_and_older_pages_are_followed():
    for flag, key in (("has_more_headload_comments", "next_min_id"), ("has_more_comments", "next_max_id")):
        c = Pages([{"comments": [cm("1")], flag: True, key: "c2"}, {"comments": [cm("2")]}])
        assert run(oss._find_comment(c, "9", "2"))["pk"] == "2", flag
        assert c.calls[1][2] == {key.removeprefix("next_"): "c2"}


def test_a_repeated_cursor_stops_the_search():
    c = Pages([{"comments": [cm("1")], "has_more_comments": True, "next_min_id": "same"}])
    with pytest.raises(Exception, match="wasn't found"):
        run(oss._find_comment(c, "9", "404"))
    assert len(c.calls) == 2


def test_a_known_parent_is_searched_first():
    c = Pages([{"comments": [cm("1", child_comment_count=3)]}], {"77": [cm("78", parent_comment_id="77")]})
    assert run(oss._find_comment(c, "9", "78", parent="77"))["pk"] == "78"
    assert [x[0] for x in c.calls] == ["ig.child_comments"]


def test_the_recheck_of_a_reply_uses_its_parent(web):
    w = web()
    w.fake.comments[0]["child_comment_count"] = 1
    w.fake.children = {"5001": [cm("5010", parent_comment_id="5001")]}
    ask(w, ("igweb.like", {"target": LINK, "comment": "5010"}))
    n = len(w.fake.calls)
    assert approve(w)["status"] == "done"
    assert not any(c.path.endswith("/comments/") for c in w.fake.calls[n:])


_ = (io, RunState, Pipeline, PipeServices, FakeAPI, FakeRunner)
