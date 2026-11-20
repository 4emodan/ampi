import asyncio
import io
import json
import os
import socket
import tempfile

import pytest

from assistant_bridge import permission_mcp
from assistant_bridge.claude_runner import DENIED_TOOLS, build_args
from assistant_bridge.config import ConfigError, Profile, _check_flags
from assistant_bridge.permissions import PermissionBroker, describe, visible
from assistant_bridge.services import ServiceUnavailable
from assistant_bridge.telegram import TelegramError

OWNER = 42


class FakeAPI:
    def __init__(self):
        self.buttons: list[dict] = []
        self.edits: list[tuple[int, str]] = []
        self.answers: list[str | None] = []
        self.fail_send = False

    async def send_buttons(self, chat_id, text, buttons):
        if self.fail_send:
            raise TelegramError("sendMessage", "blocked", 403)
        self.buttons.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return {"message_id": 100 + len(self.buttons)}

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((message_id, text))

    async def answer_callback_query(self, cq_id, text=None):
        self.answers.append(text)


class FakeBot:
    def __init__(self, tmp_path):
        self.api = FakeAPI()
        self.profile = Profile(
            name="personal", bot_token_env="TG", bot_token="t", owner_ids=frozenset({OWNER}),
            workspace=tmp_path, model=None, allowed_tools=(), extra_args=(), ask_permissions=True,
        )


def make(tmp_path, ask_fn=None, timeout=5, pkg="/run/ampi-pkg/pkg.sock"):
    bot = FakeBot(tmp_path)
    broker = PermissionBroker(bot, str(tmp_path / "p.sock"), pkg, ask_fn=ask_fn or _no_ask, timeout=timeout)
    return broker, bot


async def _no_ask(*a, **k):
    raise AssertionError("no service call expected")


def tap(bot, n=-1, answer="y", user=OWNER, **message_extra):
    sent = bot.api.buttons[n]
    data = sent["buttons"][0][0 if answer == "y" else 1]["callback_data"]
    message = {"message_id": 100 + (len(bot.api.buttons) if n == -1 else n + 1),
               "chat": {"id": OWNER, "type": "private"}, **message_extra}
    return {"id": "cq", "from": {"id": user}, "message": message, "data": data}


async def until(cond, steps=200):
    for _ in range(steps):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# --- what the owner sees ---------------------------------------------------------------


def test_visible_reveals_hidden_characters():
    assert visible("rm‮ -rf") == "rm\\u202e -rf"
    assert visible("a​b️") == "a\\u200bb\\ufe0f"
    assert visible("line1\nline2\tx") == "line1\nline2\tx"


def test_describe_bash_in_full_or_not_at_all():
    text = describe("Bash", {"command": "python3 scripts/x.py", "description": "convert"})
    assert "python3 scripts/x.py" in text and "(convert)" in text
    assert describe("Bash", {"command": "x" * 3001}) is None


def test_describe_other_tools():
    assert "the file /tmp/a" in describe("Write", {"file_path": "/tmp/a", "content": "hi"})
    assert describe("WebFetch", {"url": "https://example.com"}).startswith("Use WebFetch")


# --- approve / reject ------------------------------------------------------------------


def test_approve_allows(tmp_path):
    async def go():
        broker, bot = make(tmp_path)
        with broker.run("private chat", outside_content=False) as token:
            task = asyncio.create_task(broker.dispatch(
                {"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": "ls -la"}}))
            await until(lambda: bot.api.buttons)
            assert "ls -la" in bot.api.buttons[0]["text"]
            await broker.handle_callback(tap(bot))
            assert await task == {"allow": True}
        assert bot.api.edits[-1][1].endswith("✅ Allowed")
        assert bot.api.answers == ["Allowed"]
    asyncio.run(go())


def test_reject_denies(tmp_path):
    async def go():
        broker, bot = make(tmp_path)
        with broker.run("private chat", outside_content=False) as token:
            task = asyncio.create_task(broker.dispatch(
                {"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": "ls"}}))
            await until(lambda: bot.api.buttons)
            await broker.handle_callback(tap(bot, answer="n"))
            reply = await task
        assert reply["allow"] is False and "rejected" in reply["message"]
        assert bot.api.edits[-1][1].endswith("❌ Rejected")
    asyncio.run(go())


def test_forged_or_foreign_taps_do_nothing(tmp_path):
    async def go():
        broker, bot = make(tmp_path, timeout=0.3)
        with broker.run("private chat", outside_content=False) as token:
            task = asyncio.create_task(broker.dispatch(
                {"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": "ls"}}))
            await until(lambda: bot.api.buttons)
            stranger = tap(bot, user=7)
            wrong_nonce = tap(bot)
            wrong_nonce["data"] = wrong_nonce["data"].replace(wrong_nonce["data"].split(":")[2], "0" * 16)
            other_message = tap(bot)
            other_message["message"]["message_id"] = 5
            edited = tap(bot, edit_date=1)
            group = tap(bot)
            group["message"]["chat"] = {"id": -100, "type": "supergroup"}
            for cq in (stranger, wrong_nonce, other_message, edited, group):
                await broker.handle_callback(cq)
            reply = await task  # times out: none of those taps counted
        assert reply["allow"] is False and "No answer" in reply["message"]
    asyncio.run(go())


def test_unknown_run_is_refused(tmp_path):
    async def go():
        broker, bot = make(tmp_path)
        reply = await broker.dispatch({"cmd": "permission", "run": "nope", "tool": "Bash", "input": {}})
        assert reply["allow"] is False and not bot.api.buttons
    asyncio.run(go())


def test_ending_the_run_withdraws_its_requests(tmp_path):
    async def go():
        broker, bot = make(tmp_path)
        cm = broker.run("private chat", outside_content=False)
        token = cm.__enter__()
        task = asyncio.create_task(broker.dispatch(
            {"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": "ls"}}))
        await until(lambda: bot.api.buttons)
        cm.__exit__(None, None, None)
        reply = await task
        assert reply["allow"] is False
        assert "Withdrawn" in bot.api.edits[-1][1]
        await broker.handle_callback(tap(bot))  # a late tap changes nothing
        assert bot.api.answers[-1] == "This request is closed."
    asyncio.run(go())


def test_too_long_command_is_refused_without_asking(tmp_path):
    async def go():
        broker, bot = make(tmp_path)
        with broker.run("private chat", outside_content=False) as token:
            reply = await broker.dispatch(
                {"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": "x" * 5000}})
        assert reply["allow"] is False and "script file" in reply["message"] and not bot.api.buttons
    asyncio.run(go())


def test_group_runs_carry_a_warning_and_pending_is_capped(tmp_path):
    async def go():
        broker, bot = make(tmp_path, timeout=0.5)
        with broker.run('group "Clipboard"', outside_content=True) as token:
            tasks = [asyncio.create_task(broker.dispatch(
                {"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": f"echo {i}"}}))
                for i in range(5)]
            replies = await asyncio.gather(*tasks)
        assert len(bot.api.buttons) == 3
        assert 'group "Clipboard"' in bot.api.buttons[0]["text"] and "written by others" in bot.api.buttons[0]["text"]
        assert all(r["allow"] is False for r in replies)
    asyncio.run(go())


def test_telegram_failure_denies(tmp_path):
    async def go():
        broker, bot = make(tmp_path)
        bot.api.fail_send = True
        with broker.run("private chat", outside_content=False) as token:
            reply = await broker.dispatch({"cmd": "permission", "run": token, "tool": "Bash", "input": {"command": "ls"}})
        assert reply["allow"] is False
    asyncio.run(go())


# --- system packages --------------------------------------------------------------------


def test_packages_plan_ask_install(tmp_path):
    calls = []

    async def ask_fn(path, payload, timeout=0):
        calls.append(payload)
        if payload["cmd"] == "plan":
            return {"ok": True, "new": ["librsvg2-bin", "librsvg2-2"]}
        return {"ok": True, "text": "done"}

    async def go():
        broker, bot = make(tmp_path, ask_fn=ask_fn)
        with broker.run("private chat", outside_content=False) as token:
            task = asyncio.create_task(broker.dispatch(
                {"cmd": "packages", "run": token, "packages": ["librsvg2-bin"], "reason": "convert SVG"}))
            await until(lambda: bot.api.buttons)
            text = bot.api.buttons[0]["text"]
            assert "librsvg2-bin" in text and "1 other package(s): librsvg2-2" in text and "convert SVG" in text
            await broker.handle_callback(tap(bot))
            reply = await task
        assert reply["ok"] and [c["cmd"] for c in calls] == ["plan", "install"]
        assert bot.api.edits[-1][1].endswith("✅ Installed")
    asyncio.run(go())


def test_packages_rejected_installs_nothing(tmp_path):
    calls = []

    async def ask_fn(path, payload, timeout=0):
        calls.append(payload["cmd"])
        return {"ok": True, "new": ["ffmpeg"]}

    async def go():
        broker, bot = make(tmp_path, ask_fn=ask_fn)
        with broker.run("private chat", outside_content=False) as token:
            task = asyncio.create_task(broker.dispatch(
                {"cmd": "packages", "run": token, "packages": ["ffmpeg"], "reason": "video"}))
            await until(lambda: bot.api.buttons)
            await broker.handle_callback(tap(bot, answer="n"))
            reply = await task
        assert not reply["ok"] and calls == ["plan"]
    asyncio.run(go())


@pytest.mark.parametrize("packages", [[], ["Bad"], ["-o=foo"], ["a;b"], ["x"] * 11, "ffmpeg"])
def test_bad_package_names_never_reach_the_helper(tmp_path, packages):
    async def go():
        broker, bot = make(tmp_path)
        with broker.run("private chat", outside_content=False) as token:
            reply = await broker.dispatch({"cmd": "packages", "run": token, "packages": packages, "reason": ""})
        assert not reply["ok"] and not bot.api.buttons
    asyncio.run(go())


def test_packages_already_installed_or_helper_down(tmp_path):
    async def installed(path, payload, timeout=0):
        return {"ok": True, "new": []}

    async def down(path, payload, timeout=0):
        raise ServiceUnavailable("nope")

    async def go():
        for fn, expect in ((installed, "Already installed"), (down, "isn't reachable")):
            broker, bot = make(tmp_path, ask_fn=fn)
            with broker.run("private chat", outside_content=False) as token:
                reply = await broker.dispatch({"cmd": "packages", "run": token, "packages": ["jq"], "reason": ""})
            assert expect in reply["text"] and not bot.api.buttons
    asyncio.run(go())


# --- socket round trip and the MCP server -----------------------------------------------


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
def test_socket_round_trip_and_disconnect_withdraws(tmp_path):
    async def go():
        short = tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)
        bot = FakeBot(tmp_path)
        broker = PermissionBroker(bot, os.path.join(short, "p.sock"), None, timeout=5)
        await broker.start()
        with broker.run("private chat", outside_content=False) as token:
            reader, writer = await asyncio.open_unix_connection(broker.path)
            writer.write(json.dumps({"cmd": "permission", "run": token, "tool": "Bash",
                                     "input": {"command": "ls"}}).encode() + b"\n")
            await writer.drain()
            await until(lambda: bot.api.buttons)
            await broker.handle_callback(tap(bot))
            assert json.loads(await reader.readline()) == {"allow": True}
            writer.close()
            # A client that disappears (the run was cancelled) withdraws its request.
            reader, writer = await asyncio.open_unix_connection(broker.path)
            writer.write(json.dumps({"cmd": "permission", "run": token, "tool": "Bash",
                                     "input": {"command": "pwd"}}).encode() + b"\n")
            await writer.drain()
            await until(lambda: len(bot.api.buttons) == 2)
            writer.close()
            await until(lambda: any("Withdrawn" in t for _, t in bot.api.edits))
        await broker.close()
    asyncio.run(go())


def test_mcp_server_protocol(monkeypatch):
    out = io.StringIO()
    server = permission_mcp.Server(out)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    server.handle({"jsonrpc": "2.0", "id": 3, "method": "bogus"})
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"approve", "install_system_packages"}
    assert replies[2]["error"]["code"] == -32601


def test_mcp_approve_maps_the_bridge_answer(monkeypatch):
    answers = iter([{"allow": True}, {"allow": False, "message": "no"}, {"error": "down"}])
    seen = []

    def fake(payload):
        seen.append(payload)
        return next(answers)

    monkeypatch.setattr(permission_mcp, "ask_bridge", fake)
    args = {"tool_name": "Bash", "input": {"command": "ls"}}
    allow, _ = permission_mcp.call_tool("approve", args)
    assert json.loads(allow) == {"behavior": "allow", "updatedInput": {"command": "ls"}}
    deny, _ = permission_mcp.call_tool("approve", args)
    assert json.loads(deny) == {"behavior": "deny", "message": "no"}
    down, _ = permission_mcp.call_tool("approve", args)
    assert json.loads(down)["behavior"] == "deny"
    assert seen[0] == {"cmd": "permission", "tool": "Bash", "input": {"command": "ls"}}


def test_mcp_bridge_unreachable_denies(monkeypatch):
    monkeypatch.setenv("AMPI_PERMISSION_SOCKET", "/nonexistent/p.sock")
    text, _ = permission_mcp.call_tool("approve", {"tool_name": "Bash", "input": {"command": "ls"}})
    assert json.loads(text)["behavior"] == "deny"


# --- wiring ---------------------------------------------------------------------------


def test_setup_args_and_flags(tmp_path):
    broker, _ = make(tmp_path)
    args, env = broker.mcp_setup("tok", "/venv/bin/python")
    config = json.loads(args[args.index("--mcp-config") + 1])
    server = config["mcpServers"]["ampi"]
    assert server["args"] == ["-I", "-m", "assistant_bridge.permission_mcp"]
    assert server["env"]["AMPI_RUN"] == "tok"
    assert args[args.index("--permission-prompt-tool") + 1] == "mcp__ampi__approve"
    assert int(env["MCP_TOOL_TIMEOUT"]) > 5000
    assert "mcp__ampi__approve" in DENIED_TOOLS
    built = build_args(["claude"], session_id="s", resume=False, model=None, allowed_tools=["Read"],
                       append_system_prompt=None, extra_args=args)
    assert "--strict-mcp-config" in built and "--mcp-config" in built
    for flag in ("--permission-prompt-tool", "--permission-prompts"):
        with pytest.raises(ConfigError):
            _check_flags((flag,), "extra_args")
