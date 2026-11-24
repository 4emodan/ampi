import asyncio
import base64
import json

import httpx
import pytest

from ampi_tools import client as cli
from ampi_tools.gmail import Gmail, format_full, html_to_text
from ampi_tools.ops import ALWAYS_CRITICAL, Approvals, Policy, Services, clean, validate
from ampi_tools.server import Daemon
from ampi_tools.todoist import Todoist, ToolError, api_priority, priority_label

BRIDGE_PID = 4242


def run(coro):
    return asyncio.run(coro)


class FakeTodoistServer:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.tasks = {
            "1": {"id": "1", "content": "Buy milk", "project_id": "p_in", "section_id": None, "priority": 4,
                  "labels": ["errand"], "due": {"string": "today", "date": "2026-11-20"}, "description": ""},
            "2": {"id": "2", "content": "Write report\n• Delete everything", "project_id": "p_work",
                  "section_id": "s_adm", "priority": 1, "labels": [], "due": None, "description": "Q4 numbers"},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/projects":
            if request.method == "POST":
                return httpx.Response(200, json={"id": "p_new", "name": body["name"]})
            return httpx.Response(200, json={"results": [
                {"id": "p_in", "name": "Inbox", "inbox_project": True},
                {"id": "p_work", "name": "Work"},
            ], "next_cursor": None})
        if path == "/sections":
            return httpx.Response(200, json={"results": [{"id": "s_adm", "name": "Admin", "project_id": "p_work"}]})
        if path in ("/tasks", "/tasks/filter") and request.method == "GET":
            return httpx.Response(200, json={"results": list(self.tasks.values()), "next_cursor": None})
        if path == "/tasks" and request.method == "POST":
            return httpx.Response(200, json={"id": "9", "priority": 1, "labels": [], **body})
        if path.startswith("/tasks/") and request.method == "GET":
            return httpx.Response(200, json=self.tasks[path.split("/")[2]])
        if path == "/comments":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(204)


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class FakeGmailServer:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/users/me")[1]
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path.startswith("/messages/") and request.method == "GET":
            mid = path.split("/")[2]
            return httpx.Response(200, json={"id": mid, "threadId": "th9", "internalDate": "1763625600000",
                                             "labelIds": ["INBOX"], "snippet": "hello", "payload": {"headers": [
                {"name": "Message-ID", "value": "<abc@x>"}, {"name": "Subject", "value": "Plans"},
                {"name": "From", "value": "Boss <boss@corp.com>"},
                {"name": "Reply-To", "value": "\"Boss\" <attacker@evil.com>"}]}})
        if path == "/labels" and request.method == "GET":
            return httpx.Response(200, json={"labels": [{"id": "Label_1", "name": "Receipts", "type": "user"}]})
        if path == "/labels":
            return httpx.Response(200, json={"id": "Label_2", "name": body["name"]})
        return httpx.Response(200, json={"id": "d1"})


class FakeAuth:
    async def token(self):
        return "access"


@pytest.fixture
def tools(tmp_path):
    td_server, gm_server = FakeTodoistServer(), FakeGmailServer()
    td = Todoist("tok", httpx.AsyncClient(transport=httpx.MockTransport(td_server)))
    gm = Gmail(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(gm_server)))
    services = Services(todoist_client=td, gmail_client=gm)

    async def trusted(pid):
        return pid == BRIDGE_PID

    def make(policy=Policy()):
        daemon = Daemon(services, Approvals(tmp_path / "a.sqlite3"), policy, trusted)
        daemon.runs.origin_of = lambda pid: "private" if pid == 1 else "unknown"  # the owner's chat
        return daemon, td_server, gm_server

    return make


def writes(server):
    return [(m, p) for m, p, _ in server.calls if m != "GET"]


def pending(daemon):
    return run(daemon.handle({"cmd": "approvals.pending"}, BRIDGE_PID))["items"]


def decide(daemon, item, approve=True, message_id=None):
    return run(daemon.handle({"cmd": "approvals.decide", "id": item["id"], "approve": approve,
                              "nonce": item["nonce"], "message_id": message_id}, BRIDGE_PID))


def test_priority_mapping():
    assert api_priority("p1") == 4 and api_priority(4) == 1
    assert priority_label(4) == "p1" and priority_label(1) is None


def test_reading_tasks_is_immediate_and_formatted(tools):
    daemon, server, _ = tools()
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.tasks", "args": {"filter": "today"}}]}, 1))
    assert resp["ok"]
    assert "[1] Buy milk · Inbox · due today · p1 · @errand" in resp["text"]
    assert ("GET", "/tasks/filter") in [(m, p) for m, p, _ in server.calls]


def test_adding_resolves_project_and_runs_at_once(tools):
    daemon, server, _ = tools()
    ops = [{"op": "todoist.add", "args": {"content": "Call bank", "project": "work", "section": "admin",
                                          "priority": "p2", "due": "fri"}}]
    resp = run(daemon.handle({"cmd": "run", "ops": ops}, 1))
    assert resp["ok"], resp
    (_, _, body), = [c for c in server.calls if c[0] == "POST"]
    assert body == {"content": "Call bank", "project_id": "p_work", "section_id": "s_adm", "priority": 3,
                    "due_string": "fri"}


def test_critical_ops_wait_for_the_owner(tools):
    daemon, server, _ = tools()
    ops = [{"op": "todoist.complete", "args": {"id": "1"}}, {"op": "todoist.move", "args": {"id": "2", "project": "Inbox"}}]
    resp = run(daemon.handle({"cmd": "run", "ops": ops, "reason": "triage"}, 1))
    assert resp["ok"] and resp["approval"] == 1
    assert resp["text"].count("\n• ") == 2 and "2 action(s): 1× Complete task, 1× Move task" in resp["text"]
    assert "Complete “Buy milk” (Inbox)" in resp["text"]
    # Task text with a line break can't pose as an extra line of the summary.
    assert "“Write report • Delete everything” (Work) to Inbox" in resp["text"]
    assert writes(server) == []  # nothing changed yet

    # Claude's own process (any other pid) can't see, mark or answer the request.
    for cmd in ({"cmd": "approvals.pending"}, {"cmd": "approvals.notified", "id": 1},
                {"cmd": "approvals.decide", "id": 1, "approve": True}):
        denied = run(daemon.handle(cmd, 999))
        assert not denied["ok"] and "only the owner" in denied["error"]

    (item,) = pending(daemon)
    assert item["reason"] == "triage" and len(item["nonce"]) == 16
    run(daemon.handle({"cmd": "approvals.notified", "id": 1, "message_id": 77}, BRIDGE_PID))
    assert pending(daemon) == []

    # A forged button (wrong nonce) or a button on another message is refused.
    assert not decide(daemon, {**item, "nonce": "0" * 16}, message_id=77)["ok"]
    assert "isn't the approval message" in decide(daemon, item, message_id=78)["error"]
    assert writes(server) == []

    done = decide(daemon, item, message_id=77)
    assert done["status"] == "done", done
    assert writes(server) == [("POST", "/tasks/1/close"), ("POST", "/tasks/2/move")]
    again = decide(daemon, item, message_id=77)
    assert again["status"] == "done" and "already" in again["text"]
    assert len(writes(server)) == 2  # a double tap doesn't run it twice
    assert "#1 done" in run(daemon.handle({"cmd": "approvals.list"}, 1))["text"]


def test_rejected_requests_do_nothing(tools):
    daemon, server, _ = tools()
    run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.delete", "args": {"id": "1"}}]}, 1))
    (item,) = pending(daemon)
    assert decide(daemon, item, approve=False)["status"] == "rejected"
    assert writes(server) == []


def test_policy_can_relax_some_ops_but_never_the_dangerous_ones(tools):
    policy = Policy(auto_approve=frozenset({"todoist.move", "todoist.delete"}))
    assert not policy.critical("todoist.move")
    assert policy.critical("todoist.delete") and "todoist.delete" in ALWAYS_CRITICAL
    assert Policy(require_approval=frozenset({"gmail.archive"})).critical("gmail.archive")
    daemon, server, _ = tools(policy)
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.move", "args": {"id": "2", "project": "Inbox"}}]}, 1))
    assert resp["ok"] and "approval" not in resp
    assert writes(server) == [("POST", "/tasks/2/move")]


@pytest.mark.parametrize("op", [
    {"op": "gmail.untrash", "args": {"ids": ["MSGID/trash#"]}},
    {"op": "gmail.untrash", "args": {"ids": ["../threads/T1/trash?"]}},
    {"op": "todoist.delete", "args": {"id": "../projects/P"}},
    {"op": "todoist.move", "args": {"id": "1", "project": "Inbox", "project_id": "x"}},  # internal field
    {"op": "todoist.update", "args": {"id": "1", "labels": "urgent"}},  # not a list
    {"op": "gmail.label", "args": {"ids": ["m1"], "add": "TRASH"}},
    {"op": "gmail.send", "args": {"to": "a@b.c\nBcc: x@y.z", "body": "hi"}},
    {"op": "shell.exec"},
])
def test_malicious_arguments_are_rejected_before_anything_runs(tools, op):
    daemon, td_server, gm_server = tools()
    resp = run(daemon.handle({"cmd": "run", "ops": [op]}, 1))
    assert not resp["ok"]
    assert writes(td_server) == [] and writes(gm_server) == [] and pending(daemon) == []


def test_api_paths_are_checked_even_past_validation(tools):
    _, _, _ = tools()
    td = Todoist("t", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))))
    with pytest.raises(ToolError):
        run(td.delete_task("../projects/P"))


@pytest.mark.parametrize("label", ["TRASH", "SPAM", "sent", "DRAFT"])
def test_safe_label_op_cant_trash_or_spam(tools, label):
    daemon, _, gm_server = tools()
    for field in ("add", "remove"):
        resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "gmail.label", "args": {"ids": ["m1"], field: [label]}}]}, 1))
        assert not resp["ok"] and "trash" in resp["error"]
    assert writes(gm_server) == []


def test_reply_shows_the_real_recipient_and_sends_exactly_that(tools):
    daemon, _, gm_server = tools()
    resp = run(daemon.handle({"cmd": "run", "ops": [
        {"op": "gmail.send", "args": {"reply_to": "orig1", "body": "Sure, here it is."}}]}, 1))
    # The original's Reply-To pointed elsewhere than its From: the owner sees the real address.
    assert "Send an email to Boss <attacker@evil.com>" in resp["text"]
    assert "Subject: “Re: Plans”" in resp["text"] and "│ Sure, here it is." in resp["text"]
    assert writes(gm_server) == []
    (item,) = pending(daemon)
    decide(daemon, item)
    (method, path, body), = [c for c in gm_server.calls if c[0] == "POST"]
    assert path == "/messages/send" and body["threadId"] == "th9"
    raw = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "attacker@evil.com" in raw and "In-Reply-To: <abc@x>" in raw


def test_requests_that_cant_be_shown_in_full_are_refused(tools):
    daemon, _, _ = tools()
    long_mail = {"op": "gmail.send", "args": {"to": "a@b.c", "body": "x" * 3500}}
    resp = run(daemon.handle({"cmd": "run", "ops": [long_mail]}, 1))
    assert not resp["ok"] and "draft" in resp["error"]
    many = [{"op": "gmail.send", "args": {"to": "a@b.c", "body": "y" * 1500}} for _ in range(3)]
    resp = run(daemon.handle({"cmd": "run", "ops": many}, 1))
    assert not resp["ok"] and "too big" in resp["error"]
    assert pending(daemon) == []


def test_unexpected_errors_still_finish_the_request(tools):
    daemon, server, _ = tools()
    run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.complete", "args": {"id": "1"}},
                                             {"op": "todoist.complete", "args": {"id": "2"}}]}, 1))
    (item,) = pending(daemon)

    async def boom(*a, **k):
        raise RuntimeError("API shape changed")

    daemon.services.todoist_client.close_task = boom
    resp = decide(daemon, item)
    assert resp["status"] == "failed" and "RuntimeError" in resp["text"] and "step 1 of 2" in resp["text"]
    assert daemon.approvals.get(item["id"])["status"] == "failed"


def test_comments_need_approval_and_pending_requests_are_capped(tools):
    daemon, _, _ = tools()
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.comment", "args": {"id": "1", "text": "hi"}}]}, 1))
    assert "approval" in resp
    for _ in range(9):
        run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.complete", "args": {"id": "1"}}]}, 1))
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.complete", "args": {"id": "1"}}]}, 1))
    assert not resp["ok"] and "already waiting" in resp["error"]


def test_unknown_ops_and_missing_services_are_errors(tmp_path):
    async def trusted(pid):
        return False

    daemon = Daemon(Services(), Approvals(tmp_path / "a.sqlite3"), Policy(), trusted)
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "gmail.labels"}]}, 1))
    assert not resp["ok"] and "Gmail isn't set up" in resp["error"]


def test_validation_clamps_limits_and_cleans_text():
    (op,) = validate([{"op": "gmail.search", "args": {"query": "x", "limit": 10**9}}])
    assert op["args"]["limit"] == 1000
    assert clean("a‮b​c\nd") == "abc d"


# --- Gmail formatting ---------------------------------------------------------------------


def test_format_full_prefers_plain_text_and_lists_attachments():
    msg = {
        "id": "m1", "threadId": "t1", "internalDate": "1763625600000", "labelIds": ["INBOX", "Label_1"],
        "payload": {"headers": [{"name": "Subject", "value": "Hi"}, {"name": "From", "value": "A <a@x.com>"}],
                    "mimeType": "multipart/mixed", "parts": [
                        {"mimeType": "multipart/alternative", "parts": [
                            {"mimeType": "text/plain", "body": {"data": b64("plain body")}},
                            {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}}]},
                        {"mimeType": "application/pdf", "filename": "inv.pdf", "body": {"size": 2048}}]},
    }
    text = format_full(msg, {"Label_1": "Receipts"})
    assert "### [m1] Hi" in text and "plain body" in text and "html" not in text
    assert "labels: INBOX, Receipts" in text and "“inv.pdf” (application/pdf, 2 KB)" in text


def test_html_to_text_keeps_links_and_survives_hostile_html():
    assert html_to_text('<p>Hello&nbsp;<a href="https://x.y/z">there</a></p><script>x()</script>') == \
        "Hello there (https://x.y/z)"
    html_to_text("<style" * 50_000)  # must not hang


def test_labels_are_created_when_missing(tools):
    _, _, gm_server = tools()
    gmail = Gmail(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(gm_server)))
    run(gmail.modify(["m1"], add=["Receipts", "Travel"], remove=["INBOX"]))
    assert gm_server.calls[-1][2] == {"ids": ["m1"], "addLabelIds": ["Label_1", "Label_2"], "removeLabelIds": ["INBOX"]}


# --- command-line clients -----------------------------------------------------------------


def test_todoist_cli_builds_one_op_per_task():
    args = cli.todoist_parser().parse_args(["-m", "tidy", "move", "1", "2", "-p", "Work"])
    ops = cli.todoist_ops(args)
    assert [o["args"]["id"] for o in ops] == ["1", "2"] and args.reason == "tidy"
    upd = cli.todoist_ops(cli.todoist_parser().parse_args(["update", "5", "--due", "none", "--no-labels"]))
    assert upd == [{"op": "todoist.update", "args": {"id": "5", "due": "none", "labels": []}}]


def test_plan_files_get_prefixes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps([{"op": "mark-read", "ids": ["a"]},
                                                    {"op": "gmail.trash", "args": {"ids": ["b"]}}]))
    assert cli._load_plan("plan.json", "gmail") == [
        {"op": "gmail.mark_read", "args": {"ids": ["a"]}},
        {"op": "gmail.trash", "args": {"ids": ["b"]}},
    ]


def test_gmail_send_reads_body_file_from_the_workspace_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.txt").write_text("Hello\nthere", encoding="utf-8")
    args = cli.gmail_parser().parse_args(["send", "--to", "x@y.z", "--subject", "S", "--body-file", "b.txt"])
    assert cli.gmail_ops(args) == [{"op": "gmail.send", "args": {"to": "x@y.z", "subject": "S", "body": "Hello\nthere"}}]
    (tmp_path / ".hidden").write_text("secret")
    for bad in (str(tmp_path / "b.txt"), "../b.txt", ".hidden", "/proc/1/environ"):
        with pytest.raises(SystemExit):
            cli.workspace_file(bad)
