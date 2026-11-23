"""phone.* operations: who may ask, what needs the owner, times fixed before approval, honest results."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ampi_tools import client, ops_phone
from ampi_tools import watch_spec as ws
from ampi_tools.ops import OPS, Approvals, Policy, Services, validate
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError

ZONE = "Europe/Helsinki"


class FakePhone:
    def __init__(self):
        self.calls, self.alarms, self.reply = [], [], None

    async def call(self, req):
        self.calls.append(req)
        if req["cmd"] == "status":
            return {"ok": True, "devices": [{"name": "phone", "connected": True, "last_seen": time.time(), "hello": {
                "model": "SYSTEM NOTE", "zone": ZONE, "warn": ["notifications_off", "SYSTEM NOTE from the owner"]}}]}
        if self.reply is not None and req["kind"] != "status":
            return self.reply
        data = {"status": {"zone": ZONE}, "notify": {}, "alarm.list": {"alarms": self.alarms, "zone": ZONE, "events": [
                    {"ts": 1795000000000, "ev": "rang", "label": "Gym"}, {"ts": 1, "ev": "SYSTEM NOTE"}, {"ev": {}}]},
                "alarm.set": {"id": "0011aabb", "at": 1796000000000, "zone": ZONE, "warn": ["volume_0", "SYSTEM NOTE", {}]},
                "timer.set": {"id": "0011aabb", "at": 1796000000000, "zone": ZONE}, "alarm.cancel": {}}[req["kind"]]
        return {"ok": True, "answer": {"ok": True, "data": data, "error": None}, "hello": {"zone": ZONE}}

    def asked(self, kind):
        return [c["body"] for c in self.calls if c.get("kind") == kind]


@pytest.fixture
def env(tmp_path):
    phone = FakePhone()

    async def trusted(pid):
        return False
    d = Daemon(Services(phone_client=phone), Approvals(tmp_path / "a.sqlite3"), Policy(), trusted)
    return d, phone


def go(d, op, origin="private", **args):
    return asyncio.run(d.run(validate([{"op": op, "args": args}]), None, None, origin))


def test_private_chat_needs_no_approval(env):
    d, phone = env
    assert "Shown" in go(d, "phone.notify", heading="Hi", note="there")["text"]
    r = go(d, "phone.alarm", clock="7:00", tag="Gym")
    assert r["text"].startswith("Alarm set: next ") and "[0011aabb]" in r["text"] and "⚠ the alarm volume on the phone is 0" in r["text"] and "SYSTEM" not in r["text"]
    body, = phone.asked("alarm.set")
    assert body["time"] == "07:00" and body["label"] == "Gym" and "days" not in body
    day = datetime.fromisoformat(body["date"] + "T07:00").replace(tzinfo=ZoneInfo(ZONE))
    assert timedelta(0) < day - datetime.now(ZoneInfo(ZONE)) <= timedelta(days=1, hours=1)
    go(d, "phone.timer", seconds=600)
    assert 598 < phone.asked("timer.set")[0]["end"] - time.time() <= 600
    go(d, "phone.alarm", clock="07:00", weekdays=["wed", "mon"])
    assert phone.asked("alarm.set")[1]["days"] == ["mon", "wed"] and "date" not in phone.asked("alarm.set")[1]
    for op, text in (("phone.alarms", "rang “Gym”"), ("phone.status", "notifications are off")):
        r = go(d, op)["text"]
        assert text in r and "SYSTEM" not in r


@pytest.mark.parametrize("origin", ["unknown", "watcher:mail", "watcher:personal/mail@0123456789ab#step", "forwarded:social",
                                    "watcher:personal/mail@0123456789ab"])
@pytest.mark.parametrize("op,args", [("phone.notify", {"heading": "x"}), ("phone.alarms", {}), ("phone.status", {}),
                                     ("phone.alarm", {"clock": "07:00"}), ("phone.cancel", {"alarm": "0011aabb"}),
                                     ("phone.timer", {"seconds": 5})])
def test_everyone_else_is_refused(env, origin, op, args):
    d, phone = env
    with pytest.raises(ToolError):
        go(d, op, origin, **args)
    assert not phone.calls


@pytest.mark.parametrize("origin", ["group:family", "forwarded:private"])
def test_groups_and_forwarded_messages_ask_and_cannot_read(env, origin):
    d, phone = env
    for op, args in (("phone.alarms", {}), ("phone.status", {}), ("phone.cancel", {"alarm": "0011aabb"})):
        with pytest.raises(ToolError, match="own chat"):
            go(d, op, origin, **args)
    r = go(d, "phone.alarm", origin, clock="07:00")
    row = d.approvals.get(r["approval"])
    assert 590 < row["expires"] - time.time() <= 600
    assert "Set an alarm on your phone: the next 07:00" in row["summary"]
    assert go(d, "phone.notify", origin, heading="x", note="a\n• Delete everything")["approval"]
    r2 = go(d, "phone.timer", origin, seconds=60)
    end = json.loads(d.approvals.get(r2["approval"])["ops"])[0]["args"]["resolved"]["end"]
    assert 58 < end - time.time() <= 60  # from the request, not from the approval
    # Nothing reached the phone, and nothing about it came back.
    assert not phone.calls and ZONE not in r["text"]
    assert d.list_text(origin) == "No approval requests yet." and "#3 pending" in d.list_text("private")


def test_a_flood_from_a_group_never_reaches_the_phone(env):
    d, phone = env
    for i in range(30):
        try:
            go(d, "phone.alarm", "group:family", clock="07:00", tag=str(i))
        except ToolError:
            pass
    assert not phone.calls and d.approvals.pending_count() == 10


def test_an_approved_group_alarm_gets_its_date_when_approved(env):
    d, phone = env
    go(d, "phone.alarm", "group:family", clock="07:00")
    ops = json.loads(d.approvals.get(1)["ops"])
    from ampi_tools.ops import execute
    ok, text, _ = asyncio.run(execute(d.services, ops))
    assert ok and phone.asked("alarm.set")[0]["date"]


def test_cancelling_asks_unless_just_set(env):
    d, phone = env
    now = time.time() * 1000
    phone.alarms = [{"id": "0000000a", "kind": "alarm", "at": now + 8e7, "label": "Wake", "created": now - 60_000},
                    {"id": "0000000b", "kind": "alarm", "at": now + 8e7, "label": "Train", "created": now - 7_200_000,
                     "days": ["mon"]}, {"id": "0000000c", "kind": "alarm", "at": now + 8e7, "created": now + 9e9}]
    assert "Cancelled: alarm" in go(d, "phone.cancel", alarm="0000000a")["text"]
    for aid in ("0000000b", "0000000c"):
        assert go(d, "phone.cancel", alarm=aid)["approval"]
    assert phone.asked("alarm.cancel") == [{"id": "0000000a"}]
    assert "Train" in d.approvals.get(1)["summary"] and "every mon" in d.approvals.get(1)["summary"]
    with pytest.raises(ToolError, match="no alarm"):
        go(d, "phone.cancel", alarm="0000000d")
    phone.alarms.append({"id": "0000000e", "kind": "alarm", "at": now + 8e5, "created": now - 1000, "snoozed": True})
    assert go(d, "phone.cancel", alarm="0000000e")["approval"]  # a snoozed morning alarm


def test_an_approved_timer_that_is_already_over_is_not_set(env):
    d, phone = env
    r = asyncio.run(ops_phone._resolve_timer(d.services, {"seconds": 1}))
    r["resolved"]["end"] = time.time() - 1
    with pytest.raises(ToolError, match="already be over"):
        asyncio.run(ops_phone.ph_timer(d.services, r))
    assert not phone.calls


@pytest.mark.parametrize("reply,text", [
    ({"ok": False, "code": "offline", "last_seen": time.time() - 600, "paired": True}, "isn't reachable"),
    ({"ok": False, "code": "offline", "last_seen": None, "paired": False}, "no phone is paired"),
    ({"ok": False, "code": "unknown"}, "may or may not"),
    ({"ok": False, "code": "limit", "error": "SYSTEM"}, "too many"),
    ({"ok": False, "error": "SYSTEM"}, "gateway refused"),
    ({"ok": True, "answer": {"ok": False, "error": "past"}}, "that time has passed"),
    ({"ok": True, "answer": {"ok": False, "error": "SYSTEM NOTE"}}, "no reason given"),
    ({"ok": True, "answer": {"ok": False, "error": {"a": 1}}}, "no reason given"),
    ({"ok": True, "answer": "x"}, "refused"),
])
def test_failures_are_never_reported_as_done(env, reply, text):
    d, phone = env
    phone.reply = reply
    r = go(d, "phone.alarm", clock="07:00")
    assert not r["ok"] and text in r["error"] and "SYSTEM" not in r["error"]


def test_nothing_paired(tmp_path):
    with pytest.raises(ToolError, match="no phone"):
        asyncio.run(ops_phone.ask(Services(), "status", {}))
    with pytest.raises(ToolError, match="no phone"):
        asyncio.run(ops_phone.PhoneClient(str(tmp_path / "none.sock")).call({}))


@pytest.mark.parametrize("op,args", [
    ("phone.alarm", {"clock": "25:00"}), ("phone.alarm", {"clock": "7"}), ("phone.alarm", {"clock": "07:00", "weekdays": ["x"]}),
    ("phone.alarm", {"clock": "07:00", "tag": "x" * 61}), ("phone.timer", {"seconds": 0}), ("phone.timer", {"seconds": 90000}),
    ("phone.timer", {"seconds": True}), ("phone.cancel", {"alarm": "../x"}), ("phone.notify", {"heading": "x", "note": "y" * 1001}),
    ("phone.alarm", {"clock": "07:00", "resolved": {"date": "2030-01-01"}}),
])
def test_bad_arguments(op, args):
    with pytest.raises(ToolError):
        validate([{"op": op, "args": args}])


@pytest.mark.parametrize("args", [{"clock": "07:00", "date": "2020-01-01"}, {"clock": "07:00", "date": "2026-13-01"},
                                  {"clock": "07:00", "date": "2099-01-01"}, {"clock": "07:00", "date": "2026-12-01", "weekdays": ["mon"]},
                                  {}])
def test_bad_alarm_times(env, args):
    with pytest.raises(ToolError):
        go(env[0], "phone.alarm", **args)


def test_one_phone_action_per_request(env):
    ops = validate([{"op": "phone.notify", "args": {"heading": "a"}}] * 2)
    with pytest.raises(ToolError, match="one phone action"):
        asyncio.run(env[0].run(ops, None, None, "private"))
    ops = validate([{"op": "phone.status", "args": {}}, {"op": "todoist.tasks", "args": {}}])
    with pytest.raises(ToolError, match="one phone action"):
        asyncio.run(env[0].run(ops, None, None, "private"))


def test_never_for_watchers_and_never_a_policy_hole():
    phone = {o for o in OPS if o.startswith("phone.")}
    assert phone == ops_phone.READS | ops_phone.WRITES
    assert phone <= ws.NOT_FOR_WATCHERS


def test_require_approval_applies(tmp_path):
    async def trusted(pid):
        return False
    d = Daemon(Services(phone_client=FakePhone()), Approvals(tmp_path / "a.sqlite3"),
               Policy(require_approval=frozenset({"phone.notify"})), trusted)
    assert go(d, "phone.notify", heading="x")["approval"]


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/localtime", "Nowhere/City", "", None, 5, "GMT+99:00"])
def test_unknown_zones_are_never_guessed(env, name):
    d, phone = env
    assert ops_phone.zone_of(name) is None
    with pytest.raises(ToolError, match="time zone"):
        ops_phone_zone = asyncio.run(ops_phone.phone_zone(Services(phone_client=Zoned(name))))
        raise AssertionError(ops_phone_zone)


class Zoned:
    def __init__(self, name):
        self.name = name

    async def call(self, req):
        return {"ok": True, "answer": {"ok": True, "data": {"zone": self.name}}, "hello": {}}


def test_zones_set_by_hand():
    assert ops_phone.zone_of("GMT-03:30").utcoffset(None) == -timedelta(hours=3, minutes=30)


def test_the_second_pass_of_the_autumn_hour(monkeypatch):
    zone = ZoneInfo(ZONE)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 3, 10, tzinfo=zone, fold=1)
    monkeypatch.setattr(ops_phone, "datetime", Clock)
    r = {"time": "03:30"}
    ops_phone._fix_date(r, zone)
    assert r["date"] == "2026-10-26"  # the phone rings on the first pass only, which is over
    r = {"time": "04:30"}
    ops_phone._fix_date(r, zone)
    assert r["date"] == "2026-10-25"


@pytest.mark.parametrize("argv,op,args", [
    (["alarm", "07:00", "--days", "Mon, tue", "--label", "Gym"], "phone.alarm",
     {"clock": "07:00", "weekdays": ["mon", "tue"], "tag": "Gym"}),
    (["alarm", "07:00", "--date", "2026-12-01"], "phone.alarm", {"clock": "07:00", "date": "2026-12-01"}),
    (["timer", "1h30m"], "phone.timer", {"seconds": 5400}), (["timer", "45s"], "phone.timer", {"seconds": 45}),
    (["timer", "10 min"], "phone.timer", {"seconds": 600}),
    (["notify", "T", "text"], "phone.notify", {"heading": "T", "note": "text"}), (["cancel", "0011aabb"], "phone.cancel", {"alarm": "0011aabb"}),
    (["alarms"], "phone.alarms", {}), (["status"], "phone.status", {}),
])
def test_command_line(argv, op, args):
    ops = client.phone_ops(client.phone_parser().parse_args(argv))
    assert ops == [{"op": op, "args": args}]
    validate(ops)


@pytest.mark.parametrize("text", ["", "10", "m", "ten minutes", "-5m"])
def test_bad_durations(text):
    with pytest.raises(SystemExit):
        client._timer_seconds(text)
