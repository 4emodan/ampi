"""Triggers: each poll returns (new items, new memory). The first poll of a trigger only records what exists.

Memory formats match the old watchers' (seen / last / last_id+peer_id / seen+channels), so their state carries over.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ampi_tools.watch_spec import DAYS

from ..config import zone

SEEN_MAX = 2000
MAX_ITEMS = 50          # per poll; the rest come next time
GMAIL_PER_POLL = 20     # one run per email: fewer per check
REGEX_TIMEOUT = 10
LATE_LIMIT = timedelta(hours=3)
WEB_TEXT_MAX = 60_000

Ask = Callable[[dict], Awaitable[dict]]          # one ampi-tools request (trusted bridge path)
AskTg = Callable[[dict], Awaitable[dict]]        # one tg-reader request
Browse = Callable[[str], Awaitable[dict]]        # read a page's text: {"ok", "text"} / {"ok": False, "error"}


class TriggerError(Exception):
    pass


async def _run(ask: Ask, op: str, args: dict) -> dict:
    resp = await ask({"cmd": "run", "ops": [{"op": op, "args": args}]})
    if not resp.get("ok"):
        raise TriggerError(str(resp.get("error") or f"{op} failed"))
    return resp


def _fresh(ids: list[str], state: dict | None, cap: int = MAX_ITEMS) -> tuple[list[str], dict | None]:
    """(new ids, new memory) or ([], baseline memory) on the first poll. Ids over the cap stay unseen and come next
    time."""
    if state is None:
        return [], {"seen": ids[:SEEN_MAX]}
    seen = set(state.get("seen", []))
    new = list(dict.fromkeys(i for i in ids if i not in seen))[:cap]
    merged = (new + [i for i in state.get("seen", []) if i not in new])[:SEEN_MAX]
    return new, {**state, "seen": merged}


async def poll(t: dict, state: dict | None, *, ask: Ask, ask_tg: AskTg, browse: Browse, tz: str,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> tuple[list[dict], dict]:
    kind = t["type"]
    if kind == "time.every":
        return _every(t, state, clock())
    if kind == "time.at":
        return _at(t, state, clock(), tz)
    if kind == "manual":
        return [], state or {}
    if kind == "gmail.new":
        resp = await _run(ask, "gmail.ids", {"query": t["query"], "limit": 200})
        # Anyone can email: one run per email is capped harder, so a flood can't become many runs at once.
        new, mem = _fresh([i for i in resp.get("ids", []) if isinstance(i, str)], state,
                          MAX_ITEMS if t["batch"] else GMAIL_PER_POLL)
        if not new:
            return [], mem
        if t["batch"]:
            show = await _run(ask, "gmail.show", {"ids": new})
            return [{"ids": new, "count": len(new), "text": show.get("text", "")}], mem
        meta = await _run(ask, "gmail.meta", {"ids": new})
        by_id = {m.get("id"): m for m in meta.get("items", []) if isinstance(m, dict)}
        return [{"id": i, **{k: v for k, v in by_id.get(i, {}).items() if k != "id"}} for i in new], mem
    if kind == "todoist.new":
        resp = await _run(ask, "todoist.ids", {"filter": t["filter"], "limit": 500})
        new, mem = _fresh([i for i in resp.get("ids", []) if isinstance(i, str)], state)
        if not new:
            return [], mem
        if t["batch"]:
            show = await _run(ask, "todoist.show", {"ids": new, "filter": t["filter"]})
            return [{"ids": new, "count": len(new), "text": show.get("text", "")}], mem
        items = []
        for i in new:
            show = await _run(ask, "todoist.show", {"ids": [i], "filter": t["filter"]})
            items.append({"id": i, "text": show.get("text", "")})
        return items, mem
    if kind == "telegram.new":
        after = (state or {}).get("last_id")
        chat = t["chat"] if t["chat"].startswith("@") else (state or {}).get("peer_id") or t["chat"]
        resp = await ask_tg({"cmd": "since", "chat": chat, "after_id": after})
        if not resp.get("ok", True) and resp.get("error"):
            raise TriggerError(str(resp["error"]))
        mem = {"last_id": resp.get("last_id", after), "peer_id": resp.get("peer_id") or (state or {}).get("peer_id")}
        if after is None or not resp.get("count"):
            return [], mem
        return [{"chat": resp.get("chat", t["chat"]), "count": resp["count"], "text": resp.get("text", ""),
                 "more": bool(resp.get("more"))}], mem
    if kind == "youtube.new":
        resp = await _run(ask, "youtube.ids", {"source": t["channels"]})
        pairs = [(p[0], p[1]) for p in resp.get("pairs", [])
                 if isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p)]
        ok = [c for c in resp.get("channels_ok", []) if isinstance(c, str)]
        if state is None:
            return [], {"seen": [v for v, _ in pairs][:SEEN_MAX], "channels": ok[:SEEN_MAX]}
        old_seen = list(state.get("seen", []))
        seen = set(old_seen)
        known = list(state.get("channels", []))
        fresh = set(ok) - set(known)  # channels read for the first time: their uploads are backlog, not news
        absorbed = [v for v, c in pairs if c in fresh and v not in seen]
        new = list(dict.fromkeys(v for v, c in pairs if c not in fresh and v not in seen))[:MAX_ITEMS]
        mem = {"seen": list(dict.fromkeys(new + absorbed + old_seen))[:SEEN_MAX],
               "channels": list(dict.fromkeys(known + ok))[-SEEN_MAX:]}
        if not new:
            return [], mem
        channel_of = dict(pairs)
        if t["batch"]:
            show = await _run(ask, "youtube.show", {"ids": new})
            return [{"ids": new, "count": len(new), "text": show.get("text", "")}], mem
        items = []
        for v in new:
            show = await _run(ask, "youtube.show", {"ids": [v]})
            items.append({"id": v, "channel": channel_of.get(v, ""), "text": show.get("text", "")})
        return items, mem
    if kind in ("threads.reply", "instagram.comment"):
        op = "threads" if kind == "threads.reply" else "instagram"
        query = "replies" if op == "threads" else "comments"
        resp = await _run(ask, f"{op}.ids", {"query": query})
        new, mem = _fresh([i for i in resp.get("ids", []) if isinstance(i, str)], state)
        if not new:
            return [], mem
        new = new[:30]  # strangers write these: a flood mustn't become many runs
        show = await _run(ask, f"{op}.show", {"ids": new})
        if t["batch"]:
            return [{"ids": new, "count": len(new), "text": show.get("text", "")}], mem
        return [{"id": i, "text": show.get("text", "")} for i in new][:10], mem
    if kind == "web.change":
        page = await browse(t["url"])
        if not page.get("ok"):
            raise TriggerError(str(page.get("error") or "the page couldn't be read"))
        text = str(page.get("text", ""))[:WEB_TEXT_MAX]
        # The page is written by others and the patterns could backtrack badly: filter off the event loop, with a
        # time limit (a stuck pattern then fails this check instead of freezing the bridge).
        try:
            lines = await asyncio.wait_for(asyncio.to_thread(_filter_lines, text, t.get("ignore", [])), REGEX_TIMEOUT)
        except asyncio.TimeoutError:
            raise TriggerError("the ignore patterns took too long on this page") from None
        digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        mem = {"hash": digest, "lines": lines[:3000]}
        if state is None:
            return [], mem
        if state.get("hash") == digest:
            return [], state
        old = set(state.get("lines", []))
        new_set = set(lines)
        added = [line for line in lines if line not in old][:200]
        removed = [line for line in state.get("lines", []) if line not in new_set][:200]
        return [{"url": t["url"], "added": added, "removed": removed, "text": "\n".join(lines)[:20000]}], mem
    raise TriggerError(f"unknown trigger {kind}")


def _filter_lines(text: str, ignore: list[str]) -> list[str]:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return [line for line in lines if line and not any(re.search(p, line, re.I) for p in ignore)]


def _every(t: dict, state: dict | None, now: datetime) -> tuple[list[dict], dict]:
    nxt = (state or {}).get("next")
    period = timedelta(minutes=t["minutes"])
    if nxt is None:
        return [], {"next": (now + period).isoformat(timespec="seconds")}
    due = datetime.fromisoformat(nxt)
    if now < due:
        return [], state or {}
    # Missed ticks (server down) collapse into one.
    following = due + period * (int((now - due) / period) + 1)
    return [{"time": now.isoformat(timespec="seconds")}], {"next": following.isoformat(timespec="seconds")}


def _at(t: dict, state: dict | None, now_utc: datetime, tz_name: str) -> tuple[list[dict], dict]:
    tz = zone(tz_name)
    now = now_utc.astimezone(tz)
    due = None
    for day_offset in (1, 0):  # yesterday's late slots, then today's
        day = (now - timedelta(days=day_offset)).date()
        if DAYS[day.weekday()] not in t["days"]:
            continue
        for slot_text in t["at"]:
            hh, mm = map(int, slot_text.split(":"))
            slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if slot <= now:
                due = slot
    if due is None:
        return [], dict(state or {})
    key = due.strftime("%Y-%m-%d %H:%M")
    if state is None or state.get("last", "") >= key:
        return [], {"last": max(key, (state or {}).get("last", ""))}
    if now - due > LATE_LIMIT:
        return [], {"last": key}
    return [{"slot": due.strftime("%A %Y-%m-%d %H:%M"), "timezone": tz_name,
             "time": now.isoformat(timespec="seconds")}], {"last": key}


def describe_item(item: Any) -> str:
    """One line for logs and run lists."""
    if not isinstance(item, dict):
        return ""
    for k in ("subject", "slot", "url", "chat", "id", "time"):
        if item.get(k):
            return f"{k}: {str(item[k])[:80]}"
    if item.get("count"):
        return f"{item['count']} item(s)"
    return ""
