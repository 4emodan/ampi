"""The watcher engine of one profile: keeps the approved watchers in sync with ampi-tools' keeper, polls their
triggers, queues items, runs pipelines, parks runs that wait for the owner, writes run logs and status files,
and runs the builder (always on Opus)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ampi_tools import watch_spec as ws

from ..services import ServiceError, ServiceUnavailable, ask
from . import triggers as trig
from .db import FINAL, WatchDB, iso, now
from .pipeline import Active, Parked, Pipeline, RunState, Services, StepError, approval_until

log = logging.getLogger(__name__)

SYNC_EVERY = 60
PARKED_EVERY = 20
FAILS_BEFORE_FAILING = 5
TRIGGER_FAILS_BEFORE_NOTICE = 3
RETRY_DELAYS = (60, 300, 1800)
TRANSIENT = re.compile(r"usage limit|rate limit|overloaded|not reachable|timed? ?out|temporar|try again|network|"
                       r"connection|\b(?:HTTP|status)\s*5\d\d\b", re.I)
BUILDER_TOOLS = ("Read", "Glob", "Grep", "Bash(watchers:*)")
BUILDER_GUIDE = Path(__file__).with_name("BUILDER.md")
RUNS_KEEP_DAYS = 60
BUILDS_PER_DAY = 10
TESTS_PER_DAY = 30


@dataclass
class Caller:
    origin: str                 # private | group:… | forwarded:… | watcher:… | builder:<profile>/<name>
    tag: Any = None             # ("chat", chat_id, thread_id, owner_text) for private chat runs


@dataclass
class WatcherTasks:
    tasks: list[asyncio.Task] = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    trigger_failures: dict[str, int] = field(default_factory=dict)
    told: set[str] = field(default_factory=set)
    paused_until: datetime | None = None
    stop: bool = False
    worker: asyncio.Task | None = None


class WatchEngine:
    def __init__(self, bot: Any, db: WatchDB, *, tools_socket: str, tg_socket: str, data_dir: Path,
                 sandbox: Callable[[dict], Awaitable[dict]] | None = None,
                 browse: Callable[[str], Awaitable[dict]] | None = None):
        self.bot = bot
        self.profile = bot.profile
        self.db = db
        self.tools_socket = tools_socket
        self.tg_socket = tg_socket
        self.runs_dir = data_dir / "watch-runs" / self.profile.name
        self.watchers: dict[str, Active] = {}
        self.tasks: dict[str, WatcherTasks] = {}
        self.last_test: dict[str, tuple[str, str]] = {}  # name -> (snapshot hash, report)
        self.builds: dict[str, asyncio.Task] = {}
        self.slots = {"structured": asyncio.Semaphore(1), "agent": asyncio.Semaphore(1)}
        self.pipeline = Pipeline(Services(
            tools=self.tools, sandbox=sandbox or self._sandbox, claude=bot.runner.run_turn, notify=self._notify_text,
            browse=browse or self._browse, workspace=self.profile.workspace, system_prompt="",
            mark_acting=self._mark_acting), self.slots)
        self._status_dirty = True
        self._resuming: set[str] = set()
        self._synced = False
        self._sync_lock = asyncio.Lock()
        self._draining: dict[str, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        self.streak_since: dict[str, str] = {}
        self.home_timezone: str | None = None  # from personal info (vault-set home), via ampi-tools
        self.build_count: dict[str, int] = {}
        self.test_count: dict[str, int] = {}

    # --- service calls ---------------------------------------------------------------------------

    async def tools(self, payload: dict, timeout: float = 300) -> dict:
        try:
            return await ask(self.tools_socket, {**payload, "profile": payload.get("profile", self.profile.name)}
                             if payload.get("cmd", "").startswith("watch.") else payload, timeout=timeout)
        except ServiceError as e:
            return {"ok": False, "error": str(e), "unavailable": isinstance(e, ServiceUnavailable)}

    async def _ask_tg(self, payload: dict) -> dict:
        return await ask(self.tg_socket, payload, timeout=120)

    async def _sandbox(self, req: dict) -> dict:
        try:
            return await ask("/run/ampi-script.sock", req, timeout=float(req.get("timeout", 60)) + 30)
        except ServiceError as e:
            return {"ok": False, "error": f"the script sandbox isn't reachable ({e})"}

    async def _browse(self, url: str) -> dict:
        from ampi_tools.client import browser_request
        reply = await asyncio.to_thread(browser_request, {"op": "read", "url": url, "text": True, "origin": None})
        return reply or {"ok": False, "error": "the browser is busy"}

    def _social(self, name: str | None) -> bool:
        w = self.watchers.get(name or "")
        return w is not None and w.spec.social

    @staticmethod
    def _kept_error(w: Active, error: str | None) -> str | None:
        """An error as it may be stored or shown: the text of a step that read Instagram/Threads can quote messages."""
        if not error or not w.spec.social:
            return error
        m = re.match(r"step ([a-z][a-z0-9_]*):", error)
        return f"step {m.group(1)} failed (details aren't kept for a watcher that reads Instagram/Threads)" if m else error

    async def _notify_text(self, text: str, fmt: str, name: str | None = None) -> None:
        header = f"🔔 {name}\n\n" if name else ""
        try:
            await self.bot._send_text(self.profile.owner_chat_id, 0, header + text, markdown=(fmt == "markdown"),
                                      **({"social": True} if self._social(name) else {}))
        except Exception as e:  # noqa: BLE001
            if self._social(name):
                log.warning("[%s] watcher message failed: %s", self.profile.name, type(e).__name__)
                raise StepError(f"couldn't send the message ({type(e).__name__})") from None
            log.warning("[%s] watcher message failed: %s", self.profile.name, e)
            raise StepError(f"couldn't send the message: {e}") from None

    async def notify(self, text: str) -> None:
        await self.bot.notify(text)

    # --- main loop ----------------------------------------------------------------------------------

    async def run_forever(self) -> None:
        await asyncio.sleep(3)
        try:
            await self._recover()
        except Exception:
            log.exception("[%s] recovering watcher runs failed", self.profile.name)
        last_sync = 0.0
        last_parked = 0.0
        while True:
            try:
                if time.monotonic() - last_sync >= SYNC_EVERY:
                    last_sync = time.monotonic()
                    await self.sync()
                # Parked runs resume only once the approved watchers are known (else they'd look deleted).
                if self._synced and time.monotonic() - last_parked >= PARKED_EVERY:
                    last_parked = time.monotonic()
                    await self._check_parked()
                if self._status_dirty:
                    self._status_dirty = False
                    await self.write_status()
            except Exception:
                log.exception("[%s] watcher engine loop failed; continuing", self.profile.name)
            await asyncio.sleep(5)

    @property
    def timezone(self) -> str:
        """The owner's timezone: personal info if set there, else the profile's config."""
        return self.home_timezone or self.profile.timezone

    async def _refresh_timezone(self) -> None:
        resp = await self.tools({"cmd": "home.timezone"})
        if not resp.get("ok"):
            return
        tz = resp.get("timezone")
        if tz is None and self.home_timezone is not None:
            log.info("[%s] timezone removed from personal info; using the config's", self.profile.name)
            self.home_timezone = None
            return
        if isinstance(tz, str) and tz != self.home_timezone:
            try:
                from ..config import zone
                zone(tz)
            except Exception:  # noqa: BLE001
                log.warning("[%s] ignoring unknown timezone %r from personal info", self.profile.name, tz)
                return
            log.info("[%s] timezone from personal info: %s", self.profile.name, tz)
            self.home_timezone = tz

    async def sync_soon(self) -> None:
        async with self._sync_lock:
            await self.sync()

    async def sync(self) -> None:
        resp = await self.tools({"cmd": "watch.list"})
        if not resp.get("ok"):
            if not resp.get("unavailable"):
                log.warning("[%s] watch.list: %s", self.profile.name, resp.get("error"))
            return
        self._synced = True
        await self._refresh_timezone()
        seen = set()
        for info in resp.get("watchers", []):
            name, status = info.get("name"), info.get("status")
            if not isinstance(name, str) or status not in ("active", "paused", "failing"):
                continue
            seen.add(name)
            current = self.watchers.get(name)
            if current is None or current.spec.hash != info.get("hash"):
                got = await self.tools({"cmd": "watch.get", "name": name})
                if not got.get("ok"):
                    continue
                try:
                    files = {p: base64.b64decode(d) for p, d in got["files"].items()}
                    spec = ws.parse(files, expected_name=name)
                except (ws.SpecError, KeyError, ValueError) as e:
                    log.error("[%s] watcher %s: approved snapshot doesn't parse: %s", self.profile.name, name, e)
                    continue
                if spec.hash != got.get("hash"):
                    log.error("[%s] watcher %s: snapshot hash mismatch; not running it", self.profile.name, name)
                    continue
                self._stop_tasks(name)
                current = Active(self.profile.name, spec, got.get("mode", "ask"), got.get("risk", "high"),
                                 got.get("status", status), got.get("request", ""))
                self.watchers[name] = current
                self.streak_since[name] = iso(now())  # a new version starts with a clean failure count
                self._status_dirty = True
            if current.status != status:
                if status == "active":
                    self.streak_since[name] = iso(now())  # resumed: count failures from here
                current.status = status
                self._status_dirty = True
            if status == "active" and name not in self.tasks:
                self._start_tasks(current)
            elif status != "active" and name in self.tasks:
                self._stop_tasks(name)
        for name in list(self.watchers):
            if name not in seen:
                self._stop_tasks(name)
                del self.watchers[name]
                self._status_dirty = True

    def _start_tasks(self, w: Active) -> None:
        wt = WatcherTasks()
        self.tasks[w.name] = wt
        for t in w.spec.triggers:
            if t["type"] != "manual":
                wt.tasks.append(asyncio.create_task(self._poll_loop(w, t, wt), name=f"wt-poll-{w.name}"))
        previous = self._draining.pop(w.name, None)  # the old version's worker finishing its current run
        wt.worker = asyncio.create_task(self._worker(w, wt, previous), name=f"wt-work-{w.name}")

    def _stop_tasks(self, name: str) -> None:
        """Stops checking at once; a run in progress finishes first (pausing must not cut a run in half)."""
        wt = self.tasks.pop(name, None)
        if wt:
            for t in wt.tasks:
                t.cancel()
            wt.stop = True
            wt.wake.set()
            if wt.worker is not None and not wt.worker.done():
                self._draining[name] = wt.worker

    async def shutdown(self) -> None:
        workers = [wt.worker for wt in self.tasks.values() if wt.worker] + list(self._draining.values())
        for name in list(self.tasks):
            self._stop_tasks(name)
        for t in workers:
            t.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    # --- triggers -------------------------------------------------------------------------------------

    async def _poll_loop(self, w: Active, t: dict, wt: WatcherTasks) -> None:
        ident = ws.trigger_identity(t)
        interval = 30.0 if t["type"] in ("time.every", "time.at") else t["every_minutes"] * 60
        await asyncio.sleep(2)
        while True:
            try:
                state = self.db.trigger_state(self.profile.name, w.name, ident)
                items, new_state = await trig.poll(
                    t, state, ask=self._trusted_tools, ask_tg=self._ask_tg, browse=self.pipeline.svc.browse,
                    tz=self.timezone)
                if items:
                    self.db.enqueue(self.profile.name, w.name, ident, t["type"], items, t["ttl_minutes"],
                                    t["latest_only"])
                    wt.wake.set()
                self.db.set_trigger_state(self.profile.name, w.name, ident, new_state)
                if wt.trigger_failures.get(ident, 0) >= TRIGGER_FAILS_BEFORE_NOTICE:
                    await self.notify(f"✅ Watcher {w.name}: checking {ws.trigger_text(t)} works again.")
                wt.trigger_failures[ident] = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                n = wt.trigger_failures.get(ident, 0) + 1
                wt.trigger_failures[ident] = n
                log.warning("[%s] watcher %s trigger %s: %s", self.profile.name, w.name, t["type"], e)
                if n == TRIGGER_FAILS_BEFORE_NOTICE and "isn't set up yet" not in str(e):
                    await self.notify(f"⚠️ Watcher {w.name} can't check {ws.trigger_text(t)}: {str(e)[:300]}")
            await asyncio.sleep(interval)

    async def _trusted_tools(self, payload: dict) -> dict:
        resp = await self.tools(payload)
        if resp.get("unavailable"):
            raise trig.TriggerError(resp.get("error", "ampi-tools isn't reachable"))
        return resp

    # --- the worker -----------------------------------------------------------------------------------

    async def _worker(self, w: Active, wt: WatcherTasks, previous: asyncio.Task | None = None) -> None:
        if previous is not None:
            try:
                await previous
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # we are being stopped ourselves
            except Exception:  # noqa: BLE001 - the old worker's problem, logged there
                pass
        while not wt.stop:
            if wt.paused_until and now() < wt.paused_until:
                await asyncio.sleep(60)
                continue
            wt.paused_until = None
            row = self.db.next_item(self.profile.name, w.name)
            if row is None:
                wt.wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wt.wake.wait(), 30)
                continue
            try:
                await self.run_item(w, row, wt)
            except asyncio.CancelledError:
                raise
            except Exception:
                if w.spec.social:  # the exception's text could quote a message
                    log.error("[%s] watcher %s: run crashed", self.profile.name, w.name)
                else:
                    log.exception("[%s] watcher %s: run crashed", self.profile.name, w.name)
                self.db.set_item(row["id"], "failed")

    def _cap_reached(self, w: Active) -> str | None:
        today = self.db.today(self.profile.name, w.name)
        lim = w.spec.limits
        if today["runs"] >= lim["runs_per_day"]:
            return f"{lim['runs_per_day']} runs"
        if today["llm"] >= lim["llm_calls_per_day"] and any(s["type"] == "llm" for s in w.spec.steps):
            return f"{lim['llm_calls_per_day']} AI calls"
        if today["actions"] >= lim["actions_per_day"] and lim["actions_per_day"] > 0 and any(
                e["kind"] == "action" and e["write"] for e in ws.manifest(w.spec)):
            return f"{lim['actions_per_day']} actions"
        return None

    async def run_item(self, w: Active, row: dict, wt: WatcherTasks | None = None) -> str:
        cap = self._cap_reached(w)
        if cap:
            tomorrow = (now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            if wt is not None:
                wt.paused_until = tomorrow
            key = f"cap-{now().date()}"
            if wt is None or key not in wt.told:
                if wt is not None:
                    wt.told.add(key)
                await self.notify(f"⏸ Watcher {w.name} reached its daily limit ({cap}); it continues tomorrow. "
                                  f"New items wait in its queue.")
            return "capped"
        run_id = self.db.start_run(self.profile.name, w.name, w.spec.hash, row["id"], row["trigger"])
        self.db.set_item(row["id"], "running", run_id)
        st = await self._new_state(w, run_id, row["item"], row["trigger"], test=False)
        try:
            status = await self._execute(w, st, 0, None)
        except asyncio.CancelledError:
            self._cancelled(st, row)
            raise
        return await self._finish(w, st, status, row, wt)

    def _cancelled(self, st: RunState, row: dict | None) -> None:
        """The bridge is stopping in the middle of a run: requeue it if nothing was done yet, else mark it."""
        if st.actions:
            self.db.update_run(st.run_id, status="maybe", actions=st.actions,
                               error="stopped during the run; an action may have happened")
            if row:
                self.db.set_item(row["id"], "failed")
        else:
            self.db.update_run(st.run_id, status="cancelled", error="stopped before any action")
            if row:
                self.db.requeue(row["id"])

    async def _mark_acting(self, run_id: str, actions: int) -> None:
        """Called before anything that may act, so a crash right after it is never retried blindly."""
        self.db.update_run(run_id, actions=actions)

    async def _new_state(self, w: Active, run_id: str, item: Any, trigger: str, test: bool,
                         space: dict[str, bytes] | None = None) -> RunState:
        if space is None:
            got = await self.tools({"cmd": "watch.space_get", "name": w.name})
            space = {p: base64.b64decode(d) for p, d in (got.get("files") or {}).items()} if got.get("ok") else {}
        ctx = {"item": item, "steps": {},
               "run": {"id": run_id, "time": iso(now()), "trigger": trigger, "test": test,
                       "date": datetime.now(timezone.utc).astimezone().date().isoformat()},
               "watcher": {"name": w.name}}
        return RunState(run_id=run_id, test=test, ctx=ctx, space=space)

    async def _execute(self, w: Active, st: RunState, start: int, resume: dict | None) -> str:
        timeout = max(w.spec.limits["run_timeout"],
                      max((s.get("timeout", 60) for s in w.spec.steps), default=60) + 60)
        try:
            return await asyncio.wait_for(self.pipeline.run(w, st, start, resume), timeout)
        except asyncio.TimeoutError:
            st.ctx["error"] = f"the run took longer than {int(timeout)} s"
            return "failed"

    async def _push_space(self, w: Active, st: RunState) -> bool:
        if st.test or not st.space_dirty:
            return True
        changes = {p: (base64.b64encode(d).decode() if d is not None else None) for p, d in st.space_dirty.items()}
        resp = await self.tools({"cmd": "watch.space_put", "name": w.name, "hash": w.spec.hash, "run": st.run_id,
                                 "changes": changes})
        st.space_dirty.clear()
        if not resp.get("ok"):
            st.log.append({"id": "(space)", "status": "error", "error": str(resp.get("error"))[:500]})
            log.warning("[%s] watcher %s: saving space failed: %s", self.profile.name, w.name, resp.get("error"))
            return False
        return True

    async def _finish(self, w: Active, st: RunState, status: str, row: dict | None,
                      wt: WatcherTasks | None) -> str:
        if not await self._push_space(w, st) and not status.startswith("parked:"):
            # Its saved data (seen lists, last values) didn't stick: the next run would misbehave silently.
            status = "failed"
            st.ctx.setdefault("error", "saving the watcher's data failed")
        error = st.ctx.get("error")
        if status.startswith("parked:") and w.spec.social:
            status, error = "failed", "a watcher that reads Instagram/Threads can't wait for an approval"
            st.ctx["error"] = error
        if status.startswith("parked:"):
            approval = int(status.split(":", 1)[1])
            step_index = next(i for i, s in enumerate(w.spec.steps) if s["id"] == st.log[-1]["id"])
            self.db.park(st.run_id, self.profile.name, w.name, step_index, approval,
                         {"ctx": st.ctx, "log": st.log, "llm": st.llm_calls, "actions": st.actions,
                          "hash": w.spec.hash, "item_id": row["id"] if row else None},
                         approval_until())
            self.db.update_run(st.run_id, status="parked", steps_done=len(st.log), llm_calls=st.llm_calls,
                               actions=st.actions)
            if row:
                self.db.set_item(row["id"], "parked")
            self._write_run_file(w, st, "parked")
            return status
        final = status
        retry = False
        if status == "failed" and row is not None and st.actions == 0 and error and TRANSIENT.search(error):
            attempts = row.get("attempts", 0)
            if attempts < len(RETRY_DELAYS):
                retry = True
                self.db.set_item(row["id"], "queued", retry_at=now() + timedelta(seconds=RETRY_DELAYS[attempts]))
        error = self._kept_error(w, error)
        summary = self._summary(st)
        self.db.update_run(st.run_id, status="retrying" if retry else final, steps_done=len(st.log),
                           llm_calls=st.llm_calls, actions=st.actions, error=(error or "")[:1000] or None,
                           summary=summary[:500])
        if row is not None and not retry:
            self.db.set_item(row["id"], "done" if final in ("ok", "stopped") else "failed")
        self._write_run_file(w, st, final)
        if not st.test and w.risk == "high" and st.actions:
            acted = [e for e in st.log if e.get("type") in ("adapter", "script", "llm") and e.get("status") == "ok"]
            await self.notify(f"🔴 Watcher {w.name} acted (run {st.run_id}): "
                              + "; ".join(f"{e['id']}" for e in acted)[:800])
        if final == "failed" and not retry:
            streak = self.db.failed_streak(self.profile.name, w.name, self.streak_since.get(w.name))
            if streak >= FAILS_BEFORE_FAILING:
                await self.tools({"cmd": "watch.status", "name": w.name, "status": "failing"})
                await self.notify(f"⚠️ Watcher {w.name} failed {streak} times in a row and is paused. Last error: "
                                  f"{(error or '?')[:300]}\nResume it with /watcher {w.name} once it's fixed.")
                self._status_dirty = True
        return final

    def _summary(self, st: RunState) -> str:
        return ", ".join(f"{e['id']}:{e['status']}" for e in st.log)

    def _write_run_file(self, w: Active, st: RunState, status: str) -> None:
        try:
            d = self.runs_dir / w.name / st.run_id[:6]
            d.mkdir(parents=True, exist_ok=True)
            data = {"run": st.run_id, "watcher": w.name, "hash": w.spec.hash, "status": status, "test": st.test,
                    "item": st.ctx.get("item"), "error": st.ctx.get("error"), "llm_calls": st.llm_calls,
                    "actions": st.actions, "steps": st.log, "previews": st.previews}
            if w.spec.social:
                data = self._bare(data, w)
            (d / f"{st.run_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str),
                                                 encoding="utf-8")
        except OSError as e:
            log.warning("[%s] couldn't write run file: %s", self.profile.name, e)

    def _bare(self, data: dict, w: Active) -> dict:
        """A run of a watcher that reads Instagram/Threads: which steps ran and how long, never what they saw."""
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        return {**{k: data.get(k) for k in ("run", "watcher", "hash", "status", "test", "llm_calls", "actions")},
                "item": {k: item[k] for k in ("manual", "owner", "by", "time") if k in item},
                "error": self._kept_error(w, data.get("error")),
                "steps": [{k: e[k] for k in ("id", "type", "at", "status", "ms") if k in e}
                          for e in data.get("steps") or []]}

    def run_file(self, name: str, run_id: str) -> dict | None:
        if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", run_id) or not ws.NAME_RE.fullmatch(name):
            return None
        p = self.runs_dir / name / run_id[:6] / f"{run_id}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        w = self.watchers.get(name)
        return self._bare(data, w) if w is not None and w.spec.social and isinstance(data, dict) else data

    # --- parked runs ----------------------------------------------------------------------------------

    async def _check_parked(self) -> None:
        for p in self.db.parked():
            if p["profile"] != self.profile.name or p["run_id"] in self._resuming:
                continue
            resp = await self.tools({"cmd": "approvals.status", "id": p["approval"]})
            status = resp.get("status") if resp.get("ok") else None
            expired = datetime.fromisoformat(p["until"]) < now()
            if status in (None, "pending", "approved") and not expired:
                continue  # still waiting (approved = executing right now)
            if status in (None, "pending", "approved"):
                status = "expired"
            self._resuming.add(p["run_id"])
            task = asyncio.create_task(self._resume(p, {"status": status, "text": resp.get("text", "")}))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _resume(self, p: dict, result: dict) -> None:
        st: RunState | None = None
        row = None
        try:
            w = self.watchers.get(p["name"])
            saved = p["ctx"]
            row = self.db.item(saved["item_id"]) if saved.get("item_id") else None
            if w is None or w.spec.hash != saved["hash"]:
                self.db.update_run(p["run_id"], status="cancelled", error="the watcher changed while waiting")
                if row:
                    self.db.set_item(row["id"], "failed")
                self.db.unpark(p["run_id"])
                return
            st = await self._new_state(w, p["run_id"], saved["ctx"]["item"], saved["ctx"]["run"]["trigger"], False)
            st.ctx = saved["ctx"]
            st.log, st.llm_calls, st.actions = saved["log"], saved["llm"], saved["actions"]
            # Running again before it leaves the parked list, so a crash from here on is seen by _recover.
            self.db.update_run(p["run_id"], status="running")
            if row:
                self.db.set_item(row["id"], "running", p["run_id"])
            self.db.unpark(p["run_id"])
            status = await self._execute(w, st, p["step"], {"step": p["step"], "result": result})
            await self._finish(w, st, status, row, self.tasks.get(w.name))
        except asyncio.CancelledError:
            if st is not None:
                self._cancelled(st, row)
            raise
        except Exception:
            log.exception("[%s] resuming run %s failed", self.profile.name, p["run_id"])
        finally:
            self._resuming.discard(p["run_id"])

    async def _recover(self) -> None:
        """Runs that were in progress when the bridge stopped."""
        for row in self.db.recover():
            if row["profile"] != self.profile.name:
                continue
            run = self.db.run(row["run_id"]) if row.get("run_id") else None
            audit = await self.tools({"cmd": "watch.audit", "name": row["name"], "limit": 50})
            acted = [a for a in audit.get("items", []) if isinstance(a, dict)
                     and a.get("run") == row.get("run_id")] if audit.get("ok") else None
            # run["actions"] is written before every action and before agent steps that can act (whose own
            # actions the keeper can't tie to a run).
            if acted is None or acted or (run and run.get("actions")):
                if run:
                    self.db.update_run(run["id"], status="maybe", error="the bridge stopped during this run; "
                                       "an action may have happened")
                self.db.set_item(row["id"], "failed")
                await self.notify(f"⚠️ Watcher {row['name']}: the bridge restarted during run {row.get('run_id')}; "
                                  "an action may or may not have happened. It won't be repeated automatically "
                                  f"(`watchers retry {row.get('run_id')}` in chat if needed).")
            else:
                if run:
                    self.db.update_run(run["id"], status="cancelled", error="restarted before any action")
                self.db.requeue(row["id"])

    # --- manual runs, retries, tests --------------------------------------------------------------------

    def manual(self, name: str, by: str, owner: bool = False) -> str:
        """`owner`: the owner tapped Run themselves (/watchers); only then `item.owner` is true (e.g. the DM watcher
        then checks even in quiet hours)."""
        w = self.watchers.get(name)
        if w is None or w.status != "active":
            return f"{name} isn't an active watcher."
        self.db.enqueue(self.profile.name, name, "manual", "manual", [{"manual": True, "owner": owner, "by": by,
                                                                       "time": iso(now())}], 60, False)
        if name in self.tasks:
            self.tasks[name].wake.set()
        return f"Queued a run of {name}."

    def retry(self, run_id: str) -> str:
        run = self.db.run(run_id)
        if run is None or run["profile"] != self.profile.name or run["item_id"] is None:
            return "No such run."
        if run["status"] not in ("failed", "maybe", "cancelled"):
            return f"Run {run_id} ended as {run['status']}; only failed runs can be retried."
        self.db.disown(run["item_id"])  # a retry asked for in chat isn't the owner's own tap
        self.db.requeue(run["item_id"])
        if run["name"] in self.tasks:
            self.tasks[run["name"]].wake.set()
        return f"Queued {run['name']} again with the same item."

    def draft_files(self, name: str) -> dict[str, bytes]:
        """The editable copy in the workspace, plus the lib scripts it uses, as a snapshot."""
        if not ws.NAME_RE.fullmatch(name):
            raise ws.SpecError("bad watcher name")
        base = self.profile.workspace / "watchers"
        root = base / name
        # No links anywhere on the way: a linked folder or file could pull in something outside the watcher.
        for p in (base, root, root / "watcher.toml", root / "scripts", root / "prompts", root / "templates",
                  base / "lib"):
            if p.is_symlink():
                raise ws.SpecError(f"{p.relative_to(self.profile.workspace)} is a link; use plain files and folders")
        if not (root / "watcher.toml").is_file():
            raise ws.SpecError(f"watchers/{name}/watcher.toml doesn't exist")
        files: dict[str, bytes] = {"watcher.toml": (root / "watcher.toml").read_bytes()}
        for sub in ("scripts", "prompts", "templates"):
            d = root / sub
            if d.is_dir():
                for f in sorted(d.rglob("*")):
                    if f.is_symlink() or any((root / q).is_symlink() for q in f.relative_to(root).parents
                                             if str(q) != "."):
                        raise ws.SpecError(f"{f.relative_to(root)} is a link; use plain files")
                    if f.is_file():
                        rel = f.relative_to(root).as_posix()
                        ws.check_path(rel)
                        files[rel] = f.read_bytes()
        # The lib scripts it uses, copied in (the snapshot keeps them, so lib changes never touch approved watchers).
        for m in re.finditer(r'run\s*=\s*"lib/([a-z][a-z0-9_]{0,40})(?:\.py)?"', files["watcher.toml"].decode("utf-8",
                                                                                                          "replace")):
            lib = self.profile.workspace / "watchers" / "lib" / f"{m.group(1)}.py"
            if lib.is_file() and not lib.is_symlink():
                files[f"lib/{m.group(1)}.py"] = lib.read_bytes()
        return files

    async def sample_items(self, t: dict, n: int) -> list[dict]:
        kind = t["type"]
        try:
            if kind == "gmail.new":
                ids = (await self.tools({"cmd": "run", "ops": [{"op": "gmail.ids", "args": {"query": t["query"],
                                                                                         "limit": n}}]})).get("ids", [])
                if t["batch"]:
                    show = await self.tools({"cmd": "run", "ops": [{"op": "gmail.show", "args": {"ids": ids}}]})
                    return [{"ids": ids, "count": len(ids), "text": show.get("text", "")}]
                meta = await self.tools({"cmd": "run", "ops": [{"op": "gmail.meta", "args": {"ids": ids}}]})
                return [dict(m) for m in meta.get("items", [])][:n]
            if kind in ("todoist.new", "youtube.new", "threads.reply", "instagram.comment", "telegram.new",
                        "web.change"):
                items, _ = await trig.poll(t, {"seen": [], "channels": [], "last_id": 0, "hash": "", "lines": []},
                                           ask=self._trusted_tools, ask_tg=self._ask_tg,
                                           browse=self.pipeline.svc.browse, tz=self.timezone)
                return items[:n]
        except Exception as e:  # noqa: BLE001
            return [{"_sample_error": str(e)[:300]}]
        if kind == "time.at":
            return [{"slot": now().strftime("%A %Y-%m-%d %H:%M"), "timezone": self.timezone,
                     "time": iso(now())}]
        return [{"time": iso(now()), "manual": kind == "manual"}]

    async def test(self, name: str, n: int = 3) -> str:
        today = now().date().isoformat()
        if self.test_count.get(today, 0) >= TESTS_PER_DAY:
            return f"{TESTS_PER_DAY} watcher tests today is the limit (they use AI calls); try again tomorrow."
        self.test_count = {today: self.test_count.get(today, 0) + 1}
        files = self.draft_files(name)
        spec = ws.parse(files, expected_name=name)
        w = Active(self.profile.name, spec, "ask", ws.risk(ws.manifest(spec))[0], "draft")
        items = await self.sample_items(spec.triggers[0], n)
        if not items:
            items = [{}]
        current = self.watchers.get(name)
        space = None if current else {}
        lines = [f"Test of {name} (version {spec.hash[:12]}) on {len(items)} recent item(s); nothing was changed "
                 "and no messages were sent:"]
        for k, item in enumerate(items, 1):
            run_id = self.db.start_run(self.profile.name, name, spec.hash, None, spec.triggers[0]["type"], test=True)
            st = await self._new_state(w, run_id, item, spec.triggers[0]["type"], test=True, space=space)
            status = await self._execute(w, st, 0, None)
            self.db.update_run(run_id, status=status if status in FINAL else "ok", steps_done=len(st.log),
                               llm_calls=st.llm_calls, error=self._kept_error(w, st.ctx.get("error")),
                               summary=self._summary(st))
            self._write_run_file(w, st, status)
            lines.append(f"\n— item {k}: {trig.describe_item(item) or json.dumps(item, ensure_ascii=False)[:120]}")
            for e in st.log:
                detail = e.get("error") or (e.get("output") or "")
                lines.append(f"  {e['id']} [{e['status']}] {str(detail)[:300]}")
            for p in st.previews:
                lines.append(f"  ▸ {p[:1500]}")
            lines.append(f"  → {status}" + (f": {st.ctx['error']}" if st.ctx.get("error") else ""))
        report = "\n".join(lines)
        self.last_test[name] = (spec.hash, report)
        return report

    # --- proposing --------------------------------------------------------------------------------------

    async def propose(self, name: str) -> str:
        b = self.db.builder(self.profile.name, name)
        if b is None:
            return "Only a watcher started with `watchers build` can be proposed."
        files = self.draft_files(name)
        spec = ws.parse(files, expected_name=name)
        tested = self.last_test.get(name)
        if not tested or tested[0] != spec.hash:
            await self.test(name)
            tested = self.last_test[name]
        resp = await self.tools({"cmd": "watch.propose", "name": name,
                                 "files": {p: base64.b64encode(d).decode() for p, d in files.items()},
                                 "request": b["request"], "requester": b["requester"], "test_text": tested[1][:6000]},
                                timeout=120)
        if not resp.get("ok"):
            return f"Not proposed: {resp.get('error')}"
        return (f"Proposed {name} (version {spec.hash[:12]}, risk {resp.get('risk')}). The owner got the request "
                f"with buttons (#{resp.get('approval')}). Tell them briefly what it does; don't propose again "
                "unless they ask for changes.")

    # --- the builder -------------------------------------------------------------------------------------

    def start_build(self, name: str, text: str, caller: Caller, change: bool) -> str:
        if not ws.NAME_RE.fullmatch(name):
            return "The name must be lowercase letters, digits, - or _ (up to 40)."
        if any(not t.done() for t in self.builds.values()):
            return "The builder is already working on another watcher; one at a time. Wait for its report."
        today = now().date().isoformat()
        if self.build_count.get(today, 0) >= BUILDS_PER_DAY:
            return f"{BUILDS_PER_DAY} watcher builds today is the limit; try again tomorrow."
        tag = caller.tag
        if not (isinstance(tag, tuple) and len(tag) == 5 and tag[0] == "chat"):
            return "Watchers can only be built from the owner's private chat."
        _, _, chat_id, thread_id, owner_text = tag
        existing = self.db.builder(self.profile.name, name)
        if change and existing is None and name not in self.watchers:
            return f"There is no watcher {name} to change."
        if not change and (existing is not None or name in self.watchers):
            return f"{name} exists already; use `watchers build {name} --change \"...\"`."
        if existing is None:
            import uuid
            self.db.set_builder(self.profile.name, name, str(uuid.uuid4()), owner_text[:4000],
                                f"you in chat ({'topic ' + str(thread_id) if thread_id else 'main chat'})",
                                chat_id, thread_id)
        else:
            self.db.set_builder(self.profile.name, name, existing["session_id"], existing["request"],
                                existing["requester"], chat_id, thread_id)
        (self.profile.workspace / "watchers" / name).mkdir(parents=True, exist_ok=True)
        self.build_count = {today: self.build_count.get(today, 0) + 1}
        self.builds[name] = asyncio.create_task(self._build(name, text, owner_text, change, chat_id, thread_id))
        return (f"The watcher builder (Opus) is working on {name}. It reports back here and then the owner gets the "
                "proposal with buttons. Nothing runs before the owner approves.")

    async def _build(self, name: str, text: str, owner_text: str, change: bool, chat_id: int, thread_id: int) -> None:
        b = self.db.builder(self.profile.name, name)
        assert b is not None
        guide = BUILDER_GUIDE.read_text(encoding="utf-8") if BUILDER_GUIDE.is_file() else ""
        lib = self._lib_catalog()
        tools = (*BUILDER_TOOLS, f"Edit(./watchers/{name}/**)", f"Write(./watchers/{name}/**)")
        if change:
            prompt = (f"The owner wants a change to the watcher `{name}`.\nTheir words: {owner_text}\n"
                      f"Ampi's summary: {text}\n\nUpdate the files in watchers/{name}/, validate, test, propose.")
        else:
            prompt = (f"Build the watcher `{name}`.\nThe owner's request (their own words): {owner_text}\n"
                      f"Ampi's summary of what they want: {text}\n\nWrite the files in watchers/{name}/, then run "
                      f"`watchers validate {name}`, `watchers test {name}` and `watchers propose {name}`.")
        system = guide.replace("{LIB_CATALOG}", lib).replace("{NAME}", name)
        try:
            result = await self.bot.runner.run_turn(
                prompt, cwd=self.profile.workspace, session_id=b["session_id"], started=bool(b["started"]),
                model="opus", allowed_tools=tools, append_system_prompt=system,
                origin=None, tag=("builder", self.profile.name, name), watchers_edit=True)
            if result.ok or result.text:
                self.db.builder_started(self.profile.name, name)
            reply = (result.text or "").strip() or (result.error or "no answer")
            prefix = "🛠 Watcher builder" + ("" if result.ok else " (failed)")
            await self.bot._send_text(chat_id, thread_id, f"{prefix} · {name}\n\n{reply}", markdown=True)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] builder for %s failed", self.profile.name, name)
            with contextlib.suppress(Exception):
                await self.bot._send_text(chat_id, thread_id, f"🛠 Watcher builder · {name} failed: {e}", markdown=False)

    def _lib_catalog(self) -> str:
        d = self.profile.workspace / "watchers" / "lib"
        out = []
        for f in sorted(d.glob("*.py")) if d.is_dir() else []:
            doc = f.read_text(encoding="utf-8", errors="replace").split('"""')
            out.append(f"### lib/{f.stem}\n{doc[1].strip() if len(doc) > 2 else '(no description)'}")
        return "\n\n".join(out) or "(no shared helpers installed)"

    # --- status files -------------------------------------------------------------------------------------

    async def write_status(self) -> None:
        resp = await self.tools({"cmd": "watch.list"})
        if not resp.get("ok"):
            return
        root = self.profile.workspace / "watchers"
        root.mkdir(parents=True, exist_ok=True)
        lines = ["# Watchers", "", "Generated by the bridge; don't edit. Details: `watchers show <name>`.", ""]
        for info in sorted(resp.get("watchers", []), key=lambda x: (x.get("status") != "active", x.get("name", ""))):
            name = info.get("name", "?")
            w = self.watchers.get(name)
            today = self.db.today(self.profile.name, name)
            icon = ws.RISK_ICON.get(info.get("risk", ""), "")
            when = "; ".join(ws.trigger_text(t) for t in w.spec.triggers) if w else ""
            lines.append(f"- **{name}** {icon} {info.get('status')} · {info.get('mode')} · {when} · today "
                         f"{today['runs']} runs, {today['failed']} failed — {info.get('description', '')}")
            if ws.NAME_RE.fullmatch(name):
                d = root / name
                if d.is_dir():
                    self._write_watcher_status(d, info, w)
        try:
            (root / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            log.warning("couldn't write watchers/INDEX.md: %s", e)

    def _write_watcher_status(self, d: Path, info: dict, w: Active | None) -> None:
        name = info["name"]
        lines = [f"# {name}", "", "Generated by the bridge; don't edit.", "",
                 f"- Status: {info.get('status')} ({info.get('mode')}), risk {info.get('risk')}",
                 f"- Approved version: {str(info.get('hash', ''))[:12]} on {info.get('approved', '?')}",
                 f"- Original request: {info.get('request', '')}"]
        try:
            draft = ws.snapshot_hash(self.draft_files(name))
            if draft != info.get("hash"):
                lines.append("- ⚠ The files here differ from the approved version (not running until approved).")
        except (ws.SpecError, OSError):
            pass
        if w:
            lines.append("")
            lines.append(ws.describe(w.spec, ws.manifest(w.spec)))
        lines += ["", "## Recent runs", ""]
        for r in self.db.runs(self.profile.name, name, 10):
            lines.append(f"- {r['started']} {r['id']} [{r['status']}] {r.get('summary') or ''}"[:300])
        with contextlib.suppress(OSError):
            (d / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def mark_dirty(self) -> None:
        self._status_dirty = True
