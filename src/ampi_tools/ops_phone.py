"""The owner's phone (companion app, docs/COMPANION.md): notifications, alarms and timers (registered into ops.OPS).

Who may ask (check_phone, called by Daemon.run): the owner's private chat; group and forwarded-message runs only with
the owner's approval and never to read; nothing else. Cancelling an alarm asks the owner too, unless it was set less
than an hour ago. Times are fixed (a date, a moment) before anyone approves.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .ops import FIELDS, OPS, PRE_RESOLVE, Op, Result, Services, clean, clean_block, need
from .todoist import ToolError

SOCKET = "/run/ampi-phone.sock"
READS = frozenset({"phone.status", "phone.alarms"})
WRITES = frozenset({"phone.notify", "phone.alarm", "phone.timer", "phone.cancel"})
APPROVAL_TTL = 600
RECENT = 3600  # an alarm set less than this ago may be cancelled without asking
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALARM_ID = re.compile(r"[0-9a-f]{8}")
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
UNKNOWN = ("no answer from the phone: it may or may not have happened. Check with `phone alarms` before trying again "
           "(setting the same alarm twice makes only one)")


class PhoneClient:
    """One JSON line to the gateway's socket, one JSON line back."""

    def __init__(self, path: str = SOCKET, timeout: float = 20):
        self.path, self.timeout = path, timeout

    async def call(self, req: dict) -> dict:
        try:
            rd, wr = await asyncio.wait_for(asyncio.open_unix_connection(self.path, limit=1024 * 1024), 5)
        except (OSError, AttributeError, asyncio.TimeoutError):
            raise ToolError("no phone is paired (docs/SETUP.md, companion app)") from None
        try:
            wr.write(json.dumps(req).encode() + b"\n")
            await wr.drain()
            line = await asyncio.wait_for(rd.readline(), self.timeout)
            resp = json.loads(line)
        except (asyncio.TimeoutError, OSError):
            raise ToolError(UNKNOWN) from None
        except ValueError:
            raise ToolError("the phone gateway sent a broken reply") from None
        finally:
            wr.close()
        if not isinstance(resp, dict):
            raise ToolError("the phone gateway sent a broken reply")
        return resp


def check_phone(ops: list[dict], origin: str) -> None:
    """Refuses phone operations from anyone who may not ask for them."""
    phone = [o["op"] for o in ops if o["op"].startswith("phone.")]
    if not phone:
        return
    if origin.startswith("group:") or (origin.startswith("forwarded:") and origin != "forwarded:social"):
        if any(op not in WRITES for op in phone):
            raise ToolError("the phone's state is only for the owner's own chat")
    elif origin != "private":
        raise ToolError("the phone is only for the owner's own chat (watchers report in Telegram)")
    if len(ops) > 1:
        raise ToolError("one phone action per request")


def asks_owner(o: dict, origin: str) -> bool:
    return origin != "private" or (o["op"] == "phone.cancel" and not o["args"]["resolved"].get("recent"))


# --- arguments ----------------------------------------------------------------------------------


def _line(limit: int):
    def check(v: Any) -> str:
        if not isinstance(v, str):
            raise ToolError("expected text")
        s = clean(v).strip()
        if len(s) > limit:
            raise ToolError(f"at most {limit} characters")
        return s
    return check


def _lines(limit: int):
    def check(v: Any) -> str:
        if not isinstance(v, str):
            raise ToolError("expected text")
        s = "\n".join(clean(part).rstrip() for part in v.replace("\r\n", "\n").split("\n")).strip()
        if len(s) > limit:
            raise ToolError(f"at most {limit} characters")
        return s
    return check


def _clock(v: Any) -> str:
    s = v.strip() if isinstance(v, str) else ""
    if re.fullmatch(r"\d:[0-5]\d", s):
        s = "0" + s
    if not TIME_RE.fullmatch(s):
        raise ToolError("the time must be HH:MM, e.g. 07:30")
    return s


def _weekdays(v: Any) -> list[str]:
    if not isinstance(v, list) or not v or any(d not in WEEKDAYS for d in v):
        raise ToolError("days must be from mon, tue, wed, thu, fri, sat, sun")
    return [d for d in WEEKDAYS if d in v]


def _seconds(v: Any) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 24 * 3600:
        raise ToolError("a timer is 1 second to 24 hours")
    return v


def _alarm_id(v: Any) -> str:
    if not isinstance(v, str) or not ALARM_ID.fullmatch(v):
        raise ToolError("not an alarm id (see `phone alarms`)")
    return v


FIELDS.update({"heading": _line(80), "note": _lines(1000), "clock": _clock, "weekdays": _weekdays, "seconds": _seconds,
               "tag": _line(60), "alarm": _alarm_id})


# --- talking to the phone -----------------------------------------------------------------------


def client(s: Services) -> Any:
    if s.phone_client is None:
        raise ToolError("no phone is paired (docs/SETUP.md, companion app)")
    return s.phone_client


def _ago(ts: Any, now: float) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return "never since the gateway started"
    m = max(0, int((now - ts) // 60))
    return "just now" if m < 1 else f"{m} min ago" if m < 120 else f"{m // 60} h ago" if m < 48 * 60 else f"{m // 1440} days ago"


async def ask(s: Services, kind: str, body: dict) -> tuple[dict, dict]:
    """(the phone's data, what it said about itself); ToolError when it didn't happen or nobody knows."""
    r = await client(s).call({"cmd": "ask", "kind": kind, "body": body})
    if not r.get("ok"):
        code = r.get("code")
        if code == "offline":
            if r.get("paired") is False:
                raise ToolError("no phone is paired (docs/SETUP.md, companion app)")
            raise ToolError(f"the phone isn't reachable (last contact: {_ago(r.get('last_seen'), time.time())}); nothing was "
                            "done there. Tell the owner here instead.")
        if code == "unknown":
            raise ToolError(UNKNOWN)
        raise ToolError(f"phone: {clean(str(r.get('error') or 'failed'), 200)}")
    a = r.get("answer") if isinstance(r.get("answer"), dict) else {}
    if a.get("ok") is not True:
        raise ToolError(f"the phone refused: {clean(str(a.get('error') or 'no reason given'), 200)}")
    data, hello = a.get("data"), r.get("hello")
    return data if isinstance(data, dict) else {}, hello if isinstance(hello, dict) else {}


def zone_of(name: Any) -> ZoneInfo | timezone:
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_+\-/]{1,60}", name) and ".." not in name:
        try:
            return ZoneInfo(name)
        except Exception:  # unknown zone, or a path that isn't one
            pass
    return timezone.utc


async def phone_zone(s: Services) -> tuple[ZoneInfo | timezone, str]:
    data, hello = await ask(s, "status", {})
    name = data.get("zone") or hello.get("zone")
    return zone_of(name), clean(str(name or "UTC"), 60)


def _when(ms: Any, zone: Any) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or not 0 < ms < 4e12:
        return "?"
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%a %d %b %H:%M")


def _warnings(data: dict) -> str:
    warn = data.get("warn")
    return "".join(f"\n⚠ {clean(w, 160)}" for w in warn[:6] if isinstance(w, str)) if isinstance(warn, list) else ""


def _label(text: Any) -> str:
    t = clean(str(text or ""), 60)
    return f" “{t}”" if t else ""


# --- operations ---------------------------------------------------------------------------------


async def ph_status(s: Services, a: dict) -> Result:
    r = await client(s).call({"cmd": "status"})
    devices = r.get("devices") if isinstance(r.get("devices"), list) else []
    if not devices:
        return "No phone is paired (docs/SETUP.md, companion app)."
    lines = []
    for d in devices[:5]:
        hello = d.get("hello") if isinstance(d.get("hello"), dict) else {}
        state = "connected" if d.get("connected") else f"not reachable, last contact {_ago(d.get('last_seen'), time.time())}"
        lines.append(f"{clean(str(d.get('name')), 40)}: {state}"
                     + (f" · {clean(str(hello.get('model')), 60)}" if hello.get("model") else "")
                     + (f" · {clean(str(hello.get('zone')), 60)}" if hello.get("zone") else "") + _warnings(hello))
    return "\n".join(lines)


async def ph_notify(s: Services, a: dict) -> Result:
    title, = need(a, "heading")
    data, _ = await ask(s, "notify", {"title": title, "text": a.get("note", "")})
    return "Shown on the phone." + _warnings(data)


async def _resolve_alarm(s: Services, a: dict) -> dict:
    clock, = need(a, "clock")
    if a.get("date") and a.get("weekdays"):
        raise ToolError("give a date or days, not both")
    zone, name = await phone_zone(s)
    now = datetime.now(zone)
    r: dict[str, Any] = {"time": clock, "label": a.get("tag", ""), "zone": name}
    if a.get("weekdays"):
        r["days"] = a["weekdays"]
        r["text"] = f"every {', '.join(r['days'])} at {clock}"
        return {"resolved": r}
    if a.get("date"):
        try:
            day = datetime.strptime(a["date"], "%Y-%m-%d").date()
        except ValueError:
            raise ToolError("no such date") from None
        if not now.date() <= day <= now.date() + timedelta(days=366):
            raise ToolError("the date must be within the next year")
    else:
        h, m = map(int, clock.split(":"))
        today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        day = now.date() if today > now else now.date() + timedelta(days=1)
    r["date"] = day.isoformat()
    r["text"] = f"{day:%a %d %b} at {clock}"
    return {"resolved": r}


async def ph_alarm(s: Services, a: dict) -> Result:
    r = a["resolved"]
    body = {k: r[k] for k in ("time", "date", "days", "label") if r.get(k)}
    data, hello = await ask(s, "alarm.set", body)
    zone = zone_of(data.get("zone") or hello.get("zone"))
    return (f"{'That alarm was already set' if data.get('existed') is True else 'Alarm set'}: next "
            f"{_when(data.get('at'), zone)} ({clean(str(data.get('zone') or ''), 60)})"
            + (f", {r['text']}" if r.get("days") else "") + f"{_label(r.get('label'))} [{clean(str(data.get('id')), 8)}]"
            + _warnings(data))


async def _resolve_timer(s: Services, a: dict) -> dict:
    seconds, = need(a, "seconds")
    return {"resolved": {"end": time.time() + seconds, "seconds": seconds, "label": a.get("tag", "")}}


def _duration(n: int) -> str:
    h, m, sec = n // 3600, n % 3600 // 60, n % 60
    return " ".join(f"{v} {u}" for v, u in ((h, "h"), (m, "min"), (sec, "s")) if v)


async def ph_timer(s: Services, a: dict) -> Result:
    r = a["resolved"]
    if r["end"] <= time.time():
        raise ToolError("that timer would already be over; nothing was set")
    data, hello = await ask(s, "timer.set", {"end": r["end"], "label": r["label"]})
    zone = zone_of(data.get("zone") or hello.get("zone"))
    return (f"Timer set: {_duration(r['seconds'])}, rings {_when(data.get('at'), zone)}{_label(r['label'])} "
            f"[{clean(str(data.get('id')), 8)}]" + _warnings(data))


def _alarm_rows(data: dict) -> list[dict]:
    rows = data.get("alarms")
    return [x for x in rows if isinstance(x, dict) and isinstance(x.get("id"), str) and ALARM_ID.fullmatch(x["id"])][:60] \
        if isinstance(rows, list) else []


def _alarm_text(x: dict, zone: Any) -> str:
    days = [d for d in x.get("days") or [] if d in WEEKDAYS] if isinstance(x.get("days"), list) else []
    kind = "timer" if x.get("kind") == "timer" else "alarm"
    return (f"{kind} {_when(x.get('at'), zone)}" + (f" (every {', '.join(days)})" if days else "")
            + (" (snoozed)" if x.get("snoozed") is True else "") + _label(x.get("label")))


async def ph_alarms(s: Services, a: dict) -> Result:
    data, hello = await ask(s, "alarm.list", {})
    zone = zone_of(data.get("zone") or hello.get("zone"))
    lines = [f"[{x['id']}] {_alarm_text(x, zone)}" for x in _alarm_rows(data)] or ["No alarms from Ampi on the phone."]
    events = data.get("events")
    if isinstance(events, list) and events:
        lines.append("Recently:")
        lines += [f"  {_when(e.get('ts'), zone)}: {clean(str(e.get('ev')), 40)}{_label(e.get('label'))}"
                  for e in events[-15:] if isinstance(e, dict)]
    lines.append("(Alarms set in the phone's own Clock app aren't listed.)")
    return "\n".join(lines)


async def _resolve_cancel(s: Services, a: dict) -> dict:
    aid, = need(a, "alarm")
    data, hello = await ask(s, "alarm.list", {})
    row = next((x for x in _alarm_rows(data) if x["id"] == aid), None)
    if row is None:
        raise ToolError(f"no alarm {aid} on the phone (see `phone alarms`)")
    created = row.get("created")
    age = time.time() - created / 1000 if isinstance(created, (int, float)) and not isinstance(created, bool) else 1e9
    return {"resolved": {"id": aid, "text": _alarm_text(row, zone_of(data.get("zone") or hello.get("zone"))),
                         "recent": 0 <= age < RECENT}}


async def ph_cancel(s: Services, a: dict) -> Result:
    r = a["resolved"]
    await ask(s, "alarm.cancel", {"id": r["id"]})
    return f"Cancelled: {r['text']}"


async def describe_notify(s: Services, a: dict) -> str:
    return f"Show on your phone: “{clean(a.get('heading', ''))}”" + (f"\n{clean_block(a['note'])}" if a.get("note") else "")


async def describe_alarm(s: Services, a: dict) -> str:
    r = a["resolved"]
    return f"Set an alarm on your phone: {r['text']} ({r['zone']}){_label(r.get('label'))}"


async def describe_timer(s: Services, a: dict) -> str:
    r = a["resolved"]
    at = datetime.fromtimestamp(r["end"], timezone.utc).strftime("%H:%M:%S UTC")
    return f"Set a timer on your phone: {_duration(r['seconds'])} from the request (until {at}){_label(r['label'])}"


async def describe_cancel(s: Services, a: dict) -> str:
    return f"Cancel on your phone: {a['resolved']['text']}"


PHONE_OPS = [
    Op("phone.status", False, ph_status),
    Op("phone.notify", False, ph_notify, describe_notify, "Phone notification"),
    Op("phone.alarm", False, ph_alarm, describe_alarm, "Set phone alarm"),
    Op("phone.timer", False, ph_timer, describe_timer, "Set phone timer"),
    Op("phone.alarms", False, ph_alarms),
    Op("phone.cancel", False, ph_cancel, describe_cancel, "Cancel phone alarm"),
]
OPS.update({op.name: op for op in PHONE_OPS})
PRE_RESOLVE.update({"phone.alarm": _resolve_alarm, "phone.timer": _resolve_timer, "phone.cancel": _resolve_cancel})
