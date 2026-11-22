"""Watchers v2: the keeper in ampi-tools (approved snapshots, manifest checks, grants, idempotency) and the bridge's
rendering of choice buttons."""

import asyncio
import base64
import json
import time

import httpx
import pytest
from test_ampi_tools import BRIDGE_PID, FakeTodoistServer, writes
from test_bot import OWNER, setup  # noqa: F401 (setup is a fixture)
from test_watchers import ButtonAPI, FakeTools, callback

from ampi_tools import watch_spec as ws
from ampi_tools.keeper import Keeper, parse_origin, route_fits
from ampi_tools.ops import Approvals, Policy, Services
from ampi_tools.server import Daemon
from ampi_tools.todoist import Todoist
from assistant_bridge.approvals import ApprovalRelay, buttons_for


def run(coro):
    return asyncio.run(coro)


TASKER = '''
name = "tasker"
description = "Adds a task for every new thing"

[[triggers]]
type = "time.every"
minutes = 60

[[steps]]
id = "pick"
type = "script"
run = "lib/pick"
args = { fields = { title = "item.title" } }

[[steps]]
id = "add"
type = "adapter"
op = "todoist.add"
args = { content = "{{steps.pick.title}}", project = "Work" }
'''

AGENT = '''
name = "agent"
description = "An agent that may add tasks"

[[triggers]]
type = "manual"

[[steps]]
id = "think"
type = "llm"
mode = "agent"
prompt = "prompts/p.md"
tools = ["Bash(todoist:*)"]
actions = [{ op = "todoist.add", project = "Work", section = "*", parent = "*" }]
'''

MAILER = '''
name = "mailer"
description = "Sends a fixed email"

[[triggers]]
type = "manual"

[[steps]]
id = "send"
type = "adapter"
op = "gmail.send"
args = { to = "me@example.com", subject = "hi", body = "x" }
'''


def files(toml, desc=None, extra=None):
    if desc:
        toml = toml.replace('description = "Adds a task for every new thing"', f'description = "{desc}"')
    f = {"watcher.toml": toml.encode()}
    if "lib/pick" in toml:
        f["lib/pick.py"] = b"print('{}')"
    if "prompts/p.md" in toml:
        f["prompts/p.md"] = b"Do things."
    f.update(extra or {})
    return {p: base64.b64encode(d).decode() for p, d in f.items()}


@pytest.fixture
def keeper_daemon(tmp_path):
    td_server = FakeTodoistServer()
    td = Todoist("tok", httpx.AsyncClient(transport=httpx.MockTransport(td_server)))
    services = Services(todoist_client=td)

    async def trusted(pid):
        return pid == BRIDGE_PID

    approvals = Approvals(tmp_path / "a.sqlite3")
    daemon = Daemon(services, approvals, Policy(), trusted, keeper=Keeper(tmp_path / "w.sqlite3"))
    return daemon, td_server


def call(daemon, req, pid=BRIDGE_PID):
    return run(daemon.handle(req, pid))


def propose(daemon, toml, name="tasker", **kw):
    return call(daemon, {"cmd": "watch.propose", "profile": "personal", "name": name, "files": files(toml, **kw),
                         "request": "add a task for each thing", "requester": "Ampi in your chat",
                         "test_text": "would add: Buy milk"})


def pending(daemon):
    return call(daemon, {"cmd": "approvals.pending"})["items"]


def decide(daemon, item, choice, confirmed=False, nonce=None):
    return call(daemon, {"cmd": "approvals.decide", "id": item["id"], "nonce": nonce or item["nonce"],
                         "message_id": None, "choice": choice, "confirmed": confirmed, "approve": choice != "reject"})


def activate(daemon, toml, choice="all", name="tasker", **kw):
    resp = propose(daemon, toml, name=name, **kw)
    assert resp["ok"], resp
    item = next(i for i in pending(daemon) if i["id"] == resp["approval"])
    out = decide(daemon, item, choice, confirmed=True)
    assert out["ok"] and out["status"] == "done", out
    return resp["hash"]


def act(daemon, h, op="todoist.add", args=None, step="add", idem=None, ttl=1800, name="tasker"):
    req = {"cmd": "watch.run", "origin": f"watcher:personal/{name}@{h[:12]}", "step": step,
           "ops": [{"op": op, "args": args if args is not None else {"content": "Buy milk", "project": "Work"}}],
           "ttl_seconds": ttl}
    if idem:
        req["idem_key"] = idem
    return call(daemon, req)


def test_origin_parsing():
    assert parse_origin("watcher:personal/tasker@0123456789ab#think") == ("personal", "tasker", "0123456789ab", "think")
    assert parse_origin("watcher:personal/tasker@0123456789ab") == ("personal", "tasker", "0123456789ab", None)
    assert parse_origin("watcher:gmail-inbox") is None
    assert parse_origin("watcher:personal/tasker@xyz") is None


def test_watch_commands_are_only_for_the_bridge(keeper_daemon):
    daemon, _ = keeper_daemon
    resp = call(daemon, {"cmd": "watch.propose", "profile": "personal", "name": "tasker", "files": files(TASKER)}, pid=1)
    assert not resp["ok"] and "owner" in resp["error"]
    resp = call(daemon, {"cmd": "watch.run", "origin": "watcher:personal/tasker@0123456789ab", "step": "add",
                         "ops": [{"op": "todoist.add", "args": {"content": "x"}}]}, pid=1)
    assert not resp["ok"]


def test_propose_shows_choices_and_the_frozen_manifest(keeper_daemon):
    daemon, _ = keeper_daemon
    resp = propose(daemon, TASKER)
    assert resp["ok"] and resp["risk"] == "medium"
    (item,) = pending(daemon)
    assert [c["key"] for c in item["choices"]] == ["all", "ask", "reject"]
    assert "confirm_keys" not in item  # only high risk needs two taps
    assert "New watcher “tasker”" in item["summary"] and "todoist.add → project: Work" in item["summary"]
    assert "│ would add: Buy milk" in item["summary"]  # test output quoted as data
    assert resp["hash"][:12] in item["summary"]
    # One proposal at a time per watcher.
    again = propose(daemon, TASKER, desc="Other")
    assert not again["ok"] and "already waiting" in again["error"]
    # An invalid snapshot is refused before anything is stored.
    bad = call(daemon, {"cmd": "watch.propose", "profile": "personal", "name": "other",
                        "files": files(TASKER.replace('"tasker"', '"other"'), extra={"scripts/evil.py": b"x"})})
    assert not bad["ok"] and "not used" in bad["error"]


def test_allow_all_runs_declared_actions_without_asking(keeper_daemon):
    daemon, td = keeper_daemon
    h = activate(daemon, TASKER, "all")
    got = call(daemon, {"cmd": "watch.get", "profile": "personal", "name": "tasker"})
    assert got["ok"] and got["hash"] == h and got["mode"] == "all" and "lib/pick.py" in got["files"]
    resp = act(daemon, h, idem="run1:add")
    assert resp["ok"] and "approval" not in resp, resp
    assert ("POST", "/tasks") in writes(td)
    assert pending(daemon) == []
    audit = call(daemon, {"cmd": "watch.audit", "profile": "personal", "name": "tasker"})["items"]
    assert audit[0]["status"] == "done" and audit[0]["op"] == "todoist.add"
    # The same idempotency key never acts twice.
    n = len(writes(td))
    again = act(daemon, h, idem="run1:add")
    assert again["ok"] and again["replay"] and len(writes(td)) == n


def test_wrong_hash_is_refused(keeper_daemon):
    daemon, td = keeper_daemon
    activate(daemon, TASKER, "all")
    resp = act(daemon, "0" * 64)
    assert not resp["ok"] and "approved version" in resp["error"] and writes(td) == []


def test_undeclared_op_is_refused_without_asking(keeper_daemon):
    daemon, td = keeper_daemon
    h = activate(daemon, TASKER, "all")
    resp = act(daemon, h, op="todoist.delete", args={"id": "1"})
    assert not resp["ok"] and "didn't declare" in resp["error"]
    resp = act(daemon, h, step="pick")  # declared op, but not by this step
    assert not resp["ok"] and "didn't declare" in resp["error"]
    assert pending(daemon) == [] and writes(td) == []


def test_route_outside_the_declared_one_is_refused(keeper_daemon):
    daemon, td = keeper_daemon
    h = activate(daemon, TASKER, "all")
    resp = act(daemon, h, args={"content": "x", "project": "Inbox"})
    assert not resp["ok"] and "didn't declare" in resp["error"] and writes(td) == []
    assert route_fits("todoist.add", {"project": ["Home", "Work"]}, {"project": "work"})
    assert not route_fits("todoist.add", {"project": ["Home", "Work"]}, {"project": "Inbox"})
    assert route_fits("gmail.send", {"to": ["a@x.com", "b@x.com"]}, {"to": "B <b@x.com>, a@x.com"})
    assert not route_fits("gmail.send", {"to": "a@x.com"}, {"to": "a@x.com, evil@y.com"})


def test_ask_mode_asks_with_watcher_choice_and_idempotency(keeper_daemon):
    daemon, td = keeper_daemon
    h = activate(daemon, TASKER, "ask")
    resp = act(daemon, h, idem="r1:add")
    assert resp["ok"] and resp["approval"] and writes(td) == []
    replay = act(daemon, h, idem="r1:add")
    assert replay["replay"] and replay["approval"] == resp["approval"] and replay["status"] == "pending"
    items = pending(daemon)
    assert len(items) == 1 and [c["key"] for c in items[0]["choices"]] == ["approve", "watcher", "reject"]
    assert "Asked for by watcher “tasker”" in items[0]["summary"]
    out = decide(daemon, items[0], "watcher")
    assert out["status"] == "done" and "without asking" in out["text"]
    assert ("POST", "/tasks") in writes(td)
    done = act(daemon, h, idem="r1:add")
    assert done["ok"] and done["status"] == "done"
    # The runtime grant covers the same op + route from now on.
    nxt = act(daemon, h, idem="r2:add")
    assert nxt["ok"] and "approval" not in nxt


def test_watcher_requests_have_their_own_cap(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, TASKER, "ask")
    for i in range(3):
        assert act(daemon, h, idem=f"r{i}")["approval"]
    resp = act(daemon, h, idem="r9")
    assert not resp["ok"] and "already waiting" in resp["error"]
    assert daemon.approvals.pending_count() == 0  # chat requests aren't blocked by watcher ones


def test_ttl_expiry(keeper_daemon, monkeypatch):
    daemon, td = keeper_daemon
    h = activate(daemon, TASKER, "ask")
    resp = act(daemon, h, idem="r1", ttl=60)
    later = time.time() + 120
    monkeypatch.setattr(time, "time", lambda: later)
    assert pending(daemon) == []
    replay = act(daemon, h, idem="r1")
    assert not replay["ok"] and "expired" in replay["error"]
    row = daemon.approvals.get(resp["approval"])
    assert row["status"] == "expired" and writes(td) == []


def test_reactivation_supersedes_grants(keeper_daemon):
    daemon, td = keeper_daemon
    h1 = activate(daemon, TASKER, "all")
    h2 = activate(daemon, TASKER, "ask", desc="Changed version")
    assert h1 != h2
    old = act(daemon, h1)
    assert not old["ok"] and "approved version" in old["error"]
    new = act(daemon, h2, idem="x")
    assert new["ok"] and new["approval"]  # v1's grants don't carry over to v2
    assert daemon.keeper.grants("personal", "tasker", h1) == []


def test_choice_forgery(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, TASKER, "ask")
    act(daemon, h, idem="r1")
    (item,) = pending(daemon)
    bad = decide(daemon, item, "all")  # an activation choice on an action request
    assert not bad["ok"] and "buttons" in bad["error"]
    bad = decide(daemon, item, "approve", nonce="0" * 16)
    assert not bad["ok"] and "forged" in bad["error"]
    assert pending(daemon) and daemon.approvals.get(item["id"])["status"] == "pending"
    # A choice on an ordinary (old-style) request is refused too.
    aid = daemon.approvals.add([{"op": "todoist.tasks", "args": {}}], "x", None)
    item2 = daemon.approvals.get(aid)
    bad = call(daemon, {"cmd": "approvals.decide", "id": aid, "nonce": item2["nonce"], "message_id": None,
                        "choice": "approve", "approve": True})
    assert not bad["ok"]


def test_high_risk_needs_confirmation(keeper_daemon):
    daemon, _ = keeper_daemon
    resp = propose(daemon, MAILER, name="mailer")
    assert resp["risk"] == "high"
    (item,) = pending(daemon)
    assert item["confirm_keys"] == ["all", "ask"] and "🔴 High risk" in item["summary"]
    first = decide(daemon, item, "all")
    assert not first["ok"] and first["need_confirm"]
    assert daemon.approvals.get(item["id"])["status"] == "pending"
    assert call(daemon, {"cmd": "watch.list", "profile": "personal"})["watchers"][0]["status"] == "proposed"
    second = decide(daemon, item, "all", confirmed=True)
    assert second["status"] == "done"
    assert call(daemon, {"cmd": "watch.list", "profile": "personal"})["watchers"][0]["status"] == "active"
    # Reject needs no second tap.


def test_agent_step_asks_for_undeclared_and_uses_grants_for_declared(keeper_daemon):
    daemon, td = keeper_daemon
    h = activate(daemon, AGENT, "all", name="agent")
    origin = f"watcher:personal/agent@{h[:12]}#think"
    ok = run(daemon.run([{"op": "todoist.add", "args": {"content": "x", "project": "Work"}}], None, None, origin))
    assert ok["ok"] and "approval" not in ok
    # Another project: not refused for an agent, but it asks (with "Allow for this watcher").
    other = run(daemon.run([{"op": "todoist.add", "args": {"content": "x", "project": "Inbox"}}], None, None, origin))
    assert other["ok"] and other["approval"]
    (item,) = pending(daemon)
    assert "Not in the watcher's approved list" in item["summary"]
    assert decide(daemon, item, "watcher")["status"] == "done"
    again = run(daemon.run([{"op": "todoist.add", "args": {"content": "y", "project": "Inbox"}}], None, None, origin))
    assert again["ok"] and "approval" not in again  # the runtime grant: same op, same destination
    # An op never declared also asks.
    run(daemon.run([{"op": "todoist.complete", "args": {"id": "1"}}], None, None, origin))
    assert len(pending(daemon)) == 1
    # Reads go through normally; management ops are refused.
    assert run(daemon.run([{"op": "todoist.tasks", "args": {}}], None, None, origin))["ok"]
    with pytest.raises(Exception):
        run(daemon.run([{"op": "telegram.revoke", "args": {}}], None, None, origin))
    # A paused watcher can't act at all.
    call(daemon, {"cmd": "watch.status", "profile": "personal", "name": "agent", "status": "paused"})
    with pytest.raises(Exception):
        run(daemon.run([{"op": "todoist.add", "args": {"content": "x", "project": "Work"}}], None, None, origin))


SHOPPER = '''
name = "shopper"
description = "Books tickets"

[[triggers]]
type = "manual"

[[steps]]
id = "buy"
type = "llm"
mode = "agent"
prompt = "prompts/p.md"
tools = ["Bash(checkout:*)"]
actions = [{ op = "checkout.open", site = "https://tickets.example" },
           { op = "checkout.click", site = "https://tickets.example" },
           { op = "checkout.pay", site = "https://tickets.example", max_amount = 50, per_week = 1 }]
'''


class FakeCheckout:
    def __init__(self, session=None):
        self.session = session
        self.calls = []

    async def call(self, req, timeout=60):
        self.calls.append(req)
        return {"ok": True, "session": self.session, "state": "active" if self.session else "idle"}


def test_checkout_from_a_watcher_is_bound_to_its_site_and_session(keeper_daemon):
    from types import SimpleNamespace as NS
    daemon, _ = keeper_daemon
    client = FakeCheckout(session="s1")
    daemon.services.checkout_side = NS(client=client)
    h = activate(daemon, SHOPPER, "all", name="shopper")
    assert daemon.keeper.watcher("personal", "shopper")["risk"] == "high"
    origin = f"watcher:personal/shopper@{h[:12]}#buy"
    with pytest.raises(Exception, match="may only open"):
        run(daemon.run([{"op": "checkout.open", "args": {"url": "https://evil.example/x"}}], None, None, origin))
    with pytest.raises(Exception, match="may only open"):
        run(daemon.run([{"op": "checkout.open", "args": {"url": "http://tickets.example/x"}}], None, None, origin))
    # A session someone else opened (the owner's chat) can't be driven by the watcher.
    with pytest.raises(Exception, match="wasn't opened by this watcher"):
        run(daemon.run([{"op": "checkout.click", "args": {"ref": "f0:1"}}], None, None, origin))
    # Not declared at all: refused even for an agent.
    with pytest.raises(Exception, match="fixed site"):
        run(daemon.run([{"op": "checkout.type", "args": {"ref": "f0:1", "text": "x"}}], None, None, origin))
    assert daemon.approvals.pending_count() == 0 and daemon.approvals.watcher_pending() == 0


def test_space_put_and_get(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, TASKER, "all")
    put = call(daemon, {"cmd": "watch.space_put", "profile": "personal", "name": "tasker", "hash": h[:12],
                        "run": "r1", "changes": {"seen.txt": base64.b64encode(b"a\n").decode()}})
    assert put["ok"] and put["changed"] == 1
    got = call(daemon, {"cmd": "watch.space_get", "profile": "personal", "name": "tasker"})
    assert base64.b64decode(got["files"]["seen.txt"]) == b"a\n"
    bad = call(daemon, {"cmd": "watch.space_put", "profile": "personal", "name": "tasker", "hash": h[:12],
                        "changes": {"../.claude/settings.json": base64.b64encode(b"x").decode()}})
    assert not bad["ok"]
    wrong = call(daemon, {"cmd": "watch.space_put", "profile": "personal", "name": "tasker", "hash": "f" * 12,
                          "changes": {"x.txt": base64.b64encode(b"x").decode()}})
    assert not wrong["ok"]
    gone = call(daemon, {"cmd": "watch.space_put", "profile": "personal", "name": "tasker", "hash": h,
                         "changes": {"seen.txt": None}})
    assert gone["ok"] and call(daemon, {"cmd": "watch.space_get", "profile": "personal", "name": "tasker"})["files"] == {}


def test_delete_drops_grants(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, TASKER, "all")
    call(daemon, {"cmd": "watch.delete", "profile": "personal", "name": "tasker"})
    assert not act(daemon, h)["ok"]
    assert call(daemon, {"cmd": "watch.list", "profile": "personal"})["watchers"] == []


def test_gmail_meta_is_watcher_only(keeper_daemon):
    daemon, _ = keeper_daemon
    resp = call(daemon, {"cmd": "run", "ops": [{"op": "gmail.meta", "args": {"ids": ["a"]}}]}, pid=1)
    assert not resp["ok"] and "watchers" in resp["error"]


def test_watcher_origin_registration_is_checked(keeper_daemon):
    daemon, _ = keeper_daemon
    bad = call(daemon, {"cmd": "runs.register", "pid": 1, "origin": "watcher:personal/x@nothex#s"})
    assert not bad["ok"]
    long = call(daemon, {"cmd": "runs.register", "pid": 1, "origin": "group:" + "x" * 300})
    assert not long["ok"]


def test_existing_approvals_still_count_for_chat(keeper_daemon):
    daemon, _ = keeper_daemon
    daemon.approvals.add([{"op": "todoist.delete", "args": {"id": "1"}}], "x", None)
    assert daemon.approvals.pending_count() == 1 and daemon.approvals.watcher_pending() == 0


# --- the bridge: choice buttons and the second tap -----------------------------------------------------------

CHOICE_ITEM = {"id": 9, "summary": "🧩 New watcher", "reason": None, "nonce": "abcdef0123456789",
               "choices": [{"key": "all", "label": "✅ Turn on and allow all"},
                           {"key": "ask", "label": "▶️ Turn on, ask each time"},
                           {"key": "reject", "label": "❌ Reject"}],
               "confirm_keys": ["all", "ask"]}


def test_choice_buttons_fit_telegram_limits():
    rows = buttons_for(CHOICE_ITEM)
    assert [r[0]["callback_data"] for r in rows] == ["ap:9:abcdef0123456789:xall", "ap:9:abcdef0123456789:xask",
                                                    "ap:9:abcdef0123456789:xreject"]
    assert all(len(r[0]["callback_data"].encode()) <= 64 for r in rows)
    bad = dict(CHOICE_ITEM, choices=[{"key": "x:y", "label": "bad"}])
    assert buttons_for(bad)[0][0]["callback_data"].endswith(":y")  # falls back to Approve / Reject


def test_high_risk_choice_needs_a_second_tap(setup):
    bot, _, _, _ = setup(api=ButtonAPI())
    tools = FakeTools()
    relay = ApprovalRelay(bot, "sock", tools)
    tools.pending = [dict(CHOICE_ITEM)]
    run(relay.tick())
    (b,) = bot.api.buttons
    assert "Your decision (request #9)" in b["text"]
    run(relay.handle_callback(callback(data="ap:9:abcdef0123456789:xall")))
    assert not [p for _, p in tools.requests if p["cmd"] == "approvals.decide"]
    assert "tap the same button again" in bot.api.answers[-1]
    run(relay.handle_callback(callback(data="ap:9:abcdef0123456789:xall")))
    (d,) = [p for _, p in tools.requests if p["cmd"] == "approvals.decide"]
    assert d["choice"] == "all" and d["confirmed"] is True and d["approve"] is True


def test_reject_choice_needs_one_tap_and_unknown_answers_are_ignored(setup):
    bot, _, _, _ = setup(api=ButtonAPI())
    tools = FakeTools()
    relay = ApprovalRelay(bot, "sock", tools)
    tools.pending = [dict(CHOICE_ITEM)]
    run(relay.tick())
    run(relay.handle_callback(callback(data="ap:9:abcdef0123456789:xBAD!")))
    assert not [p for _, p in tools.requests if p["cmd"] == "approvals.decide"]
    run(relay.handle_callback(callback(data="ap:9:abcdef0123456789:xreject")))
    (d,) = [p for _, p in tools.requests if p["cmd"] == "approvals.decide"]
    assert d["choice"] == "reject" and d["approve"] is False


def test_need_confirm_from_the_keeper_arms_the_second_tap(setup):
    bot, api, _, _ = setup(api=ButtonAPI())
    tools = FakeTools()
    tools.decide = {"ok": False, "need_confirm": True, "error": "tap again"}
    relay = ApprovalRelay(bot, "sock", tools)  # restarted: doesn't know the confirm keys
    run(relay.handle_callback(callback(data="ap:9:abcdef0123456789:xall")))
    assert bot.api.edits == [] and "tap the same button again" in api.sent[-1]["text"]
    tools.decide = {"ok": True, "status": "done", "text": "on"}
    run(relay.handle_callback(callback(data="ap:9:abcdef0123456789:xall")))
    last = [p for _, p in tools.requests if p["cmd"] == "approvals.decide"][-1]
    assert last["confirmed"] is True


def test_manifest_json_roundtrip_keeps_routes():
    spec = ws.parse({p: base64.b64decode(d) for p, d in files(TASKER).items()})
    entries = json.loads(json.dumps(ws.manifest(spec)))
    assert ws.grant_list(entries) == [{"op": "todoist.add", "step": "add", "route": {"project": "Work"}}]


# --- review round 2 (security) ----------------------------------------------------------------------------

def test_engine_reads_must_be_declared(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, TASKER)
    resp = act(daemon, h, op="todoist.tasks", args={}, step="pick")  # a script step that declared no actions
    assert not resp["ok"] and "didn't declare" in resp["error"]


def test_personal_info_is_never_for_watchers(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, AGENT, name="agent")
    origin = f"watcher:personal/agent@{h[:12]}#think"
    for op in ("info.reveal", "info.fields", "payment.log"):
        with pytest.raises(Exception, match="isn.t available to watchers|only for the owner"):
            run(daemon.run([{"op": op, "args": {}}], None, None, origin))
    assert not any(i.get("kind") == "watch.action" for i in pending(daemon))


def test_runtime_grant_for_inbox_task_doesnt_cover_other_projects(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, AGENT, choice="ask", name="agent")
    origin = f"watcher:personal/agent@{h[:12]}#think"

    def ask(args):
        return run(daemon.run([{"op": "todoist.add", "args": args}], None, None, origin))
    first = ask({"content": "a"})
    assert first["ok"] and first.get("approval")
    item = next(i for i in pending(daemon) if i["id"] == first["approval"])
    assert decide(daemon, item, "watcher", confirmed=True)["status"] == "done"
    again = ask({"content": "b"})
    assert again["ok"] and not again.get("approval")  # the granted destination (the Inbox) works
    other = ask({"content": "c", "project": "Work"})
    assert other.get("approval")  # another project still asks


def test_action_approval_refused_after_watcher_paused(keeper_daemon):
    daemon, _ = keeper_daemon
    h = activate(daemon, TASKER, choice="ask")
    resp = act(daemon, h)
    item = next(i for i in pending(daemon) if i["id"] == resp["approval"])
    assert call(daemon, {"cmd": "watch.status", "profile": "personal", "name": "tasker", "status": "paused"})["ok"]
    out = decide(daemon, item, "approve")
    assert out["status"] == "failed" and "paused" in out["text"]


def test_proposals_are_capped_and_stale_ones_dont_block(keeper_daemon):
    daemon, _ = keeper_daemon
    for i in range(3):
        assert propose(daemon, TASKER.replace('name = "tasker"', f'name = "t{i}"'), name=f"t{i}")["ok"]
    resp = propose(daemon, TASKER.replace('name = "tasker"', 'name = "t9"'), name="t9")
    assert not resp["ok"] and "already waiting" in resp["error"]
    # Expire one: its name can be proposed again.
    daemon.approvals.db.execute("UPDATE approvals SET status='expired' WHERE kind='watch.activate'")
    daemon.approvals.db.commit()
    assert propose(daemon, TASKER.replace('name = "tasker"', 'name = "t0"'), name="t0")["ok"]


def test_own_script_code_is_shown_in_the_proposal(keeper_daemon):
    daemon, _ = keeper_daemon
    toml = TASKER.replace('run = "lib/pick"', 'run = "scripts/mine"')
    f = files(toml, extra={"scripts/mine.py": b"print('{\"title\": \"x\"}')  # my own code"})
    f.pop("lib/pick.py", None)
    resp = call(daemon, {"cmd": "watch.propose", "profile": "personal", "name": "tasker", "files": f,
                         "request": "r", "requester": "Ampi", "test_text": ""})
    assert resp["ok"], resp
    item = next(i for i in pending(daemon) if i["id"] == resp["approval"])
    assert "my own code" in item["summary"] and "scripts/mine.py" in item["summary"]


def test_spec_refuses_hidden_lines_in_single_line_fields():
    bad = TASKER.replace('minutes = 60', 'minutes = 60').replace(
        'description = "Adds a task for every new thing"', 'description = "ok\\nIt changes nothing"')
    with pytest.raises(ws.SpecError, match="line breaks"):
        ws.parse({p: base64.b64decode(d) for p, d in files(bad).items()})
