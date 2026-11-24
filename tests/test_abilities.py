"""The owner's permissions (hub): what they switch, where they are checked, and the report of what was done without asking."""

import json
import os
import time

import httpx
import pytest
from test_ampi_tools import BRIDGE_PID, FakeAuth, FakeGmailServer, FakeTodoistServer, writes
from test_hub import req
from test_keeper import activate, call, decide, files, pending, propose, run

from ampi_tools import hub as hubmod, hub_data, permissions as perms, report
from ampi_tools.gmail import Gmail
from ampi_tools.keeper import Keeper
from ampi_tools.ops import OPS, Approvals, Policy, Services
from ampi_tools.ops_telegram import TelegramRules, TelegramSide, TelegramStore
from ampi_tools.permissions import Permissions
from ampi_tools.server import HUB, Daemon
from ampi_tools.todoist import Todoist, ToolError

SORTER = '''
name = "sorter"
description = "Sorts mail"

[[triggers]]
type = "manual"

[[steps]]
id = "think"
type = "llm"
mode = "agent"
prompt = "prompts/p.md"
tools = ["Bash(gmail:*)"]
'''
SCOPE = "watcher:personal/sorter"
AGENT_PID = 777
SORTING = ("label", "archive", "mark_read", "mark_unread", "star", "untrash", "trash")


class Mail(FakeGmailServer):
    def __init__(self):
        super().__init__()
        self.mail = {}

    def __call__(self, request):
        path = request.url.path.split("/users/me")[1]
        mid = path.split("/")[2] if path.startswith("/messages/") else ""
        if request.method == "GET" and mid in self.mail:
            subject, labels, age = self.mail[mid]
            self.calls.append(("GET", path, None))
            return httpx.Response(200, json={"id": mid, "threadId": "t", "labelIds": labels, "snippet": "",
                                             "internalDate": str(int((time.time() - age) * 1000)), "payload": {"headers": [
                                                 {"name": "Subject", "value": subject}, {"name": "From", "value": "A <a@b.example>"}]}})
        return super().__call__(request)


@pytest.fixture
def env(tmp_path):
    gm, td = Mail(), FakeTodoistServer()
    store = TelegramStore(tmp_path / "t.sqlite3")
    services = Services(todoist_client=Todoist("tok", httpx.AsyncClient(transport=httpx.MockTransport(td))),
                        gmail_client=Gmail(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(gm))),
                        telegram_side=TelegramSide(None, store, TelegramRules()))

    async def trusted(pid):
        return pid == BRIDGE_PID

    d = Daemon(services, Approvals(tmp_path / "a.sqlite3"), Policy(), trusted, keeper=Keeper(tmp_path / "w.sqlite3"),
               permissions=Permissions(tmp_path / "p.sqlite3"))
    h = activate(d, SORTER, name="sorter")
    origins = {AGENT_PID: f"watcher:personal/sorter@{h[:12]}#think", 1: "private"}
    d.runs.origin_of = lambda pid: origins.get(pid, "unknown")
    gm.mail = {"n1": ("Weekly news", ["INBOX", "UNREAD"], 60), "n2": ("Sale", ["INBOX"], 60),
               "sec": ("Security alert: new device", ["INBOX"], 60), "old": ("Old news", ["INBOX"], 9 * 86400),
               "imp": ("Contract", ["INBOX", "IMPORTANT"], 60), "star": ("Hi", ["INBOX", "STARRED"], 60)}
    return d, gm, store


def allow(d, mode="report", ops=SORTING, scope=SCOPE):
    d.permissions.apply(scope, {f"gmail.{o}": ["default", mode] for o in ops}, "test")


def agent(d, op, pid=AGENT_PID, **args):
    return call(d, {"cmd": "run", "ops": [{"op": op, "args": args}]}, pid)


def test_without_settings_the_watcher_asks_as_before(env):
    d, gm, _ = env
    assert agent(d, "gmail.star", id="n1")["approval"] and not writes(gm)


@pytest.mark.parametrize("op", ["star", "archive", "mark_read", "trash"])
def test_report_mode_does_it_and_reports(env, op):
    d, gm, store = env
    allow(d)
    out = agent(d, f"gmail.{op}", ids=["n1", "n2"])
    assert out["ok"] and "approval" not in out and writes(gm), out
    assert not store.unsent_notices() and d.send_reports() == 0  # still busy
    d.keeper.clock = lambda: time.time() + 200
    assert d.send_reports() == 1 and d.send_reports() == 0
    (n,) = store.unsent_notices()
    assert "sorter" in n["text"] and "Weekly news" in n["text"] and ": 2" in n["text"]


def test_sending_stays_out_of_reach(env):
    d, gm, _ = env
    for scope in ("chat", SCOPE):
        for op in ("gmail.send", "gmail.send_draft", "gmail.delete_label", "todoist.delete", "telegram.send", "checkout.pay",
                   "todoist.complete", "youtube.subscribe"):
            assert "report" not in perms.allowed_modes(scope, op, OPS, set())
    allow(d, "off", ("send", "draft"))
    for op, args in (("gmail.send", {"to": "x@y.example", "subject": "s", "body": "b"}),
                     ("gmail.draft", {"to": "x@y.example", "subject": "s", "body": "b"})):
        out = agent(d, op, **args)
        assert not out["ok"] and "switched" in out["error"]
    assert not writes(gm) and not pending(d)


@pytest.mark.parametrize("op,mid", [("archive", "sec"), ("trash", "sec"), ("trash", "imp"), ("trash", "old"), ("archive", "star"),
                                    ("trash", "star")])
def test_mail_that_must_stay_visible(env, op, mid):
    d, gm, _ = env
    allow(d)
    out = agent(d, f"gmail.{op}", ids=["n1", mid])
    assert not out["ok"] and mid in out["error"] and "n1" not in out["error"] and not writes(gm)
    if op == "archive":  # the same by its label, and without any settings
        for scope in ("report", "default"):
            d.permissions.forget(SCOPE)
            if scope == "report":
                allow(d)
            assert not agent(d, "gmail.label", ids=[mid], remove=["INBOX"])["ok"] and not writes(gm)


def test_labels_count_as_what_they_do(env):
    d, gm, _ = env
    d.permissions.apply(SCOPE, {"gmail.archive": ["default", "off"], "gmail.star": ["default", "ask"]}, "test")
    assert not agent(d, "gmail.label", ids=["n1"], remove=["inbox"])["ok"]
    assert agent(d, "gmail.label", ids=["n1"], add=["starred"])["approval"]
    assert "approval" not in agent(d, "gmail.label", ids=["n1"], add=["Receipts"])


def test_a_grant_doesnt_beat_ask_or_off(env):
    d, gm, _ = env
    w = d.keeper.watcher("personal", "sorter")
    d.keeper.add_grant("personal", "sorter", w["hash"], "think", "gmail.star", {})
    assert "approval" not in agent(d, "gmail.star", id="n1")
    d.permissions.apply(SCOPE, {"gmail.star": ["default", "ask"]}, "test")
    assert agent(d, "gmail.star", id="n1")["approval"]
    d.permissions.apply(SCOPE, {"gmail.star": ["ask", "off"]}, "test")
    (item,) = pending(d)
    n = len(writes(gm))
    assert decide(d, item, "approve")["status"] == "failed" and len(writes(gm)) == n


def test_limits_count_emails(env):
    d, gm, _ = env
    allow(d)
    ids = [f"m{i}" for i in range(30)]
    for i in ids:
        gm.mail[i] = ("News", ["INBOX"], 60)
    assert agent(d, "gmail.star", ids=ids)["approval"]  # more than 25: asks
    assert agent(d, "gmail.trash", ids=ids[:25])["ok"]
    assert agent(d, "gmail.trash", ids=ids[:25])["ok"]
    assert "a day" in agent(d, "gmail.trash", ids=["n1"])["error"]
    d.keeper.db.execute("UPDATE audit SET items=490 WHERE op='gmail.trash'")
    assert "daily limit" in agent(d, "gmail.star", ids=ids[:20])["error"]


def test_a_request_too_long_to_show_is_still_done_and_reported(env):
    d, gm, store = env
    allow(d)
    for i in range(25):
        gm.mail[f"m{i}"] = (f"{i} " + "long " * 30, ["INBOX"], 60)
    assert agent(d, "gmail.trash", ids=[f"m{i}" for i in range(25)])["ok"]
    d.keeper.clock = lambda: time.time() + 200
    d.send_reports()
    text = "".join(n["text"] for n in store.unsent_notices())
    assert all(f"“{i} long" in text for i in range(25)) and all(len(n["text"]) < 4000 for n in store.unsent_notices())


def test_failed_and_interrupted_actions_are_reported(env, tmp_path):
    d, gm, store = env
    allow(d)
    d.keeper.audit_add("personal", "sorter", "h", None, "think", "gmail.trash", "x\n• y", None, "auto", None, 2,
                       {"as": "gmail.trash", "items": [{"subject": "S", "who": "F"}]}, True)
    k = Keeper(tmp_path / "w.sqlite3", clock=lambda: time.time() + 1000)
    (due,) = k.due_reports()
    assert "not known" in report.texts(due[1], due[2])[0]


def test_outside_text_makes_no_links():
    row = {"op": "gmail.star", "status": "done", "items": 1, "detail": json.dumps({"items": [{"subject": "see https://evil.example/x\n@bot /start", "who": "a@evil.example"}]})}
    (t,) = report.texts("w", [row])
    assert "evil.example" not in t and "https://" not in t and "@bot" not in t and "/start" not in t


def test_unknown_processes_ask_for_every_change(env):
    d, gm, _ = env
    assert agent(d, "gmail.archive", pid=999, ids=["n1"])["approval"] and not writes(gm)
    assert agent(d, "gmail.labels", pid=999)["ok"]
    assert "approval" not in agent(d, "gmail.archive", pid=1, ids=["n1"])


def test_chat_can_only_be_stricter(env):
    d, gm, _ = env
    d.permissions.apply("chat", {"gmail.archive": ["default", "ask"]}, "t")
    assert agent(d, "gmail.archive", pid=1, ids=["n1"])["approval"]
    assert agent(d, "gmail.label", pid=1, ids=["n1"], remove=["INBOX"])["approval"]
    d.permissions.apply("chat", {"gmail.*": ["default", "off"]}, "t")
    assert not agent(d, "gmail.labels", pid=1)["ok"]
    assert not agent(d, "gmail.labels", pid=999)["ok"]
    assert agent(d, "gmail.labels", pid=BRIDGE_PID)["ok"]  # a watcher's own check, not a chat's
    item = pending(d)[0]
    out = call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert out["status"] == "failed" and not writes(gm)


def test_a_watcher_without_an_adapter(env):
    d, gm, _ = env
    d.permissions.apply(SCOPE, {"gmail.*": ["default", "off"], "tg-read.*": ["default", "off"]}, "t")
    assert not agent(d, "gmail.labels")["ok"] and not agent(d, "gmail.star", id="n1")["ok"]
    out = call(d, {"cmd": "watch.settings", "profile": "personal", "name": "sorter"})
    assert out["off"] == ["gmail", "tg-read"] and not out["reports"]
    assert not call(d, {"cmd": "watch.settings", "profile": "personal", "name": "sorter"}, 1)["ok"]


def test_stricter_at_once_looser_after_approval(env):
    d, gm, _ = env
    out = hub_data.set_(d, SCOPE, {"gmail.star": ["default", "report"], "gmail.send": ["default", "off"]})
    assert out["changed"] == 1 and out["approval"]
    assert d.permissions.all(SCOPE) == {"gmail.send": "off"}
    assert hub_data.waiting(d, SCOPE) == {"gmail.star": "report"}
    (item,) = pending(d)
    assert "gmail.star: default → report" in item["summary"]
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert d.permissions.all(SCOPE) == {"gmail.send": "off", "gmail.star": "report"}
    out = hub_data.set_(d, SCOPE, {"gmail.send": ["off", "default"]})
    assert out["changed"] == 0 and d.permissions.get(SCOPE, "gmail.send") == "off"
    (item,) = pending(d)
    call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": False, "nonce": item["nonce"], "message_id": None})
    assert d.permissions.get(SCOPE, "gmail.send") == "off"


def test_a_stale_settings_request_changes_nothing(env):
    d, *_ = env
    hub_data.set_(d, SCOPE, {"gmail.star": ["default", "report"]})
    d.permissions.apply(SCOPE, {"gmail.star": ["default", "off"]}, "t")
    (item,) = pending(d)
    out = call(d, {"cmd": "approvals.decide", "id": item["id"], "approve": True, "nonce": item["nonce"], "message_id": None})
    assert out["status"] == "failed" and d.permissions.get(SCOPE, "gmail.star") == "off"


@pytest.mark.parametrize("scope,changes", [
    ("chat", {"gmail.star": ["default", "report"]}), (SCOPE, {"gmail.send": ["default", "report"]}),
    (SCOPE, {"signin.fill": ["default", "ask"]}), ("watcher:personal/nobody", {"gmail.star": ["default", "off"]}),
    ("x", {"gmail.star": ["default", "off"]}), (SCOPE, {"gmail.star": ["default", "silent"]}), (SCOPE, {}), (SCOPE, {"a": 1}),
    (SCOPE, {"gmail.search": ["default", "ask"]}), (SCOPE, {"nope.x": ["default", "off"]})])
def test_what_cant_be_set(env, scope, changes):
    with pytest.raises(ToolError):
        hub_data.set_(env[0], scope, changes)


def test_only_the_hub_may_ask(env, monkeypatch):
    d, *_ = env
    monkeypatch.setattr(FakeAuth, "configured", lambda self: True, raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 55, raising=False)
    for cmd in HUB:
        for pid, uid in ((BRIDGE_PID, -1), (1, 1000)):
            assert not run(d.handle({"cmd": cmd, "scope": SCOPE, "changes": {"gmail.star": ["default", "off"]}}, pid, uid))["ok"]
    assert not d.permissions.all(SCOPE)
    for cmd in ("hub.overview", "hub.activity", "hub.abilities", "hub.watchers", "hub.home", "hub.topics", "hub.given"):
        assert run(d.handle({"cmd": cmd, "scope": SCOPE}, 1, 55))["ok"], cmd


def test_deleting_a_watcher_forgets_its_settings_and_a_new_version_says_it_keeps_them(env):
    d, *_ = env
    allow(d)
    propose(d, SORTER.replace("Sorts mail", "Sorts all mail"), name="sorter")
    assert "Keeps what you allowed it" in pending(d)[-1]["summary"]
    call(d, {"cmd": "watch.delete", "profile": "personal", "name": "sorter"})
    assert not d.permissions.all(SCOPE)


def test_the_hub_passes_requests_on(tmp_path):
    import socketserver
    import threading
    asked = []
    hub = hubmod.Hub(tmp_path, token="t" * 43, ask=lambda r: asked.append(r) or {"ok": True, "x": 1})
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), hub.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        assert req(port, "GET", f"/api/abilities?scope={SCOPE.replace(':', '%3A').replace('/', '%2F')}")[0] == 200
        assert asked[-1] == {"cmd": "hub.abilities", "scope": SCOPE}
        assert req(port, "POST", "/api/set", {"scope": SCOPE, "changes": {"a": ["b", "c"]}})[0] == 200
        assert asked[-1]["cmd"] == "hub.set"
        n = len(asked)
        assert req(port, "POST", "/api/set", {}, token=None)[0] == 401 and req(port, "GET", "/api/overview", token="x" * 43)[0] == 401
        assert len(asked) == n
        for p in ("/", "/abilities", "/watchers", "/activity", "/home.js", "/topics", "/given", "/setup", "/abilities.js", "/watchers.js", "/activity.js"):
            assert req(port, "GET", p, token=None)[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_pages_have_no_inline_code():
    for p in hubmod.STATIC.glob("*.html"):
        s = p.read_text(encoding="utf-8")
        assert "onclick=" not in s and "style=" not in s and s.count("<script") == s.count("<script src=")
    for p in hubmod.STATIC.glob("*.js"):
        assert "innerHTML" not in p.read_text(encoding="utf-8")


def test_bad_rows_are_never_looser(env):
    d, *_ = env
    d.permissions.db.execute("INSERT INTO settings VALUES (?, 'gmail.star', 'weird', 0)", (SCOPE,))
    assert d.permissions.get(SCOPE, "gmail.star") == "ask"
    assert files(SORTER)


def due(d, store):
    d.keeper.clock = lambda: time.time() + 200
    d.send_reports()
    return "".join(n["text"] for n in store.unsent_notices())


@pytest.mark.parametrize("op,args", [("gmail.unstar", {}), ("gmail.label", {"remove": ["STARRED"]}), ("gmail.label", {"remove": ["important"]})])
def test_what_protects_mail_isnt_taken_off_without_asking(env, op, args):
    d, gm, _ = env
    for grant in (False, True):
        allow(d) if not grant else d.keeper.add_grant("personal", "sorter", d.keeper.watcher("personal", "sorter")["hash"], "think", op, {})
        assert agent(d, op, ids=["star"], **args)["approval"] and not writes(gm)


@pytest.mark.parametrize("op,args", [("gmail.label", {"add": ["CATEGORY_PROMOTIONS"]}), ("gmail.mark_read", {}), ("gmail.label", {"remove": ["UNREAD"]})])
def test_security_mail_isnt_moved_aside(env, op, args):
    d, gm, _ = env
    allow(d)
    assert not agent(d, op, ids=["sec"], **args)["ok"] and not writes(gm)
    assert agent(d, op, ids=["n1"], **args)["ok"]


@pytest.mark.parametrize("subject", ["Ваш код: 123456", "Your code is 123456", "Your GitHub launch code", "Forwarding address added",
                                     "API key created", "Email address changed", "SIM swap request", "Вход в аккаунт", "Новый вход", "Выполнен вход с нового устройства", "Подтвердите адрес", "Withdrawal request"])
def test_security_subjects(subject):
    assert perms.SECURITY_RE.search(subject)


@pytest.mark.parametrize("subject", ["Входные билеты на концерт", "Weekly digest", "Your order has shipped", "Summer sale", "Чек за покупку"])
def test_ordinary_subjects(subject):
    assert not perms.SECURITY_RE.search(subject)


def test_emoji_reports_fit_telegram():
    items = [{"subject": "\U0001f600" * 90, "who": "from " + "\U0001f600" * 60} for _ in range(50)]
    out = report.texts("w", [{"op": "gmail.trash", "status": "done", "items": 50, "detail": json.dumps({"as": "gmail.trash", "items": items})}])
    assert all(report.units(t) <= 3900 for t in out) and sum(t.count("•") for t in out) == 50


def test_archiving_by_label_is_reported_as_archiving(env):
    d, gm, store = env
    allow(d)
    assert agent(d, "gmail.label", ids=["n1"], add=["Receipts"], remove=["INBOX"])["ok"]
    t = due(d, store)
    assert "Archived: 1" in t and "added Receipts; removed INBOX" in t


def test_routine_labels_are_reported_once_any_report_is_on(env):
    d, gm, store = env
    allow(d, ops=("trash",))
    assert agent(d, "gmail.label", ids=["n1"], remove=["INBOX"])["ok"]
    assert "Archived: 1" in due(d, store)


def test_drafts_are_reported_with_their_recipient(env):
    d, gm, store = env
    allow(d, ops=("draft",))
    assert agent(d, "gmail.draft", body="x" * 5000, to="who@y.example", subject="Hi")["ok"]
    t = due(d, store).replace("\u2060", "")
    assert "who@y.example" in t and "Hi" in t


def test_payments_off_stops_payments(env):
    d, *_ = env
    d.permissions.apply("chat", {"payment.*": ["default", "off"], "info.*": ["default", "off"]}, "t")
    assert d.permissions.mode("chat", "checkout.pay") == d.permissions.mode("chat", "checkout.fill") == "off"
    assert d.permissions.mode("chat", "checkout.open") == "default"


def test_ask_isnt_offered_where_the_answer_is_needed():
    crit = {n for n in OPS if OPS[n].critical}
    for op in ("info.collect", "checkout.snapshot", "checkout.open", "checkout.click"):
        assert "ask" not in perms.allowed_modes("chat", op, OPS, crit) and "ask" not in perms.allowed_modes(SCOPE, op, OPS, crit)
    assert "ask" in perms.allowed_modes("chat", "checkout.pay", OPS, crit)


def test_a_second_request_keeps_the_first(env):
    d, *_ = env
    hub_data.set_(d, SCOPE, {"gmail.star": ["default", "report"]})
    hub_data.set_(d, SCOPE, {"gmail.archive": ["default", "report"]})
    (item,) = pending(d)
    assert "gmail.star" in item["summary"] and "gmail.archive" in item["summary"]
    call(d, {"cmd": "watch.delete", "profile": "personal", "name": "sorter"})
    assert not pending(d)


def test_tools_that_run_other_commands_cant_be_proposed(env):
    d, *_ = env
    out = propose(d, SORTER.replace('"Bash(gmail:*)"', '"Bash(find:*)"'), name="sorter")
    assert not out["ok"] and "find" in out["error"]


def test_declared_actions_are_checked_too_once_reports_are_on(env):
    d, gm, store = env
    toml = SORTER.replace('tools = ["Bash(gmail:*)"]', 'tools = ["Bash(gmail:*)"]\nactions = [{ op = "gmail.label" }, { op = "gmail.archive" }]')
    h = activate(d, toml, name="sorter")
    d.runs.origin_of = lambda pid: f"watcher:personal/sorter@{h[:12]}#think"
    assert "approval" not in agent(d, "gmail.archive", ids=["sec"])  # as approved
    allow(d, ops=("label", "trash"))
    n = len(writes(gm))
    assert agent(d, "gmail.label", ids=["imp"], remove=["IMPORTANT"])["approval"]
    assert not agent(d, "gmail.archive", ids=["sec"])["ok"]
    assert len(writes(gm)) == n
    assert agent(d, "gmail.label", ids=["n1"], remove=["INBOX"])["ok"]
    assert "Archived: 1" in due(d, store)
