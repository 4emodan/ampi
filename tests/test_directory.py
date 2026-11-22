"""The personal info directory: entries, old names, migration, reveal/fill/pay rules, places per origin, masking,
the owner's commands, /info, and what watchers may declare."""

import asyncio
import json

import pytest
from test_payment import CARD, PROFILE, SHOP, approve, make, req, run

from ampi_tools import info_cli
from ampi_tools import watch_spec as ws
from ampi_tools.todoist import ToolError
from ampi_tools.vault import Vault

CARD2 = "5555555555554444"


def add_work_and_card(v: Vault):
    v.set_entry("work", "place", {"company": "Acme Ltd", "address": {"line1": "Makarios Avenue 12", "city": "Nicosia",
                                                                      "country": "cy"}, "location": "35.17,33.36"})
    v.set_entry("card-travel", "card", {"number": CARD2, "exp_month": 6, "exp_year": 2031, "cvc": "321",
                                         "name": "Anna Virtanen", "label": "travel"})


# --- the directory ----------------------------------------------------------------------------------

def test_version1_vault_migrates_and_keeps_grants(tmp_path):
    (tmp_path / "vault.json").write_text(json.dumps({
        "profile": {**PROFILE, "address": {**PROFILE["address"], "country": "FI"}},
        "card": {"number": CARD, "exp_month": 12, "exp_year": 2030, "cvc": "123", "name": "Anna Virtanen"},
        "home": {"timezone": "Europe/Nicosia"}}))
    import sqlite3
    db = sqlite3.connect(tmp_path / "vault.sqlite3")
    db.execute("CREATE TABLE grants (origin TEXT, fields TEXT, created REAL, expires REAL, PRIMARY KEY (origin, fields))")
    db.execute("INSERT INTO grants VALUES (?, ?, 0, 9999999999)", (SHOP, "email,name"))
    db.commit()
    db.close()
    v = Vault(tmp_path)
    names = {e["name"]: e["type"] for e in v.entries()}
    assert names == {"me": "person", "home": "place", "card-main": "card"}
    assert v.default_card_name() == "card-main" and v.home() == {"timezone": "Europe/Nicosia"}
    assert v.value("email") == v.value("me.email") == PROFILE["email"]
    assert v.value("address.city") == "Helsinki" and v.value("card.number") == CARD
    assert v.granted(SHOP, ["email", "name"]) and v.granted(SHOP, ["me.email"])
    assert json.loads((tmp_path / "vault.json").read_text())["version"] == 2


def test_names_fields_and_kinds(tmp_path):
    d, side, _ = make(tmp_path)
    v = side.vault
    add_work_and_card(v)
    assert v.value("work.address.full") == "Makarios Avenue 12, Nicosia, CY"
    assert v.kind("work.address.city") == "address.city" and v.kind("me.email") == "email"
    assert v.kind("card.number", card="card-travel") == "card.number" and v.value("card.number", "card-travel") == CARD2
    assert v.kind("work.company") is None  # not a form field: reveal only
    for bad in ("Work", "a.b", "card", "address", "1x"):
        with pytest.raises(ToolError):
            v.set_entry(bad, "note", {"text": "x"})
    with pytest.raises(ToolError, match="is a place"):
        v.set_entry("work", "note", {"text": "x"})
    v.set_entry("work", "place", {"address": {"line2": "Floor 3"}})  # change one field, keep the rest
    assert v.value("work.address.line2") == "Floor 3" and v.value("work.company") == "Acme Ltd"


def test_delete_takes_its_grants_and_reuse_starts_clean(tmp_path):
    d, side, _ = make(tmp_path)
    v = side.vault
    v.set_entry("mom", "place", {"address": {"line1": "Street 1", "city": "Limassol", "country": "CY"}})
    v.grant(SHOP, ["mom.address.line1"])
    v.delete("mom")
    v.set_entry("mom", "place", {"address": {"line1": "Other 2", "city": "Paphos", "country": "CY"}})
    assert not v.granted(SHOP, ["mom.address.line1"])


# --- reveal ---------------------------------------------------------------------------------------------

def test_reveal_asks_and_refuses_cards_places_locations(tmp_path):
    d, side, _ = make(tmp_path)
    add_work_and_card(side.vault)
    res = run(d.run(req("info.reveal", fields=["work.address.full"]), None, origin="private"))
    assert res["approval"]  # always asks
    for f in (["card-travel.number"], ["card.number"], ["work.location"], ["home.timezone"]):
        with pytest.raises(ToolError):
            run(d.run(req("info.reveal", fields=f), None, origin="private"))
    side.vault.set_flag("work", "revealable", False)
    with pytest.raises(ToolError, match="never shown"):
        run(d.run(req("info.reveal", fields=["work.address.full"]), None, origin="private"))
    with pytest.raises(ToolError):  # groups never
        run(d.run(req("info.reveal", fields=["me.email"]), None, origin="group:clipboard"))


# --- fill and pay ----------------------------------------------------------------------------------------

def test_fill_with_new_names_and_notes_not_fillable(tmp_path):
    d, side, ctl = make(tmp_path)
    side.vault.set_entry("loyalty", "note", {"text": "ABC-123-456"})
    res = run(d.run(req("checkout.fill", fills=[{"ref": "f0:4", "field": "me.email"}]), None, origin="private"))
    assert res["approval"]
    with pytest.raises(ToolError, match="can't be filled"):
        run(d.run(req("checkout.fill", fills=[{"ref": "f0:5", "field": "loyalty.text"}]), None, origin="private"))


def test_pay_with_another_card_and_one_card_per_payment(tmp_path):
    d, side, ctl = make(tmp_path)
    add_work_and_card(side.vault)
    pay = dict(fills=[{"ref": "f2:3", "field": "card.number"}, {"ref": "f2:5", "field": "card.exp"},
                      {"ref": "f2:7", "field": "card.cvc"}], button="f0:20", amount="23.90", currency="EUR",
               what="2 × Pad thai")
    res = run(d.run(req("checkout.pay", **pay, card="card-travel"), None, origin="private"))
    assert res["approval"] and "card-travel" in res["text"] and "4444" in res["text"]
    out = approve(d, res)
    sent = [c for c in ctl.calls if c["cmd"] == "pay"][-1]
    assert CARD2 in {f["value"] for f in sent["fields"]} and CARD not in {f["value"] for f in sent["fields"]}
    assert "4444" in side.vault.purchases(1)[0]["card"]
    ctl.state = "active"  # a new session
    mixed = dict(pay, fills=[{"ref": "f2:3", "field": "card-main.number"}])
    with pytest.raises(ToolError, match="one card per payment"):
        run(d.run(req("checkout.pay", **mixed, card="card-travel"), None, origin="private"))


# --- places and masking -------------------------------------------------------------------------------------

def test_places_only_for_private_chat(tmp_path):
    d, side, _ = make(tmp_path)
    side.vault.set_entry("home", "place", {"location": "35.18,33.38"})
    side.vault.set_flag("home", "search", True)
    add_work_and_card(side.vault)  # every place is searchable by default
    assert d.places_for("private") == {"home": "35.18000,33.38000", "work": "35.17000,33.36000"}
    side.vault.set_flag("work", "search", False)
    assert d.places_for("private") == {"home": "35.18000,33.38000"}
    for origin in ("group:clipboard", "forwarded:private", "unknown", "watcher:old-style"):
        assert d.places_for(origin) == {}


def test_replies_mask_coordinates_everywhere_and_values_in_maps(tmp_path):
    d, side, _ = make(tmp_path)
    side.vault.set_entry("home", "place", {"location": "35.18561,33.38230"})
    out = d._masked({"ok": True, "text": "pin at 35.1857, 33.3820 and card " + CARD}, [{"op": "gmail.read"}])
    assert "35.18" not in out["text"] and CARD not in out["text"] and "[place]" in out["text"]
    out = d._masked({"ok": True, "text": "near " + PROFILE["email"]}, [{"op": "maps.search"}])
    assert PROFILE["email"] not in out["text"]
    out = d._masked({"ok": True, "text": "from " + PROFILE["email"]}, [{"op": "gmail.read"}])
    assert PROFILE["email"] in out["text"]  # her own emails stay readable
    out = d._masked({"ok": True, "text": PROFILE["email"]}, [{"op": "info.collect"}])
    assert PROFILE["email"] in out["text"]  # the approved reveal


# --- the owner's commands ------------------------------------------------------------------------------------

def test_info_cli_set_edit_delete(tmp_path):
    answers = iter(["Acme", "Street 5", "", "1010", "Nicosia", "", "CY", "35.1,33.3", "Asia/Nicosia"])
    out = info_cli.run(tmp_path, "info-set", ["work"], geocode=lambda a: "", ask=lambda p: next(answers),
                       secret=lambda p: "")
    assert "work (place)" in out
    out = info_cli.run(tmp_path, "info-set", ["work", "address.line2"], geocode=lambda a: "",
                       ask=lambda p: "Floor 2", secret=lambda p: "")
    assert Vault(tmp_path).value("work.address.line2") == "Floor 2"
    secrets = iter([CARD2, "999"])
    info_cli.run(tmp_path, "info-set", ["card-x"], geocode=lambda a: "",
                 ask=lambda p, it=iter(["7", "2032", "Anna V", ""]): next(it), secret=lambda p: next(secrets))
    assert Vault(tmp_path).default_card_name() == "card-x"
    assert "Not deleted" in info_cli.run(tmp_path, "info-delete", ["work"], geocode=None, ask=lambda p: "no")
    assert "Deleted" in info_cli.run(tmp_path, "info-delete", ["work"], geocode=None, ask=lambda p: "work")
    info_cli.run(tmp_path, "info-search", ["card-x", "on"], geocode=None) if False else None
    with pytest.raises(ToolError):
        info_cli.run(tmp_path, "info-search", ["card-x", "on"], geocode=None)


def test_info_overview_and_revoke_only_for_the_bridge(tmp_path):
    from test_ampi_tools import BRIDGE_PID
    from ampi_tools.ops import Approvals, Policy, Services
    from ampi_tools.server import Daemon
    d, side, _ = make(tmp_path)
    side.vault.grant(SHOP, ["me.email"])

    async def trusted(pid):
        return pid == BRIDGE_PID
    d2 = Daemon(Services(checkout_side=side), Approvals(tmp_path / "b.sqlite3"), Policy(), trusted)
    assert not run(d2.handle({"cmd": "info.overview"}, BRIDGE_PID + 1))["ok"]
    ov = run(d2.handle({"cmd": "info.overview"}, BRIDGE_PID))
    assert ov["ok"] and ov["grants"][0]["origin"] == SHOP and PROFILE["email"] not in json.dumps(ov)
    assert run(d2.handle({"cmd": "info.revoke_grant", "origin": SHOP, "fields": "me.email"}, BRIDGE_PID))["removed"] == 1


# --- watchers --------------------------------------------------------------------------------------------------

AGENT_PAY = b'''
name = "gym"
description = "Pays the gym monthly"
uses = ["card-main", "me.email"]
[[triggers]]
type = "time.at"
at = ["09:00"]
[[steps]]
id = "pay"
type = "llm"
mode = "agent"
prompt = "prompts/p.md"
tools = ["Bash(checkout:*)"]
actions = [{ op = "checkout.pay", site = "https://gym.example", max_amount = 40, per_week = 1, per_month = 1, card = "card-main", item = "monthly fee" }]
'''


def test_watcher_spec_uses_and_payment_caps_are_shown():
    spec = ws.parse({"watcher.toml": AGENT_PAY, "prompts/p.md": b"Pay the fee."})
    entries = ws.manifest(spec)
    assert {"kind": "uses", "uses": ["card-main", "me.email"]} in entries
    text = ws.describe(spec, entries)
    assert "card-main, me.email" in text and "1/month" in text and "card card-main" in text and "monthly fee" in text
    assert ws.risk(entries)[0] == "high"
    with pytest.raises(ws.SpecError):
        ws.parse({"watcher.toml": AGENT_PAY.replace(b'"me.email"]', b'"Me Email"]'), "prompts/p.md": b"x"})


# --- review fixes ----------------------------------------------------------------------------------------------

def test_upgraded_vault_keeps_the_env_home_area(tmp_path):
    d, side, _ = make(tmp_path)  # set_profile: home has only an address, like a migrated vault
    d.services.env_home = "35.18,33.38"
    assert d.places_for("private") == {"home": "35.18,33.38"}
    side.vault.set_entry("home", "place", {"location": "35.1,33.3"})
    assert d.places_for("private") == {"home": "35.10000,33.30000"}  # its own location wins
    side.vault.set_flag("home", "search", False)
    assert d.places_for("private") == {}  # switched off stays off


def test_unreadable_vault_is_never_overwritten(tmp_path):
    (tmp_path / "vault.json").write_text('{"version": 2, "entries": {,}')
    with pytest.raises(ToolError, match="valid JSON"):
        Vault(tmp_path)
    assert (tmp_path / "vault.json").read_text().startswith('{"version": 2')


def test_compat_commands_keep_their_old_meaning(tmp_path):
    d, side, _ = make(tmp_path)
    v = side.vault
    v.set_home({"location": "35.1,33.3", "timezone": "Europe/Nicosia"})
    v.set_profile({**PROFILE, "phone": ""})
    assert "phone" not in v.entry("me")["fields"]  # replaced as a whole, like before
    v.clear("profile")
    assert v.entry("me") is None and v.home() == {"location": "35.10000,33.30000", "timezone": "Europe/Nicosia"}
    v.clear("home")
    assert v.entry("home") is None


def test_masking_levels(tmp_path):
    d, side, _ = make(tmp_path)
    out = d._masked({"ok": True, "text": "Cafe in Helsinki near Mannerheimintie 1 A 2"}, [{"op": "maps.search"}])
    assert "Helsinki" in out["text"] and "Mannerheimintie 1 A 2" not in out["text"]
    out = d._masked({"ok": True, "text": "chat id -1001234567896"}, [{"op": "telegram.chats"}])
    assert "-1001234567896" in out["text"]  # a Luhn-valid chat id isn't a card
    out = d._masked({"ok": True, "items": [{"snippet": "card " + CARD}]}, [{"op": "gmail.meta"}])
    assert CARD not in json.dumps(out)
    out = run(d.handle({"cmd": "run", "ops": [{"op": "checkout.open", "args": {"url": "https://x.example/" + CARD}}]}, 1))
    assert CARD not in json.dumps(out)


def test_routes_from_a_place_are_rounded(tmp_path):
    from ampi_tools import ops_maps
    from ampi_tools.ops import Services

    class M:
        near = None
        async def route(self, o, dst, **kw):
            return {"routes": [{"duration": "1234s", "distanceMeters": 12345}]}
    s = Services(maps_client=M())
    out = run(ops_maps.maps_route(s, {"origin": "home", "destination": "X", "places_allowed": {"home": "35.1,33.3"}}))
    assert "about 12 km" in out and "about 20 min" in out and "12.3" not in out
    out = run(ops_maps.maps_route(s, {"origin": "A", "destination": "X"}))
    assert "about" not in out


def test_watcher_payment_item_and_monthly_cap_optional():
    spec = ws.parse({"watcher.toml": AGENT_PAY.replace(b"per_month = 1, ", b""), "prompts/p.md": b"x"})
    lim = next(e for e in ws.manifest(spec) if e.get("op") == "checkout.pay")["limits"]
    assert "per_month" not in lim and lim["item"] == "monthly fee"


def test_named_places_in_wolt_and_maps(tmp_path):
    d, side, _ = make(tmp_path)
    add_work_and_card(side.vault)
    side.vault.set_entry("mom", "place", {"location": "34.68,33.04"})
    ops = [{"op": "wolt.venues", "args": {"place": "work"}}]
    d._add_places(ops, "private")
    assert ops[0]["args"]["lat_lng"] == {"latitude": 35.17, "longitude": 33.36} and "place" not in ops[0]["args"]
    ops = [{"op": "wolt.venues", "args": {"place": "mom"}}]
    d._add_places(ops, "private")
    assert ops[0]["args"]["lat_lng"] == {"latitude": 34.68, "longitude": 33.04}
    with pytest.raises(ToolError, match="no place"):
        d._add_places([{"op": "wolt.venues", "args": {"place": "gym"}}], "private")
    with pytest.raises(ToolError):
        d._add_places([{"op": "wolt.venues", "args": {"place": "work"}}], "group:clipboard")
    from ampi_tools import ops_maps
    from ampi_tools.ops import Services

    class M:
        near = None
        calls = []
        async def route(self, o, dst, **kw):
            self.calls.append((o, dst))
            return {"routes": [{"duration": "600s", "distanceMeters": 4000}]}
    m = M()
    places = d.places_for("private")
    out = run(ops_maps.maps_route(Services(maps_client=m), {"origin": "work", "destination": "mom",
                                                            "places_allowed": places}))
    assert m.calls[-1] == ("35.17000,33.36000", "34.68000,33.04000") and "from work to mom" in out
    assert "35.17" not in out and "34.68" not in out


# --- Ampi saves places (each save asks the owner) ---------------------------------------------------------------

def test_ampi_saves_a_place_after_approval(tmp_path):
    d, side, _ = make(tmp_path)

    class M:
        api_key = "k"
        async def search(self, q, **kw):
            return {"places": [{"location": {"latitude": 35.16, "longitude": 33.35}}]}
    d.services.maps_client = M()
    args = dict(entry="gym", street="Gym Street 5", city="Nicosia", country="cy", company="FitCo")
    res = run(d.run(req("info.save_place", **args), None, origin="private"))
    assert res["approval"] and "Save your place “gym”" in res["text"] and "found on Google Maps" in res["text"]
    assert side.vault.entry("gym") is None  # nothing before the owner taps
    out = approve(d, res)
    assert out["status"] == "done" and "Saved the place “gym”" in out["text"]
    e = side.vault.entry("gym")
    assert e["fields"]["location"] == "35.16000,33.35000" and e.get("search")
    assert d.places_for("private")["gym"] == "35.16000,33.35000"
    # Replacing an address drops the old location unless a new one is found, and says it replaces
    d.services.maps_client = None
    res = run(d.run(req("info.save_place", entry="gym", street="New Road 1", city="Limassol", country="CY"), None,
                    origin="private"))
    assert "Replace the address of" in res["text"] and "not found" in res["text"]
    approve(d, res)
    assert "location" not in side.vault.entry("gym")["fields"] and side.vault.value("gym.address.city") == "Limassol"


def test_ampi_cannot_save_over_other_kinds_or_outside_private(tmp_path):
    d, side, _ = make(tmp_path)
    with pytest.raises(ToolError, match="only save places"):
        run(d.run(req("info.save_place", entry="me", street="x 1", city="y", country="CY"), None, origin="private"))
    with pytest.raises(ToolError):
        run(d.run(req("info.save_place", entry="card", street="x 1", city="y", country="CY"), None, origin="private"))
    with pytest.raises(ToolError):
        run(d.run(req("info.save_place", entry="gym", street="x 1", city="y", country="CY"), None,
                  origin="group:clipboard"))
    assert "info.save_place" in ws.NOT_FOR_WATCHERS
