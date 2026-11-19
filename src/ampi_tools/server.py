"""The `ampi-tools` daemon (runs as user `ampitools`, see deploy/ampi-tools.service).

    ampi-tools serve         run by systemd
    ampi-tools gmail-login   once, interactively: connect your Gmail account
    ampi-tools check         show what is configured and working

Environment (/home/ampitools/env): TODOIST_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET.
Policy (/home/ampitools/config.toml, optional):
    auto_approve = ["todoist.move"]          # critical ops to allow without asking
    require_approval = ["gmail.archive"]     # safe ops to ask about anyway
    trusted_unit = "claude-assistant.service"
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import socket
import struct
import sys
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .gmail import Gmail, GmailAuth
from .ops import MAX_PENDING, OPS, Approvals, Policy, Services, clean, execute, prepare, summarize, validate
from .todoist import Todoist, ToolError

DEFAULT_SOCKET = "/run/ampi-tools/tools.sock"
MAX_REQUEST = 1024 * 1024
ADMIN = {"approvals.pending", "approvals.notified", "approvals.decide"}
log = logging.getLogger("ampi-tools")

PidCheck = Callable[[int], Awaitable[bool]]


def _int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ToolError("bad id")
    return v


def _opt_int(v: Any) -> int | None:
    return None if v is None else _int(v)


def socket_path() -> str:
    return os.environ.get("AMPI_TOOLS_SOCKET") or DEFAULT_SOCKET


def load_policy(path: Path) -> tuple[Policy, str]:
    raw: dict = {}
    if path.is_file():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in ("auto_approve", "require_approval"):
        unknown = set(raw.get(key, [])) - set(OPS)
        if unknown:
            sys.exit(f"{path}: unknown operations in {key}: {sorted(unknown)}")
    policy = Policy(frozenset(raw.get("auto_approve", [])), frozenset(raw.get("require_approval", [])))
    return policy, raw.get("trusted_unit", "claude-assistant.service")


def systemd_main_pid_check(unit: str) -> PidCheck:
    """Only the bridge's main process (not the claude runs it starts) may answer approvals."""

    async def check(pid: int) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "show", "-p", "MainPID", "--value", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        try:
            return int(out.strip() or 0) == pid and pid > 0
        except ValueError:
            return False

    return check


def peer_pid(writer: asyncio.StreamWriter) -> int:
    sock = writer.get_extra_info("socket")
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[0]
    except (AttributeError, OSError):
        return -1


class Daemon:
    def __init__(self, services: Services, approvals: Approvals, policy: Policy, trusted: PidCheck):
        self.services = services
        self.approvals = approvals
        self.policy = policy
        self.trusted = trusted

    async def handle(self, request: dict, pid: int) -> dict:
        try:
            cmd = request.get("cmd")
            if cmd in ADMIN and not await self.trusted(pid):
                raise ToolError("only the owner can answer approval requests (Telegram buttons)")
            if cmd == "run":
                return await self.run(validate(request.get("ops")), request.get("reason"))
            if cmd == "approvals.list":
                return {"ok": True, "text": self.list_text()}
            if cmd == "approvals.pending":
                return {"ok": True, "items": self.approvals.unnotified()}
            if cmd == "approvals.notified":
                self.approvals.mark_notified(_int(request.get("id")), _opt_int(request.get("message_id")))
                return {"ok": True}
            if cmd == "approvals.decide":
                return await self.decide(
                    _int(request.get("id")), request.get("approve") is True,
                    str(request.get("nonce") or ""), _opt_int(request.get("message_id")),
                )
            raise ToolError(f"unknown command {cmd!r}")
        except ToolError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # never let one request take the daemon down
            log.exception("request failed")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def run(self, ops: list[dict], reason: Any) -> dict:
        if any(self.policy.critical(o["op"]) for o in ops):
            if self.approvals.pending_count() >= MAX_PENDING:
                raise ToolError(f"{MAX_PENDING} requests are already waiting for the owner; wait for their answer")
            ops = await prepare(self.services, ops)
            summary = await summarize(self.services, ops, self.policy)
            reason = clean(reason, 300) if isinstance(reason, str) and reason.strip() else None
            approval_id = self.approvals.add(ops, summary, reason)
            log.info("approval #%s requested: %s", approval_id, [o["op"] for o in ops])
            return {
                "ok": True,
                "approval": approval_id,
                "text": (
                    f"⏳ Not done yet: this needs the owner's approval (request #{approval_id}). They get "
                    "Approve/Reject buttons in Telegram and the result is reported to them there. Tell the "
                    "owner briefly what you asked for; don't ask again or try another way.\n" + summary
                ),
            }
        ok, text, extra = await execute(self.services, ops)
        return {"ok": ok, "text": text, **extra} if ok else {"ok": False, "error": text}

    async def decide(self, approval_id: int, approve: bool, nonce: str, message_id: int | None) -> dict:
        current = self.approvals.get(approval_id)
        # The nonce is only ever given to the bridge (approvals.pending) and the button it sent; the
        # message id is the one the bridge recorded for that button. A forged button fails both.
        if current is None or not hmac.compare_digest(nonce, current.get("nonce") or ""):
            raise ToolError("unknown or forged approval button")
        if current.get("message_id") is not None and message_id != current["message_id"]:
            raise ToolError("this button isn't the approval message that was sent for this request")
        item = self.approvals.claim(approval_id, "approved" if approve else "rejected")
        if item is None:
            state = (self.approvals.get(approval_id) or {}).get("status", "unknown")
            return {"ok": True, "status": state, "text": f"Request #{approval_id} is already {state}."}
        if not approve:
            log.info("approval #%s rejected", approval_id)
            return {"ok": True, "status": "rejected", "text": "Rejected; nothing was done."}
        ok, text, status = False, "interrupted", "failed"
        try:
            ok, text, _ = await execute(self.services, json.loads(item["ops"]))
            status = "done" if ok else "failed"
        finally:
            self.approvals.finish(approval_id, status, text)
            log.info("approval #%s %s", approval_id, status)
        return {"ok": True, "status": status, "text": text}

    def list_text(self) -> str:
        rows = self.approvals.recent(10)
        if not rows:
            return "No approval requests yet."
        return "\n".join(
            f"#{r['id']} {r['status']}: {r['summary'].splitlines()[0][:150]}"
            + (f"\n   result: {r['result'][:200]}" if r.get("result") else "")
            for r in rows
        )

    async def connection(self, rd: asyncio.StreamReader, wr: asyncio.StreamWriter) -> None:
        try:
            try:
                request = json.loads(await asyncio.wait_for(rd.readline(), 10))
                if not isinstance(request, dict):
                    raise ValueError
            except (ValueError, asyncio.TimeoutError):
                response = {"ok": False, "error": "bad request"}
            else:
                response = await self.handle(request, peer_pid(wr))
            wr.write(json.dumps(response).encode() + b"\n")
            await wr.drain()
        except Exception:
            log.exception("connection failed")
        finally:
            wr.close()


async def serve_socket(daemon: Daemon, path: str) -> asyncio.AbstractServer:
    p = Path(path)
    if p.exists() or p.is_symlink():
        p.unlink()
    server = await asyncio.start_unix_server(daemon.connection, path=path, limit=MAX_REQUEST)
    os.chmod(path, 0o660)
    return server


def build_services(client: httpx.AsyncClient, home: Path) -> Services:
    services = Services()
    token = os.environ.get("TODOIST_TOKEN", "").strip()
    if token:
        services.todoist_client = Todoist(token, client)
    cid, secret = os.environ.get("GMAIL_CLIENT_ID", "").strip(), os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if cid and secret:
        services.gmail_client = Gmail(GmailAuth(cid, secret, home / "gmail-token.json", client), client)
    return services


async def _serve(home: Path) -> None:
    policy, unit = load_policy(home / "config.toml")
    async with httpx.AsyncClient() as client:
        services = build_services(client, home)
        log.info("todoist: %s, gmail: %s", "on" if services.todoist_client else "off",
                 "on" if services.gmail_client and services.gmail_client.auth.configured() else "off")
        daemon = Daemon(services, Approvals(home / "approvals.sqlite3"), policy, systemd_main_pid_check(unit))
        server = await serve_socket(daemon, socket_path())
        log.info("listening on %s", socket_path())
        async with server:
            await server.serve_forever()


async def _gmail_login(home: Path) -> None:
    cid, secret = os.environ.get("GMAIL_CLIENT_ID", "").strip(), os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in ~/env first (see docs/SETUP.md).")
    async with httpx.AsyncClient() as client:
        auth = GmailAuth(cid, secret, home / "gmail-token.json", client)
        url, state, verifier = auth.login_url()
        print("1. Open this link in a browser where you're signed in to Gmail, and allow access:\n")
        print(url)
        print("\n2. The browser then goes to a localhost page that doesn't load. That's expected.")
        print("   Copy the whole address from the address bar and paste it here.\n")
        redirected = input("Address: ")
        try:
            await auth.finish_login(redirected, state, verifier)
        except ToolError as e:
            sys.exit(f"Failed: {e}")
        gmail = Gmail(auth, client)
        labels = await gmail.labels()
        print(f"Connected ({len(labels)} labels). Now: systemctl restart ampi-tools")


async def _check(home: Path) -> None:
    async with httpx.AsyncClient() as client:
        services = build_services(client, home)
        for name, op in (("Todoist", "todoist.projects"), ("Gmail", "gmail.labels")):
            ok, text, _ = await execute(services, [{"op": op, "args": {}}])
            print(f"{name}: {'OK' if ok else text}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ampi-tools", description="Todoist and Gmail for the assistant")
    parser.add_argument("command", choices=["serve", "gmail-login", "check"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    home = Path(os.environ.get("AMPI_TOOLS_HOME") or Path.home())
    runner = {"serve": _serve, "gmail-login": _gmail_login, "check": _check}[args.command]
    asyncio.run(runner(home))


if __name__ == "__main__":
    main()
