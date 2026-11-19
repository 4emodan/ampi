import asyncio
import base64
import json

import httpx
import pytest

from ampi_tools import client as cli
from ampi_tools.gmail import Gmail, format_full, html_to_text
from ampi_tools.ops import ALWAYS_CRITICAL, Approvals, Policy, Services
from ampi_tools.server import Daemon
from ampi_tools.todoist import Todoist, api_priority, priority_label

BRIDGE_PID = 4242


def run(coro):
    return asyncio.run(coro)


class FakeTodoistServer:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.tasks = {
            "1": {"id": "1", "content": "Buy milk", "project_id": "p_in", "section_id": None, "priority": 4,
                  "labels": ["errand"], "due": {"string": "today", "date": "2026-11-20"}, "description": ""},
            "2": {"id": "2", "content": "Write report", "project_id": "p_work", "section_id": "s_adm",
                  "priority": 1, "labels": [], "due": None, "description": "Q4 numbers"},
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


@pytest.fixture
def tools(tmp_path):
    server = FakeTodoistServer()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    services = Services(todoist_client=Todoist("tok", client))

    async def trusted(pid):
        return pid == BRIDGE_PID

    def make(policy=Policy()):
        return Daemon(services, Approvals(tmp_path / "a.sqlite3"), policy, trusted), server

    return make


def writes(server):
    return [(m, p) for m, p, _ in server.calls if m != "GET"]


def test_priority_mapping():
    assert api_priority("p1") == 4 and api_priority(4) == 1
    assert priority_label(4) == "p1" and priority_label(1) is None


def test_reading_tasks_is_immediate_and_formatted(tools):
    daemon, server = tools()
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.tasks", "args": {"filter": "today"}}]}, 1))
    assert resp["ok"]
    assert "[1] Buy milk · Inbox · due today · p1 · @errand" in resp["text"]
    assert "[2] Write report · Work / Admin" in resp["text"] and "Q4 numbers" in resp["text"]
    assert ("GET", "/tasks/filter") in [(m, p) for m, p, _ in server.calls]


def test_adding_resolves_project_and_runs_at_once(tools):
    daemon, server = tools()
    ops = [{"op": "todoist.add", "args": {"content": "Call bank", "project": "work", "section": "admin",
                                          "priority": "p2", "due": "fri"}}]
    resp = run(daemon.handle({"cmd": "run", "ops": ops}, 1))
    assert resp["ok"], resp
    (_, _, body), = [c for c in server.calls if c[0] == "POST"]
    assert body == {"content": "Call bank", "project_id": "p_work", "section_id": "s_adm", "priority": 3,
                    "due_string": "fri"}


def test_critical_ops_wait_for_the_owner(tools):
    daemon, server = tools()
    ops = [{"op": "todoist.complete", "args": {"id": "1"}}, {"op": "todoist.move", "args": {"id": "2", "project": "Inbox"}}]
    resp = run(daemon.handle({"cmd": "run", "ops": ops, "reason": "triage"}, 1))
    assert resp["ok"] and resp["approval"] == 1
    assert "Complete “Buy milk” (Inbox)" in resp["text"] and "Move “Write report” (Work) to Inbox" in resp["text"]
    assert writes(server) == []  # nothing changed yet

    # Claude's own process (any other pid) can't see or answer the request.
    for cmd in ({"cmd": "approvals.pending"}, {"cmd": "approvals.decide", "id": 1, "approve": True}):
        denied = run(daemon.handle(cmd, 999))
        assert not denied["ok"] and "only the owner" in denied["error"]
    assert writes(server) == []

    pending = run(daemon.handle({"cmd": "approvals.pending"}, BRIDGE_PID))["items"]
    assert [p["id"] for p in pending] == [1] and pending[0]["reason"] == "triage"
    run(daemon.handle({"cmd": "approvals.notified", "id": 1}, BRIDGE_PID))
    assert run(daemon.handle({"cmd": "approvals.pending"}, BRIDGE_PID))["items"] == []

    done = run(daemon.handle({"cmd": "approvals.decide", "id": 1, "approve": True}, BRIDGE_PID))
    assert done["status"] == "done"
    assert writes(server) == [("POST", "/tasks/1/close"), ("POST", "/tasks/2/move")]
    again = run(daemon.handle({"cmd": "approvals.decide", "id": 1, "approve": True}, BRIDGE_PID))
    assert again["status"] == "done" and "already" in again["text"]
    assert len(writes(server)) == 2  # a double tap doesn't run it twice
    assert "#1 done" in run(daemon.handle({"cmd": "approvals.list"}, 1))["text"]


def test_rejected_requests_do_nothing(tools):
    daemon, server = tools()
    run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.delete", "args": {"id": "1"}}]}, 1))
    resp = run(daemon.handle({"cmd": "approvals.decide", "id": 1, "approve": False}, BRIDGE_PID))
    assert resp["status"] == "rejected"
    assert writes(server) == []


def test_policy_can_relax_some_ops_but_never_the_dangerous_ones(tools):
    policy = Policy(auto_approve=frozenset({"todoist.move", "todoist.delete"}))
    assert not policy.critical("todoist.move")
    assert policy.critical("todoist.delete") and "todoist.delete" in ALWAYS_CRITICAL
    assert Policy(require_approval=frozenset({"gmail.archive"})).critical("gmail.archive")
    daemon, server = tools(policy)
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "todoist.move", "args": {"id": "2", "project": "Inbox"}}]}, 1))
    assert resp["ok"] and "approval" not in resp
    assert writes(server) == [("POST", "/tasks/2/move")]


def test_unknown_ops_and_missing_services_are_errors(tools):
    daemon, _ = tools()
    assert not run(daemon.handle({"cmd": "run", "ops": [{"op": "shell.exec"}]}, 1))["ok"]
    resp = run(daemon.handle({"cmd": "run", "ops": [{"op": "gmail.labels"}]}, 1))
    assert not resp["ok"] and "Gmail isn't set up" in resp["error"]


# --- Gmail --------------------------------------------------------------------------------


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


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
    assert "labels: INBOX, Receipts" in text and "inv.pdf (application/pdf, 2 KB)" in text


def test_html_to_text_keeps_links():
    assert html_to_text('<p>Hello&nbsp;<a href="https://x.y/z">there</a></p><script>x()</script>') == \
        "Hello there (https://x.y/z)"


class FakeAuth:
    async def token(self):
        return "access"


def test_reply_keeps_thread_and_labels_are_created(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/users/me")[1]
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if path == "/messages/orig":
            return httpx.Response(200, json={"id": "orig", "threadId": "th9", "payload": {"headers": [
                {"name": "Message-ID", "value": "<abc@x>"}, {"name": "Subject", "value": "Plans"},
                {"name": "From", "value": "Bob <bob@x.com>"}]}})
        if path == "/labels" and request.method == "GET":
            return httpx.Response(200, json={"labels": [{"id": "Label_1", "name": "Receipts", "type": "user"}]})
        if path == "/labels":
            return httpx.Response(200, json={"id": "Label_2", "name": body["name"]})
        return httpx.Response(200, json={"id": "d1"})

    gmail = Gmail(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    run(gmail.draft("", "", "Sounds good", reply_to="orig"))
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/drafts") and body["message"]["threadId"] == "th9"
    raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
    assert "To: Bob <bob@x.com>" in raw and "Subject: Re: Plans" in raw and "In-Reply-To: <abc@x>" in raw

    run(gmail.modify(["m1"], add=["Receipts", "Travel"], remove=["INBOX"]))
    assert calls[-1][2] == {"ids": ["m1"], "addLabelIds": ["Label_1", "Label_2"], "removeLabelIds": ["INBOX"]}


# --- command-line clients -----------------------------------------------------------------


def test_todoist_cli_builds_one_op_per_task():
    args = cli.todoist_parser().parse_args(["-m", "tidy", "move", "1", "2", "-p", "Work"])
    ops = cli.todoist_ops(args)
    assert [o["args"]["id"] for o in ops] == ["1", "2"] and args.reason == "tidy"
    upd = cli.todoist_ops(cli.todoist_parser().parse_args(["update", "5", "--due", "none", "--no-labels"]))
    assert upd == [{"op": "todoist.update", "args": {"id": "5", "due": "none", "labels": []}}]


def test_plan_files_get_prefixes(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"op": "mark-read", "ids": ["a"]}, {"op": "gmail.trash", "args": {"ids": ["b"]}}]))
    assert cli._load_plan(str(plan), "gmail") == [
        {"op": "gmail.mark_read", "args": {"ids": ["a"]}},
        {"op": "gmail.trash", "args": {"ids": ["b"]}},
    ]


def test_gmail_send_reads_body_file(tmp_path):
    body = tmp_path / "b.txt"
    body.write_text("Hello\nthere", encoding="utf-8")
    args = cli.gmail_parser().parse_args(["send", "--to", "x@y.z", "--subject", "S", "--body-file", str(body)])
    assert cli.gmail_ops(args) == [{"op": "gmail.send", "args": {"to": "x@y.z", "subject": "S", "body": "Hello\nthere"}}]
