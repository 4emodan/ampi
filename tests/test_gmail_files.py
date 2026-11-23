"""Files attached to emails: numbering, size limits, who may download, and where they are saved."""

import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from ampi_tools import client as cli
from ampi_tools import gmail as gm
from ampi_tools import watch_spec as ws
from ampi_tools.gmail import Gmail, format_full
from ampi_tools.ops import Approvals, Policy, Services, validate
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError

PDF = bytes(range(256)) * 10
AID = "ANGjdJ" + "a_-Z9" * 80  # Google's are this long


def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


MESSAGE = {"id": "m1", "threadId": "t1", "labelIds": [], "payload": {
    "mimeType": "multipart/mixed", "headers": [{"name": "Subject", "value": "Invoice"}], "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": b64u(b"See attached")}},
            {"mimeType": "text/html", "body": {"data": b64u(b"<p>See</p>")}}]},
        {"mimeType": "application/pdf", "filename": "in\u202evoice\n 1.pdf", "body": {"size": len(PDF), "attachmentId": AID}},
        {"mimeType": "text/plain", "filename": "note.txt", "body": {"size": 5, "data": b64u(b"hello")}},
        {"mimeType": "message/rfc822", "filename": "", "parts": [
            {"mimeType": "image/png", "filename": "deep.png", "body": {"size": 30 * 1024 * 1024, "attachmentId": "x"}}]}]}}


class FakeAuth:
    async def token(self):
        return "tok"

    def configured(self):
        return True


def server(message=MESSAGE, data=None):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"size": len(PDF), "data": b64u(PDF) if data is None else data})
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": []})
        return httpx.Response(200, json=message)
    return Gmail(FakeAuth(), httpx.AsyncClient(transport=httpx.MockTransport(handler))), seen


def run(coro):
    return asyncio.run(coro)


def test_files_are_numbered_everywhere_the_same():
    g, _ = server()
    text = format_full(MESSAGE, {})
    assert "Attached files:\n  1) “invoice 1.pdf” (application/pdf, 2 KB)\n  2) “note.txt” (text/plain, 1 KB)\n  3) “deep.png”" in text
    assert "See attached" in text and "hello" not in text
    assert [f["name"] for f in run(g.files("m1"))] == ["invoice 1.pdf", "note.txt", "deep.png"]


def test_download():
    g, seen = server()
    f = run(g.download("m1", 1))
    assert base64.b64decode(f["data"], validate=True) == PDF and f["name"] == "invoice 1.pdf" and f["size"] == len(PDF)
    assert seen[-1].endswith(f"/messages/m1/attachments/{AID}")
    f = run(g.download("m1", 2))  # a small one comes with the message itself
    assert base64.b64decode(f["data"]) == b"hello" and not seen[-1].count("/attachments/")


@pytest.mark.parametrize("number,words", [(0, "3 file"), (4, "3 file"), (3, "too big")])
def test_wrong_numbers_and_big_files(number, words):
    g, seen = server()
    with pytest.raises(ToolError, match=words):
        run(g.download("m1", number))
    assert not any("/attachments/" in p for p in seen)


def test_a_name_cannot_pretend_to_be_another_file():
    m = json.loads(json.dumps(MESSAGE))
    m["payload"]["parts"][1]["filename"] = "a.pdf” (x, 1 KB)\n  2) “Contract.pdf"
    assert format_full(m, {}).count("\n  2) ") == 1
    assert "“a.pdf' (x, 1 KB) 2) 'Contract.pdf”" in format_full(m, {})


def test_many_files_are_cut():
    part = {"filename": "x", "mimeType": "a/b", "body": {"size": 1}}
    assert len(gm.attachments({"parts": [part] * 5000})) == gm.MAX_FILES
    assert len(gm.attachments({"filename": "y" * 5_000_000, "body": {}})[0]["name"]) == gm.MAX_NAME


def test_a_file_bigger_than_it_said(monkeypatch):
    monkeypatch.setattr(gm, "MAX_FILE", 4000)
    g, _ = server(data="A" * 9000)
    with pytest.raises(ToolError, match="too big"):
        run(g.download("m1", 1))


@pytest.mark.parametrize("data", ["not base64!", "AA\n", "AAAAA", 5])
def test_broken_data(data):
    g, _ = server(data=data)
    with pytest.raises(ToolError, match="can't read"):
        run(g.download("m1", 1))


@pytest.mark.parametrize("aid", ["../../drafts", "a/b", "a?b", "x" * 3000, ""])
def test_odd_ids_never_reach_the_api(aid):
    m = json.loads(json.dumps(MESSAGE))
    m["payload"]["parts"][1]["body"]["attachmentId"] = aid
    g, seen = server(m)
    if aid:
        with pytest.raises(ToolError, match="odd"):
            run(g.download("m1", 1))
    else:
        assert run(g.download("m1", 1))["data"] == ""
    assert not any("/attachments/" in p for p in seen)


def daemon(tmp_path):
    async def trusted(pid):
        return False
    return Daemon(Services(gmail_client=server()[0]), Approvals(tmp_path / "a.sqlite3"), Policy(), trusted)


def go(d, origin, *ops):
    return run(d.run(validate(list(ops)), None, None, origin))


DL = {"op": "gmail.download", "args": {"id": "m1", "number": 1}}


def test_only_in_the_owners_own_chat(tmp_path):
    d = daemon(tmp_path)
    r = go(d, "private", DL)
    assert r["ok"] and base64.b64decode(r["file"]["data"]) == PDF
    masked = d._masked(r, validate([DL]))
    assert masked["file"]["data"] == base64.b64encode(PDF).decode()  # untouched
    for origin in ("group:family", "forwarded:private", "forwarded:social", "unknown", "watcher:mail",
                   "watcher:personal/mail@0123456789ab#step"):
        with pytest.raises(ToolError):
            go(d, origin, DL)
    with pytest.raises(ToolError, match="one file"):
        go(d, "private", DL, DL)
    for n in (0, -3, 101, True, "1"):
        with pytest.raises(ToolError):
            go(d, "private", {"op": "gmail.download", "args": {"id": "m1", "number": n}})
    d.policy = Policy(require_approval=frozenset({"gmail.download"}))
    with pytest.raises(ToolError, match="switched"):
        go(d, "private", DL)
    d.policy = Policy()
    assert "gmail.download" in ws.NOT_FOR_WATCHERS
    assert "invoice 1.pdf" in go(d, "group:family", {"op": "gmail.files", "args": {"id": "m1"}})["text"]


def test_saving(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = datetime(2026, 11, 24, 7, 5, 9, tzinfo=timezone.utc)

    def save(name, n=1, data=b"x"):
        return cli.save_mail_file(n, {"name": name, "data": base64.b64encode(data).decode()}, now).as_posix()
    assert save("Invoice 1.pdf", data=PDF) == "inbox/mail/2026-11-24/070509_1_Invoice_1.pdf"
    assert (tmp_path / "inbox/mail/2026-11-24/070509_1_Invoice_1.pdf").read_bytes() == PDF
    assert save("Invoice 1.pdf").endswith("070509_1-1_Invoice_1.pdf")  # never over another one
    assert save("../../../.claude/settings.json").endswith("/070509_1_claude_settings.json")
    assert save("CLAUDE.md").endswith("/070509_1_CLAUDE.md")  # with the number in front it isn't one Claude Code loads
    assert save("счёт за свет.pdf").endswith("_счёт_за_свет.pdf")
    for odd in ("", "...", "漢" * 200 + ".numbers", "\U00020000" * 100 + ".pdf", "/", "\\", "a" * 300, "x." + "y" * 50, ".bashrc", "con\x00.txt"):
        p = tmp_path / save(odd)
        assert p.parent == tmp_path / "inbox/mail/2026-11-24" and len(p.name.encode()) < 220 and p.name.startswith("070509_1")


@pytest.mark.parametrize("name", ["inbox/mail/x/note.txt", "./inbox/mail/note.txt"])
def test_other_runs_cannot_pass_them_on(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / name
    f.parent.mkdir(parents=True)
    f.write_text("secret")
    assert cli.workspace_file(name) == "secret"  # the owner's own chat
    monkeypatch.setenv("AMPI_ORIGIN", "group:family")
    with pytest.raises(SystemExit, match="own chat"):
        cli.workspace_file(name)
    for inside in ("inbox", "inbox/mail"):  # (also from inside it)
        monkeypatch.chdir(tmp_path / inside)
        with pytest.raises(SystemExit, match="own chat"):
            cli.workspace_file(str(f.relative_to(tmp_path / inside)))
    (tmp_path / "plan.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    assert cli.workspace_file("plan.json") == "{}"


def test_never_through_a_link(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    w = tmp_path / "w"
    w.mkdir()
    try:
        (w / "inbox").symlink_to(out, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    monkeypatch.chdir(w)
    with pytest.raises(SystemExit):
        cli.save_mail_file(1, {"name": "a", "data": ""})
    assert not list(out.iterdir())


def test_one_at_a_time_until_it_was_taken(tmp_path):
    d, log = daemon(tmp_path), []
    d.runs = type("R", (), {"origin_of": lambda self, pid: "private"})()

    async def handle(req, pid):
        log.append("start")
        await asyncio.sleep(0.05)
        return {"ok": True, "file": {"data": "x"}}
    d.handle = handle

    class Wr:
        def __init__(self):
            self.transport = self

        def get_extra_info(self, name):
            return None

        def write(self, data):
            log.append("write")

        async def drain(self):
            await asyncio.sleep(0.05)
            log.append("taken")

        def close(self):
            pass

    async def both():
        async def one():
            rd = asyncio.StreamReader()
            rd.feed_data(json.dumps({"cmd": "run", "ops": [DL]}).encode() + b"\n")
            await d.connection(rd, Wr())
        await asyncio.gather(one(), one(), one())
    import ampi_tools.server as srv

    real, srv.peer_pid = srv.peer_pid, lambda wr: 1
    try:
        run(both())
    finally:
        srv.peer_pid = real
    assert log == ["start", "write", "taken"] * 3
    # One that doesn't take its answer is cut off, and the next has its turn.
    log.clear()
    real_wait, srv.peer_pid = srv.asyncio.wait_for, lambda wr: 1
    aborted = []

    class Stuck(Wr):
        async def drain(self):
            await asyncio.sleep(30)

        def abort(self):
            aborted.append(1)

    async def stuck_then_fine():
        async def short(aw, t):
            return await real_wait(aw, 0.1 if t == 60 else t)
        srv.asyncio.wait_for = short
        for w in (Stuck(), Wr()):
            rd = asyncio.StreamReader()
            rd.feed_data(json.dumps({"cmd": "run", "ops": [DL]}).encode() + b"\n")
            await d.connection(rd, w)
    try:
        run(stuck_then_fine())
    finally:
        srv.asyncio.wait_for, srv.peer_pid = real_wait, real
    assert aborted == [1] and log == ["start", "write", "start", "write", "taken"]


def test_a_plan_cannot_download(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plan.json").write_text(json.dumps([{"op": "download", "args": {"id": "m1", "number": 1}}]))
    with pytest.raises(SystemExit, match="gmail download"):
        cli._load_plan("plan.json", "gmail")


def test_what_is_said_about_the_file_is_masked(tmp_path):
    d = daemon(tmp_path)
    d._mask_obj = lambda obj, level: {k: "•" if isinstance(v, str) else v for k, v in obj.items()}
    out = d._masked({"ok": True, "text": "", "file": {"name": "4242.pdf", "mime": "x", "size": 1, "data": "AAAA"}}, validate([DL]))
    assert out["file"] == {"name": "•", "mime": "•", "size": 1, "data": "AAAA"}


def test_the_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    asked = []

    def request(payload, **kw):
        asked.append(payload["ops"])
        n = payload["ops"][0]["args"]["number"]
        if n == 3:
            return {"ok": False, "error": "too big"}
        return {"ok": True, "text": "", "file": {"name": f"f{n}.pdf", "mime": "application/pdf", "size": 2000,
                                                 "data": base64.b64encode(b"x").decode()}}
    monkeypatch.setattr(cli, "request", request)
    assert cli.gmail_main(["download", "m1", "1", "2", "2", "3"]) == 1
    out = capsys.readouterr()
    assert out.out.count("inbox/mail/") == 2 and "f2.pdf (application/pdf, 1 KB)" in out.out and "file 3: too big" in out.err
    assert [o[0]["args"]["number"] for o in asked] == [1, 2, 3]
    with pytest.raises(SystemExit):
        cli.gmail_main(["download", "m1", *map(str, range(1, 12))])
    assert cli.gmail_ops(cli.gmail_parser().parse_args(["files", "m1"])) == [{"op": "gmail.files", "args": {"id": "m1"}}]
