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
import contextlib
import hmac
import json
import logging
import os
import socket
import struct
import sys
import time
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from .gmail import Gmail, GmailAuth, GoogleAuth
from .ops import (
    MAX_PENDING, MAX_SUMMARY, OPS, Approvals, Policy, Services, clean, clean_block, execute, id_list, pre_resolve,
    prepare, shares_with_others, summarize, validate,
)
from . import cards, hub_data, permissions as perms, report, watch_spec as ws
from .keeper import (
    ACTIVATE_TTL, ALL_WATCHERS_PENDING_CAP, DEFAULT_ACTION_TTL, MAX_ACTION_TTL, ORIGIN_RE, WATCHER_PENDING_CAP, Keeper,
    actual_route, b64decode_files, b64encode_files, parse_origin, resolve_routes, route_fits,
)
from .permissions import Permissions
from .todoist import Todoist, ToolError
from . import ops_social
from . import meta_session
from .ops_social_session import SOCIAL_ORIGIN, check_origin as check_social_origin, clip, is_social_request
from .maps import LATLNG_RE, Maps, latlng, zone
from .ops_wolt import BASKET_OPS as WOLT_BASKET_OPS
from .ops_checkout import PRIVATE_ONLY as CHECKOUT_PRIVATE, CheckoutClient, CheckoutSide, PaymentRules
from . import ops_phone
from . import ops_flow, ops_signin
from .vault import Vault, mask_cards, mask_coords, mask_text
from .wolt import Wolt, WoltAuth
from .meta import INSTAGRAM, THREADS, MetaAuth, MetaClient, MetaService, RedactingFormatter, redact, register_secret
from .ops_telegram import (
    TG_PENDING_CAP, WRITE_OPS, RunRegistry, TelegramRules, TelegramSide, TelegramStore, TgWriterClient,
    check_approval_size, grant_offer, needs_approval, note_requested,
)
from .youtube import SCOPE_READ, SCOPE_WRITE, YouTube

DEFAULT_SOCKET = "/run/ampi-tools/tools.sock"
MAX_REQUEST = 32 * 1024 * 1024  # watcher snapshots and space files (base64) travel in one line
# Only the bridge's main process may: answer approvals, post notices, say which claude runs exist and where
# they come from, and say which chats the assistant reads itself.
ADMIN = {"approvals.pending", "approvals.notified", "approvals.decide", "notices.ack", "runs.register", "home.timezone",
         "info.overview", "info.revoke_grant",
         "runs.unregister", "telegram.protect",
         # watchers v2: only the bridge's engine talks to the keeper
         "watch.propose", "watch.get", "watch.list", "watch.status", "watch.delete", "watch.space_get",
         "watch.space_put", "watch.audit", "watch.run", "watch.settings"}
# The hub (the same user as this service; Ampi and the bridge run as another).
HUB = {"hub.overview", "hub.activity", "hub.abilities", "hub.watchers", "hub.set"}
WATCHER_ONLY = {"youtube.ids", "youtube.show", "gmail.meta"} | ops_social.WATCHER_ONLY
ACTIVATE_CHOICES = [{"key": "all", "label": "✅ Turn on and allow all"},
                    {"key": "ask", "label": "▶️ Turn on, ask each time"},
                    {"key": "reject", "label": "❌ Reject"}]
ACTION_CHOICES = [{"key": "approve", "label": "✅ Approve"},
                  {"key": "watcher", "label": "✅ Allow for this watcher"},
                  {"key": "reject", "label": "❌ Reject"}]
# For an action with no destination to pin (nothing a lifetime grant could be limited to): approve once only.
ONCE_CHOICES = [ACTION_CHOICES[0], ACTION_CHOICES[2]]
# Changes a watcher's AI step may make without declaring or asking (the owner's decision, 2026-11-23), if the watcher
# was turned on with "allow all". Reversible, reaches nobody else, can't trash or mark as spam (BLOCKED_LABELS).
AGENT_ROUTINE = frozenset({"gmail.label"})
AGENT_ROUTINE_MAX = 25  # emails per request; more asks
OUTSIDE = "Not asked by you in chat: outside content (emails, messages, pages) may be behind it"
PROPOSALS_PENDING_CAP = 5
RISK_ICON = ws.RISK_ICON
SETTINGS_TTL = 1800
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


def load_payment_rules(path: Path) -> PaymentRules:
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return PaymentRules.from_toml(raw.get("payment"))


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


def peer_uid(writer: asyncio.StreamWriter) -> int:
    sock = writer.get_extra_info("socket")
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except (AttributeError, OSError):
        return -1


class Daemon:
    def __init__(self, services: Services, approvals: Approvals, policy: Policy, trusted: PidCheck,
                 runs: RunRegistry | None = None, keeper: Keeper | None = None,
                 permissions: Permissions | None = None):
        self.services = services
        self.approvals = approvals
        self.policy = policy
        self.trusted = trusted
        self.runs = runs or RunRegistry()
        self.keeper = keeper
        self.permissions = permissions or Permissions(":memory:")
        self._tg_lock = asyncio.Lock()
        self._files = asyncio.Lock()  # gmail.download (see connection)
        self._paying: set[tuple[str, str]] = set()
        self._limits: dict[str, dict] = {}  # snapshot hash -> its [limits]

    @property
    def tg_store(self) -> TelegramStore | None:
        side = self.services.telegram_side
        return side.store if side is not None else None

    async def handle(self, request: dict, pid: int, uid: int = -1) -> dict:
        try:
            cmd = request.get("cmd")
            if cmd in HUB:
                if uid < 0 or uid != os.geteuid():
                    raise ToolError("only for the Ampi hub")
                return hub_data.answer(self, cmd, request)
            if cmd in ADMIN and not await self.trusted(pid):
                raise ToolError("only the owner can answer approval requests (Telegram buttons)")
            if cmd == "run":
                ops = validate(request.get("ops"))
                # The watchers' YouTube ops use their own reserve of quota and requests; only the bridge has it.
                if any(o["op"] in WATCHER_ONLY for o in ops) and not await self.trusted(pid):
                    raise ToolError("youtube.ids/youtube.show are for watchers; use `youtube feed` / `youtube video`")
                # Where the request really comes from: the claude run it descends from (never what it says).
                origin = self.runs.origin_of(pid)
                # The bridge's own reads (its watchers' checks) aren't a chat's.
                own = origin == "unknown" and not any(ws.is_write(o["op"]) for o in ops) and await self.trusted(pid)
                resp = self._masked(await self.run(ops, request.get("reason"), request.get("origin"), origin, own), ops)
                if origin == SOCIAL_ORIGIN and isinstance(resp.get("text"), str):
                    resp["text"] = clip(resp["text"])
                return resp
            if cmd == "home.timezone":
                # Only the bridge (schedules); Ampi never asks for it.
                side = self.services.checkout_side
                tz = side.vault.home().get("timezone") if side is not None else None
                return {"ok": True, "timezone": tz}
            if cmd in ("info.overview", "info.revoke_grant"):
                # /info in the owner's Telegram chat (through the bridge): masked entries, and removing site grants.
                side = self.services.checkout_side
                if side is None:
                    raise ToolError("personal info isn't set up")
                if cmd == "info.revoke_grant":
                    origin, fields = request.get("origin"), request.get("fields")
                    if not isinstance(origin, str) or not isinstance(fields, str):
                        raise ToolError("origin and fields")
                    n = side.vault.db.execute("DELETE FROM grants WHERE origin=? AND fields=?", (origin, fields)).rowcount
                    return {"ok": True, "removed": n}
                return {"ok": True, "lines": side.vault.masked(),
                        "grants": [{"origin": r["origin"], "fields": r["fields"], "expires": r["expires"]}
                                   for r in side.vault.grants()]}
            if cmd == "social.sessions":
                # Which web sessions exist (no names, no state): the instagram/threads commands pick their ops by it.
                return {"ok": True, **{k: bool(c is not None and c.configured()) for k, c in (
                    ("instagram", self.services.igweb_client), ("threads", self.services.thweb_client))}}
            if cmd == "approvals.list":
                return {"ok": True, "text": self.list_text(self.runs.origin_of(pid))}
            if cmd == "approvals.status":
                row = self.approvals.get(_int(request.get("id")))
                if row is not None and not self._may_see(row, self.runs.origin_of(pid)) and not await self.trusted(pid):
                    raise ToolError("no such request")
                return self.status_of(_int(request.get("id")))
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
                choice = request.get("choice")
                resp = await self.decide(
                    _int(request.get("id")), request.get("approve") is True,
                    str(request.get("nonce") or ""), _opt_int(request.get("message_id")),
                    always=request.get("always") is True,
                    choice=choice if isinstance(choice, str) else None,
                    confirmed=request.get("confirmed") is True,
                )
                row = self.approvals.get(_int(request.get("id")))
                if row is not None and is_social_request(row):
                    resp["social"] = True  # the bridge keeps it out of the chat logs
                return resp
            if cmd == "runs.register":
                origin = request.get("origin")
                if not isinstance(origin, str) or not (
                        origin == "private" or origin.startswith(("group:", "watcher:", "forwarded:"))):
                    raise ToolError("origin must be private, group:NAME, watcher:NAME or forwarded:…")
                if origin.startswith("watcher:") and ("/" in origin or "@" in origin) and not ORIGIN_RE.fullmatch(origin):
                    raise ToolError("bad watcher origin (watcher:<profile>/<name>@<hash12>#<step>)")
                if len(origin) > 200:
                    raise ToolError("origin too long")
                self.runs.register(_int(request.get("pid")), origin)
                return {"ok": True}
            if isinstance(cmd, str) and cmd.startswith("watch."):
                return await self.watch_cmd(cmd, request)
            if cmd == "runs.unregister":
                self.runs.unregister(_int(request.get("pid")))
                return {"ok": True}
            if cmd == "telegram.protect":
                ids = request.get("ids")
                if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
                    raise ToolError("ids must be numbers")
                if self.tg_store:
                    self.tg_store.set_protected(ids, request.get("complete") is not False)
                    bots = request.get("bots")
                    if isinstance(bots, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in bots):
                        self.tg_store.add_own_bots(bots)  # tg-reader hears it from keep_telling_tg_reader
                return {"ok": True}
            raise ToolError(f"unknown command {cmd!r}")
        except ToolError as e:
            # Errors can quote pages, shops or places: mask them like the strictest reply.
            return {"ok": False, "error": self._mask_obj(redact(e), "all")}
        except Exception as e:  # never let one request take the daemon down
            log.exception("request failed")
            return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")}

    async def run(self, ops: list[dict], reason: Any, claimed_origin: Any = None, origin: str = "unknown",
                  own: bool = False) -> dict:
        """`origin` is where the request really came from ("private", "group:…", "watcher:…" or "unknown");
        `claimed_origin` is what the caller said, used only to label approvals from unknown processes."""
        # Instagram/Threads web-session reads: only social-mode conversations; direct messages: never through here.
        check_social_origin(ops, origin, "run")
        scope = self._scope(origin)
        if not own:
            self.permissions.refuse(scope, ops)
        ops_phone.check_phone(ops, origin)
        ops_signin.check(ops, origin)
        if any(o["op"].startswith(("checkout.", "signin.")) for o in ops):
            await ops_flow.free(self.services)
        ops_flow.check_busy(self.services, ops)
        if any(o["op"] == "gmail.download" for o in ops):
            # A stranger's file lands in the workspace: only when the owner asked in their own chat.
            if origin != "private":
                raise ToolError("files from emails are only for the owner's own chat")
            if len(ops) > 1:
                raise ToolError("one file per request")
            if "gmail.download" in self.policy.require_approval:
                raise ToolError("the owner switched downloading files from emails off (require_approval can't cover it)")
        parsed = parse_origin(origin)
        if parsed is not None:
            # A Claude run of a watcher's agent step (watchers v2): changes go through the keeper.
            if any(ws.is_write(o["op"]) or o["op"] in ws.NOT_FOR_WATCHERS for o in ops):
                profile, name, hash12, step = parsed
                if step is None:
                    raise ToolError("watcher actions go through the watcher engine")
                return await self.watcher_action(profile, name, hash12, step, ops, reason, None,
                                                 DEFAULT_ACTION_TTL, None, agent=True)
        if sum(o["op"] in WOLT_BASKET_OPS for o in ops) > 1:  # each would replace the one before it
            raise ToolError("one Wolt basket per request")
        if any(o["op"] in CHECKOUT_PRIVATE for o in ops):
            # Personal info, the checkout browser and payments: only the owner's own chat, never a group, watcher,
            # forwarded message or unknown process (not even with approval).
            if origin != "private":
                raise ToolError("personal info, the checkout browser and payments are only for the owner's own chat")
            if len(ops) > 1:
                raise ToolError("one checkout or personal-info action per request")
            if ops[0]["op"] == "checkout.pay" and self._pending_pay():
                raise ToolError("a payment is already waiting for the owner's answer")
            if ops[0]["op"] == "checkout.submit" and self._pending_pay("checkout.submit"):
                raise ToolError("a button press is already waiting for the owner's answer")
            if ops[0]["op"] == "checkout.flow" and self._pending_pay("checkout.flow"):
                raise ToolError("a booking is already waiting for the owner's answer")
        if any(o["op"] in ops_phone.WRITES for o in ops) and origin != "private":
            if self.approvals.pending_count() >= MAX_PENDING:  # (again below: nothing is even looked at before this)
                raise ToolError(f"{MAX_PENDING} requests are already waiting for the owner; wait for their answer")
            ops_phone.defer(ops, origin)
        asked = [{"op": o["op"], "args": dict(o["args"])} for o in ops]  # as they came: what they do is judged by these
        self._add_places(ops, origin)
        ops = await pre_resolve(self.services, ops)
        for o in ops:
            if o["op"] == "telegram.revoke":
                o["args"]["origin_checked"] = origin
        telegram = [o for o in ops if o["op"] in WRITE_OPS]
        if len(telegram) > 1:  # caps and the duplicate guard are per message: one per request
            raise ToolError("one Telegram write per request (send them one at a time)")
        # A process outside any known run could be one a watcher left behind: its changes ask.
        critical = origin == "unknown" and any(ws.is_write(o["op"]) for o in ops)
        critical = critical or any(self.permissions.mode(scope, o["op"], o["args"]) == "ask" for o in asked)
        for o in ops:
            if o["op"] in WOLT_BASKET_OPS and origin != "private":
                critical = True  # a basket wanted by something other than the owner in chat: ask them
            elif o["op"] == "checkout.fill":
                # Profile fields on a site the owner allowed (30-day grant) go straight in; otherwise ask.
                critical = critical or not o["args"]["resolved"].get("granted") \
                    or o["op"] in self.policy.require_approval
            elif o["op"] in WRITE_OPS:
                ask = needs_approval(self.services, o, origin)  # also refuses "never" chats
                if ask or o["op"] in self.policy.require_approval:
                    critical = True
            elif o["op"] in ops_phone.WRITES:
                critical = critical or ops_phone.asks_owner(o, origin) or o["op"] in self.policy.require_approval
            elif origin == SOCIAL_ORIGIN and OPS[o["op"]].critical:
                critical = True  # no relaxations for a run that reads strangers' text
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
            actions, who = summary, None
            if origin.startswith(("group:", "watcher:", "forwarded:")):
                who = ("a social-mode conversation (Instagram/Threads content)" if origin == SOCIAL_ORIGIN
                       else "a forwarded message in your chat" if origin.startswith("forwarded:") else origin)
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
            if ops[0]["op"] == "checkout.fill":  # "✅ + always here": these fields on this exact site, 30 days
                r = ops[0]["args"]["resolved"]
                grants = [{"kind": "site", "origin": r["origin"], "fields": r["fields"]}]
            if ops[0]["op"] in ("checkout.fill", "checkout.pay", "checkout.submit"):
                # Keep the session open while the owner decides (the controller caps it at 30 minutes).
                with contextlib.suppress(ToolError):
                    await self.services.checkout_side.client.call(
                        {"cmd": "hold", "session": ops[0]["args"]["resolved"]["session"],
                         "until": ops[0]["args"]["resolved"]["expires"]})
            reason = clean(reason, 300) if isinstance(reason, str) and reason.strip() else None
            if ops[0]["op"] == "checkout.flow" and self._pending_pay("checkout.flow"):
                raise ToolError("a booking is already waiting for the owner's answer")
            if ops[0]["op"] == "checkout.pay" and self._pending_pay():  # again: another may have arrived meanwhile
                raise ToolError("a payment is already waiting for the owner's answer")
            if ops[0]["op"] == "checkout.submit" and self._pending_pay("checkout.submit"):
                raise ToolError("a button press is already waiting for the owner's answer")
            card = cards.make_card(ops, actions, who or ("Ampi, in your chat" if origin == "private" else
                                                         "an unidentified process"), OUTSIDE if who else None)
            # A phone action approved much later would do something else than meant (an alarm for tomorrow, a timer).
            ttl = ops_phone.APPROVAL_TTL if any(o["op"] in ops_phone.WRITES for o in ops) else None
            approval_id = self.approvals.add(ops, summary, reason, grants, meta={"origin": origin}, card=card, ttl=ttl)
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
                    "owner briefly what you asked for; don't ask again or try another way.\n"
                    # What is filled in on the page is for the owner's eyes (the page may have put things there).
                    + (summary.split("\n", 1)[0] if ops[0]["op"] == "checkout.submit" else summary)
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

    def _masked(self, resp: dict, ops: list[dict]) -> dict:
        """Every reply to Ampi: card numbers and coordinates near the owner's places are masked everywhere; Maps, Wolt,
        checkout and payment replies also mask every stored personal value. (`info collect` is the one reply meant
        to carry values, after the owner approved; cards are never in it.)"""
        if any(o["op"] == "info.collect" for o in ops):
            return resp
        if any(o["op"].startswith(("checkout.", "payment.", "signin.")) for o in ops):
            level = "all"
        elif any(o["op"].startswith(("maps.", "wolt.")) for o in ops):
            level = "places"  # places in the owner's city stay readable: street, full address, email, phone, names
        else:
            level = "light"
        file = resp.pop("file", None) if any(o["op"] == "gmail.download" for o in ops) else None
        masked = self._mask_obj(resp, level)
        if file is None:
            return masked
        # What is in it can't be masked; what is said about it is.
        return {**masked, "file": {**self._mask_obj({k: v for k, v in file.items() if k != "data"}, level), "data": file.get("data")}}

    def _locations(self) -> list[tuple[float, float]]:
        vault = getattr(self.services.checkout_side, "vault", None)
        locs = vault.locations() if vault is not None else []
        env_home = getattr(self.services, "env_home", None)
        if env_home:
            ll = latlng(env_home)
            if ll:
                locs.append((ll["latitude"], ll["longitude"]))
        return locs

    def _mask_obj(self, obj: Any, level: str) -> Any:
        """Masks every string in a reply. light: the stored card numbers (not every Luhn-valid run: chat ids and
        timestamps look like that) and coordinates of the owner's places; places/all: personal values too."""
        side = self.services.checkout_side
        mask_any = getattr(side, "mask_any", None)
        vault = getattr(side, "vault", None)
        cards = vault.card_numbers() if vault is not None else []
        locs = self._locations()

        def one(t: str) -> str:
            if mask_any is not None and level != "light":
                t = mask_any(t, level=level)
            t = mask_cards(t, cards)
            return mask_coords(t, locs) if locs else t

        def walk(x: Any) -> Any:
            if isinstance(x, str):
                return one(x)
            if isinstance(x, list):
                return [walk(v) for v in x]
            if isinstance(x, dict):
                return {k: walk(v) for k, v in x.items()}
            return x
        return walk(obj)

    def places_for(self, origin: str) -> dict[str, str]:
        """Places a request may use for searches ({name: "lat,lng"}): the owner's private chat gets the places with
        searches switched on; a watcher only those it declares in `uses`; groups, forwarded messages and unknown
        processes none (a few routes from "home" would pinpoint it)."""
        side = self.services.checkout_side
        places = dict(side.vault.search_places()) if side is not None else {}
        env_home = getattr(self.services, "env_home", None)
        home = side.vault.entry("home") if side is not None else None
        # The env setting stands in until the home entry has a location of its own (an upgraded vault's home has
        # only the address); a home with a location but searches switched off stays off.
        if env_home and "home" not in places and not (home and (home["fields"].get("location") or not home.get("search", True))):
            places["home"] = env_home
        if origin == "private":
            return places
        parsed = parse_origin(origin)
        if parsed is not None and self.keeper is not None:
            try:
                w = self.keeper.active(parsed[0], parsed[1], parsed[2])
            except ToolError:
                return {}
            uses = next((e.get("uses", []) for e in self.keeper.manifest(w) if e.get("kind") == "uses"), [])
            names = {u.split(".", 1)[0] for u in uses if u.endswith(".location") or "." not in u}
            return {n: v for n, v in places.items() if n in names}
        return {}

    def _refresh_timezone(self) -> None:
        """Route times use home's timezone as it is now (the owner may change it in the hub without a restart)."""
        maps, side = getattr(self.services, "maps_client", None), self.services.checkout_side
        if maps is None or side is None:
            return
        try:
            tz = side.vault.home().get("timezone") or getattr(self.services, "env_tz", "UTC")
            maps.tz = zone(tz)
        except (ToolError, ValueError):
            pass

    def _add_places(self, ops: list[dict], origin: str) -> None:
        if not any(o["op"].startswith(("maps.", "wolt.")) for o in ops):
            return
        self._refresh_timezone()
        places = self.places_for(origin)
        for o in ops:
            if o["op"].startswith("maps."):
                o["args"]["places_allowed"] = places
            elif o["op"].startswith("wolt."):
                name = o["args"].pop("place", None)
                if name:
                    from .ops_maps import _place_name
                    key = _place_name(name, places)
                    if key is None:
                        raise ToolError(f"no place “{clean(name, 40)}” for searches here (the owner's places: "
                                        f"{', '.join(sorted(places)) or 'none set up, or not in this chat'})")
                    o["args"]["lat_lng"] = latlng(places[key])
                elif not o["args"].get("lat_lng"):
                    o["args"]["lat_lng"] = latlng(places["home"]) if places.get("home") else None

    async def decide(self, approval_id: int, approve: bool, nonce: str, message_id: int | None,
                     always: bool = False, choice: str | None = None, confirmed: bool = False) -> dict:
        current = self.approvals.get(approval_id)
        # The nonce is only ever given to the bridge (approvals.pending) and the button it sent; the
        # message id is the one the bridge recorded for that button. A forged button fails both.
        if current is None or not hmac.compare_digest(nonce, current.get("nonce") or ""):
            raise ToolError("unknown or forged approval button")
        if current.get("message_id") is not None and message_id != current["message_id"]:
            raise ToolError("this button isn't the approval message that was sent for this request")
        choices = json.loads(current.get("choices") or "null")
        if choices or choice is not None:
            keys = [c["key"] for c in choices or []]
            if choice not in keys:
                raise ToolError("that isn't one of this request's buttons")
            if choice in json.loads(current.get("confirm_keys") or "[]") and not confirmed:
                return {"ok": False, "need_confirm": True,
                        "error": "High risk: tap the same button again to confirm."}
            if current.get("kind") == "watch.activate":
                return self._decide_activate(current, choice)
            if current.get("kind") != "watch.action":
                raise ToolError("unknown kind of request")
            return await self._decide_watch_action(current, choice)
        if current.get("kind") == "settings.change":
            return self._decide_settings(current, approve)
        if current.get("kind"):
            raise ToolError("unknown kind of request")
        grants = json.loads(current.get("grants") or "null")
        if always and not (approve and grants):
            raise ToolError("this request has no “always” option")
        if approve:
            await ops_flow.free(self.services)
            ops_flow.check_busy(self.services, json.loads(current["ops"]))
        item = self.approvals.claim(approval_id, "approved" if approve else "rejected")
        if item is None:
            state = (self.approvals.get(approval_id) or {}).get("status", "unknown")
            return {"ok": True, "status": state, "text": f"Request #{approval_id} is already {state}."}
        if not approve:
            log.info("approval #%s rejected", approval_id)
            return {"ok": True, "status": "rejected", "text": "Rejected; nothing was done."}
        ok, text, status = False, "interrupted", "failed"
        try:
            ops = json.loads(item["ops"])
            try:  # (switched off while the request waited)
                self.permissions.refuse(self._scope(str(json.loads(current.get("meta") or "{}").get("origin") or "")), ops)
            except ToolError as e:
                text = f"Not done: {e}"
                return {"ok": True, "status": "failed", "text": text}
            for o in ops:  # ops that hand something back to Ampi later (info.reveal) key it by the request
                o["args"]["approval_id"] = approval_id
            ok, text, _ = await execute(self.services, ops)
            status = "done" if ok else "failed"
        finally:
            self.approvals.finish(approval_id, status, text)
            log.info("approval #%s %s", approval_id, status)
        if always and ok and grants and grants[0].get("kind") == "site":
            g = grants[0]  # from the stored request, never from the button
            self.services.checkout_side.vault.grant(g["origin"], g["fields"])
            text += f"\nFrom now on (30 days) filling {', '.join(g['fields'])} on {g['origin']} doesn't ask."
            log.info("approval #%s approved with a site permission", approval_id)
        elif always and ok and self.tg_store:
            for g in grants:  # from the stored request, never from the button
                self.tg_store.grant(int(g["peer_id"]), g["action"], g["title"])
            text += f"\nFrom now on (30 days) sending there doesn't need your approval: {grants[0]['title']}."
            log.info("approval #%s approved with a standing permission", approval_id)
        return {"ok": True, "status": status, "text": text}

    # --- watchers v2 (the keeper) ----------------------------------------------------------------------

    def _keeper(self) -> Keeper:
        if self.keeper is None:
            raise ToolError("the watcher keeper isn't set up")
        return self.keeper

    @staticmethod
    def _scope(origin: str) -> str:
        parsed = parse_origin(origin)
        return perms.watcher_scope(parsed[0], parsed[1]) if parsed else "chat"

    def ask_settings(self, scope: str, changes: dict[str, list[str]]) -> int:
        """Anything looser asks in Telegram, whoever sent it here."""
        lines = [f"Change what {perms.words(scope)} may do:"]
        for op, (old, new) in changes.items():
            note = "" if op.endswith(".*") or op.startswith("flow:") else f" ({cards.note_of(op)})"
            lines.append(f"• {op}: {old} → {new}{note}")
        summary = "\n".join(lines)
        if len(summary) > 2800:
            raise ToolError("too many changes at once; save a few at a time")
        self.approvals.expire()
        for row in self.approvals.recent(100):  # the newest request for this page counts
            if row["status"] == "pending" and row.get("kind") == "settings.change" \
                    and json.loads(row.get("meta") or "{}").get("scope") == scope:
                self.approvals.claim(row["id"], "expired")
        card = {"who": "you, in the Ampi hub", "what": "Change permissions", "details": "\n".join(lines[1:]), "level": "high",
                "risks": [f"For {perms.words(scope)}", "Approve only if you just changed this yourself"]}
        return self.approvals.add([], summary, None, kind="settings.change", ttl=SETTINGS_TTL,
                                  meta={"scope": scope, "changes": changes}, card=card)

    def _decide_settings(self, current: dict, approve: bool) -> dict:
        item = self.approvals.claim(current["id"], "approved" if approve else "rejected")
        if item is None:
            state = (self.approvals.get(current["id"]) or {}).get("status", "unknown")
            return {"ok": True, "status": state, "text": f"Request #{current['id']} is already {state}."}
        status, text = "rejected", "Rejected; nothing was changed."
        if approve:
            meta = json.loads(item["meta"])
            status, text = "done", "Changed."
            try:
                hub_data.check_changes(self, meta["scope"], meta["changes"])
                self.permissions.apply(meta["scope"], meta["changes"], f"hub, approved in Telegram (#{current['id']})")
            except ToolError as e:
                status, text = "failed", str(e)
        self.approvals.finish(current["id"], status, text)
        return {"ok": True, "status": status, "text": text}

    def send_reports(self) -> int:
        """What watchers did without asking, as notices (kept until Telegram took them)."""
        if self.keeper is None or self.tg_store is None:
            return 0
        n = 0
        for _, name, rows in self.keeper.due_reports():
            for text in report.texts(name, rows):
                self.tg_store.notice(text)
                n += 1
            self.keeper.reported([r["id"] for r in rows])
        return n

    async def keep_reporting(self) -> None:
        while True:
            try:
                self.send_reports()
            except Exception:
                log.exception("reports failed")
            await asyncio.sleep(30)

    def _decide_activate(self, current: dict, choice: str) -> dict:
        k = self._keeper()
        meta = json.loads(current.get("meta") or "{}")
        item = self.approvals.claim(current["id"], "rejected" if choice == "reject" else "approved")
        if item is None:
            state = (self.approvals.get(current["id"]) or {}).get("status", "unknown")
            return {"ok": True, "status": state, "text": f"Request #{current['id']} is already {state}."}
        pid = int(meta.get("proposal", 0))
        if choice == "reject":
            k.close_proposal(pid, "rejected")
            self.approvals.finish(current["id"], "rejected", "Rejected; the watcher wasn't turned on.")
            return {"ok": True, "status": "rejected", "text": "Rejected; the watcher wasn't turned on.",
                    "watcher": meta.get("name")}
        try:
            w = k.activate(pid, "all" if choice == "all" else "ask")
        except ToolError as e:
            k.close_proposal(pid, "failed")
            self.approvals.finish(current["id"], "failed", str(e))
            return {"ok": True, "status": "failed", "text": str(e), "watcher": meta.get("name")}
        how = ("everything it lists is allowed until you change or delete it" if choice == "all"
               else "each change it wants asks you first")
        text = f"Watcher “{w.get('name')}” is on ({how})."
        self.approvals.finish(current["id"], "done", text)
        log.info("watcher %s/%s activated (%s, %s)", w.get("profile"), w.get("name"), choice, (w.get("hash") or "")[:12])
        return {"ok": True, "status": "done", "text": text, "watcher": w.get("name")}

    async def _decide_watch_action(self, current: dict, choice: str) -> dict:
        k = self._keeper()
        meta = json.loads(current.get("meta") or "{}")
        approve = choice in ("approve", "watcher")
        if approve:
            ops_flow.check_busy(self.services, json.loads(current["ops"]))
        item = self.approvals.claim(current["id"], "approved" if approve else "rejected")
        audit = k.audit_for_approval(current["id"])
        if item is None:
            state = (self.approvals.get(current["id"]) or {}).get("status", "unknown")
            if audit and state == "expired":
                k.audit_update(audit["id"], "expired")
            return {"ok": True, "status": state, "text": f"Request #{current['id']} is already {state}."}
        if not approve:
            if audit:
                k.audit_update(audit["id"], "rejected", "rejected by the owner")
            return {"ok": True, "status": "rejected", "text": "Rejected; nothing was done."}
        try:  # the watcher may have been paused, deleted or replaced while the request waited
            k.active(meta["profile"], meta["name"], str(meta["hash"])[:12])
        except (ToolError, KeyError):
            text = "Not done: the watcher was paused, changed or deleted since it asked."
            self.approvals.finish(current["id"], "failed", text)
            if audit:
                k.audit_update(audit["id"], "failed", text)
            return {"ok": True, "status": "failed", "text": text}
        scope = perms.watcher_scope(meta["profile"], meta["name"])
        try:
            self.permissions.refuse(scope, json.loads(item["ops"]))
        except ToolError as e:
            text = f"Not done: {e}"
            self.approvals.finish(current["id"], "failed", text)
            if audit:
                k.audit_update(audit["id"], "failed", text)
            return {"ok": True, "status": "failed", "text": text}
        if audit:
            k.audit_update(audit["id"], "approved")
        ok, text, status = False, "interrupted", "failed"
        try:
            ops = json.loads(item["ops"])
            for o in ops:
                o["args"]["approval_id"] = current["id"]
            ok, text, _ = await execute(self.services, ops)
            status = "done" if ok else "failed"
        finally:
            self.approvals.finish(current["id"], status, text)
            if audit:
                k.audit_update(audit["id"], status, text)
        if choice == "watcher" and ok and self.permissions.mode(scope, meta["op"]) not in ("off", "ask"):
            k.add_grant(meta["profile"], meta["name"], meta["hash"], meta["step"], meta["op"], meta["route"])
            text += (f"\nFrom now on watcher “{meta['name']}” may do this ({meta['op']}, same destination) without "
                     "asking, until you change or delete it.")
        return {"ok": True, "status": status, "text": text}

    def _limits_of(self, h: str) -> dict:
        if h not in self._limits:
            self._limits[h] = ws.parse(self._keeper().files(h)).limits
        return self._limits[h]

    async def watch_cmd(self, cmd: str, req: dict) -> dict:
        k = self._keeper()
        profile, name = req.get("profile"), req.get("name")
        if cmd not in ("watch.run",) and (not isinstance(profile, str) or not ws.NAME_RE.fullmatch(profile)):
            raise ToolError("bad profile")
        if cmd not in ("watch.list", "watch.run") and (not isinstance(name, str) or not ws.NAME_RE.fullmatch(name)):
            raise ToolError("bad watcher name")
        if cmd == "watch.propose":
            return await self.watch_propose(profile, name, req)
        if cmd == "watch.get":
            w = k.watcher(profile, name)
            if not w or w["status"] not in ("active", "paused", "failing") or not w["hash"]:
                raise ToolError(f"no approved watcher {name}")
            return {"ok": True, "status": w["status"], "hash": w["hash"], "mode": w["mode"], "risk": w["risk"],
                    "manifest": k.manifest(w), "files": b64encode_files(k.files(w["hash"])), "request": w["request"],
                    "created": w["created"], "approved": w["approved"], "description": w["description"]}
        if cmd == "watch.list":
            return {"ok": True, "watchers": k.list(profile)}
        if cmd == "watch.status":
            status = req.get("status")
            if status not in ("paused", "failing", "active"):
                raise ToolError("status must be paused, failing or active")
            k.set_status(profile, name, status)
            return {"ok": True, "status": status}
        if cmd == "watch.delete":
            k.delete(profile, name)
            self.permissions.forget(perms.watcher_scope(profile, name))
            return {"ok": True}
        if cmd == "watch.settings":
            scope = perms.watcher_scope(profile, name)
            return {"ok": True, "off": self.permissions.off(scope),
                    "reports": "report" in self.permissions.all(scope).values()}
        if cmd == "watch.space_get":
            return {"ok": True, "files": b64encode_files(k.space_get(profile, name))}
        if cmd == "watch.space_put":
            w = k.watcher(profile, name)
            h = req.get("hash")
            if not w or not isinstance(h, str) or len(h) < 12 or not (w["hash"] or "").startswith(h):
                raise ToolError("space can only be changed by the approved version of the watcher")
            raw = req.get("changes")
            if not isinstance(raw, dict):
                raise ToolError("changes must be an object")
            changes = {p: None for p, v in raw.items() if v is None}
            changes.update(b64decode_files({p: v for p, v in raw.items() if v is not None}, "changes"))
            run_id = req.get("run") if isinstance(req.get("run"), str) else None
            try:
                n = k.space_put(profile, name, w["hash"], run_id, changes)
            except ws.SpecError as e:
                raise ToolError(str(e)) from None
            return {"ok": True, "changed": n}
        if cmd == "watch.audit":
            limit = req.get("limit", 20)
            return {"ok": True, "items": k.audit(profile, name, min(_int(limit), 200))}
        if cmd == "watch.run":
            parsed = parse_origin(req.get("origin") or "")
            if parsed is None or parsed[3] is not None:
                raise ToolError("origin must be watcher:<profile>/<name>@<hash12>")
            step = req.get("step")
            if not isinstance(step, str) or not ws.STEP_ID_RE.fullmatch(step):
                raise ToolError("step must be the step id")
            idem = req.get("idem_key")
            if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 200):
                raise ToolError("idem_key must be text (run id + step)")
            ttl = req.get("ttl_seconds", DEFAULT_ACTION_TTL)
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 60 <= ttl <= MAX_ACTION_TTL:
                raise ToolError("ttl_seconds must be between 60 and 3 days")
            run_id = req.get("run") if isinstance(req.get("run"), str) else None
            ops = validate(req.get("ops"))
            return self._masked(await self.watcher_action(parsed[0], parsed[1], parsed[2], step, ops, req.get("reason"),
                                                          idem, float(ttl), run_id, agent=False), ops)
        raise ToolError(f"unknown command {cmd!r}")

    async def watch_propose(self, profile: str, name: str, req: dict) -> dict:
        k = self._keeper()
        files = b64decode_files(req.get("files"))
        try:
            spec = ws.parse(files, expected_name=name)
        except ws.SpecError as e:
            raise ToolError(f"the watcher isn't valid: {e}") from None
        current = k.watcher(profile, name)
        if current and current["status"] == "deleted":
            pass  # a new watcher under an old name: allowed
        pending = k.pending_proposal(profile, name)
        if pending:
            # A proposal whose request expired (or failed) is closed, so it can't block the name forever.
            self.approvals.expire()
            ap = self.approvals.get(pending["approval_id"]) if pending.get("approval_id") else None
            if ap is not None and ap.get("status") == "pending":
                raise ToolError(f"a proposal for {name} is already waiting for the owner's answer")
            k.close_proposal(pending["id"], "expired")
        if current and current.get("hash") == spec.hash and current["status"] in ("active", "paused", "failing"):
            raise ToolError("that exact version is already approved")
        self.approvals.expire()
        waiting = self.approvals.db.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending' AND kind='watch.activate'").fetchone()[0]
        if waiting >= PROPOSALS_PENDING_CAP:
            raise ToolError(f"{PROPOSALS_PENDING_CAP} watcher proposals are already waiting for the owner's answer")
        entries = await resolve_routes(self.services, ws.manifest(spec))
        level, _ = ws.risk(entries)
        request = req.get("request") if isinstance(req.get("request"), str) else ""
        requester = req.get("requester") if isinstance(req.get("requester"), str) else ""
        test_text = req.get("test_text") if isinstance(req.get("test_text"), str) else ""
        pid = k.store_proposal(profile, name, spec, entries, level, request[:4000], requester[:200], test_text[:20000])
        changed = current and current.get("hash") and current["status"] != "deleted"
        head = (f"🧩 {'Changed' if changed else 'New'} watcher “{name}”" + (f" (profile {profile})" if profile != "personal" else "")
                + f"\n{clean(spec.description, 300)}")
        asked = f"Requested by {clean(requester, 80) or 'Ampi'}; your words:\n{clean_block(request[:600] or '(none recorded)')}"
        body = ws.describe(spec, entries)
        titles = [f"  chat {cid} = {clean(t, 60)}" for e in entries for cid, t in
                  zip((e['route']['chat'] if isinstance(e.get('route', {}).get('chat'), list)
                       else [e.get('route', {}).get('chat')]), e.get("chat_titles", []))]
        if titles:
            body += "\nTelegram chats:\n" + "\n".join(dict.fromkeys(titles))
        footer = (f"Version {spec.hash[:12]}. Tapping a button approves exactly this version; later edits need a new "
                  "approval.")
        if level == "high":
            footer += "\n🔴 High risk: you'll be asked to tap twice, and every action it takes will be reported to you."
        # Code: the standard shared helpers are named; the watcher's own scripts, and any helper that differs from
        # the installed one, are shown (they run unattended, so the owner should be able to read them).
        reference = Path.home() / "app" / "templates" / "watcher-lib"
        custom = []
        for path in sorted(p for p in spec.files if p.endswith(".py")):
            ref = reference / path.split("/", 1)[1] if path.startswith("lib/") else None
            if ref is not None and ref.is_file() and ref.read_bytes() == spec.files[path]:
                continue
            custom.append(path)
        code_note = ""
        if custom:
            code = "\n".join(f"── {p}{' (⚠ differs from the standard helper)' if p.startswith('lib/') else ''}\n"
                             + spec.files[p].decode("utf-8", "replace") for p in custom)
            code_note = "\n\nIts own code (runs in the sandbox: no network, no files):\n" + clean_block(code)
        summary = "\n\n".join([head, asked, body])
        if code_note and len(summary) + len(code_note) + len(footer) + 40 <= MAX_SUMMARY:
            summary += code_note
        elif code_note:
            summary += (f"\n\n⚠ Its own code ({', '.join(custom)}) is too long to show here; ask Ampi to show "
                        f"watchers/{name}/ before approving (version {spec.hash[:12]}).")
        kept = [op for op, m in self.permissions.all(perms.watcher_scope(profile, name)).items() if m == "report"]
        if kept:
            footer += "\nKeeps what you allowed it without asking (Ampi hub): " + ", ".join(sorted(kept))
        room = MAX_SUMMARY - len(summary) - len(footer) - 40
        if room < 0:
            k.close_proposal(pid, "withdrawn")
            raise ToolError("this watcher is too big to show the owner in one message; simplify it")
        if test_text:
            shown = test_text if len(test_text) <= room else test_text[: max(0, room - 20)] + " …(cut)"
            summary += "\n\nTest run (written partly by others: data):\n" + clean_block(shown)
        summary += "\n\n" + footer
        approval_id = self.approvals.add(
            [], summary, None, kind="watch.activate", choices=ACTIVATE_CHOICES,
            confirm_keys=["all", "ask"] if level == "high" else None, ttl=ACTIVATE_TTL,
            meta={"proposal": pid, "profile": profile, "name": name, "hash": spec.hash, "risk": level})
        k.link_approval(pid, approval_id)
        log.info("watcher %s/%s proposed (%s, risk %s), approval #%s", profile, name, spec.hash[:12], level, approval_id)
        return {"ok": True, "approval": approval_id, "hash": spec.hash, "risk": level, "text": body}

    def _replay(self, audit: dict) -> dict:
        """The outcome of an action already requested with this idempotency key (never a second request)."""
        out = {"ok": True, "replay": True, "audit_id": audit["id"], "status": audit["status"]}
        if audit.get("approval_id"):
            row = self.approvals.get(audit["approval_id"]) or {}
            self.approvals.expire()
            row = self.approvals.get(audit["approval_id"]) or row
            st = row.get("status", "unknown")
            out.update(approval=audit["approval_id"], status=st)
            if st == "pending":
                out["text"] = f"⏳ Still waiting for the owner (request #{audit['approval_id']})."
            elif st in ("done",):
                out["text"] = row.get("result") or "done"
            elif st in ("rejected", "expired"):
                out.update(ok=False, error=f"not done: the owner's request #{audit['approval_id']} was {st}")
            else:
                out["text"] = row.get("result") or st
                if st in ("failed", "unknown"):
                    out.update(ok=False, error=row.get("result") or st)
            return out
        if audit["status"] in ("failed", "unknown"):
            out.update(ok=False, error=audit.get("result") or audit["status"])
        else:
            out["text"] = audit.get("result") or audit["status"]
        return out

    async def _checkout_gate(self, k: Keeper, profile: str, name: str, o: dict, declared: list[dict]) -> str:
        """Checkout from a watcher: only on the exact site it declared, in the session it opened. Returns the site."""
        sites = {e["route"].get("site") for e in declared if isinstance(e["route"].get("site"), str)}
        if not sites:
            raise ToolError(f"{o['op']} isn't declared with a fixed site for this watcher step")
        side = self.services.checkout_side
        if side is None:
            raise ToolError("the checkout browser isn't set up")
        if o["op"] == "checkout.open":
            if o["args"].get("engine"):
                raise ToolError("a watcher uses the default browser engine")
            from urllib.parse import urlsplit
            u = urlsplit(o["args"].get("url", ""))
            site = f"{u.scheme}://{u.hostname or ''}" + (f":{u.port}" if u.port else "")
            if u.scheme != "https" or site not in sites:
                raise ToolError(f"this watcher may only open {', '.join(sorted(sites))}")
            return site
        st = await side.client.call({"cmd": "status"})
        rec = k.checkout_sessions.get(st.get("session") or "")
        if not rec or rec[:2] != (profile, name) or rec[2] not in sites:
            raise ToolError("the checkout session wasn't opened by this watcher on its site")
        return rec[2]

    async def watcher_action(self, profile: str, name: str, hash12: str, step: str, ops: list[dict], reason: Any,
                             idem_key: str | None, ttl: float, run_id: str | None, agent: bool) -> dict:
        """One payment of a watcher at a time, so its weekly/monthly counts can't be raced."""
        ops_flow.check_busy(self.services, ops)
        paying = any(o["op"] == "checkout.pay" for o in ops)
        key = (profile, name)
        if paying:
            if key in self._paying:
                raise ToolError("another payment of this watcher is in progress")
            self._paying.add(key)
        try:
            return await self._watcher_action(profile, name, hash12, step, ops, reason, idem_key, ttl, run_id, agent)
        finally:
            if paying:
                self._paying.discard(key)

    async def _watcher_action(self, profile: str, name: str, hash12: str, step: str, ops: list[dict], reason: Any,
                             idem_key: str | None, ttl: float, run_id: str | None, agent: bool) -> dict:
        """An action of a watcher (engine adapter step / script action, or an agent step's Claude run)."""
        k = self._keeper()
        w = k.active(profile, name, hash12)
        h = w["hash"]
        origin = f"watcher:{profile}/{name}@{h[:12]}"
        # Direct messages: only a watcher's own (engine) step; an agent step's tool call is a plain `run`.
        check_social_origin(ops, origin, "run" if agent else "watch.run")
        for o in ops:
            if o["op"] in ws.NOT_FOR_WATCHERS:
                raise ToolError(f"{o['op']} isn't available to watchers")
        scope = perms.watcher_scope(profile, name)
        self.permissions.refuse(scope, ops)
        if not any(ws.is_write(o["op"]) for o in ops):
            self._add_places(ops, f"watcher:{profile}/{name}@{h[:12]}")
            if not agent:
                # Engine reads too must be in the approved spec (an adapter step, or declared by the script step),
                # so a script can't quietly read the mailbox and pass it on.
                listed = {e.get("op") for e in k.manifest(w) if e.get("kind") == "action" and e.get("step") == step}
                missing = [o["op"] for o in ops if o["op"] not in listed]
                if missing:
                    raise ToolError(f"step {step} of watcher {name} didn't declare {', '.join(missing)}; refused")
            ops = await pre_resolve(self.services, ops)
            ok, text, extra = await execute(self.services, ops)
            return {"ok": ok, "text": text, **extra} if ok else {"ok": False, "error": text}
        if len(ops) != 1:
            raise ToolError("one change per request from a watcher (send them one at a time)")
        if idem_key:
            seen = k.audit_by_key(idem_key)
            if seen:
                return self._replay(seen)
        o = ops[0]
        op = o["op"]
        raw_args = dict(o["args"])
        declared = [e for e in k.manifest(w) if e.get("kind") == "action" and e.get("step") == step and e.get("op") == op]
        if not declared and not agent:
            raise ToolError(f"step {step} of watcher {name} didn't declare {op}; refused (not asked)")
        if k.actions_today(profile, name) >= self._limits_of(h)["actions_per_day"]:
            raise ToolError(f"watcher {name} reached its daily action limit")
        site = None
        if op.startswith("checkout."):
            site = await self._checkout_gate(k, profile, name, o, declared)
            if op == "checkout.pay" and self._pending_pay():
                raise ToolError("a payment is already waiting for the owner's answer")
        ops = await pre_resolve(self.services, ops)
        o = ops[0]
        if op in WRITE_OPS:
            needs_approval(self.services, o, origin)  # refuses "never" and protected chats; the answer itself is ours
        ops = await prepare(self.services, ops)
        o = ops[0]
        route = actual_route(op, o["args"], raw_args)
        if op.startswith("checkout."):
            route["site"] = route.get("site") or site
            if route["site"] != site:
                raise ToolError(f"the checkout page is on {route['site']}, not the watcher's site {site}")
        fits = [e for e in declared if route_fits(op, e["route"], route)]
        if declared and not fits and not agent:
            raise ToolError(f"{op} from step {step} would go somewhere the watcher didn't declare; refused")
        force_ask = False
        if op in ("checkout.fill", "checkout.pay"):
            # Personal info only as its spec declares (`uses`), shown in its approval.
            uses_entry = next((e for e in k.manifest(w) if e.get("kind") == "uses"), None)
            vault = self.services.checkout_side.vault if self.services.checkout_side else None
            allowed = set()
            for u in (uses_entry or {}).get("uses", []):
                allowed.add(u if "." not in u or vault is None else vault.canonical(u))
            # Watchers approved before `uses` existed keep working as approved (their fills still ask or use grants).
            approved_at = float(w.get("approved") or 0)
            for f in (o["args"].get("resolved") or {}).get("fills") or []:
                e = vault.entry(f["field"].split(".", 1)[0]) if vault is not None else None
                if e and float(e.get("created") or 0) > approved_at:
                    force_ask = True  # e.g. card-main deleted and re-added: the approval was for the old one
            for f in ((o["args"].get("resolved") or {}).get("fills") or []) if uses_entry is not None else []:
                entry = f["field"].split(".", 1)[0]
                if entry not in allowed and f["field"] not in allowed:
                    raise ToolError(f"watcher {name} didn't declare {f['field']} in `uses`; refused")
        amount = None
        if op == "checkout.pay":
            r = o["args"].get("resolved") or {}
            amount = str(r.get("amount"))
            lim = next((e.get("limits") for e in fits if e.get("limits")), None)
            if not lim:
                raise ToolError("checkout.pay needs a declared max_amount for this watcher")
            from decimal import Decimal
            if Decimal(amount) > Decimal(str(lim["max_amount"])):
                raise ToolError(f"{amount} is over this watcher's limit of {lim['max_amount']} per purchase")
            if k.pays_this_week(profile, name) >= int(lim["per_week"]):
                raise ToolError(f"this watcher's purchases for the week ({lim['per_week']}) are used up")
            if lim.get("per_month") is not None and k.pays_this_month(profile, name) >= int(lim["per_month"]):
                raise ToolError(f"this watcher's purchases for the month ({lim['per_month']}) are used up")
            if lim.get("item") and lim["item"].casefold() not in str(r.get("what") or "").casefold():
                raise ToolError(f"this watcher may buy only “{lim['item']}” (the purchase says: {clean(r.get('what'), 80)})")
            if lim.get("card") and r.get("card") != lim["card"]:
                raise ToolError(f"this watcher may pay only with {lim['card']}")
        # Engine actions must fit a declared entry; an agent's undeclared action may be covered by a runtime grant
        # ("Allow for this watcher": that exact op and destination, this step, this version).
        granted = (bool(fits) or agent) and k.granted(profile, name, h, step, op, route) and not force_ask
        if granted and await shares_with_others(self.services, o):
            granted = False  # a shared Todoist project or a public playlist: other people see it, so ask (as in chat)
        # `fits` / declared actions keep their own rules; "ask each time" watchers still ask.
        mode = self.permissions.mode(scope, op, raw_args)
        hiding = agent and perms.hides(op, raw_args)
        many = len(id_list(o["args"])) if op in perms.REPORTABLE and op != "gmail.draft" else 0
        if mode == "report" and op in perms.REPORTABLE and op not in self.policy.require_approval:
            granted = granted or many <= perms.MAX_ITEMS
        routine = (agent and not granted and not declared and op in AGENT_ROUTINE and w["mode"] == "all"
                   and op not in self.policy.require_approval and len(id_list(o["args"])) <= AGENT_ROUTINE_MAX)
        granted = (granted or routine) and mode != "ask"
        items: list[dict] = []
        if granted and many and (mode == "report" or hiding):
            if many > perms.MAX_ITEMS:
                raise ToolError(f"at most {perms.MAX_ITEMS} emails per request; split it")
            items = await perms.email_items(self.services, id_list(o["args"]))
            if hiding:
                perms.check_hiding(op, items, id_list(o["args"]), k.clock())
        try:
            summary = await summarize(self.services, ops, self.policy)
        except ToolError:
            if not granted or mode != "report":
                raise
            summary = f"1 action(s): 1× {op}\n• {op} {many or ''}".rstrip()
        if granted:
            # (No await between these checks and the audit row that counts.)
            k.active(profile, name, hash12)
            if self.permissions.mode(scope, op, raw_args) != mode:
                raise ToolError("the owner is changing this watcher's permissions; try again")
            if many and (mode == "report" or hiding):
                if op == "gmail.trash" and k.items_today(profile, name, op) + many > perms.TRASH_PER_DAY:
                    raise ToolError(f"{perms.TRASH_PER_DAY} emails a day go to the trash without asking; leave the rest")
                if k.items_today(profile, name) + many > perms.ITEMS_PER_DAY:
                    raise ToolError("the daily limit of emails changed without asking is reached; leave the rest")
            audit_id = k.audit_add(profile, name, h, run_id, step, op, summary, idem_key, "auto", amount, many or None,
                                   [{x: i[x] for x in ("subject", "from")} for i in items], mode == "report")
            if op in WRITE_OPS:
                o["args"]["resolved"]["auto"] = True
                try:
                    await asyncio.wait_for(self._tg_lock.acquire(), 10)
                except asyncio.TimeoutError:
                    k.audit_update(audit_id, "failed", "busy")
                    raise ToolError("another Telegram message is being sent; nothing was sent, try again shortly") from None
                try:
                    ok, text, extra = await execute(self.services, ops)
                finally:
                    self._tg_lock.release()
            else:
                ok, text, extra = await execute(self.services, ops)
            k.audit_update(audit_id, "done" if ok else "failed", text)
            if ok and op == "checkout.open":
                st = await self.services.checkout_side.client.call({"cmd": "status"})
                if st.get("session"):
                    k.checkout_sessions[st["session"]] = (profile, name, site)
            if ok and w["risk"] == "high" and self.tg_store and not routine and mode != "report":
                self.tg_store.notice(f"🔴 Watcher “{name}” did: {summary.split(chr(10), 2)[min(1, summary.count(chr(10)))][:300]}")
            out = {"ok": ok, "text": text, "audit_id": audit_id, **extra} if ok else \
                {"ok": False, "error": text, "audit_id": audit_id}
            return out
        if op == "checkout.open":
            raise ToolError("opening the checkout from a watcher needs \"allow all\" or a grant for this step")
        who = f"{profile}/{name}"
        if self.approvals.watcher_pending(who) >= WATCHER_PENDING_CAP:
            raise ToolError(f"{WATCHER_PENDING_CAP} requests of this watcher are already waiting for the owner")
        if self.approvals.watcher_pending() >= ALL_WATCHERS_PENDING_CAP:
            raise ToolError(f"{ALL_WATCHERS_PENDING_CAP} watcher requests are already waiting for the owner")
        if op in WRITE_OPS:
            check_approval_size(o)
        risk = w["risk"] or "?"
        note = "" if fits else "\n(Not in the watcher's approved list: an AI step decided this.)"
        card = cards.make_card(
            ops, summary, f"watcher {name}, {'AI step' if agent else 'step'} {step}",
            "An AI step chose this after reading outside content, which could have misled it" if agent else OUTSIDE,
            ["" if fits else "Not on the list of actions you approved for this watcher"])
        summary = (f"⚠ Asked for by watcher “{name}” ({RISK_ICON.get(risk, '')} {risk} risk, step {step}) while "
                   f"handling outside content, not by you in chat.{note}\n{summary}")
        if op in ("checkout.fill", "checkout.pay"):
            with contextlib.suppress(ToolError):
                await self.services.checkout_side.client.call(
                    {"cmd": "hold", "session": o["args"]["resolved"]["session"], "until": o["args"]["resolved"]["expires"]})
        reason = clean(reason, 300) if isinstance(reason, str) and reason.strip() else None
        info = ws.WRITE_OPS.get(op)
        pinnable = bool(route) and all(v not in (None, "*") for v in route.values())
        pinnable = pinnable and mode != "ask"
        choices = ACTION_CHOICES if pinnable else ONCE_CHOICES
        approval_id = self.approvals.add(
            ops, summary, reason, kind="watch.action", choices=choices, ttl=ttl, idem_key=idem_key,
            confirm_keys=["watcher"] if pinnable and info is not None and info.level == "high" else None,
            meta={"watcher": who, "profile": profile, "name": name, "hash": h, "step": step, "op": op, "route": route}, card=card)
        if op == "telegram.send":
            note_requested(self.services, o)
        audit_id = k.audit_add(profile, name, h, run_id, step, op, summary, idem_key, "requested", amount)
        k.audit_update(audit_id, "requested", approval_id=approval_id)
        return {"ok": True, "approval": approval_id, "audit_id": audit_id, "text": (
            f"⏳ Not done yet: this needs the owner's approval (request #{approval_id}); it expires in "
            f"{int(ttl // 60)} min.\n" + summary)}

    def _expire_checkout(self) -> None:
        """Fill/pay requests are only good for 10 minutes (and their session): mark older ones expired, so a request
        the owner never answered doesn't block new ones or count as waiting."""
        self.approvals.expire()
        side = self.services.checkout_side
        now = side.clock() if side is not None else time.time()
        for row_id, ops in self.approvals.db.execute("SELECT id, ops FROM approvals WHERE status='pending'").fetchall():
            for o in json.loads(ops):
                r = o.get("args", {}).get("resolved") if o.get("op") in (
                    "checkout.fill", "checkout.pay", "checkout.submit", "checkout.flow") else None
                if isinstance(r, dict) and r.get("expires", now + 1) < now:
                    self.approvals.db.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'",
                                              (row_id,))
        self.approvals.db.commit()

    def _pending_pay(self, op: str = "checkout.pay") -> bool:
        self._expire_checkout()
        rows = self.approvals.db.execute("SELECT ops FROM approvals WHERE status='pending'").fetchall()
        return any(o["op"] == op for (ops,) in rows for o in json.loads(ops))

    def status_of(self, approval_id: int) -> dict:
        """For a CLI waiting on its own request: pending / approved / done / failed / rejected / expired, and the
        result text (never contains vault values: those ops scrub their results)."""
        self._expire_checkout()
        row = self.approvals.get(approval_id)
        if row is None:
            raise ToolError("no such request")
        return {"ok": True, "status": row["status"], "text": self._mask_obj((row.get("result") or "")[:2000], "all")}

    @staticmethod
    def _may_see(row: dict, origin: str) -> bool:
        """Social requests only for social runs (exactly), and social runs see nothing else; requests to the phone only
        for the owner's own chat (their texts say what is set there)."""
        if origin != "private" and any(f'"op": "{k}' in (row.get("ops") or "") for k in ("phone.", "checkout.flow", "checkout.submit")):
            return False
        return is_social_request(row) == (origin == SOCIAL_ORIGIN)

    def list_text(self, origin: str = "unknown") -> str:
        rows = [r for r in self.approvals.recent(100) if self._may_see(r, origin)][:10]
        if not rows:
            return "No approval requests yet."
        return self._mask_obj("\n".join(
            f"#{r['id']} {r['status']}: {r['summary'].splitlines()[0][:150]}"
            + (f"\n   result: {r['result'][:200]}" if r.get("result") else "")
            for r in rows
        ), "all")

    async def _while_there(self, request: dict, pid: int, rd: asyncio.StreamReader) -> dict | None:
        """A sign-in asks the owner on their phone: it ends when the command that wanted it is gone (None). Everything else
        runs on as before."""
        ops = request.get("ops")
        if not (isinstance(ops, list) and any(isinstance(o, dict) and str(o.get("op")).startswith("signin.") for o in ops)):
            return await self.handle(request, pid)
        work, gone = asyncio.create_task(self.handle(request, pid)), asyncio.create_task(rd.read(1))
        try:
            await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            work.cancel()
            await asyncio.wait({work})
            return None
        finally:
            for t in (work, gone):
                t.cancel()

    def _brings_file(self, request: Any) -> bool:
        ops = request.get("ops") if isinstance(request, dict) else None
        return isinstance(ops, list) and any(isinstance(o, dict) and o.get("op") == "gmail.download" for o in ops)

    async def connection(self, rd: asyncio.StreamReader, wr: asyncio.StreamWriter) -> None:
        try:
            try:
                request = json.loads(await asyncio.wait_for(rd.readline(), 10))
                if not isinstance(request, dict):
                    raise ValueError
            except (ValueError, asyncio.TimeoutError):
                response = {"ok": False, "error": "bad request"}
            else:
                pid = peer_pid(wr)
                if self._brings_file(request) and self.runs.origin_of(pid) == "private":  # (anyone else is refused at once)
                    # One at a time from fetching it until it has been taken: each needs several times its size in memory.
                    async with self._files:
                        response = await self.handle(request, pid)
                        wr.write(json.dumps(response).encode() + b"\n")
                        response = None
                        try:
                            await asyncio.wait_for(wr.drain(), 60)
                        except asyncio.TimeoutError:
                            wr.transport.abort()
                    return
                response = await (self.handle(request, pid, peer_uid(wr)) if request.get("cmd") in HUB
                                  else self._while_there(request, pid, rd))
                if response is None:
                    return
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


async def tell_tg_reader(services: Services) -> None:
    """tg-reader keeps the bridge's own bot chats unreadable: they hold social-mode text."""
    client = getattr(services.telegram_side, "client", None)
    if client is not None:
        social = any(c is not None and c.configured() for c in (services.igweb_client, services.thweb_client))
        await client.tell_own_bots(social)


async def keep_telling_tg_reader(services: Services) -> None:
    while True:
        try:
            await tell_tg_reader(services)
        except ToolError:
            pass  # not installed or not running: it reads nothing without this
        except Exception:
            log.exception("telling tg-reader the bridge's bots failed")
        await asyncio.sleep(60)


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
    # The owner's own web sessions (meta_session.py): each with its own httpx client (no shared cookie jar). Always
    # built: the running daemon picks up a session the owner saves later, or a --resume, without a restart.
    services.igweb_client = meta_session.make_client(home, meta_session.IG)
    services.thweb_client = meta_session.make_client(home, meta_session.TH)
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

    # Home location and timezone from personal info (vault-set home) win; the env settings are fallbacks.
    vault_home = Vault(home).home() if (home / "vault.json").exists() else {}
    maps_near = vault_home.get("location") or env_latlng("MAPS_NEAR")
    tz = vault_home.get("timezone") or os.environ.get("MAPS_TIMEZONE", "").strip() or "UTC"
    services.maps_client = Maps(client, maps_key, home, language=os.environ.get("MAPS_LANGUAGE", "en").strip(),
                                region=os.environ.get("MAPS_REGION", "").strip(), near=maps_near, tz=tz)
    wolt_near = env_latlng("WOLT_NEAR") or maps_near
    near = latlng(wolt_near) if wolt_near else None
    # No built-in home: each request gets the place it may use (see Daemon.places_for).
    services.wolt_client = Wolt(client, home, near=None, language=os.environ.get("WOLT_LANGUAGE", "en").strip())
    # The env settings stand in for a `home` place in personal info (same rules: private chat only).
    services.env_home = env_latlng("MAPS_NEAR") or env_latlng("WOLT_NEAR") or None
    services.env_tz = os.environ.get("MAPS_TIMEZONE", "").strip() or "UTC"
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
        for web in (services.igweb_client, services.thweb_client):
            web.auth.notify = store.notice  # one fixed-text notice when a session pauses
        services.checkout_side = CheckoutSide(CheckoutClient(), Vault(home), load_payment_rules(home / "config.toml"))
        services.phone_client = ops_phone.PhoneClient()
        services.signin_client = ops_signin.SigninClient()
        services.flows = ops_flow.Flows(home / "flows", ops_flow.Journal(home / "flows.sqlite3"))
        approvals = Approvals(home / "approvals.sqlite3")
        for approval_id in approvals.recover():
            store.notice(f"⚠️ Request #{approval_id} was approved, but the tools service stopped while running it; "
                         "the outcome is unknown. Check before asking again.")
        daemon = Daemon(services, approvals, policy, systemd_main_pid_check(unit), keeper=Keeper(home / "watch.sqlite3"),
                        permissions=Permissions(home / "permissions.sqlite3"))
        reporter = asyncio.create_task(daemon.keep_reporting())  # noqa: F841
        refresher = asyncio.create_task(refresh_meta_tokens(services))  # noqa: F841 (runs for the daemon's life)
        teller = asyncio.create_task(keep_telling_tg_reader(services))  # noqa: F841
        freeing = asyncio.create_task(ops_flow.free(services, 3))  # noqa: F841 # a session a scenario had open when the service stopped
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
        for web in (services.igweb_client, services.thweb_client):
            print(web.auth.status() + " (not contacted by check)")  # a check request is one more request Meta sees


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


def _vault_cmd(home: Path, command: str, what: str | None) -> None:
    """vault-set / vault-show / vault-clear: only the owner, on the server, as user ampitools."""
    import getpass

    vault = Vault(home)
    if command == "vault-show":
        print("\n".join(vault.masked()))
        return
    if command == "vault-clear":
        if what not in ("card", "profile", "home"):
            sys.exit("vault-clear card|profile|home")
        vault.clear(what)
        print(f"Removed the {what}.")
        return
    if what == "card":
        print("Card details (use a virtual card with its own limit). The number and CVC aren't shown as you type.")
        card = {"number": getpass.getpass("Card number: "), "exp_month": input("Expiry month (MM): "),
                "exp_year": input("Expiry year (YY or YYYY): "), "cvc": getpass.getpass("CVC: "),
                "name": input("Name on the card: ")}
        try:
            vault.set_card(card)
        except ToolError as e:
            sys.exit(f"Not saved: {e}")
        print(f"Saved: {vault.card_mask()}. Now: systemctl restart ampi-tools")
        return
    if what == "profile":
        print("Your details (Enter to skip optional ones).")
        profile = {"name": input("Full name: "), "email": input("Email: "), "phone": input("Phone (optional): "),
                   "address": {"line1": input("Street address: "), "line2": input("Line 2 (optional): "),
                               "postal": input("Postal code: "), "city": input("City: "),
                               "region": input("Region/state (optional): "), "country": input("Country (2 letters): ")}}
        try:
            vault.set_profile(profile)
        except ToolError as e:
            sys.exit(f"Not saved: {e}")
        print("Saved:\n  " + "\n  ".join(vault.masked()[:4]))
        return
    if what == "home":
        print("Home location and timezone. The server uses them (Maps and Wolt near home, schedules); Ampi never "
              "sees them. Enter to keep the current value.")
        current = vault.home()
        loc = input(f"Home location: latitude,longitude or an address [{'set' if current.get('location') else 'not set'}]: ").strip()
        if loc and not LATLNG_RE.fullmatch(loc):
            loc = _geocode(loc)
        tz = input(f"Timezone, e.g. Europe/Nicosia [{current.get('timezone', 'not set')}]: ").strip()
        try:
            vault.set_home({k: v for k, v in (("location", loc), ("timezone", tz)) if v})
        except ToolError as e:
            sys.exit(f"Not saved: {e}")
        print("Saved: " + next((x for x in vault.masked() if x.startswith("home (")), "home")
              + "\nNow: systemctl restart ampi-tools claude-assistant")
        return
    sys.exit("vault-set card|profile|home")


def _geocode(address: str) -> str:
    """One Maps lookup to turn an address into lat,lng (only when the owner enters an address in vault-set home)."""
    key = os.environ.get("MAPS_API_KEY", "").strip()
    if not key:
        sys.exit("Looking up an address needs MAPS_API_KEY in the env file; or enter latitude,longitude.")

    async def lookup() -> str:
        async with httpx.AsyncClient() as client:
            m = Maps(client, key, Path.home(), language=os.environ.get("MAPS_LANGUAGE", "en").strip(),
                     region=os.environ.get("MAPS_REGION", "").strip())
            data = await m.search(address, limit=1)
        for p in data.get("places") or []:
            loc = (p.get("location") or {}) if isinstance(p, dict) else {}
            if loc.get("latitude") is not None:
                return f"{loc['latitude']},{loc['longitude']}"
        return ""

    try:
        found = asyncio.run(lookup())
    except ToolError as e:
        sys.exit(f"Couldn't look up the address: {e}")
    if not found:
        sys.exit("Google Maps didn't find that address; try a fuller address or enter latitude,longitude.")
    print(f"Found it (Google Maps). Using those coordinates.")
    return found


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ampi-tools", description="Todoist, Gmail and YouTube for the assistant")
    parser.add_argument("command", choices=["serve", "gmail-login", "youtube-login", "threads-login",
                                            "instagram-login", "wolt-login", "vault-set", "vault-show",
                                            "vault-clear", "check", "info-show", "info-set", "info-delete",
                                            "info-default-card", "info-search", "info-reveal", "hub", "permissions-set",
                                            "instagram-session", "threads-session"])
    parser.add_argument("what", nargs="*", help="vault-set/vault-clear: card, profile or home; info-*: NAME [FIELD|on|off]")
    parser.add_argument("--write", action="store_true", help="youtube-login: also allow changes (playlists, "
                        "subscriptions), each critical one still approved by you in Telegram")
    parser.add_argument("--resume", action="store_true", help="instagram-session/threads-session: use the session "
                        "again after confirming it's you in the app")
    parser.add_argument("--forget", action="store_true", help="instagram-session/threads-session: delete the saved "
                        "session")
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
    if args.command in ("instagram-session", "threads-session"):
        service = meta_session.IG if args.command == "instagram-session" else meta_session.TH
        meta_session.session_command(home, service, resume=args.resume, forget=args.forget)
        return
    if args.command.startswith("vault-"):
        _vault_cmd(home, args.command, args.what[0] if args.what else None)
        return
    if args.command.startswith("info-"):
        from . import info_cli
        info_cli.main(home, args.command, list(args.what), _geocode)
        return
    if args.command == "permissions-set":  # the owner on the server: SCOPE OP=MODE… (the service picks it up at once)
        db = Permissions(home / "permissions.sqlite3")
        if not args.what:
            for r in db.db.execute("SELECT scope, op, mode FROM settings ORDER BY 1, 2"):
                print(*r)
            return
        scope, changes = args.what[0], {}
        for pair in args.what[1:]:
            op, _, mode = pair.partition("=")
            if not perms.SCOPE_RE.fullmatch(scope) or mode != "default" and mode not in perms.allowed_modes(scope, op, OPS, OPS):
                sys.exit(f"{scope}: {pair}: not possible")
            if db.get(scope, op) != mode:
                changes[op] = [db.get(scope, op), mode]
        db.apply(scope, changes, "on the server")
        print(f"{len(changes)} changed")
        return
    if args.command == "hub":
        from . import hub
        hub.main(home)
        return
    if args.command == "youtube-login":
        asyncio.run(_youtube_login(home, args.write))
        return
    runner = {"serve": _serve, "gmail-login": _gmail_login, "wolt-login": _wolt_login, "check": _check}[args.command]
    asyncio.run(runner(home))


if __name__ == "__main__":
    main()
