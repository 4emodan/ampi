"""The socket behind the `watchers` command. Who calls is decided from the bridge's own process tree (the claude run
the calling process descends from), never from what the request says:

- the owner's private chat runs: everything except `propose`;
- the builder run of watcher X: read commands, `validate X`, `test X`, `propose X`;
- a social-mode run of the owner's chat: only `run instagram-dms` (`instagram dms`: start the DM watcher, whose
  summary goes to the owner, never to the run);
- anything else (groups, forwarded messages, watcher runs, unknown processes): refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import struct
from dataclasses import dataclass
from typing import Any

from ampi_tools import watch_spec as ws

from .engine import Caller, WatchEngine

log = logging.getLogger(__name__)

READ_CMDS = {"list", "show", "runs", "logs", "space", "audit", "lib", "help"}
PRIVATE_CMDS = READ_CMDS | {"validate", "test", "build", "pause", "run", "retry"}
# The builder reads real items while testing and could be misled by them: it sees only its own watcher, never run
# logs, saved data or the audit of any watcher.
BUILDER_CMDS = {"list", "lib", "help", "show", "validate", "test", "propose"}
DM_WATCHER = "instagram-dms"
SOCIAL_RUN_GAP = 600  # seconds between two DM-watcher runs started from social mode


def peer_pid(sock: socket.socket) -> int:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[0]
    except (AttributeError, OSError):
        return -1


@dataclass
class Who:
    kind: str                # private | builder | social
    profile: str
    name: str | None = None  # builder: its watcher
    caller: Caller | None = None


class Control:
    def __init__(self, engines: dict[str, WatchEngine], path: str):
        self.engines = engines
        self.path = path
        self.callers: dict[int, Caller] = {}
        self._social_runs: dict[str, float] = {}  # profile -> when social mode last started the DM watcher
        self._server: asyncio.AbstractServer | None = None

    # --- run registry (fed by the claude runner) -------------------------------------------------------

    async def on_spawn(self, pid: int, origin: str, tag: Any, limited: bool = False) -> None:
        if isinstance(tag, tuple) and tag and tag[0] == "builder":
            origin = f"builder:{tag[1]}/{tag[2]}"
        self.callers[pid] = Caller(origin, tag, limited)

    async def on_exit(self, pid: int) -> None:
        self.callers.pop(pid, None)

    def caller_of(self, pid: int) -> Caller | None:
        seen = 0
        while pid > 1 and seen < 64:
            if pid in self.callers:
                return self.callers[pid]
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                    stat = f.read()
                pid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                return None
            seen += 1
        return None

    def who(self, caller: Caller | None) -> Who | None:
        if caller is None:
            return None
        tag = caller.tag
        if caller.origin == "private" and isinstance(tag, tuple) and len(tag) == 5 and tag[0] == "chat":
            return Who("private", tag[1], caller=caller)
        if caller.origin.startswith("builder:") and isinstance(tag, tuple) and len(tag) == 3:
            return Who("builder", tag[1], tag[2], caller)
        if caller.origin == "forwarded:social" and isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "social":
            return Who("social", tag[1], caller=caller)
        return None

    # --- server ------------------------------------------------------------------------------------------

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        old = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(self._handle, self.path, limit=1024 * 1024)
        finally:
            os.umask(old)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                req = json.loads(await asyncio.wait_for(reader.readline(), 10))
            except (ValueError, asyncio.TimeoutError):
                return
            pid = peer_pid(writer.get_extra_info("socket"))
            reply = await self.dispatch(req if isinstance(req, dict) else {}, self.who(self.caller_of(pid)))
            writer.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except Exception:
            log.exception("watchers command failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def dispatch(self, req: dict, who: Who | None) -> dict:
        cmd = req.get("cmd")
        if who is None:
            return {"ok": False, "text": "The watchers command works only in the owner's private chat (or the "
                                         "watcher builder); not from groups, watchers or forwarded content."}
        if who.kind == "social" and (cmd != "run" or req.get("name") != DM_WATCHER):
            return {"ok": False, "text": f"In social mode only `instagram dms` (the {DM_WATCHER} watcher) can be "
                                         "started here."}
        allowed = PRIVATE_CMDS if who.kind == "private" else {"run"} if who.kind == "social" else BUILDER_CMDS
        if cmd not in allowed:
            return {"ok": False, "text": f"`watchers {cmd}` isn't available here."}
        if who.kind == "private" and cmd in ("build", "run", "retry") and who.caller is not None and who.caller.limited:
            return {"ok": False, "text": f"The owner limited this topic: `watchers {cmd}` isn't available here (a watcher acts with "
                                         "its own permissions)."}
        engine = self.engines.get(who.profile)
        if engine is None:
            return {"ok": False, "text": "No watcher engine for this profile."}
        name = req.get("name")
        if name is not None and (not isinstance(name, str) or not ws.NAME_RE.fullmatch(name)):
            return {"ok": False, "text": "Bad watcher name."}
        if who.kind == "builder" and cmd in ("show", "validate", "test", "propose") and name != who.name:
            return {"ok": False, "text": f"The builder of {who.name} can only work on {who.name}."}
        try:
            return {"ok": True, "text": await self._run(engine, cmd, req, who)}
        except ws.SpecError as e:
            return {"ok": False, "text": f"Invalid: {e}"}

    async def _run(self, e: WatchEngine, cmd: str, req: dict, who: Who) -> str:
        name = req.get("name")
        if cmd == "help":
            return HELP
        if cmd == "list":
            return await list_text(e)
        if cmd == "lib":
            return e._lib_catalog()
        if cmd == "show":
            return await show_text(e, name)
        if cmd == "runs":
            runs = e.db.runs(e.profile.name, name, int(req.get("limit") or 15))
            return "\n".join(f"{r['started']} {r['id']} [{r['status']}]{' (test)' if r['test'] else ''} "
                             f"{r.get('summary') or ''} {('· ' + r['error']) if r.get('error') else ''}"[:400]
                             for r in runs) or "No runs yet."
        if cmd == "logs":
            run_id = str(req.get("run") or "")
            run = e.db.run(run_id)
            if run is None or run["profile"] != e.profile.name:
                return "No such run."
            data = e.run_file(run["name"], run_id)
            return json.dumps(data, ensure_ascii=False, indent=1)[:30000] if data else "The run file is gone."
        if cmd == "space":
            w = e.watchers.get(name)
            if w is not None and w.spec.social:
                return "The saved data of a watcher that reads Instagram/Threads isn't shown."
            got = await e.tools({"cmd": "watch.space_get", "name": name})
            if not got.get("ok"):
                return f"No space: {got.get('error')}"
            import base64
            out = []
            for p, d in sorted((got.get("files") or {}).items()):
                raw = base64.b64decode(d)
                out.append(f"## {p} ({len(raw)} bytes)\n{raw[:3000].decode('utf-8', 'replace')}")
            return "\n\n".join(out) or "The space is empty."
        if cmd == "audit":
            got = await e.tools({"cmd": "watch.audit", "name": name, "limit": int(req.get("limit") or 20)})
            rows = got.get("items", []) if got.get("ok") else []
            return "\n".join(json.dumps(r, ensure_ascii=False)[:400] for r in rows) or "No actions recorded."
        if cmd == "validate":
            spec = ws.parse(e.draft_files(name), expected_name=name)
            entries = ws.manifest(spec)
            return f"Valid (version {spec.hash[:12]}).\n" + ws.describe(spec, entries)
        if cmd == "test":
            return await e.test(name, max(1, min(int(req.get("items") or 3), 5)))
        if cmd == "propose":
            return await e.propose(name)
        if cmd == "build":
            text = str(req.get("text") or "").strip()
            if not text or len(text) > 4000:
                return "Say what the watcher should do (up to 4000 characters)."
            return e.start_build(name, text, who.caller, bool(req.get("change")))
        if cmd == "pause":
            resp = await e.tools({"cmd": "watch.status", "name": name, "status": "paused"})
            e.mark_dirty()
            return f"Paused {name}." if resp.get("ok") else f"Couldn't pause: {resp.get('error')}"
        if cmd == "run" and who.kind == "social":
            # A run reading strangers' posts could be talked into starting it over and over: once per 10 minutes,
            # never while a run is waiting, and quiet hours apply (owner=False).
            import time
            if e.db.busy_count(e.profile.name, name):
                return f"A run of {name} is already queued or running; its summary will come to the owner."
            last = self._social_runs.get(e.profile.name)
            if last is not None and time.monotonic() - last < SOCIAL_RUN_GAP:
                return f"{name} was started from here less than 10 minutes ago; not again yet."
            text = e.manual(name, "Ampi in social mode")
            if text.startswith("Queued "):  # a refused start doesn't count
                self._social_runs[e.profile.name] = time.monotonic()
            return text
        if cmd == "run":
            return e.manual(name, "Ampi in chat")
        if cmd == "retry":
            return e.retry(str(req.get("run") or ""))
        return "Unknown command."


async def list_text(e: WatchEngine) -> str:
    resp = await e.tools({"cmd": "watch.list"})
    if not resp.get("ok"):
        return f"Can't list watchers: {resp.get('error')}"
    rows = []
    for info in sorted(resp.get("watchers", []), key=lambda x: x.get("name", "")):
        if info.get("status") == "deleted":
            continue
        name = info.get("name", "?")
        w = e.watchers.get(name)
        today = e.db.today(e.profile.name, name)
        when = "; ".join(ws.trigger_text(t) for t in w.spec.triggers) if w else ""
        rows.append(f"{ws.RISK_ICON.get(info.get('risk', ''), '')} {name} — {info.get('status')}"
                    f"{' (asks each time)' if info.get('mode') == 'ask' else ''} · {when} · today {today['runs']} "
                    f"runs, {today['failed']} failed, {e.db.queued_count(e.profile.name, name)} queued\n"
                    f"   {info.get('description', '')}")
    return "\n".join(rows) or "No watchers yet."


async def show_text(e: WatchEngine, name: str | None) -> str:
    if not name:
        return "Which watcher?"
    resp = await e.tools({"cmd": "watch.list"})
    info = next((x for x in resp.get("watchers", []) if x.get("name") == name), None) if resp.get("ok") else None
    w = e.watchers.get(name)
    lines = [f"{name}"]
    if info:
        lines += [f"Status: {info.get('status')} · mode: {info.get('mode')} · risk: {info.get('risk')} · version "
                  f"{str(info.get('hash', ''))[:12]}", f"Request: {info.get('request', '')}",
                  f"Created {info.get('created', '?')}, approved {info.get('approved', '?')}"]
    else:
        lines.append("Not approved yet (or unknown).")
    if w:
        lines.append(ws.describe(w.spec, ws.manifest(w.spec)))
    today = e.db.today(e.profile.name, name)
    lines.append(f"Today: {today['runs']} runs, {today['llm']} AI calls, {today['actions']} actions, "
                 f"{today['failed']} failed; {e.db.queued_count(e.profile.name, name)} queued")
    return "\n".join(lines)


HELP = """watchers list                         all watchers, status, risk, today's runs
watchers show NAME                     what a watcher does, its permissions and limits
watchers runs NAME [--limit N]         recent runs
watchers logs RUN_ID                   every step of one run (inputs, outputs, errors)
watchers space NAME                    the watcher's saved data
watchers audit NAME                    actions it took (from ampi-tools)
watchers lib                           shared helper scripts
watchers build NAME "what it should do"           (owner's chat) start the builder (Opus)
watchers build NAME --change "what to change"     (owner's chat) change an existing watcher
watchers validate NAME / test NAME [--items N]    check the files in watchers/NAME/ / dry run on recent items
watchers propose NAME                  (builder only) send the owner the approval request
watchers pause NAME / run NAME / retry RUN_ID     (owner's chat) pause / run now / retry a failed run
Resuming, deleting and approving are buttons for the owner (/watchers, /watcher NAME)."""
