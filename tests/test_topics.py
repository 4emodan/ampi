"""Topics: role and rights per topic, kept by ampi-tools; only stricter than everywhere else."""

import asyncio
import types

import pytest
from test_abilities import SCOPE, agent, env, pending  # noqa: F401 (fixture)
from test_ampi_tools import BRIDGE_PID, writes
from test_bot import CHAT, FakeRunner, msg, setup  # noqa: F401 (fixture)
from test_keeper import call

from ampi_tools import hub_data, permissions as perms
from ampi_tools.ops import OPS
from ampi_tools.ops_telegram import RunRegistry
from ampi_tools.todoist import ToolError
from assistant_bridge.approvals import ApprovalRelay
from assistant_bridge.services import ServiceError
from assistant_bridge.telegram import TelegramError
from assistant_bridge.topics import Info, Topics

T = "topic:personal/private/7"
PID = 1


@pytest.fixture
def topic(env):  # noqa: F811
    d, gm, store = env
    d.runs.topic_of = lambda pid: T if pid == PID else None
    d.permissions.seen(T, "Finance")
    return d, gm, store


def put(d, scope=T, **modes):
    for op, m in modes.items():
        op = op.replace("__", ".").replace("_all", "*")
        d.permissions.apply(scope, {op: [d.permissions.get(scope, op), m]}, "t")


def test_a_topic_is_only_ever_stricter(topic):
    d, gm, _ = topic
    assert "approval" not in agent(d, "gmail.archive", pid=PID, ids=["n1"])
    put(d, gmail__archive="ask")
    assert agent(d, "gmail.archive", pid=PID, ids=["n1"])["approval"]
    put(d, "chat", gmail__archive="off")
    assert not agent(d, "gmail.archive", pid=PID, ids=["n1"])["ok"]
    put(d, gmail__archive="default")
    assert not agent(d, "gmail.archive", pid=PID, ids=["n1"])["ok"]


@pytest.mark.parametrize("mode", ["ask", "off"])
def test_all_changes(topic, mode):
    d, gm, _ = topic
    put(d, changes___all=mode)
    out = agent(d, "gmail.star", pid=PID, id="n1")
    assert bool(out.get("approval")) == (mode == "ask") and out["ok"] == (mode == "ask") and not writes(gm)
    assert agent(d, "gmail.labels", pid=PID)["ok"]
    put(d, gmail__star="off")
    assert not agent(d, "gmail.star", pid=PID, id="n1")["ok"]


def test_every_operation_is_either_a_change_or_known_not_to_be():
    looking = {o for o in OPS if o not in perms.CHANGES}
    risky = {o for o in looking if OPS[o].critical}
    assert risky <= {"info.reveal", "info.collect", "checkout.open", "checkout.click", "checkout.type",
                     "checkout.choose", "checkout.back", "checkout.close", "checkout.snapshot"}, risky
    for o in looking:
        assert o.split(".")[1] in {"tasks", "task", "projects", "labels", "completed", "ids", "show", "search", "read", "thread", "files",
                                   "drafts", "meta", "video", "comments", "channel", "uploads", "playlist", "playlists",
                                   "subscriptions", "liked", "feed", "profile", "posts", "post", "stories", "story", "inbox", "dm",
                                   "media", "insights", "replies", "link", "place", "route", "status", "weather", "venue", "venues",
                                   "menu", "item", "dishes", "order", "orders", "log", "permissions", "fields", "reveal", "collect",
                                   "fill", "open", "click", "type", "choose", "back", "close", "snapshot", "flows", "alarms", "test"}, o


def test_an_approval_keeps_its_topic(topic):
    d, gm, _ = topic
    put(d, changes___all="ask")
    agent(d, "gmail.star", pid=PID, id="n1")
    (item,) = pending(d)
    assert item["where"] == ["personal", 7]
    put(d, gmail__star="off")
    out = call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert out["status"] == "failed" and not writes(gm)


def test_lost_processes_get_nothing_a_topic_has_off(topic):
    d, *_ = topic
    assert agent(d, "gmail.labels", pid=999)["ok"]
    put(d, gmail___all="off")
    assert not agent(d, "gmail.labels", pid=999)["ok"] and not agent(d, "gmail.labels", pid=PID)["ok"]
    assert agent(d, "gmail.labels", pid=BRIDGE_PID)["ok"]


def test_settings_from_the_hub(topic):
    d, *_ = topic
    out = hub_data.set_(d, T, {"changes.*": ["default", "ask"], "web.*": ["default", "off"]})
    assert out["changed"] == 2 and not out["approval"]
    out = hub_data.set_(d, T, {"web.*": ["off", "default"]})
    assert out["approval"] and d.permissions.off(T) == ["web"]
    for bad in ({"gmail.star": ["default", "report"]}, {"nope.*": ["default", "off"]}):
        with pytest.raises(ToolError):
            hub_data.set_(d, T, bad)
    with pytest.raises(ToolError):
        hub_data.set_(d, "topic:personal/private/99", {"changes.*": ["default", "ask"]})
    d.permissions.apply("chat", {"gmail.*": ["default", "off"]}, "t")
    assert d.permissions.off(T) == ["gmail", "web"]


def test_a_role_from_the_hub_waits_for_approval(topic):
    d, *_ = topic
    out = hub_data.answer(d, "hub.role", {"scope": T, "role": "Accountant.\nNever guess\u202e."})
    assert out["approval"] and d.permissions.role(T) == ""
    (item,) = pending(d)
    assert "│ Accountant." in item["summary"]
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert d.permissions.role(T).startswith("Accountant.\nNever guess") and "\u202e" not in d.permissions.role(T)
    with pytest.raises(ToolError):
        hub_data.answer(d, "hub.role", {"scope": T, "role": "x" * 1001})
    with pytest.raises(ToolError):
        hub_data.answer(d, "hub.role", {"scope": "chat", "role": "x"})


def test_one_request_for_role_and_settings(topic):
    d, *_ = topic
    put(d, web___all="off")
    hub_data.answer(d, "hub.role", {"scope": T, "role": "R"})
    hub_data.set_(d, T, {"web.*": ["off", "default"]})
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert d.permissions.role(T) == "R" and d.permissions.get(T, "web.*") == "default"


def test_where_a_watcher_writes(topic):
    d, gm, store = topic
    with pytest.raises(ToolError):
        hub_data.answer(d, "hub.deliver", {"scope": SCOPE, "topic": "topic:personal/somegroup/7"})
    assert hub_data.answer(d, "hub.deliver", {"scope": SCOPE, "topic": T})["approval"]
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert call(d, {"cmd": "watch.settings", "profile": "personal", "name": "sorter"})["thread"] == 7
    agent(d, "gmail.star", id="n1")
    assert pending(d)[0]["where"] == ["personal", 7]
    d.permissions.apply(SCOPE, {"gmail.star": ["default", "report"]}, "t")
    agent(d, "gmail.star", id="n2")
    import time
    d.keeper.clock = lambda: time.time() + 300
    d.send_reports()
    n = store.unsent_notices()[-1]
    assert (n["profile"], n["thread"]) == ("personal", 7)
    assert hub_data.answer(d, "hub.deliver", {"scope": SCOPE})["changed"] == 1


def test_stop_everything_and_on_again(topic):
    d, gm, _ = topic
    out = hub_data.answer(d, "hub.stop", {})
    assert out["paused"] == 1 and d.keeper.watcher("personal", "sorter")["status"] == "paused"
    assert not agent(d, "gmail.star", pid=PID, id="n1")["ok"] and not agent(d, "gmail.star", id="n1")["ok"]
    assert agent(d, "gmail.labels", pid=PID)["ok"] and hub_data.home(d)["stopped"]
    hub_data.answer(d, "hub.stop", {})  # twice forgets nothing
    assert hub_data.answer(d, "hub.resume", {"everything": True})["approval"]
    assert d.keeper.watcher("personal", "sorter")["status"] == "paused"
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert d.keeper.watcher("personal", "sorter")["status"] == "active" and not hub_data.home(d)["stopped"]


def test_a_paused_watcher_isnt_turned_on_by_stopping(topic):
    d, *_ = topic
    d.keeper.set_status("personal", "sorter", "paused")
    hub_data.answer(d, "hub.stop", {})
    hub_data.answer(d, "hub.resume", {"everything": True})
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert d.keeper.watcher("personal", "sorter")["status"] == "paused"


def test_removing_what_was_given(topic):
    d, gm, store = topic
    store.grant(5, "send", "Anna")
    w = d.keeper.watcher("personal", "sorter")
    d.keeper.add_grant("personal", "sorter", w["hash"], "think", "gmail.star", {})
    rows = hub_data.given(d)["given"]
    assert {r["kind"] for r in rows} == {"telegram", "watcher"}
    for r in rows:
        assert hub_data.revoke(d, r["kind"], r["key"])["changed"] == 1
        with pytest.raises(ToolError):
            hub_data.revoke(d, r["kind"], r["key"])
    assert hub_data.given(d)["given"] == [] and agent(d, "gmail.star", id="n1")["approval"]
    for bad in (("x", [1]), ("telegram", ["a", "b"]), ("watcher", "1"), ("watcher", [True])):
        with pytest.raises(ToolError):
            hub_data.revoke(d, *bad)


def test_forgetting_isnt_a_way_around(topic):
    d, *_ = topic
    put(d, web___all="off")
    with pytest.raises(ToolError):
        hub_data.answer(d, "hub.forget", {"scope": T})
    put(d, web___all="default")
    assert hub_data.answer(d, "hub.forget", {"scope": T})["changed"] == 1 and d.permissions.topics() == []


def test_activity_can_be_narrowed(topic):
    d, *_ = topic
    agent(d, "gmail.star", id="n1")
    agent(d, "gmail.mark_read", id="n1")
    rows = hub_data.answer(d, "hub.activity", {"op": "gmail.star"})["watchers"]
    assert {r["op"] for r in rows} == {"gmail.star"}
    assert hub_data.answer(d, "hub.activity", {"before": rows[0]["id"]})["watchers"] == []
    assert hub_data.answer(d, "hub.activity", {"name": "nobody"})["watchers"] == []


def test_only_the_bridge_sets_topics(topic):
    d, *_ = topic
    for pid in (PID, 999):
        for cmd in ("topic.get", "topic.set", "topic.deliver"):
            assert not call(d, {"cmd": cmd, "scope": T, "changes": {"web.*": "off"}}, pid)["ok"]
    r = call(d, {"cmd": "topic.set", "scope": T, "changes": {"changes.*": "ask", "web.*": "off"}, "role": "R"})
    assert r["role"] == "R" and r["off"] == ["web"] and r["restricted"]
    assert not call(d, {"cmd": "topic.set", "scope": "chat", "changes": {"web.*": "off"}})["ok"]
    assert not call(d, {"cmd": "topic.set", "scope": T, "changes": {"nope.*": "off"}})["ok"]
    r = call(d, {"cmd": "topic.set", "scope": T, "reset": True})
    assert not r["restricted"] and not r["role"]
    assert not call(d, {"cmd": "runs.register", "pid": 1, "origin": "private", "topic": "chat"})["ok"]


def test_the_registry_knows_a_runs_topic(tmp_path):
    def proc(pid, parent, start):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (x) S {parent} " + " ".join(["0"] * 17) + f" {start} 0")

    proc(10, 1, 100)
    proc(11, 10, 101)
    r = RunRegistry(proc=tmp_path)
    r.register(10, "private", T)
    assert r.topic_of(11) == T and r.origin_of(11) == "private"
    r.unregister(10)
    assert r.topic_of(11) is None
    r.register(10, "private")
    assert r.topic_of(11) is None


# --- the bridge


def test_tools_that_are_off_are_left_out_under_any_spelling():
    i = Info(T, off=("tg-read", "web", "browse"))
    assert i.without(("Read", "WebFetch", "WebSearch", "Bash(tg-read:*)", "Bash(tg-read search:*)", "Bash(/usr/bin/browse:*)",
                      "Bash(gmail:*)")) == ("Read", "Bash(gmail:*)")
    assert set(i.denied()) == {"Bash(tg-read:*)", "WebSearch", "WebFetch", "Bash(browse:*)"}


def brun(c):
    return asyncio.run(c)


def wired(setup, reply=None, fail=False):  # noqa: F811
    bot, api, runner, store = setup()
    asked = []

    async def ask(socket, req, timeout=None):
        asked.append(req)
        if fail:
            raise ServiceError("away")
        return {"ok": True, "role": "Be brief.", "off": ["web"], "restricted": True, "limited": True, "settings": {"web.*": "off"}, "chat": {},
                "watchers": [], **(reply or {})}

    bot.topics = Topics(bot, "x", ask)
    return bot, api, runner, asked


def test_a_run_gets_its_topics_role_and_limits(setup):  # noqa: F811
    bot, api, runner, asked = wired(setup)
    brun(bot.handle_message(msg("hi", thread=7)))
    c = runner.calls[0]
    assert c["topic"] == (T, True, True) and "Be brief." in c["append_system_prompt"] and "WebFetch" in c["extra_denied"]
    assert asked[0]["scope"] == T


def test_the_tools_service_away(setup):  # noqa: F811
    bot, api, runner, _ = wired(setup, fail=True)
    brun(bot.handle_message(msg("hi", thread=7)))
    assert runner.calls[0]["topic"] == (T, False, True) and "WebFetch" in runner.calls[0]["extra_denied"]
    assert "isn't reachable" in api.sent[0]["text"]


@pytest.mark.parametrize("text,req", [
    ("/topic role You are\nan accountant", {"role": "You are\nan accountant"}), ("/topic\nrole X", {"role": "X"}),
    ("/topic role clear", {"role": ""}), ("/topic role off", {"role": ""}), ("/topic ask gmail", {"changes": {"gmail.*": "ask"}}), ("/topic changes ask", {"changes": {"changes.*": "ask"}}),
    ("/topic changes on", {"changes": {"changes.*": "default"}}), ("/topic off gmail", {"changes": {"gmail.*": "off"}}),
    ("/topic on web", {"changes": {"web.*": "default"}}), ("/topic reset", {"reset": True})])
def test_commands(setup, text, req):  # noqa: F811
    bot, api, runner, asked = wired(setup)
    brun(bot.handle_message(msg(text, thread=7)))
    assert asked[0] == {"cmd": "topic.set", "scope": T, **req} and not runner.calls and "Role: Be brief." in api.sent[0]["text"]


def test_commands_for_watchers_and_help(setup):  # noqa: F811
    bot, api, runner, asked = wired(setup)
    brun(bot.handle_message(msg("/topic watcher gmail-inbox", thread=7)))
    assert asked[-1] == {"cmd": "topic.deliver", "scope": "watcher:personal/gmail-inbox", "topic": T}
    brun(bot.handle_message(msg("/topic watcher gmail-inbox off", thread=7)))
    assert asked[-1]["topic"] == ""
    n = len(asked)
    brun(bot.handle_message(msg("/topic nonsense", thread=7)))
    assert len(asked) == n and "/topic role" in api.sent[-1]["text"]
    brun(bot.handle_message(msg("/topic off gmail", thread=7, forward_origin={"type": "hidden_user"})))
    assert not any(r.get("changes") for r in asked)


def test_approvals_are_shown_in_their_topic_and_never_lost():
    sent = []

    class API:
        gone = False

        async def send_buttons(self, chat, text, buttons, parse_mode=None, thread_id=None):
            if self.gone and thread_id:
                raise TelegramError("sendMessage", "message thread not found", 400)
            sent.append(thread_id)
            return {"message_id": 1}

        async def send_message(self, chat, text, thread_id=None):
            if self.gone and thread_id:
                raise TelegramError("sendMessage", "message thread not found", 400)
            sent.append(thread_id)

    api = API()
    bot = types.SimpleNamespace(profile=types.SimpleNamespace(name="personal", owner_chat_id=1), api=api)

    async def ask(*a, **k):
        return {"ok": True}

    relay = ApprovalRelay(bot, "x", ask)
    item = {"id": 1, "summary": "s", "nonce": "n"}
    for where, expect in ((["personal", 7], 7), (["work", 7], None), (None, None), (["personal", "7"], None), (["personal", 0], None)):
        brun(relay._send_request({**item, "where": where}, "t", []))
        assert sent[-1] == expect
    brun(relay.send_notices([{"id": 1, "text": "x", "profile": "personal", "thread": 7}]))
    assert sent[-1] == 7
    api.gone = True
    brun(relay._send_request({**item, "where": ["personal", 7]}, "t", []))
    assert sent[-1] is None
    brun(relay.send_notices([{"id": 1, "text": "x", "profile": "personal", "thread": 7}]))
    assert sent[-1] is None


def test_a_limited_topics_run_gets_nothing_through_watchers():
    from assistant_bridge.watch.control import PRIVATE_CMDS, Control, Who
    from assistant_bridge.watch.engine import Caller
    c = Control({}, "x")
    for cmd in sorted(PRIVATE_CMDS):
        out = brun(c.dispatch({"cmd": cmd, "name": "sorter"}, Who("private", "personal", caller=Caller("private", None, True))))
        assert ("limited" in out["text"]) == (cmd not in ("list", "help", "lib", "pause")), (cmd, out)


def test_the_topic_is_read_before_the_origin(topic):
    d, gm, _ = topic
    put(d, gmail___all="off")
    order = []
    d.runs.topic_of = lambda pid: order.append("topic")
    d.runs.origin_of = lambda pid: order.append("origin") or "unknown"
    assert not agent(d, "gmail.labels", pid=PID)["ok"] and order[:2] == ["topic", "origin"]


def test_stop_remembers_what_was(topic):
    d, *_ = topic
    put(d, "chat", changes___all="ask")
    hub_data.set_(d, T, {"changes.*": ["default", "off"]})
    hub_data.set_(d, T, {"changes.*": ["off", "default"]})  # waits
    hub_data.answer(d, "hub.stop", {})
    assert not pending(d)
    d.keeper.set_status("personal", "sorter", "active")
    hub_data.answer(d, "hub.stop", {})
    assert d.permissions.remembered("stopped") == {"mode": "ask", "watchers": [SCOPE]}
    hub_data.answer(d, "hub.resume", {"everything": True})
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert d.permissions.get("chat", "changes.*") == "ask" and not hub_data.home(d)["stopped"]
    with pytest.raises(ToolError):
        hub_data.answer(d, "hub.resume", {"everything": True})


def test_two_requests_to_turn_on_are_one(topic):
    from test_abilities import SORTER
    from test_keeper import activate
    d, *_ = topic
    activate(d, SORTER.replace('"sorter"', '"other"'), name="other")
    both = [SCOPE, "watcher:personal/other"]
    hub_data.answer(d, "hub.pause", {"scopes": both})
    for s in both:
        hub_data.answer(d, "hub.resume", {"scopes": [s]})
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert all(d.keeper.watcher("personal", n)["status"] == "active" for n in ("sorter", "other"))


def test_an_old_role_request_doesnt_undo_a_newer_role(topic):
    d, *_ = topic
    hub_data.answer(d, "hub.role", {"scope": T, "role": "old"})
    call(d, {"cmd": "topic.set", "scope": T, "role": "new"})
    (item,) = pending(d)
    out = call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert out["status"] == "failed" and d.permissions.role(T) == "new"
    with pytest.raises(ToolError):
        hub_data.answer(d, "hub.forget", {"scope": T})


def test_an_adapter_that_asks(topic):
    d, gm, _ = topic
    put(d, gmail___all="ask")
    assert agent(d, "gmail.star", pid=PID, id="n1")["approval"] and agent(d, "gmail.labels", pid=PID)["ok"]
    assert "ask" not in perms.allowed_modes(T, "maps.*", OPS, set()) and "ask" in perms.allowed_modes(T, "gmail.*", OPS, set())


def test_limits_before_the_first_message(env):  # noqa: F811
    d, *_ = env
    new = "topic:personal/private/55"
    assert call(d, {"cmd": "topic.set", "scope": new, "changes": {"changes.*": "off"}})["restricted"]
    assert not call(d, {"cmd": "topic.set", "scope": new, "changes": {"nonsense.*": "default"}})["ok"]
    assert not call(d, {"cmd": "topic.deliver", "scope": SCOPE, "topic": "topic:work/private/5"})["ok"]


def test_a_run_that_may_not_start_is_forgotten():
    from assistant_bridge.claude_runner import ClaudeRunner, NotRegistered
    import sys
    r = ClaudeRunner([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=20, max_concurrent=1)
    gone = []

    async def spawn(*a):
        raise NotRegistered

    async def left(pid):
        gone.append(pid)

    r.on_spawn, r.on_exit = spawn, left

    async def go():
        return await r._exec([sys.executable, "-c", "import time; time.sleep(30)"], "", __import__("pathlib").Path("."), topic=(T, True, True))

    out = brun(go())
    assert not out.ok and "nothing was run" in out.error and len(gone) == 1


def test_a_second_stop_after_a_manual_change(topic):
    d, *_ = topic
    hub_data.answer(d, "hub.stop", {})
    put(d, "chat", changes___all="ask")
    hub_data.answer(d, "hub.stop", {})
    assert d.permissions.remembered("stopped")["mode"] == "ask"


def test_a_stale_role_doesnt_spoil_the_next_request(topic):
    d, *_ = topic
    put(d, web___all="off")
    hub_data.answer(d, "hub.role", {"scope": T, "role": "old"})
    call(d, {"cmd": "topic.set", "scope": T, "role": "new"})
    hub_data.set_(d, T, {"web.*": ["off", "default"]})
    (item,) = pending(d)
    out = call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert out["status"] == "done" and d.permissions.role(T) == "new" and d.permissions.get(T, "web.*") == "default"
