"""The `ampi-tools` daemon (runs as user `ampitools`, see deploy/ampi-tools.service).

    ampi-tools serve         run by systemd
    ampi-tools gmail-login   once, interactively: connect your Gmail account
    ampi-tools youtube-login [--write]   connect your YouTube account (read-only unless --write)
    ampi-tools check         show what is configured and working

Environment (/home/ampitools/env): TODOIST_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET (also used for
YouTube; GOOGLE_CLIENT_ID/SECRET work too), YOUTUBE_API_KEY, MAPS_API_KEY (+ MAPS_LANGUAGE, MAPS_REGION,
MAPS_NEAR "lat,lng", MAPS_TIMEZONE).
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
from . import ops_social
from .maps import Maps, latlng
from .ops_wolt import BASKET_OPS as WOLT_BASKET_OPS
from .wolt import Wolt, WoltAuth
from .meta import INSTAGRAM, THREADS, MetaAuth, MetaClient, MetaService, RedactingFormatter, redact, register_secret
from .ops_telegram import (
    TG_PENDING_CAP, WRITE_OPS, RunRegistry, TelegramRules, TelegramSide, TelegramStore, TgWriterClient,
    check_approval_size, grant_offer, needs_approval, note_requested,
)
from .youtube import SCOPE_READ, SCOPE_WRITE, YouTube

DEFAULT_SOCKET = "/run/ampi-tools/tools.sock"
MAX_REQUEST = 1024 * 1024
# Only the bridge's main process may: answer approvals, post notices, say which claude runs exist and where
# they come from, and say which chats the assistant reads itself.
ADMIN = {"approvals.pending", "approvals.notified", "approvals.decide", "notices.ack", "runs.register",
         "runs.unregister", "telegram.protect"}
WATCHER_ONLY = {"youtube.ids", "youtube.show"} | ops_social.WATCHER_ONLY
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
    if set(raw.get("auto_approve", [])) & set(WRITE_OPS):
        sys.exit(f"{path}: Telegram writes can't be in auto_approve; allow chats in [telegram] instead")
    policy = Policy(frozenset(raw.get("auto_approve", [])), frozenset(raw.get("require_approval", [])))
    return policy, raw.get("trusted_unit", "claude-assistant.service")


def load_telegram_rules(path: Path) -> TelegramRules:
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    try:
        return TelegramRules.parse(raw.get("telegram"))
    except ValueError as e:
        sys.exit(f"{path}: {e}")


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
    def __init__(self, services: Services, approvals: Approvals, policy: Policy, trusted: PidCheck,
                 runs: RunRegistry | None = None):
        self.services = services
        self.approvals = approvals
        self.policy = policy
        self.trusted = trusted
        self.runs = runs or RunRegistry()
        self._tg_lock = asyncio.Lock()

    @property
    def tg_store(self) -> TelegramStore | None:
        side = self.services.telegram_side
        return side.store if side is not None else None

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
                # Where the request really comes from: the claude run it descends from (never what it says).
                origin = self.runs.origin_of(pid)
                return await self.run(ops, request.get("reason"), request.get("origin"), origin)
            if cmd == "approvals.list":
                return {"ok": True, "text": self.list_text()}
            if cmd == "approvals.pending":
                store = self.tg_store
                return {"ok": True, "items": self.approvals.unnotified(),
                        "notices": store.unsent_notices() if store else []}
            if cmd == "approvals.notified":
                self.approvals.mark_notified(_int(request.get("id")), _opt_int(request.get("message_id")))
                return {"ok": True}
            if cmd == "notices.ack":
                if self.tg_store:
                    self.tg_store.ack_notice(_int(request.get("id")))
                return {"ok": True}
            if cmd == "approvals.decide":
                return await self.decide(
                    _int(request.get("id")), request.get("approve") is True,
                    str(request.get("nonce") or ""), _opt_int(request.get("message_id")),
                    always=request.get("always") is True,
                )
            if cmd == "runs.register":
                origin = request.get("origin")
                if not isinstance(origin, str) or not (
                        origin == "private" or origin.startswith(("group:", "watcher:", "forwarded:"))):
                    raise ToolError("origin must be private, group:NAME, watcher:NAME or forwarded:…")
                self.runs.register(_int(request.get("pid")), origin[:100])
                return {"ok": True}
            if cmd == "runs.unregister":
                self.runs.unregister(_int(request.get("pid")))
                return {"ok": True}
            if cmd == "telegram.protect":
                ids = request.get("ids")
                if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
                    raise ToolError("ids must be numbers")
                if self.tg_store:
                    self.tg_store.set_protected(ids, request.get("complete") is not False)
                return {"ok": True}
            raise ToolError(f"unknown command {cmd!r}")
        except ToolError as e:
            return {"ok": False, "error": redact(e)}
        except Exception as e:  # never let one request take the daemon down
            log.exception("request failed")
            return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")}

    async def run(self, ops: list[dict], reason: Any, claimed_origin: Any = None, origin: str = "unknown") -> dict:
        """`origin` is where the request really came from ("private", "group:…", "watcher:…" or "unknown");
        `claimed_origin` is what the caller said, used only to label approvals from unknown processes."""
        if sum(o["op"] in WOLT_BASKET_OPS for o in ops) > 1:  # each would replace the one before it
            raise ToolError("one Wolt basket per request")
        ops = await pre_resolve(self.services, ops)
        for o in ops:
            if o["op"] == "telegram.revoke":
                o["args"]["origin_checked"] = origin
        telegram = [o for o in ops if o["op"] in WRITE_OPS]
        if len(telegram) > 1:  # caps and the duplicate guard are per message: one per request
            raise ToolError("one Telegram write per request (send them one at a time)")
        critical = False
        for o in ops:
            if o["op"] in WOLT_BASKET_OPS and origin != "private":
                critical = True  # a basket wanted by something other than the owner in chat: ask them
            elif o["op"] in WRITE_OPS:
                ask = needs_approval(self.services, o, origin)  # also refuses "never" chats
                if ask or o["op"] in self.policy.require_approval:
                    critical = True
            elif self.policy.critical(o["op"]) or await shares_with_others(self.services, o):
                critical = True
        if critical:
            if self.approvals.pending_count() >= MAX_PENDING:
                raise ToolError(f"{MAX_PENDING} requests are already waiting for the owner; wait for their answer")
            if telegram:
                for o in telegram:
                    check_approval_size(o)
                if self.approvals.pending_count(telegram=True) >= TG_PENDING_CAP:
                    raise ToolError(f"{TG_PENDING_CAP} Telegram requests are already waiting for the owner; wait for "
                                    "their answer")
            ops = await prepare(self.services, ops)
            summary = await summarize(self.services, ops, self.policy)
            who = None
            if origin.startswith(("group:", "watcher:", "forwarded:")):
                who = "a forwarded message in your chat" if origin.startswith("forwarded:") else origin
            elif origin == "unknown":
                if isinstance(claimed_origin, str) and claimed_origin.strip():
                    who = claimed_origin
                elif telegram:
                    who = "a process outside any known chat run"
            if who:
                # Not the owner typing in chat: a run that read outside content (or an unknown process).
                summary = (f"⚠ Asked for by {clean(who, 80)} while handling outside content, not by you in "
                           f"chat.\n{summary}")
            grants = grant_offer(self.services, ops, origin) if telegram else None
            reason = clean(reason, 300) if isinstance(reason, str) and reason.strip() else None
            approval_id = self.approvals.add(ops, summary, reason, grants)
            for o in telegram:
                if o["op"] == "telegram.send":  # the duplicate guard also sees requests that are waiting
                    note_requested(self.services, o)
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
        if telegram:
            for o in telegram:
                o["args"]["resolved"]["auto"] = True
            # One automatic write at a time, so the caps can't be raced past; a short wait keeps the caller's
            # deadline, and a request that never got its turn sent nothing.
            try:
                await asyncio.wait_for(self._tg_lock.acquire(), 10)
            except asyncio.TimeoutError:
                raise ToolError("another Telegram message is being sent; nothing was sent, try again shortly") from None
            try:
                ok, text, extra = await execute(self.services, ops)
            finally:
                self._tg_lock.release()
        else:
            ok, text, extra = await execute(self.services, ops)
        return {"ok": ok, "text": text, **extra} if ok else {"ok": False, "error": text}

    async def decide(self, approval_id: int, approve: bool, nonce: str, message_id: int | None,
                     always: bool = False) -> dict:
        current = self.approvals.get(approval_id)
        # The nonce is only ever given to the bridge (approvals.pending) and the button it sent; the
        # message id is the one the bridge recorded for that button. A forged button fails both.
        if current is None or not hmac.compare_digest(nonce, current.get("nonce") or ""):
            raise ToolError("unknown or forged approval button")
        if current.get("message_id") is not None and message_id != current["message_id"]:
            raise ToolError("this button isn't the approval message that was sent for this request")
        grants = json.loads(current.get("grants") or "null")
        if always and not (approve and grants):
            raise ToolError("this request has no “always” option")
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
        if always and ok and self.tg_store:
            for g in grants:  # from the stored request, never from the button
                self.tg_store.grant(int(g["peer_id"]), g["action"], g["title"])
            text += f"\nFrom now on (30 days) sending there doesn't need your approval: {grants[0]['title']}."
            log.info("approval #%s approved with a standing permission", approval_id)
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


def meta_auth(service: MetaService, client: httpx.AsyncClient, home: Path) -> MetaAuth | None:
    prefix = service.name.upper()
    app_id = os.environ.get(f"{prefix}_APP_ID", "").strip()
    secret = os.environ.get(f"{prefix}_APP_SECRET", "").strip()
    if not (app_id and secret):
        return None
    redirect = os.environ.get("META_REDIRECT_URI", "").strip() or "https://localhost:8765/"
    return MetaAuth(service, app_id, secret, home / f"{service.name.lower()}-token.json", client, redirect)


async def refresh_meta_tokens(services: Services) -> None:
    """Long-lived Meta tokens die after 60 days unless refreshed, used or not. Also keeps the stored usernames
    current (an old one could be taken by someone else)."""
    while True:
        for c in (services.threads_client, services.instagram_client):
            if c is not None:
                try:
                    await c.auth.refresh_if_due()
                    await c.auth.update_user(c)
                except ToolError as e:
                    log.warning("%s: %s", c.s.name, redact(e))
                except Exception:
                    log.exception("token refresh failed")
        await asyncio.sleep(6 * 3600)


def build_services(client: httpx.AsyncClient, home: Path) -> Services:
    services = Services()
    token = os.environ.get("TODOIST_TOKEN", "").strip()
    if token:
        services.todoist_client = Todoist(token, client)
    cid, secret = os.environ.get("GMAIL_CLIENT_ID", "").strip(), os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if cid and secret:
        services.gmail_client = Gmail(GmailAuth(cid, secret, home / "gmail-token.json", client), client)
    for service, attr in ((THREADS, "threads_client"), (INSTAGRAM, "instagram_client")):
        auth = meta_auth(service, client, home)
        if auth is not None:
            setattr(services, attr, MetaClient(service, auth, client))
    # RSS feeds and oEmbed work without any credentials, so YouTube is always there.
    services.youtube_client = YouTube(client, youtube_auth(client, home),
                                      os.environ.get("YOUTUBE_API_KEY", "").strip() or None, home=home)
    # Keyless Google Maps links work without a key, so Maps is always there too.
    maps_key = os.environ.get("MAPS_API_KEY", "").strip() or None
    register_secret(maps_key)
    def env_latlng(var: str) -> str:
        value = os.environ.get(var, "").strip()
        try:
            if not value or latlng(value):
                return value
        except ToolError:
            pass
        log.warning("%s=%r isn't a valid lat,lng; ignoring it", var, value)
        return ""

    maps_near = env_latlng("MAPS_NEAR")
    services.maps_client = Maps(client, maps_key, home, language=os.environ.get("MAPS_LANGUAGE", "en").strip(),
                                region=os.environ.get("MAPS_REGION", "").strip(), near=maps_near,
                                tz=os.environ.get("MAPS_TIMEZONE", "").strip() or "UTC")
    wolt_near = env_latlng("WOLT_NEAR") or maps_near
    near = latlng(wolt_near) if wolt_near else None
    services.wolt_client = Wolt(client, home, near=near, language=os.environ.get("WOLT_LANGUAGE", "en").strip())
    return services


async def _serve(home: Path) -> None:
    policy, unit = load_policy(home / "config.toml")
    async with httpx.AsyncClient() as client:
        services = build_services(client, home)
        log.info("todoist: %s, gmail: %s, youtube: %s", "on" if services.todoist_client else "off",
                 "on" if services.gmail_client and services.gmail_client.auth.configured() else "off",
                 services.youtube.status())
        store = TelegramStore(home / "telegram.sqlite3")
        services.telegram_side = TelegramSide(TgWriterClient(store), store, load_telegram_rules(home / "config.toml"))
        approvals = Approvals(home / "approvals.sqlite3")
        for approval_id in approvals.recover():
            store.notice(f"⚠️ Request #{approval_id} was approved, but the tools service stopped while running it; "
                         "the outcome is unknown. Check before asking again.")
        daemon = Daemon(services, approvals, policy, systemd_main_pid_check(unit))
        refresher = asyncio.create_task(refresh_meta_tokens(services))  # noqa: F841 (runs for the daemon's life)
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


async def _meta_login(home: Path, service: MetaService, write: bool) -> None:
    async with httpx.AsyncClient() as client:
        auth = meta_auth(service, client, home)
        if auth is None:
            prefix = service.name.upper()
            sys.exit(f"Set {prefix}_APP_ID and {prefix}_APP_SECRET in ~/env first (docs/SETUP.md, section 8c).")
        if auth.configured():
            print(f"This replaces the current {service.name} login (@{auth.load().get('username') or '?'}).\n")
        url, state = auth.login_url(write)
        access = "read, and post/reply/delete with your approval" if write or not service.write_scopes else "read-only"
        print(f"1. Open this link in a browser where you're signed in to {service.name}, and allow access ({access}):\n")
        print(url)
        print(f"\n2. The browser then goes to {auth.redirect}..., which doesn't load. That's expected.")
        print("   Copy the whole address from the address bar and paste it here.\n")
        redirected = input("Address: ")
        try:
            data = await auth.finish_login(redirected, state, write)
        except ToolError as e:
            sys.exit(f"Failed: {redact(e)}")
        print(f"Connected @{data['username']}. Now: systemctl restart ampi-tools")


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
        m = services.maps_client
        maps_line = m.status()
        if m.configured:
            # Its own budget object: the running daemon owns maps-budget.json. Resolving a name is free.
            probe = Maps(client, m.api_key, None)
            try:
                found = await probe.resolve_names(["Eiffel Tower, Paris"])
                maps_line += ", API OK" if (found.get("results") or [{}])[0].get("entity") else ", API answered oddly"
            except ToolError as e:
                maps_line += f", API failed: {e}"
        print(f"Google Maps: {maps_line}; {m.budget.summary()}")
        print(f"Wolt: {services.wolt_client.status()} (not contacted by check)")
        for name, c in (("Threads", services.threads_client), ("Instagram", services.instagram_client)):
            if c is None:
                print(f"{name}: not set up")
                continue
            try:
                me = await c.call("GET", "/me", {"fields": "username"})
                print(f"{name}: OK (@{me.get('username')}) {c.auth.expiry_note()}".rstrip())
            except ToolError as e:
                print(f"{name}: {redact(e)}")


async def _wolt_login(home: Path) -> None:
    import getpass

    print("Connect your Wolt account (Ampi can then see your orders and fill baskets; it never orders or pays).\n")
    print("1. In a desktop browser, open a PRIVATE window, go to https://wolt.com and log in.")
    print("2. Open the developer tools (F12) → Application/Storage → Cookies → https://wolt.com,")
    print("   and copy the value of the cookie named __wrtoken.")
    print("3. Close the private window WITHOUT logging out (logging out would cancel this login).\n")
    token = getpass.getpass("__wrtoken value (not shown): ").strip()
    async with httpx.AsyncClient() as client:
        auth = WoltAuth(home / "wolt-token.json", client)
        try:
            auth.save(token)
            await auth.token()  # exchanges it once, which also proves it works
        except ToolError as e:
            sys.exit(f"Failed: {redact(e)}")
    print("Connected. Now: systemctl restart ampi-tools")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ampi-tools", description="Todoist, Gmail and YouTube for the assistant")
    parser.add_argument("command", choices=["serve", "gmail-login", "youtube-login", "threads-login",
                                            "instagram-login", "wolt-login", "check"])
    parser.add_argument("--write", action="store_true", help="youtube-login: also allow changes (playlists, "
                        "subscriptions), each critical one still approved by you in Telegram")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    home = Path(os.environ.get("AMPI_TOOLS_HOME") or Path.home())
    for handler in logging.getLogger().handlers:  # tokens never reach the journal, tracebacks included
        handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))

    def excepthook(kind, value, tb) -> None:  # crashes outside logging (e.g. during a login) are redacted too
        import traceback
        sys.stderr.write(redact("".join(traceback.format_exception(kind, value, tb))))

    sys.excepthook = excepthook
    if args.command in ("threads-login", "instagram-login"):
        asyncio.run(_meta_login(home, THREADS if args.command == "threads-login" else INSTAGRAM, args.write))
        return
    if args.command == "youtube-login":
        asyncio.run(_youtube_login(home, args.write))
        return
    runner = {"serve": _serve, "gmail-login": _gmail_login, "wolt-login": _wolt_login, "check": _check}[args.command]
    asyncio.run(runner(home))


if __name__ == "__main__":
    main()
