"""The owner's personal details and payment card (ampi-tools, user `ampitools`).

Only the owner enters them, on the server (`ampi-tools vault-set`, the card number and CVC without echo). Ampi never
sees the card: card fields are filled into a payment provider's frame by the checkout controller, after the owner
approves that exact purchase. Profile fields (name, email, phone, address) are filled the same way after approval
(or a per-site grant), or revealed to Ampi after approval.

Storage: /home/ampitools/vault.json (0600; no encryption at rest: the key would live on the same box, so the
separate user and file mode are the protection, as for the other tokens). Grants and the purchase log: vault.sqlite3.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from .meta import register_secret
from .todoist import ToolError

PROFILE_FIELDS = ("name", "first_name", "last_name", "email", "phone", "address.line1", "address.line2",
                  "address.postal", "address.city", "address.region", "address.country", "address.full")
CARD_FIELDS = ("card.number", "card.exp", "card.exp_month", "card.exp_year", "card.cvc", "card.name")
ALL_FIELDS = PROFILE_FIELDS + CARD_FIELDS
GRANT_DAYS = 30


# --- checks ---------------------------------------------------------------------------------------


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or not 12 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


CARD_RUN = re.compile(r"(?<!\d)\d(?:[ \-.]?\d){12,18}(?!\d)")


def has_card_number(text: str) -> bool:
    """Any Luhn-valid 13–19 digit run, with or without spaces/dashes/dots between digits."""
    return any(luhn_ok(re.sub(r"\D", "", m.group(0))) for m in CARD_RUN.finditer(text or ""))


def brand(number: str) -> str:
    if number.startswith("4"):
        return "Visa"
    if re.match(r"5[1-5]|2[2-7]", number):
        return "Mastercard"
    if re.match(r"3[47]", number):
        return "Amex"
    return "Card"


def _line(v: Any, what: str, limit: int = 120, required: bool = True) -> str:
    s = unicodedata.normalize("NFC", str(v or "")).strip()
    if not s:
        if required:
            raise ToolError(f"{what} is required")
        return ""
    if len(s) > limit or any(unicodedata.category(c) in ("Cc", "Cf") for c in s):
        raise ToolError(f"{what}: one line, at most {limit} characters")
    return s


def check_profile(p: dict) -> dict:
    out = {"name": _line(p.get("name"), "name"),
           "email": _line(p.get("email"), "email", 200),
           "phone": _line(p.get("phone"), "phone", 30, required=False)}
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", out["email"]):
        raise ToolError("email doesn't look like an email address")
    if out["phone"] and not re.fullmatch(r"\+?[0-9 ()\-]{6,25}", out["phone"]):
        raise ToolError("phone: digits, spaces, + ( ) - only")
    a = p.get("address") or {}
    addr = {"line1": _line(a.get("line1"), "street address"), "line2": _line(a.get("line2"), "line 2", required=False),
            "postal": _line(a.get("postal"), "postal code", 12), "city": _line(a.get("city"), "city", 80),
            "region": _line(a.get("region"), "region", 80, required=False),
            "country": _line(a.get("country"), "country", 2).upper()}
    if not re.fullmatch(r"[A-Z]{2}", addr["country"]):
        raise ToolError("country: two letters, e.g. FI")
    out["address"] = addr
    return out


LATLNG = re.compile(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*")


def check_home(h: dict, allow_empty: bool = False) -> dict:
    """Home location ("lat,lng") and timezone (IANA name). Used by the server itself (Maps and Wolt searches near
    home, schedules and times); never handed to Ampi."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    out: dict[str, str] = {}
    loc = str(h.get("location") or "").strip()
    if loc:
        m = LATLNG.fullmatch(loc)
        if not m or not (-90 <= float(m.group(1)) <= 90 and -180 <= float(m.group(2)) <= 180):
            raise ToolError("home location: latitude,longitude, e.g. 35.1856,33.3823")
        out["location"] = f"{float(m.group(1)):.5f},{float(m.group(2)):.5f}"
    tz = str(h.get("timezone") or "").strip()
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ToolError(f"unknown timezone {tz!r} (e.g. Europe/Nicosia)") from None
        out["timezone"] = tz
    if not out and not allow_empty:
        raise ToolError("give a home location, a timezone, or both")
    return out


def check_card(c: dict, today: date | None = None) -> dict:
    number = re.sub(r"[ \-]", "", str(c.get("number") or ""))
    if not luhn_ok(number) or len(number) < 13:
        raise ToolError("that card number isn't valid (checksum)")
    try:
        month, year = int(c.get("exp_month")), int(c.get("exp_year"))
    except (TypeError, ValueError):
        raise ToolError("expiry month and year must be numbers") from None
    year = year + 2000 if year < 100 else year
    today = today or date.today()
    if not 1 <= month <= 12 or (year, month) < (today.year, today.month) or year > today.year + 20:
        raise ToolError("the card has expired or the expiry date is wrong")
    cvc = str(c.get("cvc") or "").strip()
    if not re.fullmatch(r"\d{3,4}", cvc):
        raise ToolError("CVC: 3 or 4 digits")
    return {"number": number, "exp_month": month, "exp_year": year, "cvc": cvc,
            "name": _line(c.get("name"), "name on the card")}


# --- the directory ----------------------------------------------------------------------------------

TYPES = ("person", "place", "card", "note")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
RESERVED = {"card", "address", "all", "person", "place", "note", "maps", "wolt", "reveal", "fill", "pay"}
# Old field names (today's skills, approved watchers, stored site grants) keep working.
ALIASES = {"name": "me.name", "first_name": "me.first_name", "last_name": "me.last_name", "email": "me.email",
           "phone": "me.phone"}
PERSON_FIELDS = ("name", "first_name", "last_name", "email", "phone", "birthday")
ADDRESS_FIELDS = ("address.line1", "address.line2", "address.postal", "address.city", "address.region",
                  "address.country", "address.full")
PLACE_FIELDS = ("company",) + ADDRESS_FIELDS + ("location", "timezone")
CARD_KEYS = ("number", "exp", "exp_month", "exp_year", "cvc", "name", "label")
TYPE_FIELDS = {"person": PERSON_FIELDS, "place": PLACE_FIELDS, "card": CARD_KEYS, "note": ("text",)}
# Fields that can go into a shop's form (by what kind of field they are); the rest are reveal-only.
FILLABLE = set(PERSON_FIELDS[:5]) | set(ADDRESS_FIELDS)
NEVER_SHOWN = {"location", "timezone"}  # used by the server only (searches, schedules); never revealed


def _address_full(a: dict) -> str:
    return ", ".join(x for x in (a.get("line1"), a.get("line2"), f"{a.get('postal', '')} {a.get('city', '')}".strip(),
                                 a.get("region"), a.get("country")) if x)


def check_entry(kind: str, fields: dict, current: dict | None = None) -> dict:
    """The stored fields of an entry, validated. `current`: the stored fields (for editing some of them)."""
    if kind == "person":
        merged = {**(current or {}), **{k: v for k, v in fields.items() if v not in (None, "")}}
        out = {"name": _line(merged.get("name"), "name"), "email": _line(merged.get("email"), "email", 200, required=False),
               "phone": _line(merged.get("phone"), "phone", 30, required=False),
               "birthday": _line(merged.get("birthday"), "birthday", 10, required=False)}
        if out["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", out["email"]):
            raise ToolError("email doesn't look like an email address")
        if out["phone"] and not re.fullmatch(r"\+?[0-9 ()\-]{6,25}", out["phone"]):
            raise ToolError("phone: digits, spaces, + ( ) - only")
        if out["birthday"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out["birthday"]):
            raise ToolError("birthday: YYYY-MM-DD")
        return {k: v for k, v in out.items() if v}
    if kind == "place":
        cur = current or {}
        a = {**(cur.get("address") or {}), **{k: v for k, v in (fields.get("address") or {}).items() if v not in (None, "")}}
        out: dict[str, Any] = {}
        if any(a.values()):
            addr = {"line1": _line(a.get("line1"), "street address"),
                    "line2": _line(a.get("line2"), "line 2", required=False),
                    "postal": _line(a.get("postal"), "postal code", 12, required=False),
                    "city": _line(a.get("city"), "city", 80),
                    "region": _line(a.get("region"), "region", 80, required=False),
                    "country": _line(a.get("country"), "country", 2).upper()}
            if not re.fullmatch(r"[A-Z]{2}", addr["country"]):
                raise ToolError("country: two letters, e.g. CY")
            out["address"] = {k: v for k, v in addr.items() if v}
        company = fields.get("company", cur.get("company"))
        if company:
            out["company"] = _line(company, "company", 120)
        home = check_home({"location": fields.get("location") or cur.get("location"),
                           "timezone": fields.get("timezone") or cur.get("timezone")}, allow_empty=True)
        out.update(home)
        if not out:
            raise ToolError("a place needs an address, a location or a timezone")
        return out
    if kind == "card":
        merged = {**(current or {}), **{k: v for k, v in fields.items() if v not in (None, "")}}
        out = check_card(merged)
        label = merged.get("label")
        if label:
            out["label"] = _line(label, "label", 60)
        return out
    if kind == "note":
        text = fields.get("text", (current or {}).get("text"))
        return {"text": _line(text, "note", 500)}
    raise ToolError(f"type must be one of {', '.join(TYPES)}")


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in RESERVED:
        raise ToolError("entry names: lowercase letters, digits and -, starting with a letter, no dots "
                        f"(not {', '.join(sorted(RESERVED))})")
    return name


class Vault:
    def __init__(self, home: Path):
        self.path = home / "vault.json"
        self.db = sqlite3.connect(home / "vault.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS grants (origin TEXT, fields TEXT, created REAL, expires REAL,
                                               PRIMARY KEY (origin, fields));
            CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, origin TEXT,
                                                  amount TEXT, currency TEXT, what TEXT, card TEXT, status TEXT,
                                                  approval INTEGER);
        """)
        self.clock = time.time
        if self.path.exists():
            data = self._read()
            if data and "version" not in data:
                self._migrate(data)
        for number in self.card_numbers():
            register_secret(number)  # the CVC is too short to register (it would garble output)
            register_secret(" ".join(number[i:i + 4] for i in range(0, len(number), 4)))

    # --- storage -------------------------------------------------------------------------

    def _read(self) -> dict:
        """The stored data; {} if there is none yet. A file that exists but can't be read is an error, never an empty
        vault (anything saved afterwards would overwrite every entry and card)."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise ToolError(f"vault.json can't be read ({e.__class__.__name__}); fix it or restore it") from None
        try:
            data = json.loads(text)
        except ValueError:
            raise ToolError("vault.json isn't valid JSON (edited by hand?); fix it or restore it before going on") from None
        if not isinstance(data, dict):
            raise ToolError("vault.json doesn't hold an object; fix it or restore it")
        return data

    def load(self) -> dict:
        data = self._read()
        if data and "version" not in data:
            data = self._converted(data)
        data.setdefault("version", 2)
        data.setdefault("entries", {})
        return data

    def save(self, data: dict) -> None:
        data["version"] = 2
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    @staticmethod
    def _converted(old: dict) -> dict:
        """Version 1 (profile, card, home) → entries me, home, card-main."""
        entries: dict[str, dict] = {}
        p, c, h = old.get("profile") or {}, old.get("card") or {}, old.get("home") or {}
        if p:
            entries["e1"] = {"name": "me", "type": "person",
                             "fields": {k: p[k] for k in ("name", "email", "phone") if p.get(k)}, "revealable": True}
        place = {}
        if p.get("address"):
            place["address"] = p["address"]
        place.update({k: h[k] for k in ("location", "timezone") if h.get(k)})
        if place:
            entries["e2"] = {"name": "home", "type": "place", "fields": place, "revealable": True, "search": True}
        default = None
        if c:
            entries["e3"] = {"name": "card-main", "type": "card", "fields": dict(c), "revealable": False}
            default = "e3"
        return {"version": 2, "entries": entries, "default_card": default, "next": 4}

    def _migrate(self, old: dict) -> None:
        self.save(self._converted(old))
        # Site grants used the old field names; the aliases keep them meaning the same, but store them canonically.
        for r in list(self.db.execute("SELECT * FROM grants")):
            fields = ",".join(sorted(self.canonical(f) for f in r["fields"].split(",") if f))
            self.db.execute("DELETE FROM grants WHERE origin=? AND fields=?", (r["origin"], r["fields"]))
            self.db.execute("INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?)",
                            (r["origin"], fields, r["created"], r["expires"]))

    # --- entries ----------------------------------------------------------------------------

    def _find(self, data: dict, name: str) -> tuple[str, dict] | None:
        for eid, e in data["entries"].items():
            if e["name"] == name:
                return eid, e
        return None

    def entries(self) -> list[dict]:
        data = self.load()
        return [{"id": eid, **e} for eid, e in data["entries"].items()]

    def entry(self, name: str) -> dict | None:
        hit = self._find(self.load(), name)
        return {"id": hit[0], **hit[1]} if hit else None

    def set_entry(self, name: str, kind: str, fields: dict) -> None:
        """Create an entry or change some of its fields."""
        data = self.load()
        hit = self._find(data, check_name(name))
        if hit:
            eid, e = hit
            if e["type"] != kind:
                raise ToolError(f"{name} is a {e['type']}, not a {kind}")
            e["fields"] = check_entry(kind, fields, e["fields"])
        else:
            if kind not in TYPES:
                raise ToolError(f"type must be one of {', '.join(TYPES)}")
            eid = self._new_id(data)
            e = {"name": name, "type": kind, "fields": check_entry(kind, fields), "revealable": kind != "card",
                 "created": self.clock()}
            if kind == "place":
                e["search"] = True  # Maps/Wolt/routes may use it (owner's private chat only); info-search … off
            data["entries"][eid] = e
            if kind == "card" and not data.get("default_card"):
                data["default_card"] = eid
        self.save(data)
        if kind == "card":
            register_secret(e["fields"]["number"])

    def save_place(self, name: str, address: dict, company: str | None = None, location: str | None = None) -> bool:
        """Creates or updates a place: the address is replaced as a whole; company and location only when given
        (an existing place keeps its timezone). Returns whether it replaced an existing place's address."""
        data = self.load()
        hit = self._find(data, check_name(name))
        fields: dict[str, Any] = {"address": address}
        if company:
            fields["company"] = company
        if location:
            fields["location"] = location
        if hit:
            eid, e = hit
            if e["type"] != "place":
                raise ToolError(f"{name} is a {e['type']}, not a place")
            replaced = bool(e["fields"].get("address"))
            keep = {k: v for k, v in e["fields"].items() if k != "address" and k not in fields}
            if not location:
                keep.pop("location", None)  # a new address with the old location would search the wrong place
            e["fields"] = check_entry("place", {**keep, **fields})
        else:
            replaced = False
            data["entries"][self._new_id(data)] = {"name": name, "type": "place", "fields": check_entry("place", fields),
                                                   "revealable": True, "search": True, "created": self.clock()}
        self.save(data)
        return replaced

    def delete(self, name: str) -> None:
        data = self.load()
        hit = self._find(data, name)
        if not hit:
            raise ToolError(f"no entry {name}")
        eid, e = hit
        del data["entries"][eid]
        if data.get("default_card") == eid:
            data["default_card"] = next((i for i, x in data["entries"].items() if x["type"] == "card"), None)
        self.save(data)
        # Its grants go with it (a new entry with the same name starts with none).
        for r in list(self.db.execute("SELECT origin, fields FROM grants")):
            if any(f.split(".", 1)[0] == name for f in r["fields"].split(",")):
                self.db.execute("DELETE FROM grants WHERE origin=? AND fields=?", (r["origin"], r["fields"]))

    def set_flag(self, name: str, flag: str, on: bool) -> None:
        data = self.load()
        hit = self._find(data, name)
        if not hit:
            raise ToolError(f"no entry {name}")
        e = hit[1]
        if flag == "search":
            if e["type"] != "place":
                raise ToolError("searches can be switched on only for places")
        elif flag == "revealable":
            if e["type"] == "card" and on:
                raise ToolError("card details are never shown to Ampi")
        else:
            raise ToolError("unknown switch")
        e[flag] = bool(on)
        self.save(data)

    def set_default_card(self, name: str) -> None:
        data = self.load()
        hit = self._find(data, name)
        if not hit or hit[1]["type"] != "card":
            raise ToolError(f"{name} isn't a card")
        data["default_card"] = hit[0]
        self.save(data)

    # --- compatibility with version 1 commands ----------------------------------------------

    def set_profile(self, profile: dict) -> None:
        """Like version 1: the profile is replaced as a whole (blank fields are cleared); home keeps its location
        and timezone."""
        p = check_profile(profile)
        data = self.load()
        me = self._find(data, "me")
        fields = {k: p[k] for k in ("name", "email", "phone") if p.get(k)}
        if me:
            me[1]["fields"] = check_entry("person", fields)
        else:
            data["entries"][self._new_id(data)] = {"name": "me", "type": "person",
                                                   "fields": check_entry("person", fields), "revealable": True}
        home = self._find(data, "home")
        address = {k: v for k, v in p["address"].items() if v}
        if home:
            home[1]["fields"] = {**{k: v for k, v in home[1]["fields"].items() if k != "address"}, "address": address}
        else:
            data["entries"][self._new_id(data)] = {"name": "home", "type": "place", "fields": {"address": address},
                                                   "revealable": True, "search": True}
        self.save(data)

    def _new_id(self, data: dict) -> str:
        eid = f"e{data.get('next', 1)}"
        data["next"] = data.get("next", 1) + 1
        while eid in data["entries"]:
            eid = f"e{data['next']}"
            data["next"] += 1
        return eid

    def _strip(self, name: str, keys: tuple[str, ...]) -> None:
        """Removes some fields of an entry; the entry goes when nothing is left."""
        data = self.load()
        hit = self._find(data, name)
        if not hit:
            return
        eid, e = hit
        e["fields"] = {k: v for k, v in e["fields"].items() if k not in keys}
        if not e["fields"]:
            self.save(data)
            self.delete(name)
            return
        self.save(data)

    def set_card(self, card: dict) -> None:
        name = self.default_card_name() or "card-main"
        self.set_entry(name, "card", card)

    def set_home(self, home: dict) -> None:
        self.set_entry("home", "place", check_home(home))

    def home(self) -> dict:
        """{"location", "timezone"} of the entry `home` (daemon-internal)."""
        e = self.entry("home")
        f = (e or {}).get("fields") or {}
        return {k: f[k] for k in ("location", "timezone") if f.get(k)}

    def clear(self, what: str) -> None:
        if what == "card":
            name = self.default_card_name()
            if name:
                self.delete(name)
        elif what == "profile":
            if self.entry("me"):
                self.delete("me")
            self._strip("home", ("address",))
        elif what == "home":
            self._strip("home", ("location", "timezone"))
        else:
            self.delete(what)

    # --- fields ------------------------------------------------------------------------

    def default_card_name(self) -> str | None:
        data = self.load()
        e = data["entries"].get(data.get("default_card") or "")
        return e["name"] if e else None

    def canonical(self, ref: str, card: str | None = None) -> str:
        """`email` → `me.email`, `address.city` → `home.address.city`, `card.number` → `<card>.number` (the given or
        the default card); `work.address.city` stays."""
        ref = str(ref or "").strip()
        if ref in ALIASES:
            return ALIASES[ref]
        if ref.startswith("address."):
            return "home." + ref
        if ref.startswith("card."):
            return f"{card or self.default_card_name() or 'card-main'}.{ref[5:]}"
        return ref

    def split(self, ref: str, card: str | None = None) -> tuple[dict, str]:
        """(entry, field) for a reference; raises if the entry or field doesn't exist."""
        canon = self.canonical(ref, card)
        name, _, fld = canon.partition(".")
        e = self.entry(name)
        if e is None:
            raise ToolError(f"no personal info entry {name!r} (the owner adds it on the server: "
                            f"`ampi-tools info-set {name}`)")
        if fld not in TYPE_FIELDS[e["type"]]:
            raise ToolError(f"{name} is a {e['type']}; its fields: {', '.join(TYPE_FIELDS[e['type']])}")
        return e, fld

    def kind(self, ref: str, card: str | None = None) -> str | None:
        """What kind of form field a reference is (the matching rules are per kind): `me.email` → `email`,
        `work.address.city` → `address.city`, `card-x.number` → `card.number`; None = not fillable."""
        e, fld = self.split(ref, card)
        if e["type"] == "card":
            return None if fld == "label" else f"card.{fld}"
        if e["type"] in ("person", "place") and fld in FILLABLE:
            return fld
        return None

    def value(self, ref: str, card: str | None = None) -> str:
        """One field's value (daemon-internal: only sent to the checkout controller, used for searches, or, after
        approval, revealed to Ampi)."""
        e, fld = self.split(ref, card)
        f = e["fields"]
        if e["type"] == "person":
            parts = (f.get("name") or "").split()
            v = {"first_name": parts[0] if parts else "",
                 "last_name": " ".join(parts[1:]) if len(parts) > 1 else ""}.get(fld, f.get(fld, ""))
        elif e["type"] == "place":
            a = f.get("address") or {}
            v = _address_full(a) if fld == "address.full" else a.get(fld[8:], "") if fld.startswith("address.") \
                else f.get(fld, "")
        elif e["type"] == "card":
            if fld == "exp":
                v = f"{int(f.get('exp_month') or 0):02d}/{int(f.get('exp_year') or 0) % 100:02d}" if f.get("exp_month") else ""
            elif fld == "exp_month":
                v = f"{int(f['exp_month']):02d}" if f.get("exp_month") else ""
            else:
                v = str(f.get(fld, "") or "")
        else:
            v = f.get("text", "")
        if not v:
            raise ToolError(f"{e['name']}.{fld} isn't stored")
        return str(v)

    def revealable(self, ref: str) -> bool:
        e, fld = self.split(ref)
        return bool(e.get("revealable")) and e["type"] != "card" and fld not in NEVER_SHOWN

    def search_places(self) -> dict[str, str]:
        """{name: "lat,lng"} of places with searches switched on and a location."""
        return {e["name"]: e["fields"]["location"] for e in self.entries()
                if e["type"] == "place" and e.get("search") and e["fields"].get("location")}

    def card_numbers(self) -> list[str]:
        return [e["fields"]["number"] for e in self.entries() if e["type"] == "card" and e["fields"].get("number")]

    def card_mask(self, name: str | None = None) -> str:
        name = name or self.default_card_name()
        e = self.entry(name) if name else None
        if not e or e["type"] != "card":
            return "no card stored" if not name else f"no card {name}"
        c = e["fields"]
        label = f" ({c['label']})" if c.get("label") else ""
        return f"{brand(c['number'])} •••• {c['number'][-4:]}, exp {c['exp_month']:02d}/{c['exp_year'] % 100:02d}{label}"

    def masked(self) -> list[str]:
        """What Ampi and /info see: which entries and fields are set, never enough to reconstruct them."""
        data = self.load()
        lines = []
        for eid, e in data["entries"].items():
            f = e["fields"]
            if e["type"] == "card":
                default = " · default" if data.get("default_card") == eid else ""
                lines.append(f"{e['name']} (card): {self.card_mask(e['name'])}{default}")
                continue
            if e["type"] == "person":
                parts = [f"name {(f.get('name') or '?')[:1]}…"] + [k for k in ("email", "phone", "birthday") if f.get(k)]
            elif e["type"] == "place":
                a = f.get("address") or {}
                parts = ([f"address ({a.get('city', '?')[:1]}…, {a.get('country', '?')})"] if a else []) + \
                        [k for k in ("company", "location", "timezone") if f.get(k)]
                if e.get("search"):
                    parts.append("used for searches")
            else:
                parts = ["text"]
            flag = "" if e.get("revealable") else " · never shown to Ampi"
            lines.append(f"{e['name']} ({e['type']}): " + ", ".join(parts) + flag)
        if not lines:
            lines.append("nothing stored yet (the owner adds entries on the server: `ampi-tools info-set NAME`)")
        return lines

    def sensitive_values(self, level: str = "all") -> list[str]:
        """Strings masked in what Ampi sees: every stored value of 6+ characters in every entry (card numbers too;
        the CVC is never matched: too short). level="places" (Maps, Wolt): only what pins the owner down — street
        lines, full addresses, email, phone, names, notes — not the city, region or company, so places in the
        owner's own city stay readable."""
        broad = ("city", "region", "postal", "company") if level == "places" else ()
        out = []
        for e in self.entries():
            f = e["fields"]
            vals = [v for k, v in f.items() if isinstance(v, str) and k not in ("timezone", "country", "label") + broad]
            vals += [v for k, v in (f.get("address") or {}).items() if k != "country" and k not in broad]
            if e["type"] == "place" and f.get("address"):
                vals.append(_address_full(f["address"]))
            out += [v for v in vals if isinstance(v, str) and len(v) >= 6]
        return sorted(set(out), key=len, reverse=True)

    def locations(self) -> list[tuple[float, float]]:
        out = []
        for e in self.entries():
            m = LATLNG.fullmatch(str(e["fields"].get("location") or ""))
            if m:
                out.append((float(m.group(1)), float(m.group(2))))
        return out

    # --- grants ------------------------------------------------------------------------

    def grant(self, origin: str, fields: list[str]) -> None:
        now = self.clock()
        self.db.execute("INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?)",
                        (origin, ",".join(sorted(self.canonical(f) for f in fields)), now, now + GRANT_DAYS * 86400))

    def granted(self, origin: str, fields: list[str]) -> bool:
        """True if one grant for this exact origin covers all these fields."""
        want = {self.canonical(f) for f in fields}
        for r in self.db.execute("SELECT fields FROM grants WHERE origin=? AND expires>?", (origin, self.clock())):
            if want <= set(r["fields"].split(",")):
                return True
        return False

    def granted_fields(self, origin: str) -> set[str]:
        out: set[str] = set()
        for r in self.db.execute("SELECT fields FROM grants WHERE origin=? AND expires>?", (origin, self.clock())):
            out |= set(r["fields"].split(","))
        return out

    def grants(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM grants WHERE expires>? ORDER BY origin", (self.clock(),))]

    def revoke(self, origin: str | None) -> int:
        cur = self.db.execute("DELETE FROM grants" + ("" if origin is None else " WHERE origin=?"),
                              () if origin is None else (origin,))
        return cur.rowcount

    # --- purchases -----------------------------------------------------------------------

    def log_purchase(self, origin: str, amount: str, currency: str, what: str, status: str, approval: int | None,
                     card: str | None = None) -> None:
        self.db.execute("INSERT INTO purchases (at, origin, amount, currency, what, card, status, approval) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.clock(), origin, amount, currency, what[:200], self.card_mask(card), status, approval))

    def purchases_today(self) -> int:
        since = self.clock() - 86400
        return self.db.execute("SELECT COUNT(*) FROM purchases WHERE at>? AND status IN ('submitted', 'unknown')",
                               (since,)).fetchone()[0]

    def purchases(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,))]


def mask_cards(text: str, cards: list[str]) -> str:
    """Only the stored card numbers (with any separators between digits), not every Luhn-valid run."""
    for number in cards:
        if number:
            text = re.sub(r"[^0-9]{0,3}".join(re.escape(d) for d in number), "[vault]", text)
    return text


def _dms(deg: str, minutes: str, sec: str | None, hemi: str) -> float:
    v = float(deg) + float(minutes) / 60 + (float(sec) / 3600 if sec else 0)
    return -v if hemi.upper() in "SW" else v


_DEC = re.compile(r"-?\d{1,3}\.\d+")
_DMS = re.compile(r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′]\s*(?:(\d{1,2}(?:\.\d+)?)\s*[\"″])?\s*([NSEWnsew])")


def mask_coords(text: str, locations: list[tuple[float, float]], radius_deg: float = 0.002) -> str:
    """Coordinates of a stored place become `[place]`, in any common form: "lat,lng" or "lng,lat", URL-encoded
    (`%2C`), query strings (`lat=…&lng=…`), JSON (`"latitude": …`), degrees/minutes/seconds, and rounded values
    (the tolerance grows with fewer decimals: 35.19,33.38 still matches)."""
    if not locations or not text:
        return text
    text = re.sub(r"%2[Cc]", ",", text) if "%2" in text else text

    def near(a: float, b: float, decimals: int) -> bool:
        tol = max(radius_deg, 0.5 * 10 ** -decimals + radius_deg)
        return any((abs(a - la) <= tol and abs(b - lo) <= tol) or (abs(b - la) <= tol and abs(a - lo) <= tol)
                   for la, lo in locations)

    spans: list[tuple[int, int]] = []
    nums = [(m.start(), m.end(), float(m.group(0)), len(m.group(0).split(".")[1])) for m in _DEC.finditer(text)]
    for i, (s1, e1, v1, d1) in enumerate(nums):
        for s2, e2, v2, d2 in nums[i + 1:i + 4]:
            if s2 - e1 > 40:
                break
            if near(v1, v2, min(d1, d2)):
                spans += [(s1, e1), (s2, e2)]
    dms = [(m.start(), m.end(), _dms(*m.groups())) for m in _DMS.finditer(text)]
    for i, (s1, e1, v1) in enumerate(dms):
        for s2, e2, v2 in dms[i + 1:i + 2]:
            if s2 - e1 <= 40 and near(v1, v2, 4):
                spans += [(s1, e1), (s2, e2)]
    for s, e in sorted(set(spans), reverse=True):
        text = text[:s] + "[place]" + text[e:]
    return text



def _loose(value: str) -> str:
    """A pattern for a value as pages may print it: any whitespace run (incl. no-break spaces, `+`, `%20`), and
    `@` also as `%40`."""
    parts = []
    for ch in value:
        if ch.isspace():
            parts.append(r"(?:\s|\+|%20|&nbsp;)+")
        elif ch == "@":
            parts.append(r"(?:@|%40)")
        else:
            parts.append(re.escape(ch))
    return "".join(parts).replace(r"(?:\s|\+|%20|&nbsp;)+(?:\s|\+|%20|&nbsp;)+", r"(?:\s|\+|%20|&nbsp;)+")


def mask_text(text: str, values: list[str], card_number: str | None, keep: list[str] | None = None) -> str:
    """Replaces vault values (case-insensitive, loose spacing) and card numbers by `[vault]`: the stored card with
    any separators between digits, and any other Luhn-valid 13–19 digit run. Values in `keep` (allowed on this
    site) stay whole even when a masked value (e.g. the first name) occurs inside them."""
    out = text
    kept: list[str] = []
    for v in sorted(keep or [], key=len, reverse=True):
        if v:
            def stash(m: re.Match) -> str:
                kept.append(m.group(0))
                return f"\x00{len(kept) - 1}\x00"
            out = re.sub(_loose(v), stash, out, flags=re.I)
    if card_number:
        out = re.sub(r"[^0-9]{0,3}".join(re.escape(d) for d in card_number), "[vault]", out)
    out = CARD_RUN.sub(lambda m: "[vault]" if luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0), out)
    for v in sorted(values, key=len, reverse=True):
        if v:
            out = re.sub(_loose(v), "[vault]", out, flags=re.I)
    return re.sub("\x00(\\d+)\x00", lambda m: kept[int(m.group(1))], out) if kept else out


def mask_obj(obj: Any, values: list[str], card_number: str | None, keep: list[str] | None = None) -> Any:
    """mask_text applied to every string inside a snapshot (labels, options, placeholders too)."""
    if isinstance(obj, str):
        return mask_text(obj.replace("\x00", ""), values, card_number, keep)
    if isinstance(obj, list):
        return [mask_obj(x, values, card_number, keep) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_obj(v, values, card_number, keep) for k, v in obj.items()}
    return obj
