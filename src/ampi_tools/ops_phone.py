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
from datetime import date, datetime, timedelta, timezone
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
UNKNOWN = ("no answer from the phone: it may or may not have happened. Check with `phone alarms` before trying again")
GONE = "no phone is paired (docs/SETUP.md, companion app)"
# The phone answers with codes; only these words reach Ampi (never text the phone sends).
REFUSALS = {
    "past": "that time has passed", "bad_time": "it didn't understand the time", "bad_date": "the date must be within a year",
    "too_long": "a timer is at most 24 hours", "too_late": "the request reached it too late", "limit_day": "its limit of 20 new "
    "alarms a day", "limit_alarms": "its limit of 50 alarms", "limit_hour": "its limit of 30 notifications an hour",
    "no_such_alarm": "there is no such alarm (see `phone alarms`)", "ringing": "it is ringing right now",
    "gone_again": "it was set before and is gone again (see `phone alarms`)", "notifications_off": "notifications are switched "
    "off for Ampi on the phone", "not_shown": "Android didn't show it", "unknown_kind": "the app on the phone is too old",
    "bad_request": "it didn't understand the request",
}
WARNINGS = {
    "volume_0": "the alarm volume on the phone is 0", "total_silence": "Do Not Disturb is on total silence",
    "notifications_off": "notifications are off for Ampi on the phone", "alarm_channel_off": "the alarm notification is "
    "switched off on the phone", "no_full_screen": "the alarm screen may not open over the lock screen",
    "no_exact_alarms": "Android doesn't let the app set exact alarms", "battery": "Android may stop the phone's connection "
    "(battery setting; the app shows how to fix it)", "hibernation": "Android may take the app's permissions away if it "
    "isn't opened for months",
}
EVENTS = {
    "set": "set", "cancelled": "cancelled by you", "rang": "rang", "stopped": "stopped by the owner", "snoozed": "snoozed",
    "missed_unanswered": "missed (nobody stopped it)", "missed_off": "missed (phone off, or the clock or zone changed)",
    "stopped_by_android": "stopped ringing (the app was ended)", "rang_notification_only": "rang only as a notification",
    "undone": "undone by the owner", "deleted": "deleted by the owner", "restored": "your cancelling was undone",
}


class PhoneClient:
    """One JSON line to the gateway's socket, one JSON line back."""

    def __init__(self, path: str = SOCKET, timeout: float = 20):
        self.path, self.timeout = path, timeout

    async def call(self, req: dict) -> dict:
        try:
            rd, wr = await asyncio.wait_for(asyncio.open_unix_connection(self.path, limit=1024 * 1024), 5)
        except (FileNotFoundError, ConnectionRefusedError, AttributeError):
            raise ToolError(GONE) from None
        except (OSError, asyncio.TimeoutError):
            raise ToolError("the phone gateway can't be reached; nothing was done") from None
        try:
            wr.write(json.dumps(req).encode() + b"\n")
            await wr.drain()
            line = await asyncio.wait_for(rd.readline(), self.timeout)
            if not line:  # it stopped while answering
                raise ToolError(UNKNOWN)
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
        # Cancelling needs an id from the list, which they can't see either.
        if any(op not in WRITES or op == "phone.cancel" for op in phone):
            raise ToolError("the phone's state is only for the owner's own chat")
    elif origin != "private":
        raise ToolError("the phone is only for the owner's own chat (watchers report in Telegram)")
    if len(ops) > 1:
        raise ToolError("one phone action per request")


def defer(ops: list[dict], origin: str) -> None:
    """Asked for by a group or a forwarded message: nothing may reach the phone (or come back from it) before the owner
    approves, so the resolvers below must not ask it anything."""
    if origin != "private":
        for o in ops:
            if o["op"] in WRITES:
                o["args"]["deferred"] = True


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
        raise ToolError(GONE)
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
                raise ToolError(GONE)
            raise ToolError(f"the phone isn't reachable (last contact: {_ago(r.get('last_seen'), time.time())}); nothing was "
                            "done there. Tell the owner here instead.")
        if code == "unknown":
            raise ToolError(UNKNOWN)
        raise ToolError("too many requests to the phone lately; nothing was done, try later" if code == "limit"
                        else "the phone gateway refused the request; nothing was done")
    a = r.get("answer") if isinstance(r.get("answer"), dict) else {}
    if a.get("ok") is not True:
        if a.get("error") == "failed":  # something broke there, perhaps after it was done
            raise ToolError("the phone had a problem: " + UNKNOWN.split(": ", 1)[1])
        raise ToolError(f"the phone refused: {REFUSALS.get(str(a.get('error')), 'no reason given')}; nothing was done")
    data, hello = a.get("data"), r.get("hello")
    return data if isinstance(data, dict) else {}, hello if isinstance(hello, dict) else {}


def zone_of(name: Any) -> ZoneInfo | timezone | None:
    """None: unknown here. Nothing may then be guessed from it."""
    if not isinstance(name, str):
        return None
    m = re.fullmatch(r"GMT([+-])(\d{2}):(\d{2})", name)  # Java's name for a zone set by hand
    if m:
        delta = timedelta(hours=int(m[2]), minutes=int(m[3]))
        return timezone(delta if m[1] == "+" else -delta, name) if delta < timedelta(hours=24) else None
    if re.fullmatch(r"[A-Za-z0-9_+\-/]{1,60}", name) and ".." not in name:
        try:
            return ZoneInfo(name)
        except Exception:  # unknown zone, or a path that isn't one
            pass
    return None


async def phone_zone(s: Services) -> tuple[ZoneInfo | timezone, str]:
    data, hello = await ask(s, "status", {})
    name = data.get("zone") or hello.get("zone")
    zone = zone_of(name)
    if zone is None:
        raise ToolError("the phone's time zone isn't known here; give --date as well")
    return zone, str(zone)


def _when(ms: Any, zone: Any) -> str:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or not 0 < ms < 4e12:
        return "?"
    if zone is None:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%a %d %b %H:%M UTC")
    return datetime.fromtimestamp(ms / 1000, zone).strftime("%a %d %b %H:%M")


def _warnings(data: dict) -> str:
    warn = data.get("warn")
    if not isinstance(warn, list):
        return ""
    return "".join(f"\n⚠ {w}" for w in dict.fromkeys(WARNINGS.get(w) for w in warn[:10] if isinstance(w, str)) if w)


def _label(text: Any) -> str:
    t = clean(text, 60) if isinstance(text, str) else ""
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
                     + (f" · {zone}" if (zone := zone_of(hello.get("zone"))) else "") + _warnings(hello))
    return "\n".join(lines)


async def ph_notify(s: Services, a: dict) -> Result:
    title, = need(a, "heading")
    data, _ = await ask(s, "notify", {"title": title, "text": a.get("note", "")})
    return "Shown on the phone." + _warnings(data)


async def _resolve_alarm(s: Services, a: dict) -> dict:
    clock, = need(a, "clock")
    if a.get("date") and a.get("weekdays"):
        raise ToolError("give a date or days, not both")
    r: dict[str, Any] = {"time": clock, "label": a.get("tag", "")}
    if a.get("weekdays"):
        r["days"] = a["weekdays"]
        r["text"] = f"every {', '.join(r['days'])} at {clock}"
    elif a.get("date"):
        try:
            day = date.fromisoformat(a["date"])
        except ValueError:
            raise ToolError("no such date") from None
        today = datetime.now(timezone.utc).date()  # the phone's own day may be one off; it checks exactly
        if not today - timedelta(days=1) <= day <= today + timedelta(days=366):
            raise ToolError("the date must be within the next year")
        r["date"] = day.isoformat()
        r["text"] = f"{day:%a %d %b} at {clock}"
    elif a.get("deferred"):
        r["next"] = time.time()  # the moment it was asked: the next 07:00 after that, whenever it is approved
        r["text"] = f"the next {clock}"
    else:
        _fix_date(r, (await phone_zone(s))[0], time.time())
    return {"resolved": r}


def _fix_date(r: dict, zone: Any, asked: float) -> None:
    """The next time the phone's clock shows this, from the moment it was asked: today or tomorrow there."""
    h, m = map(int, r["time"].split(":"))
    now = datetime.fromtimestamp(asked, zone)
    today = now.replace(hour=h, minute=m, second=0, microsecond=0, fold=0)
    day = now.date() if today.timestamp() > now.timestamp() else now.date() + timedelta(days=1)
    r["date"] = day.isoformat()
    r["text"] = f"{day:%a %d %b} at {r['time']}"


async def ph_alarm(s: Services, a: dict) -> Result:
    r = dict(a["resolved"])
    if r.get("next"):  # approved just now, at most 10 minutes after it was asked (if it has passed, the phone says so)
        _fix_date(r, (await phone_zone(s))[0], r["next"])
    body = {k: r[k] for k in ("time", "date", "days", "label") if r.get(k)}
    data, hello = await ask(s, "alarm.set", body)
    zone = zone_of(data.get("zone") or hello.get("zone"))
    return (f"{'That alarm was already set' if data.get('existed') is True else 'Alarm set'}: next "
            f"{_when(data.get('at'), zone)}" + (f" ({zone})" if zone else "")
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
    given = x.get("days")[:7] if isinstance(x.get("days"), list) else []
    days = [d for d in WEEKDAYS if d in given]
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
        lines += [f"  {_when(e.get('ts'), zone)}: {EVENTS[e['ev']]}{_label(e.get('label'))}"
                  for e in events[-15:] if isinstance(e, dict) and isinstance(e.get("ev"), str) and e["ev"] in EVENTS]
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
                         "recent": 0 <= age < RECENT and row.get("snoozed") is not True}}


async def ph_cancel(s: Services, a: dict) -> Result:
    r = a["resolved"]
    await ask(s, "alarm.cancel", {"id": r["id"]})
    return f"Cancelled: {r['text']}"


async def describe_notify(s: Services, a: dict) -> str:
    return f"Show on your phone: “{clean(a.get('heading', ''))}”" + (f"\n{clean_block(a['note'])}" if a.get("note") else "")


async def describe_alarm(s: Services, a: dict) -> str:
    r = a["resolved"]
    return f"Set an alarm on your phone: {r['text']} (the phone's time){_label(r.get('label'))}"


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
