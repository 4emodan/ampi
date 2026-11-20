"""The `ampi-tools` daemon (runs as user `ampitools`, see deploy/ampi-tools.service).

    ampi-tools serve         run by systemd
    ampi-tools gmail-login   once, interactively: connect your Gmail account
    ampi-tools youtube-login [--write]   connect your YouTube account (read-only unless --write)
    ampi-tools check         show what is configured and working

Environment (/home/ampitools/env): TODOIST_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET (also used for
YouTube; GOOGLE_CLIENT_ID/SECRET work too), YOUTUBE_API_KEY.
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

from .gmail import Gmail, GmailAuth, GoogleAuth
from .ops import (
    MAX_PENDING, OPS, Approvals, Policy, Services, clean, execute, pre_resolve, prepare, shares_with_others, summarize,
    validate,
)
from .todoist import Todoist, ToolError
from .youtube import SCOPE_READ, SCOPE_WRITE, YouTube

DEFAULT_SOCKET = "/run/ampi-tools/tools.sock"
MAX_REQUEST = 1024 * 1024
ADMIN = {"approvals.pending", "approvals.notified", "approvals.decide"}
WATCHER_ONLY = {"youtube.ids", "youtube.show"}
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
                ops = validate(request.get("ops"))
                # The watchers' YouTube ops use their own reserve of quota and requests; only the bridge has it.
                if any(o["op"] in WATCHER_ONLY for o in ops) and not await self.trusted(pid):
                    raise ToolError("youtube.ids/youtube.show are for watchers; use `youtube feed` / `youtube video`")
                return await self.run(ops, request.get("reason"))
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
        ops = await pre_resolve(self.services, ops)
        critical = False
        for o in ops:
            if self.policy.critical(o["op"]) or await shares_with_others(self.services, o):
                critical = True
                break
        if critical:
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


def google_client() -> tuple[str, str]:
    cid = (os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GMAIL_CLIENT_ID") or "").strip()
    secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or os.environ.get("GMAIL_CLIENT_SECRET") or "").strip()
    return cid, secret


def youtube_auth(client: httpx.AsyncClient, home: Path, scope: str = SCOPE_READ) -> GoogleAuth | None:
    cid, secret = google_client()
    if not (cid and secret):
        return None
    return GoogleAuth(cid, secret, home / "youtube-token.json", client, scope=scope, service="YouTube",
                      login_command="youtube-login")


def build_services(client: httpx.AsyncClient, home: Path) -> Services:
    services = Services()
    token = os.environ.get("TODOIST_TOKEN", "").strip()
    if token:
        services.todoist_client = Todoist(token, client)
    cid, secret = os.environ.get("GMAIL_CLIENT_ID", "").strip(), os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if cid and secret:
        services.gmail_client = Gmail(GmailAuth(cid, secret, home / "gmail-token.json", client), client)
    # RSS feeds and oEmbed work without any credentials, so YouTube is always there.
    services.youtube_client = YouTube(client, youtube_auth(client, home),
                                      os.environ.get("YOUTUBE_API_KEY", "").strip() or None, home=home)
    return services


async def _serve(home: Path) -> None:
    policy, unit = load_policy(home / "config.toml")
    async with httpx.AsyncClient() as client:
        services = build_services(client, home)
        log.info("todoist: %s, gmail: %s, youtube: %s", "on" if services.todoist_client else "off",
                 "on" if services.gmail_client and services.gmail_client.auth.configured() else "off",
                 services.youtube.status())
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


async def _youtube_login(home: Path, write: bool) -> None:
    async with httpx.AsyncClient() as client:
        auth = youtube_auth(client, home, SCOPE_WRITE if write else SCOPE_READ)
        if auth is None:
            sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in ~/env first (see docs/SETUP.md).")
        before = None
        if auth.configured():
            old = YouTube(client, auth)
            try:
                before = await _my_channel(old)
            except ToolError:
                pass
            print(f"This replaces the current YouTube login ({before or 'unknown channel'}; "
                  f"{'read and write' if old.can_write else 'read-only'}).\n")
        url, state, verifier = auth.login_url()
        print("1. Open this link in a browser where you're signed in to YouTube, choose the channel, and allow "
              + ("access (read and change playlists and subscriptions)" if write else "read-only access") + ":\n")
        print(url)
        print("\n2. The browser then goes to a localhost page that doesn't load. That's expected.")
        print("   Copy the whole address from the address bar and paste it here.\n")
        redirected = input("Address: ")
        try:
            await auth.finish_login(redirected, state, verifier)
            yt = YouTube(client, auth)
            channel = await _my_channel(yt)
        except ToolError as e:
            sys.exit(f"Failed: {e}")
        if write and not yt.can_write:
            print("Note: Google granted read-only access (a permission was unticked); changes won't work.")
        if before and before != channel:
            print(f"Note: this is a DIFFERENT channel than before ({before}).")
        print(f"Connected to {channel} ({'read and write' if yt.can_write else 'read-only'}). "
              "Now: systemctl restart ampi-tools")


async def _my_channel(yt: YouTube) -> str:
    data = await yt.api("GET", "/channels", {"part": "snippet", "mine": "true"}, account=True) or {}
    items = data.get("items", [])
    if not items:
        return "an account without a YouTube channel"
    return f"“{items[0].get('snippet', {}).get('title', '?')}” [{items[0].get('id')}]"


async def _check(home: Path) -> None:
    async with httpx.AsyncClient() as client:
        services = build_services(client, home)
        for name, op in (("Todoist", "todoist.projects"), ("Gmail", "gmail.labels")):
            ok, text, _ = await execute(services, [{"op": op, "args": {}}])
            print(f"{name}: {'OK' if ok else text}")
        yt = services.youtube
        quota = yt.quota.summary()
        # Its own counters: the running daemon owns youtube-quota.json, and two writers would lose counts.
        yt = YouTube(client, yt.auth, yt.api_key)
        try:
            title, _ = await yt.feed(channel_id="UCBR8-60-B28hp2BmDPdntcQ")  # YouTube's own channel
            feeds = f"RSS OK ({title})"
        except ToolError as e:
            feeds = f"RSS failed: {e}"
        api = ""
        if yt.has_api:
            try:
                await yt.channels(["UCBR8-60-B28hp2BmDPdntcQ"])
                api = ", API OK"
            except ToolError as e:
                api = f", API failed: {e}"
        print(f"YouTube: {yt.status()}; {feeds}{api}; {quota}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ampi-tools", description="Todoist, Gmail and YouTube for the assistant")
    parser.add_argument("command", choices=["serve", "gmail-login", "youtube-login", "check"])
    parser.add_argument("--write", action="store_true", help="youtube-login: also allow changes (playlists, "
                        "subscriptions), each critical one still approved by you in Telegram")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    home = Path(os.environ.get("AMPI_TOOLS_HOME") or Path.home())
    if args.command == "youtube-login":
        asyncio.run(_youtube_login(home, args.write))
        return
    runner = {"serve": _serve, "gmail-login": _gmail_login, "check": _check}[args.command]
    asyncio.run(runner(home))


if __name__ == "__main__":
    main()
