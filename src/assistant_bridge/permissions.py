"""Lets Claude ask the owner, mid-task, for permission to do something its rules don't allow.

Each owner conversation turn gets a run token and the `ampi` MCP server (permission_mcp.py). When Claude
Code wants to use a tool that isn't allowed (run a script, `uv pip install`, ...), it calls that server,
which asks this broker over a Unix socket; the broker shows the owner the exact request with Approve /
Reject buttons and answers when they tap one (or denies after PERMISSION_TIMEOUT).

Deny rules still win: what DENIED_TOOLS and the settings file block is never even asked about.
Watcher runs get no permission tool, so outside content (emails...) can't make the owner a target for
approval spam, and anything not allowed there is simply denied.

Ubuntu packages: `install_system_packages` goes through the same buttons; once approved, the bridge asks
the root helper (ampi-pkg), which only accepts requests from this process (it checks the PID).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import chatlog
from .services import ServiceError, ServiceUnavailable, ask
from .telegram import TelegramError

if TYPE_CHECKING:
    from .bot import ProfileBot

log = logging.getLogger(__name__)
Ask = Callable[..., Awaitable[dict]]

PERMISSION_TIMEOUT = 600  # seconds the owner has to answer; then the request is denied
MAX_PENDING_PER_RUN = 3
MAX_SHOWN = 3000  # characters of a request shown; longer commands are refused instead of cut
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,62}$")
MAX_PACKAGES = 10
INSTALL_TIMEOUT = 1200


def visible(text: str) -> str:
    """Shows invisible and direction-changing characters as \\uXXXX, so a request can't hide what it does."""
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn", "Zl", "Zp") or 0xFE00 <= ord(ch) <= 0xFE0F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def describe(tool: str, tool_input: dict) -> str | None:
    """What the owner is asked to approve, in full; None if it is too long to show."""
    tool = visible(tool)
    if tool == "Bash":
        command = visible(str(tool_input.get("command", "")))
        why = visible(str(tool_input.get("description", "")))
        text = f"Run a command:\n\n{command}"
        if why:
            text += f"\n\n({why})"
        return text if len(command) <= MAX_SHOWN else None
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = visible(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""))
        body = json.dumps({k: v for k, v in tool_input.items() if k not in ("file_path", "notebook_path")},
                          ensure_ascii=False, indent=1)
        body = visible(body)
        return f"{tool} the file {path}:\n\n{body}" if len(body) <= MAX_SHOWN else None
    body = visible(json.dumps(tool_input, ensure_ascii=False, indent=1))
    return f"Use {tool}:\n\n{body}" if len(body) <= MAX_SHOWN else None


@dataclass
class Run:
    label: str  # where the request comes from, e.g. "private chat" or 'group "Clipboard"'
    outside_content: bool  # the run handles text written by others (a group)
    pending: set[int] = field(default_factory=set)


@dataclass
class Pending:
    id: int
    nonce: str
    text: str
    future: asyncio.Future
    message_id: int | None = None


class PermissionBroker:
    def __init__(self, bot: "ProfileBot", path: str, pkg_socket: str | None, ask_fn: Ask = ask,
                 timeout: float = PERMISSION_TIMEOUT):
        self.bot = bot
        self.path = path
        self.pkg_socket = pkg_socket
        self.ask = ask_fn
        self.timeout = timeout
        self.runs: dict[str, Run] = {}
        self.pending: dict[int, Pending] = {}
        self._next = 1
        self._server: asyncio.AbstractServer | None = None

    # --- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        old = os.umask(0o077)  # only the assistant user (the claude runs) can connect
        try:
            self._server = await asyncio.start_unix_server(self._handle, self.path, limit=1024 * 1024)
        finally:
            os.umask(old)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

    @contextlib.contextmanager
    def run(self, label: str, outside_content: bool):
        """Registers one Claude run; yields its token. Requests still open when it ends are withdrawn."""
        token = secrets.token_hex(16)
        self.runs[token] = Run(label, outside_content)
        try:
            yield token
        finally:
            run = self.runs.pop(token, None)
            for pid in list(run.pending if run else ()):
                p = self.pending.get(pid)
                if p and not p.future.done():
                    p.future.set_result(None)

    def mcp_setup(self, token: str, python: str) -> tuple[list[str], dict[str, str]]:
        """claude arguments and environment that give a run the permission tool."""
        env = {"AMPI_PERMISSION_SOCKET": self.path, "AMPI_RUN": token}
        config = {"mcpServers": {"ampi": {
            "command": python, "args": ["-I", "-m", "assistant_bridge.permission_mcp"], "env": env,
        }}}
        args = [
            "--mcp-config", json.dumps(config),
            "--permission-prompt-tool", "mcp__ampi__approve",
        ]
        # Claude waits for the owner's answer inside the MCP call; don't let it give up first.
        return args, {"MCP_TOOL_TIMEOUT": str(int((self.timeout + INSTALL_TIMEOUT + 120) * 1000))}

    # --- requests from the runs ------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), 10)
                req = json.loads(line)
            except (asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError):
                return
            if not isinstance(req, dict):
                return
            # If the run goes away (/cancel, timeout), its MCP server dies and the connection closes:
            # withdraw the request then.
            gone = asyncio.create_task(reader.read())
            work = asyncio.create_task(self.dispatch(req))
            done, _ = await asyncio.wait({gone, work}, return_when=asyncio.FIRST_COMPLETED)
            if work not in done:
                work.cancel()
                return
            gone.cancel()
            writer.write(json.dumps(work.result(), ensure_ascii=True).encode() + b"\n")
            await writer.drain()
        except Exception:
            log.exception("permission request failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def dispatch(self, req: dict) -> dict:
        token = req.get("run")
        run = self.runs.get(token) if isinstance(token, str) else None
        if run is None:
            return {"allow": False, "error": "This request doesn't come from a running task; ignored."}
        if req.get("cmd") == "permission":
            tool_input = req.get("input") if isinstance(req.get("input"), dict) else {}
            return await self.permission(token, run, str(req.get("tool", "")), tool_input)
        if req.get("cmd") == "packages":
            return await self.packages(token, run, req.get("packages"), str(req.get("reason", "")))
        return {"allow": False, "error": "unknown request"}

    async def permission(self, token: str, run: Run, tool: str, tool_input: dict) -> dict:
        what = describe(tool, tool_input)
        if what is None:
            return {"allow": False, "message": "Too long to show the owner. Put the code in a script file in "
                                               "your workspace and run that instead."}
        answer, pid = await self._ask_owner(run, what)
        if answer is True:
            await self._finish(pid, "✅ Allowed")
            return {"allow": True}
        message = {False: "The owner rejected this. Don't retry it; ask them in your reply if you need it.",
                   None: "No answer from the owner (they didn't reply in time, or the task ended). "
                         "Don't retry; say in your reply what you need."}[answer]
        return {"allow": False, "message": message}

    async def packages(self, token: str, run: Run, packages: object, reason: str) -> dict:
        if (not isinstance(packages, list) or not 1 <= len(packages) <= MAX_PACKAGES
                or not all(isinstance(p, str) and PACKAGE_RE.match(p) for p in packages)):
            return {"ok": False, "text": f"Give 1-{MAX_PACKAGES} Ubuntu package names (lowercase, e.g. librsvg2-bin)."}
        if not self.pkg_socket:
            return {"ok": False, "text": "System package installs aren't set up on this server; ask the owner."}
        try:
            plan = await self.ask(self.pkg_socket, {"cmd": "plan", "packages": packages}, timeout=180)
        except ServiceError as e:
            return {"ok": False, "text": f"The package installer isn't reachable ({e}); ask the owner to check ampi-pkg."}
        if not plan.get("ok"):
            return {"ok": False, "text": f"apt can't install that: {plan.get('error', 'failed')}"}
        new = [p for p in plan.get("new", []) if isinstance(p, str)]
        if not new:
            return {"ok": True, "text": "Already installed; nothing to do."}
        extra = [p for p in new if p not in packages]
        what = "Install Ubuntu packages (apt):\n\n" + " ".join(packages)
        if extra:
            shown = " ".join(extra[:40]) + (f" … (+{len(extra) - 40})" if len(extra) > 40 else "")
            what += f"\n\nThis also adds {len(extra)} other package(s): {shown}"
        what += f"\n\nWhy: {visible(reason)[:500] or '(no reason given)'}"
        answer, pid = await self._ask_owner(run, what)
        if answer is not True:
            return {"ok": False, "text": "The owner didn't approve the install." if answer is False
                    else "No answer from the owner; nothing was installed."}
        await self._finish(pid, "⏳ Installing…", keep=True)
        try:
            result = await self.ask(self.pkg_socket, {"cmd": "install", "packages": packages}, timeout=INSTALL_TIMEOUT)
        except ServiceUnavailable as e:
            result = {"ok": False, "error": f"installer not reachable ({e})"}
        except ServiceError as e:
            result = {"ok": False, "error": f"no answer from the installer ({e}); check with `dpkg -l` whether it worked"}
        if result.get("ok"):
            await self._finish(pid, "✅ Installed")
            return {"ok": True, "text": "Installed: " + " ".join(packages)}
        await self._finish(pid, f"⚠️ Install failed: {str(result.get('error', ''))[:600]}")
        return {"ok": False, "text": f"Install failed: {result.get('error', 'failed')}"}

    # --- asking the owner ----------------------------------------------------------------

    async def _ask_owner(self, run: Run, what: str) -> tuple[bool | None, int]:
        """(True = approved, False = rejected, None = no answer / withdrawn / couldn't ask; request id)."""
        pid, self._next = self._next, self._next + 1
        if len(run.pending) >= MAX_PENDING_PER_RUN:
            return None, pid
        text = f"🔐 Ampi asks permission (#{pid}, from the {run.label})\n\n{what}"
        if run.outside_content:
            text += ("\n\n⚠️ This task handles messages written by others; make sure this is something "
                     "you actually want.")
        nonce = secrets.token_hex(8)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        p = Pending(pid, nonce, text, future)
        buttons = [[
            {"text": "✅ Approve", "callback_data": f"pm:{pid}:{nonce}:y"},
            {"text": "❌ Reject", "callback_data": f"pm:{pid}:{nonce}:n"},
        ]]
        try:
            sent = await self.bot.api.send_buttons(self.bot.profile.owner_chat_id, text, buttons)
        except TelegramError as e:
            log.warning("could not show permission request #%s: %s", pid, e)
            return None, pid
        p.message_id = sent.get("message_id")
        self.pending[pid] = p
        run.pending.add(pid)
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.TimeoutError:
            answer = None
            await self._finish(pid, f"⌛ No answer in {int(self.timeout // 60)} min; not allowed.")
        except asyncio.CancelledError:
            await self._finish(pid, "⏹ Withdrawn (the task ended).")
            raise
        finally:
            run.pending.discard(pid)
        if answer is None and pid in self.pending:
            await self._finish(pid, "⏹ Withdrawn (the task ended).")
        elif answer is False:
            await self._finish(pid, "❌ Rejected")
        return answer, pid

    async def _finish(self, pid: int, outcome: str, keep: bool = False) -> None:
        """Writes the outcome under the request (and removes the buttons)."""
        p = self.pending.get(pid) if keep else self.pending.pop(pid, None)
        if p is None or p.message_id is None:
            return
        text = f"{p.text}\n\n{outcome}"
        if not keep:
            log_dir = chatlog.conversation_dir(self.bot.profile.workspace, self.bot.profile.owner_chat_id, 0, None, None)
            chatlog.append(log_dir, "Approvals", text)
        try:
            await self.bot.api.edit_message_text(self.bot.profile.owner_chat_id, p.message_id, text)
        except TelegramError as e:
            log.info("could not update permission request #%s: %s", pid, e)

    async def handle_callback(self, cq: dict) -> None:
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        reply = None
        try:
            if user not in self.bot.profile.owner_ids:
                log.warning("permission button pressed by a stranger (id=%s); ignored", user)
                return
            if chat.get("type") != "private" or chat.get("id") != self.bot.profile.owner_chat_id:
                return
            if message.get("edit_date"):
                reply = "This request is closed."
                return
            try:
                _, raw_id, nonce, answer = (cq.get("data") or "").split(":")
                pid = int(raw_id)
            except ValueError:
                return
            p = self.pending.get(pid)
            if (p is None or p.future.done() or p.message_id != message.get("message_id")
                    or not secrets.compare_digest(p.nonce, nonce)):
                reply = "This request is closed."
                return
            p.future.set_result(answer == "y")
            reply = "Allowed" if answer == "y" else "Rejected"
        finally:
            with contextlib.suppress(TelegramError, KeyError):
                await self.bot.api.answer_callback_query(cq["id"], reply)
