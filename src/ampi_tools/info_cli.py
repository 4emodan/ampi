"""The owner's commands for the personal info directory, on the server as user ampitools (never through Telegram):

    ampi-tools info-show                       what's stored (masked) and the active site permissions
    ampi-tools info-set NAME [FIELD]           create an entry, or change all / one of its fields
    ampi-tools info-delete NAME                delete an entry (its site permissions go with it)
    ampi-tools info-default-card NAME          the card used when Ampi doesn't name one
    ampi-tools info-search NAME on|off         use a place for searches near it (Maps, Wolt, routes)
    ampi-tools info-reveal NAME on|off         may Ampi ask to see this entry's values (cards: never)

Secret card values are typed without echo. Restart ampi-tools afterwards.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from .todoist import ToolError
from .vault import LATLNG, TYPE_FIELDS, TYPES, Vault, check_name

TYPE_BY_NAME = {"me": "person", "home": "place", "work": "place"}
PROMPTS = {
    "person": [("name", "Full name"), ("email", "Email"), ("phone", "Phone"), ("birthday", "Birthday (YYYY-MM-DD)")],
    "place": [("company", "Company (for a workplace)"), ("address.line1", "Street address"), ("address.line2", "Line 2"),
              ("address.postal", "Postal code"), ("address.city", "City"), ("address.region", "Region/state"),
              ("address.country", "Country (2 letters)"),
              ("location", "Location: latitude,longitude or an address to look up"),
              ("timezone", "Timezone, e.g. Europe/Nicosia")],
    "card": [("number", "Card number"), ("exp_month", "Expiry month (MM)"), ("exp_year", "Expiry year (YY or YYYY)"),
             ("cvc", "CVC"), ("name", "Name on the card"), ("label", "Label, e.g. virtual, 100 € limit")],
    "note": [("text", "Text")],
}
SECRET = {"number", "cvc"}


def _fields_from(kind: str, answers: dict[str, str]) -> dict:
    out: dict = {}
    for k, v in answers.items():
        if not v:
            continue
        if k.startswith("address."):
            out.setdefault("address", {})[k[8:]] = v
        else:
            out[k] = v
    return out


def _ask(kind: str, only: str | None, current: dict | None, geocode: Callable[[str], str],
         ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass) -> dict:
    prompts = [p for p in PROMPTS[kind] if only is None or p[0] == only]
    if not prompts:
        raise ToolError(f"a {kind} has the fields: {', '.join(k for k, _ in PROMPTS[kind])}")
    answers = {}
    for key, label in prompts:
        hint = " [Enter keeps it]" if current else ""
        value = (secret if key in SECRET else ask)(f"{label}{hint}: ").strip()
        if key == "location" and value and not LATLNG.fullmatch(value):
            value = geocode(value)
        answers[key] = value
    return _fields_from(kind, answers)


def run(home: Path, command: str, args: list[str], geocode: Callable[[str], str],
        ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass) -> str:
    vault = Vault(home)
    if command == "info-show":
        grants = vault.grants()
        g = "\n".join(f"  {r['origin']}: {r['fields']}" for r in grants) or "  none"
        return "Stored (masked):\n  " + "\n  ".join(vault.masked()) + f"\nSite permissions (30 days):\n{g}"
    if command == "info-set":
        if not args:
            raise ToolError("info-set NAME [FIELD]")
        name, only = args[0], (args[1] if len(args) > 1 else None)
        e = vault.entry(name)
        if not e:
            check_name(name)  # before asking anything (a card number typed for nothing)
        if e:
            kind = e["type"]
        else:
            kind = TYPE_BY_NAME.get(name) or ("card" if name.startswith("card") else None)
            if kind is None:
                kind = ask(f"Type of {name} ({'/'.join(TYPES)}): ").strip().lower()
                if kind not in TYPES:
                    raise ToolError(f"the type is one of {', '.join(TYPES)}")
            if only:
                raise ToolError(f"{name} doesn't exist yet; create it with `info-set {name}` first")
        if only and only not in TYPE_FIELDS.get(kind, ()) and not any(k == only for k, _ in PROMPTS.get(kind, [])):
            raise ToolError(f"a {kind} has the fields: {', '.join(k for k, _ in PROMPTS.get(kind, []))}")
        if kind == "card" and not e:
            print("A card: use a virtual card with its own limit. The number and CVC aren't shown as you type.")
        fields = _ask(kind, only, e["fields"] if e else None, geocode, ask, secret)
        vault.set_entry(name, kind, fields)
        line = next(x for x in vault.masked() if x.startswith(f"{name} ("))
        return f"Saved: {line}\nNow: systemctl restart ampi-tools claude-assistant"
    if command == "info-delete":
        if not args:
            raise ToolError("info-delete NAME")
        if ask(f"Type {args[0]} to delete it and its site permissions: ").strip() != args[0]:
            return "Not deleted."
        vault.delete(args[0])
        return f"Deleted {args[0]}."
    if command == "info-default-card":
        if not args:
            raise ToolError("info-default-card NAME")
        vault.set_default_card(args[0])
        return f"Default card: {args[0]} ({vault.card_mask(args[0])})."
    if command in ("info-search", "info-reveal"):
        if len(args) != 2 or args[1] not in ("on", "off"):
            raise ToolError(f"{command} NAME on|off")
        vault.set_flag(args[0], "search" if command == "info-search" else "revealable", args[1] == "on")
        return f"{args[0]}: {'searches' if command == 'info-search' else 'can be shown to Ampi'} {args[1]}. Now: " \
               "systemctl restart ampi-tools"
    raise ToolError(f"unknown command {command}")


def main(home: Path, command: str, args: list[str], geocode: Callable[[str], str]) -> None:
    try:
        print(run(home, command, args, geocode))
    except ToolError as e:
        sys.exit(f"Not done: {e}")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled.")
