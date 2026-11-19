"""Watchers: the bridge checks Gmail, Todoist, Telegram chats or the clock on its own, and lets Claude
act on what's new (instructions in <workspace>/watchers/<name>.md).

The first check only records what already exists, so a new watcher doesn't flood you with the backlog.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .config import DAYS, Tools, Watcher, zone
from .services import ServiceError, ask

if TYPE_CHECKING:
    from .bot import ProfileBot

log = logging.getLogger(__name__)
Ask = Callable[[str, dict], Awaitable[dict]]
SEEN_MAX = 2000
MAX_ITEMS = 50  # per run; the rest are handled on the next check
FAILS_BEFORE_NOTICE = 3
LATE_LIMIT = timedelta(hours=3)  # a scheduled run missed by more than this (server down) is skipped


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

    @property
    def interval(self) -> float:
        return 30.0 if self.w.kind == "schedule" else self.w.every_minutes * 60

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
        try:
            prompt, new_state = await self.check(state)
        except ServiceError as e:
            await self._failed(str(e))
            return
        if self.failures >= FAILS_BEFORE_NOTICE:
            await self.bot.notify(f"✅ Watcher {self.w.name} works again.")
        self.failures = 0
        store.set_watch_state(profile, self.w.name, new_state)
        if prompt:
            await self.bot.run_watcher(self.w, prompt)

    async def _failed(self, error: str) -> None:
        self.failures += 1
        log.warning("[%s] watcher %s: %s", self.bot.profile.name, self.w.name, error)
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
        return await self._listing(state)

    # --- Gmail / Todoist: new ids in a search or filter ---------------------------------

    async def _listing(self, state: dict | None) -> tuple[str | None, dict]:
        kind = self.w.kind
        args = {"query": self.w.query, "limit": 200} if kind == "gmail" else {"filter": self.w.query, "limit": 500}
        resp = await self._tool(self.tools.socket, {"cmd": "run", "ops": [{"op": f"{kind}.ids", "args": args}]})
        ids: list[str] = resp.get("ids", [])
        if state is None:
            return None, {"seen": ids[:SEEN_MAX]}
        seen = set(state.get("seen", []))
        new = [i for i in ids if i not in seen][:MAX_ITEMS]
        if not new:
            return None, state
        show = {"ids": new} if kind == "gmail" else {"ids": new, "filter": self.w.query}
        listing = await self._tool(self.tools.socket, {"cmd": "run", "ops": [{"op": f"{kind}.show", "args": show}]})
        merged = (new + [i for i in state.get("seen", []) if i not in new])[:SEEN_MAX]
        what = "email(s)" if kind == "gmail" else "task(s)"
        prompt = (
            f'Watcher "{self.w.name}": {len(new)} new {what} in {kind} matching `{self.w.query}`:\n\n'
            f"{listing.get('text', '')}"
        )
        return prompt, {"seen": merged}

    # --- Telegram: messages after the last seen id ---------------------------------------

    async def _telegram(self, state: dict | None) -> tuple[str | None, dict]:
        after = state.get("last_id") if state else None
        resp = await self._tool(self.tools.tg_reader_socket, {"cmd": "since", "chat": self.w.query, "after_id": after})
        new_state = {"last_id": resp.get("last_id", after)}
        if after is None or not resp.get("count"):
            return None, new_state
        prompt = (
            f'Watcher "{self.w.name}": {resp["count"]} new message(s) in the Telegram chat '
            f"\"{resp.get('chat', self.w.query)}\":\n\n{resp.get('text', '')}"
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
            return None, state or {}
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
