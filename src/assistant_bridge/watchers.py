"""Watchers: the bridge checks Gmail, Todoist, Telegram chats, YouTube channels or the clock on its own, and lets Claude
act on what's new (instructions in <workspace>/watchers/<name>.md).

- The first check (and the first after the query changes) only records what already exists, so a new
  watcher doesn't flood the owner with the backlog.
- New items count as handled only after Claude's run succeeded; a failing run is retried on the next
  check, and after several failures in a row the owner is told once and the batch is skipped.
- Content from outside (emails, messages) is wrapped in a block with a random tag, and the watcher
  prompt tells Claude it is data, not instructions.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .config import DAYS, Tools, Watcher, zone
from .services import ServiceError, ask

if TYPE_CHECKING:
    from .bot import ProfileBot

log = logging.getLogger(__name__)
Ask = Callable[..., Awaitable[dict]]
SEEN_MAX = 2000
MAX_ITEMS = 50  # per run; the rest are handled on the next check
FAILS_BEFORE_NOTICE = 3
RUN_FAILS_BEFORE_SKIP = 5
QUICK_FAILURE_MS = 60_000  # a run that failed faster than this most likely did nothing yet
LATE_LIMIT = timedelta(hours=3)  # a scheduled run missed by more than this (server down) is skipped
SOCIAL = {"threads": "replies", "instagram": "comments"}  # watcher kinds where strangers write the items
SOCIAL_FLOOD = 30
SOCIAL_RUNS_PER_DAY = 24


def untrusted(text: str) -> str:
    tag = f"untrusted-{secrets.token_hex(4)}"
    return f"<{tag}>\n{text.replace(tag, '')}\n</{tag}>"


class Watch:
    def __init__(
        self,
        bot: "ProfileBot",
        watcher: Watcher,
        tools: Tools,
        ask_fn: Ask = ask,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.bot = bot
        self.w = watcher
        self.tools = tools
        self.ask = ask_fn
        self.clock = clock
        self.failures = 0
        self.run_failures = 0
        self.streak_noticed = False

    @property
    def interval(self) -> float:
        return 30.0 if self.w.kind == "schedule" else self.w.every_minutes * 60

    @property
    def source(self) -> str:
        """What the state belongs to; if the config changes, the state starts over."""
        return f"{self.w.kind}:{self.w.query}"

    async def run_forever(self) -> None:
        await asyncio.sleep(5)  # let the bot connect first
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("[%s] watcher %s crashed; will retry", self.bot.profile.name, self.w.name)
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        store, profile = self.bot.store, self.bot.profile.name
        state = store.get_watch_state(profile, self.w.name)
        if state is not None and state.get("source") != self.source:
            state = None  # the watcher now looks at something else: start from a fresh baseline
        try:
            prompt, new_state = await self.check(state)
        except ServiceError as e:
            await self._failed(str(e))
            return
        if self.failures >= FAILS_BEFORE_NOTICE:
            await self.bot.notify(f"✅ Watcher {self.w.name} works again.")
        self.failures = 0
        new_state["source"] = self.source
        if not prompt:
            store.set_watch_state(profile, self.w.name, new_state)
            return
        if self.w.kind == "schedule":  # a briefing is never repeated, even if the run fails
            store.set_watch_state(profile, self.w.name, new_state)
        if self.run_failures:
            prompt += ("\n\n(A previous attempt at these items failed partway. Check what was already done, "
                       "e.g. tasks added, labels set or notes written, before acting, so nothing is done twice.)")
        result = await self.bot.run_watcher(self.w, prompt, quiet_errors=True)
        if result.ok or result.cancelled:  # the owner stopped it: don't run the same batch again
            if self.streak_noticed and result.ok:
                await self.bot.notify(f"✅ Watcher {self.w.name} works again.")
            self.run_failures = 0
            self.streak_noticed = False
            store.set_watch_state(profile, self.w.name, new_state)
            return
        self.run_failures += 1
        error = result.error or "error"
        # Retry only failures that probably happened before any work was done (usage limit, network,
        # login): a run that worked for a while may have already acted, and repeating it duplicates that.
        quick = not result.timed_out and (result.duration_ms or 0) < QUICK_FAILURE_MS
        skip = self.w.kind == "schedule" or not quick or self.run_failures >= RUN_FAILS_BEFORE_SKIP
        # While the assistant keeps failing, the owner hears about it once, not on every batch.
        if not self.streak_noticed and (skip or self.run_failures >= FAILS_BEFORE_NOTICE):
            self.streak_noticed = True
            what = f" Skipped those items (they're in chats/watch-{self.w.name}/)." if skip and self.w.kind != "schedule" else ""
            await self.bot.notify(f"⚠️ Watcher {self.w.name}: the assistant's run failed ({error}).{what} "
                                  "No more notices until it works again.")
        if skip:
            store.set_watch_state(profile, self.w.name, new_state)
            self.run_failures = 0

    async def _failed(self, error: str) -> None:
        self.failures += 1
        log.warning("[%s] watcher %s: %s", self.bot.profile.name, self.w.name, error)
        if "isn't set up yet" in error:
            return  # an account not connected yet isn't news; the watcher starts working once it is
        if self.failures == FAILS_BEFORE_NOTICE:
            await self.bot.notify(f"⚠️ Watcher {self.w.name} can't check {self.w.kind}: {error}")

    async def _tool(self, socket: str, payload: dict) -> dict:
        resp = await self.ask(socket, payload)
        if not resp.get("ok"):
            raise ServiceError(resp.get("error", "failed"))
        return resp

    async def check(self, state: dict | None) -> tuple[str | None, dict]:
        """Returns (prompt for Claude or None, new state)."""
        if self.w.kind == "schedule":
            return self._schedule(state)
        if self.w.kind == "telegram":
            return await self._telegram(state)
        if self.w.kind == "youtube":
            return await self._youtube(state)
        return await self._listing(state)

    # --- YouTube: new uploads; channels not read before are taken in quietly ----------------

    async def _youtube(self, state: dict | None) -> tuple[str | None, dict]:
        resp = await self._tool(self.tools.socket, {"cmd": "run", "ops": [
            {"op": "youtube.ids", "args": {"source": self.w.query}}]})
        pairs = [(p[0], p[1]) for p in resp.get("pairs", [])
                 if isinstance(p, list) and len(p) == 2 and isinstance(p[0], str) and isinstance(p[1], str)]
        ok = [c for c in resp.get("channels_ok", []) if isinstance(c, str)]
        if state is None:
            return None, {"seen": [v for v, _ in pairs][:SEEN_MAX], "channels": ok[:SEEN_MAX]}
        old_seen = list(state.get("seen", []))
        seen = set(old_seen)
        known = list(state.get("channels", []))
        # A channel read for the first time (a new subscription, or one whose feed failed until now): its
        # recent uploads are the backlog, not news. Channels whose feed failed this time stay unknown.
        fresh = set(ok) - set(known)
        absorbed = [v for v, c in pairs if c in fresh and v not in seen]
        new = list(dict.fromkeys(v for v, c in pairs if c not in fresh and v not in seen))[:MAX_ITEMS]
        channels = list(dict.fromkeys(known + ok))[-SEEN_MAX:]
        # New and absorbed ids go first, so the size cap only ever drops the oldest ones.
        new_state = {"seen": list(dict.fromkeys(new + absorbed + old_seen))[:SEEN_MAX], "channels": channels}
        if not new:
            return None, new_state
        listing = await self._tool(self.tools.socket, {"cmd": "run", "ops": [
            {"op": "youtube.show", "args": {"ids": new}}]})
        failed = resp.get("failed") or 0
        note = f"\n({failed} channel(s) couldn't be checked this time.)" if failed else ""
        prompt = (
            f'Watcher "{self.w.name}": {len(new)} new YouTube upload(s) from `{self.w.query}`:\n\n'
            + untrusted(listing.get("text", "")) + note
        )
        return prompt, new_state

    # --- Gmail / Todoist: new ids in a search or filter ---------------------------------

    async def _listing(self, state: dict | None) -> tuple[str | None, dict]:
        kind = self.w.kind
        social = kind in SOCIAL
        if kind == "gmail":
            args = {"query": self.w.query, "limit": 200}
        elif social:
            args = {"query": self.w.query}
        else:
            args = {"filter": self.w.query, "limit": 500}
        resp = await self._tool(self.tools.socket, {"cmd": "run", "ops": [{"op": f"{kind}.ids", "args": args}]})
        ids = [i for i in resp.get("ids", []) if isinstance(i, str)]
        if state is None:
            return None, {"seen": ids[:SEEN_MAX]}
        seen = set(state.get("seen", []))
        fresh = list(dict.fromkeys(i for i in ids if i not in seen))
        new = fresh[:MAX_ITEMS]
        if not new:
            return None, dict(state)
        what = {"gmail": "email(s)", "todoist": "task(s)"}.get(kind) or SOCIAL[kind]
        if social:
            # Anyone can write replies and comments: a flood (or a busy day) mustn't turn into many Claude runs.
            today = self.clock().strftime("%Y-%m-%d")
            runs = state.get("runs", {}) if state.get("runs", {}).get("day") == today else {"day": today, "n": 0}
            if len(fresh) > SOCIAL_FLOOD or runs["n"] >= SOCIAL_RUNS_PER_DAY:
                merged = (fresh + [i for i in state.get("seen", []) if i not in fresh])[:SEEN_MAX]
                flood = len(fresh) > SOCIAL_FLOOD
                why = "too many to go through here" if flood else "the watcher's daily limit is reached"
                told = runs.get("told", [])
                reason = "flood" if flood else "limit"
                if reason not in told:  # one notice per reason a day, not one per check
                    await self.bot.notify(f"🔔 {self.w.name}: {len(fresh)} new {what} on {kind.capitalize()} ({why}; "
                                          "have a look in the app; no more notices about this today).")
                    runs = {**runs, "told": told + [reason]}
                return None, {**state, "seen": merged, "runs": runs}
            runs = {**runs, "day": today, "n": runs["n"] + 1}
        show = {"ids": new} if kind == "gmail" or social else {"ids": new, "filter": self.w.query}
        listing = await self._tool(self.tools.socket, {"cmd": "run", "ops": [{"op": f"{kind}.show", "args": show}]})
        merged = (new + [i for i in state.get("seen", []) if i not in new])[:SEEN_MAX]
        where = f"on {kind.capitalize()}" if social else f"in {kind} matching `{self.w.query}`"
        prompt = (
            f'Watcher "{self.w.name}": {len(new)} new {what} {where}:\n\n'
            + untrusted(listing.get("text", ""))
        )
        return prompt, {"seen": merged, **({"runs": runs} if social else {})}

    # --- Telegram: messages after the last seen id, oldest first -------------------------

    async def _telegram(self, state: dict | None) -> tuple[str | None, dict]:
        after = state.get("last_id") if state else None
        # Once known, the chat is followed by its id, so renaming it (or a new chat with a similar
        # name) can't switch the watcher to another chat.
        # (A public @username stays the key: such chats aren't in the chat list, so their id can't be
        # looked up there; the reader caches the username, so repeating it costs nothing.)
        chat = self.w.query if self.w.query.startswith("@") else (state or {}).get("peer_id") or self.w.query
        resp = await self._tool(self.tools.tg_reader_socket, {"cmd": "since", "chat": chat, "after_id": after})
        new_state = {"last_id": resp.get("last_id", after), "peer_id": resp.get("peer_id") or (state or {}).get("peer_id")}
        if after is None or not resp.get("count"):
            return None, new_state
        more = "\n(More new messages follow in the next run.)" if resp.get("more") else ""
        prompt = (
            f'Watcher "{self.w.name}": {resp["count"]} new message(s) in the Telegram chat '
            f"\"{resp.get('chat', self.w.query)}\", oldest first:\n\n" + untrusted(resp.get("text", "")) + more
        )
        return prompt, new_state

    # --- Schedule ------------------------------------------------------------------------

    def _schedule(self, state: dict | None) -> tuple[str | None, dict]:
        tz = zone(self.bot.profile.timezone)
        now = self.clock().astimezone(tz)
        due = None
        for day_offset in (1, 0):  # yesterday's late slots, then today's
            day = (now - timedelta(days=day_offset)).date()
            if DAYS[day.weekday()] not in self.w.days:
                continue
            for t in sorted(self.w.at):
                hh, mm = map(int, t.split(":"))
                slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
                if slot <= now:
                    due = slot
        if due is None:
            return None, dict(state or {})
        key = due.strftime("%Y-%m-%d %H:%M")
        if state is None or state.get("last", "") >= key:
            return None, {"last": max(key, (state or {}).get("last", ""))}
        if now - due > LATE_LIMIT:
            return None, {"last": key}
        prompt = (
            f'Scheduled run "{self.w.name}" for {due.strftime("%A %Y-%m-%d %H:%M")} '
            f"({self.bot.profile.timezone}); it is now {now.strftime('%H:%M')}."
        )
        return prompt, {"last": key}
