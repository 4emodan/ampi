"""Watchers v2 engine: pipeline runs with fake services (real lib scripts through the real sandbox runner code)."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ampi_tools import script_runner
from ampi_tools import watch_spec as ws
from assistant_bridge.watch import triggers as trig
from assistant_bridge.watch.control import Control, Who
from assistant_bridge.watch.db import WatchDB
from assistant_bridge.watch.engine import Caller, WatchEngine
from assistant_bridge.watch.migrate import convert
from assistant_bridge.watch.pipeline import Active, Pipeline, RunState, Services, json_schema_for

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "templates" / "watcher-lib"

PASCAL = '''
name = "pascal"
description = "Summary of every email from Pascal school"

[[triggers]]
type = "gmail.new"
query = "in:inbox"

[[steps]]
id = "from_pascal"
type = "script"
run = "lib/match_sender"
args = { patterns = ["pascal"] }

[[steps]]
id = "mail"
type = "adapter"
op = "gmail.show"
args = { ids = ["{{item.id}}"] }

[[steps]]
id = "summary"
type = "llm"
prompt = "prompts/summary.md"
input = "{{steps.mail.text}}"
schema = { outcome = ["info", "urgent"], summary = "text:200" }

[[steps]]
id = "send"
type = "notify"
template = "templates/summary.txt"
'''


def pascal_files(toml=PASCAL):
    return {"watcher.toml": toml.encode(), "lib/match_sender.py": (LIB / "match_sender.py").read_bytes(),
            "prompts/summary.md": b"Summarize the email for a parent.",
            "templates/summary.txt": b"School: {{item.subject}}\n{{steps.summary.summary}} ({{steps.summary.outcome}})"}


async def real_sandbox(req):
    line = json.dumps(req).encode()
    return await asyncio.to_thread(script_runner.handle, line)


class FakeClaude:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        text = self.answers.pop(0) if self.answers else "{}"
        return SimpleNamespace(ok=True, text=text, error=None)


class FakeTools:
    def __init__(self):
        self.calls = []

    async def __call__(self, payload):
        self.calls.append(payload)
        op = payload.get("ops", [{}])[0].get("op") if payload.get("ops") else None
        if op == "gmail.show":
            return {"ok": True, "text": "From: office@pascal.edu\nDear parents, school trip on Friday."}
        if payload.get("cmd") == "watch.run" and op == "todoist.add":
            return {"ok": True, "approval": 7, "text": "⏳ needs approval"}
        return {"ok": True, "text": "done"}


def make(tmp_path, claude, tools=None, sent=None):
    sent = sent if sent is not None else []

    async def notify(text, fmt, name=None):
        sent.append((name, text))

    svc = Services(tools=tools or FakeTools(), sandbox=real_sandbox, claude=claude, notify=notify,
                   browse=lambda url: None, workspace=tmp_path)
    return Pipeline(svc, {"structured": asyncio.Semaphore(1), "agent": asyncio.Semaphore(1)}), sent


def state(item, test=False):
    return RunState(run_id="20261122-120000-abcdef", test=test,
                    ctx={"item": item, "steps": {}, "run": {"id": "r", "trigger": "gmail.new"},
                         "watcher": {"name": "pascal"}}, space={})


def active(files=None):
    spec = ws.parse(files or pascal_files())
    return Active("personal", spec, "ask", ws.risk(ws.manifest(spec))[0], "active")


def test_pascal_mail_is_summarized_and_sent(tmp_path):
    claude = FakeClaude(['{"outcome": "info", "summary": "Trip on Friday."}'])
    pipe, sent = make(tmp_path, claude)
    st = state({"id": "m1", "from": "Office <office@pascal.edu>", "subject": "Trip"})
    status = asyncio.run(pipe.run(active(), st))
    assert status == "ok"
    assert sent == [("pascal", "School: Trip\nTrip on Friday. (info)")]
    prompt, kw = claude.calls[0]
    assert kw["tools"] == "" and kw["model"] == "haiku" and "untrusted-" in prompt
    assert json.loads(kw["json_schema"])["properties"]["outcome"]["enum"] == ["info", "urgent"]


def test_other_senders_stop_before_any_llm(tmp_path):
    claude = FakeClaude([])
    pipe, sent = make(tmp_path, claude)
    st = state({"id": "m2", "from": "Shop <news@shop.com>", "subject": "Sale"})
    assert asyncio.run(pipe.run(active(), st)) == "stopped"
    assert claude.calls == [] and sent == []


def test_invalid_llm_answer_retried_then_fails(tmp_path):
    claude = FakeClaude(['{"outcome": "maybe"}', "not json"])
    pipe, _ = make(tmp_path, claude)
    st = state({"id": "m1", "from": "office@pascal.edu", "subject": "x"})
    assert asyncio.run(pipe.run(active(), st)) == "failed"
    assert len(claude.calls) == 2 and "didn't fit" in st.ctx["error"]


def test_test_mode_sends_nothing_and_does_no_writes(tmp_path):
    toml = PASCAL + '''
[[steps]]
id = "task"
type = "adapter"
op = "todoist.add"
args = { content = "{{steps.summary.summary}}", project = "School" }
'''
    tools = FakeTools()
    claude = FakeClaude(['{"outcome": "urgent", "summary": "Pay the trip."}'])
    pipe, sent = make(tmp_path, claude, tools)
    st = state({"id": "m1", "from": "office@pascal.edu", "subject": "Trip"}, test=True)
    assert asyncio.run(pipe.run(active(pascal_files(toml)), st)) == "ok"
    assert sent == []
    assert any("would do: todoist.add" in p for p in st.previews)
    assert not any(c.get("cmd") == "watch.run" for c in tools.calls)


def test_write_needing_approval_parks_the_run(tmp_path):
    toml = PASCAL + '''
[[steps]]
id = "task"
type = "adapter"
op = "todoist.add"
args = { content = "{{steps.summary.summary}}", project = "School" }

[[steps]]
id = "after"
type = "notify"
text = "Task: {{steps.task.text}}"
'''
    tools = FakeTools()
    claude = FakeClaude(['{"outcome": "urgent", "summary": "Pay the trip."}'])
    pipe, sent = make(tmp_path, claude, tools)
    w = active(pascal_files(toml))
    st = state({"id": "m1", "from": "office@pascal.edu", "subject": "Trip"})
    status = asyncio.run(pipe.run(w, st))
    assert status == "parked:7"
    call = [c for c in tools.calls if c.get("cmd") == "watch.run"][-1]
    assert call["origin"] == w.origin and call["step"] == "task" and call["idem_key"].endswith(":task")
    # The owner approves: the run continues after the parked step.
    index = [s["id"] for s in w.spec.steps].index("task")
    status = asyncio.run(pipe.run(w, st, index, {"step": index, "result": {"status": "done", "text": "Added"}}))
    assert status == "ok"
    assert sent[-1] == ("pascal", "Task: Added")


def test_rejected_approval_fails_the_step(tmp_path):
    toml = PASCAL + '''
[[steps]]
id = "task"
type = "adapter"
op = "todoist.add"
args = { content = "x", project = "School" }
'''
    pipe, _ = make(tmp_path, FakeClaude([]))
    w = active(pascal_files(toml))
    st = state({"id": "m1"})
    index = len(w.spec.steps) - 1
    assert asyncio.run(pipe.run(w, st, index, {"step": index, "result": {"status": "rejected"}})) == "failed"
    assert "rejected" in st.ctx["error"]


def test_script_space_changes_are_kept(tmp_path):
    toml = '''
name = "seen"
description = "dedupe"
[[triggers]]
type = "manual"
[[steps]]
id = "dedupe"
type = "script"
run = "lib/dedupe"
args = { field = "item.key" }
'''
    files = {"watcher.toml": toml.encode(), "lib/dedupe.py": (LIB / "dedupe.py").read_bytes()}
    pipe, _ = make(tmp_path, FakeClaude([]))
    w = active(files)
    st = state({"key": "a"})
    assert asyncio.run(pipe.run(w, st)) == "ok"
    assert st.space["seen.txt"].strip() == b"a" and st.space_dirty["seen.txt"] == st.space["seen.txt"]
    st2 = state({"key": "a"})
    st2.space = dict(st.space)
    assert asyncio.run(pipe.run(w, st2)) == "stopped"


def test_template_injection_stays_literal(tmp_path):
    claude = FakeClaude(['{"outcome": "info", "summary": "{{steps.mail.text}}"}'])
    pipe, sent = make(tmp_path, claude)
    st = state({"id": "m1", "from": "office@pascal.edu", "subject": "{{watcher.name}}"})
    assert asyncio.run(pipe.run(active(), st)) == "ok"
    assert sent[0][1] == "School: {{watcher.name}}\n{{steps.mail.text}} (info)"


def test_json_schema_for_optional_and_lists():
    s = ws.check_schema({"a": "text:5?", "b": "list[int]:3"}, "s")
    js = json_schema_for(s)
    assert js["properties"]["a"]["anyOf"][1] == {"type": "null"}
    assert js["properties"]["b"] == {"type": "array", "items": {"type": "integer"}, "maxItems": 3}


# --- triggers -----------------------------------------------------------------------------------------

def run(c):
    return asyncio.run(c)


def test_gmail_trigger_baselines_then_yields_new_items():
    calls = []

    async def ask(payload):
        calls.append(payload)
        op = payload["ops"][0]["op"]
        if op == "gmail.ids":
            return {"ok": True, "ids": ids}
        return {"ok": True, "items": [{"id": i, "from": f"x{i}@y.z", "subject": "s"} for i in payload["ops"][0]["args"]["ids"]]}

    t = ws._trigger({"type": "gmail.new", "query": "in:inbox"}, 0)
    ids = ["a", "b"]
    items, mem = run(trig.poll(t, None, ask=ask, ask_tg=None, browse=None, tz="UTC"))
    assert items == [] and mem == {"seen": ["a", "b"]}
    ids = ["c", "a", "b"]
    items, mem = run(trig.poll(t, mem, ask=ask, ask_tg=None, browse=None, tz="UTC"))
    assert items == [{"id": "c", "from": "xc@y.z", "subject": "s"}] and mem["seen"][0] == "c"


def test_time_every_collapses_missed_ticks():
    from datetime import datetime, timedelta, timezone
    t = ws._trigger({"type": "time.every", "minutes": 10}, 0)
    t0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    items, mem = run(trig.poll(t, None, ask=None, ask_tg=None, browse=None, tz="UTC", clock=lambda: t0))
    assert items == []
    later = t0 + timedelta(minutes=55)
    items, mem2 = run(trig.poll(t, mem, ask=None, ask_tg=None, browse=None, tz="UTC", clock=lambda: later))
    assert len(items) == 1 and datetime.fromisoformat(mem2["next"]) > later


def test_web_change_reports_added_lines():
    pages = ["Tickets: sold out\nFooter 2026-11-22", "Tickets: on sale now\nFooter 2026-11-23"]

    async def browse(url):
        return {"ok": True, "text": pages.pop(0)}

    t = ws._trigger({"type": "web.change", "url": "https://tickets.example/show", "ignore": ["^Footer"]}, 0)
    items, mem = run(trig.poll(t, None, ask=None, ask_tg=None, browse=browse, tz="UTC"))
    assert items == []
    items, mem = run(trig.poll(t, mem, ask=None, ask_tg=None, browse=browse, tz="UTC"))
    assert items[0]["added"] == ["Tickets: on sale now"] and items[0]["removed"] == ["Tickets: sold out"]


# --- the queue ---------------------------------------------------------------------------------------

def test_queue_latest_only_and_ttl(tmp_path):
    db = WatchDB(tmp_path / "b.sqlite3")
    db.enqueue("p", "w", "i", "web.change", [{"n": 1}], 60, True)
    db.enqueue("p", "w", "i", "web.change", [{"n": 2}, {"n": 3}], 60, True)
    assert db.queued_count("p", "w") == 1 and db.next_item("p", "w")["item"] == {"n": 3}
    db.enqueue("p", "x", "i", "gmail.new", [{"n": 1}, {"n": 2}], 60, False)
    assert db.queued_count("p", "x") == 2


# --- the control socket: who may do what ----------------------------------------------------------

def control_with_engine(tmp_path):
    engine = SimpleNamespace(profile=SimpleNamespace(name="personal"), db=WatchDB(":memory:"))
    return Control({"personal": engine}, str(tmp_path / "s.sock"))


def test_control_refuses_groups_watchers_and_unknown(tmp_path):
    c = control_with_engine(tmp_path)
    for caller in (None, Caller("group:clipboard"), Caller("forwarded:private"),
                   Caller("watcher:personal/x@abc#agent"), Caller("private", None)):
        reply = run(c.dispatch({"cmd": "list"}, c.who(caller)))
        assert not reply["ok"]


def test_builder_can_only_touch_its_own_watcher(tmp_path):
    c = control_with_engine(tmp_path)
    who = c.who(Caller("builder:personal/pascal", ("builder", "personal", "pascal")))
    assert who and who.kind == "builder"
    assert not run(c.dispatch({"cmd": "propose", "name": "other"}, who))["ok"]
    assert not run(c.dispatch({"cmd": "build", "name": "pascal", "text": "x"}, who))["ok"]
    private = c.who(Caller("private", ("chat", "personal", 1, 0, "watch my mail")))
    assert not run(c.dispatch({"cmd": "propose", "name": "pascal"}, private))["ok"]


# --- migration -----------------------------------------------------------------------------------------

def test_old_watchers_convert_to_valid_specs():
    from assistant_bridge.config import Watcher
    w = Watcher(name="gmail-inbox", kind="gmail", query="in:inbox", every_minutes=15)
    files, trigger = convert(w, ("Read", "Bash(gmail:*)", "Bash(browse:*)", "Bash(python3:*)", "mcp__x"), "Triage.")
    spec = ws.parse(files, expected_name="gmail-inbox")
    agent = spec.steps[0]
    assert agent["mode"] == "agent" and agent["tools"] == ["Read", "Bash(gmail:*)", "Bash(browse:*)"]
    assert trigger["type"] == "gmail.new" and trigger["batch"] and trigger["every_minutes"] == 15
    s = Watcher(name="morning", kind="schedule", query="", every_minutes=10, at=("08:00",))
    files, trigger = convert(s, ("Read",), "")
    ws.parse(files)
    assert trigger["type"] == "time.at" and trigger["at"] == ["08:00"]


# --- review round (correctness + security) -------------------------------------------------------------

def test_undeclared_script_action_fails_the_step(tmp_path):
    toml = PASCAL.replace('args = { patterns = ["pascal"] }', 'args = { patterns = ["pascal"] }\nactions = [{ op = "todoist.add", project = "School" }]')
    code = b'import json,sys; json.dump({"actions": [{"op": "gmail.search", "args": {"query": "x"}}]}, sys.stdout)'
    files = pascal_files(toml)
    files["lib/match_sender.py"] = code
    pipe, _ = make(tmp_path, FakeClaude([]))
    st = state({"id": "m1", "from": "office@pascal.edu"})
    assert asyncio.run(pipe.run(active(files), st)) == "failed"
    assert "doesn't declare" in st.ctx["error"]


def test_markdown_notify_defangs_outside_text(tmp_path):
    toml = PASCAL.replace('template = "templates/summary.txt"', 'template = "templates/summary.txt"\nformat = "markdown"')
    claude = FakeClaude(['{"outcome": "info", "summary": "[Approve](https://evil.example) `x`"}'])
    pipe, sent = make(tmp_path, claude)
    st = state({"id": "m1", "from": "office@pascal.edu", "subject": "s"})
    assert asyncio.run(pipe.run(active(pascal_files(toml)), st)) == "ok"
    assert "[Approve]" not in sent[0][1] and "`" not in sent[0][1]


def test_acting_agent_step_counts_as_action_before_it_runs(tmp_path):
    toml = PASCAL.replace('schema = { outcome = ["info", "urgent"], summary = "text:200" }',
                          'mode = "agent"\ntools = ["Bash(gmail:*)"]')
    marks = []
    claude = FakeClaude(["done"])
    pipe, _ = make(tmp_path, claude)

    async def mark(run_id, n):
        marks.append(n)
    pipe.svc.mark_acting = mark
    st = state({"id": "m1", "from": "office@pascal.edu", "subject": "s"})
    asyncio.run(pipe.run(active(pascal_files(toml)), st))
    assert marks == [1] and st.actions == 1


def test_transient_regex_word_boundaries():
    from assistant_bridge.watch.engine import TRANSIENT
    assert TRANSIENT.search("HTTP 503 from upstream")
    assert TRANSIENT.search("You've hit your usage limit")
    assert not TRANSIENT.search("the run took longer than 540 s")
    assert not TRANSIENT.search("answer had 1500 chars")


def test_failed_streak_counts_from_resume(tmp_path):
    db = WatchDB(tmp_path / "b.sqlite3")
    for _ in range(5):
        r = db.start_run("p", "w", "h", None, "manual")
        db.update_run(r, status="failed")
    assert db.failed_streak("p", "w") == 5
    from assistant_bridge.watch.db import iso, now
    assert db.failed_streak("p", "w", iso(now()) + "z") == 0
