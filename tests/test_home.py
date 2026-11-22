"""Home location and timezone in personal info: used by the server (Maps, Wolt, schedules), never shown to Ampi."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from test_ampi_tools import BRIDGE_PID

from ampi_tools import ops_maps
from ampi_tools.ops import Approvals, Policy, Services
from ampi_tools.server import Daemon, build_services
from ampi_tools.todoist import ToolError
from ampi_tools.vault import Vault, check_home


def run(c):
    return asyncio.run(c)


def test_check_home():
    assert check_home({"location": " 35.18561234, 33.3823 ", "timezone": "Europe/Nicosia"}) == {
        "location": "35.18561,33.38230", "timezone": "Europe/Nicosia"}
    with pytest.raises(ToolError):
        check_home({"location": "Nicosia"})
    with pytest.raises(ToolError):
        check_home({"timezone": "Mars/Olympus"})
    with pytest.raises(ToolError):
        check_home({})


def test_vault_home_merges_and_masks(tmp_path):
    v = Vault(tmp_path)
    v.set_home({"timezone": "Europe/Nicosia"})
    v.set_home({"location": "35.1,33.3"})
    assert v.home() == {"timezone": "Europe/Nicosia", "location": "35.10000,33.30000"}
    shown = "\n".join(v.masked())
    assert "35.1" not in shown and "Nicosia" not in shown and "home (place): location, timezone" in shown


def test_services_use_home_from_the_vault(tmp_path, monkeypatch):
    for var in ("MAPS_NEAR", "WOLT_NEAR", "MAPS_TIMEZONE", "MAPS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MAPS_NEAR", "60.1,24.9")
    monkeypatch.setenv("MAPS_TIMEZONE", "Europe/Helsinki")
    Vault(tmp_path).set_home({"location": "35.1,33.3", "timezone": "Europe/Nicosia"})

    async def build():
        async with httpx.AsyncClient() as client:
            return build_services(client, tmp_path)
    s = run(build())
    assert s.maps_client.near == {"latitude": 35.1, "longitude": 33.3}
    assert s.maps_client.tz.key == "Europe/Nicosia"
    assert s.wolt_client.near is None  # no built-in home: each request gets the places it may use
    assert s.env_home == "60.1,24.9"


class FakeMaps:
    def __init__(self, near):
        self.near = near
        self.calls = []

    async def route(self, origin, destination, **kw):
        self.bias = kw.get("bias")
        self.calls.append((origin, destination))
        return {"routes": [{"duration": "600s", "distanceMeters": 4200}]}


def test_route_from_and_to_home_never_shows_coordinates():
    m = FakeMaps({"latitude": 35.18561, "longitude": 33.3823})
    s = Services(maps_client=m)
    places = {"home": "35.18561,33.3823"}
    out = run(ops_maps.maps_route(s, {"origin": "home", "destination": "Larnaca Airport", "places_allowed": places}))
    assert m.calls[-1] == ("35.18561,33.3823", "Larnaca Airport")
    assert "35.18" not in out and "from home to Larnaca Airport" in out and "origin=" not in out
    out = run(ops_maps.maps_route(s, {"origin": "Larnaca Airport", "destination": "Home", "places_allowed": places}))
    assert m.calls[-1] == ("Larnaca Airport", "35.18561,33.3823")
    assert "35.18" not in out and "choose it as the destination" in out


def test_route_home_without_a_home_says_so():
    s = Services(maps_client=FakeMaps(None))
    with pytest.raises(ToolError, match="isn.t available here"):
        run(ops_maps.maps_route(s, {"origin": "home", "destination": "x"}))  # no places allowed (e.g. a group)


def test_timezone_only_for_the_bridge(tmp_path):
    async def trusted(pid):
        return pid == BRIDGE_PID

    vault = Vault(tmp_path)
    vault.set_home({"timezone": "Europe/Nicosia"})
    services = Services()
    services.checkout_side = SimpleNamespace(vault=vault)
    d = Daemon(services, Approvals(tmp_path / "a.sqlite3"), Policy(), trusted)
    assert run(d.handle({"cmd": "home.timezone"}, BRIDGE_PID)) == {"ok": True, "timezone": "Europe/Nicosia"}
    assert not run(d.handle({"cmd": "home.timezone"}, BRIDGE_PID + 1))["ok"]  # a claude run or other process


def test_engine_prefers_the_vault_timezone(tmp_path):
    from assistant_bridge.watch.db import WatchDB
    from assistant_bridge.watch.engine import WatchEngine

    bot = SimpleNamespace(profile=SimpleNamespace(name="personal", timezone="Europe/Athens", workspace=tmp_path,
                                                  owner_chat_id=1),
                          runner=SimpleNamespace(run_turn=None))
    e = WatchEngine(bot, WatchDB(":memory:"), tools_socket="x", tg_socket="y", data_dir=tmp_path)

    async def tools(payload, timeout=300):
        return {"ok": True, "timezone": "Europe/Nicosia"} if payload["cmd"] == "home.timezone" else {"ok": True}
    e.tools = tools
    assert e.timezone == "Europe/Athens"
    run(e._refresh_timezone())
    assert e.timezone == "Europe/Nicosia"
