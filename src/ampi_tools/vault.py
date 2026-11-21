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


# --- the vault --------------------------------------------------------------------------------------


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
        data = self.load()
        if data.get("card"):
            register_secret(data["card"]["number"])  # the CVC is too short to register (it would garble output)
            register_secret(" ".join(data["card"]["number"][i:i + 4] for i in range(0, len(data["card"]["number"]), 4)))

    # --- storage -------------------------------------------------------------------------

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def set_profile(self, profile: dict) -> None:
        data = self.load()
        data["profile"] = check_profile(profile)
        self.save(data)

    def set_card(self, card: dict) -> None:
        data = self.load()
        data["card"] = check_card(card)
        register_secret(data["card"]["number"])
        self.save(data)

    def clear(self, what: str) -> None:
        data = self.load()
        data.pop(what, None)
        self.save(data)

    # --- values ------------------------------------------------------------------------

    def value(self, field: str) -> str:
        """One field's value (daemon-internal: only sent to the checkout controller or, after approval, to Ampi)."""
        data = self.load()
        p, c = data.get("profile") or {}, data.get("card") or {}
        a = p.get("address") or {}
        if field.startswith("card.") and not c:
            raise ToolError("no card is stored (`ampi-tools vault-set card` on the server)")
        if not field.startswith("card.") and not p:
            raise ToolError("no personal details are stored (`ampi-tools vault-set profile` on the server)")
        parts = (p.get("name") or "").split()
        values = {
            "name": p.get("name", ""), "first_name": parts[0] if parts else "",
            "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
            "email": p.get("email", ""), "phone": p.get("phone", ""),
            "address.line1": a.get("line1", ""), "address.line2": a.get("line2", ""),
            "address.postal": a.get("postal", ""), "address.city": a.get("city", ""),
            "address.region": a.get("region", ""), "address.country": a.get("country", ""),
            "address.full": ", ".join(x for x in (a.get("line1"), a.get("line2"),
                                                  f"{a.get('postal', '')} {a.get('city', '')}".strip(),
                                                  a.get("region"), a.get("country")) if x),
            "card.number": c.get("number", ""),
            "card.exp": f"{int(c.get('exp_month') or 0):02d}/{int(c.get('exp_year') or 0) % 100:02d}",
            "card.exp_month": f"{int(c.get('exp_month') or 0):02d}", "card.exp_year": str(c.get("exp_year", "")),
            "card.cvc": c.get("cvc", ""), "card.name": c.get("name", ""),
        }
        if field not in values:
            raise ToolError(f"unknown field {field!r}")
        if not values[field]:
            raise ToolError(f"{field} isn't stored")
        return values[field]

    def card_mask(self) -> str:
        c = self.load().get("card")
        if not c:
            return "no card stored"
        return f"{brand(c['number'])} •••• {c['number'][-4:]}, exp {c['exp_month']:02d}/{c['exp_year'] % 100:02d}"

    def masked(self) -> list[str]:
        """What `info fields` shows: which fields are set, never enough to reconstruct them."""
        data = self.load()
        p = data.get("profile") or {}
        lines = []
        if p:
            email = p.get("email", "")
            user, _, domain = email.partition("@")
            lines.append(f"name: {(p.get('name') or '?')[:1]}… ({len((p.get('name') or '').split())} words)")
            lines.append(f"email: {user[:1]}…@{domain[:1]}…{domain[-3:] if len(domain) > 3 else ''}")
            lines.append(f"phone: {'set' if p.get('phone') else 'not set'}")
            a = p.get("address") or {}
            lines.append(f"address: set ({a.get('city', '?')[:1]}…, {a.get('country', '?')})")
        else:
            lines.append("personal details: not stored")
        lines.append(f"card: {self.card_mask()}")
        return lines

    def sensitive_values(self) -> list[str]:
        """Strings to mask in anything shown to Ampi unless approved for that site: the card number (the CVC is
        never matched: too short) and profile values of 6+ characters."""
        data = self.load()
        out = []
        c = data.get("card") or {}
        if c:
            out.append(c["number"])
        p = data.get("profile") or {}
        a = p.get("address") or {}
        for v in (p.get("name"), p.get("email"), p.get("phone"), a.get("line1"), a.get("line2")):
            if v and len(v) >= 6:
                out.append(v)
        return out

    # --- grants ------------------------------------------------------------------------

    def grant(self, origin: str, fields: list[str]) -> None:
        now = self.clock()
        self.db.execute("INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?)",
                        (origin, ",".join(sorted(fields)), now, now + GRANT_DAYS * 86400))

    def granted(self, origin: str, fields: list[str]) -> bool:
        """True if one grant for this exact origin covers all these fields."""
        want = set(fields)
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

    def log_purchase(self, origin: str, amount: str, currency: str, what: str, status: str, approval: int | None) -> None:
        self.db.execute("INSERT INTO purchases (at, origin, amount, currency, what, card, status, approval) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.clock(), origin, amount, currency, what[:200], self.card_mask(), status, approval))

    def purchases_today(self) -> int:
        since = self.clock() - 86400
        return self.db.execute("SELECT COUNT(*) FROM purchases WHERE at>? AND status IN ('submitted', 'unknown')",
                               (since,)).fetchone()[0]

    def purchases(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,))]


def mask_text(text: str, values: list[str], card_number: str | None) -> str:
    """Replaces vault values (exact, case-insensitive) and the card number with any separators by `[vault]`."""
    out = text
    if card_number:
        pattern = r"[ \-.]?".join(re.escape(d) for d in card_number)
        out = re.sub(pattern, "[vault]", out)
    for v in sorted(values, key=len, reverse=True):
        if v:
            out = re.sub(re.escape(v), "[vault]", out, flags=re.I)
    return out


def mask_obj(obj: Any, values: list[str], card_number: str | None) -> Any:
    """mask_text applied to every string inside a snapshot (labels, options, placeholders too)."""
    if isinstance(obj, str):
        return mask_text(obj, values, card_number)
    if isinstance(obj, list):
        return [mask_obj(x, values, card_number) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_obj(v, values, card_number) for k, v in obj.items()}
    return obj
