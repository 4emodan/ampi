"""The report of what a watcher did without asking: written from the audit, never by the AI."""

from __future__ import annotations

import json
import re

from .ops import clean

ORDER = ("gmail.trash", "gmail.draft", "gmail.untrash", "gmail.star", "gmail.unstar", "todoist.add", "todoist.move",
         "gmail.archive", "gmail.mark_read", "gmail.mark_unread", "gmail.label")
TITLES = {"gmail.trash": "🗑 Moved to the trash", "gmail.draft": "📝 Drafts saved (not sent)", "gmail.untrash": "↩️ Taken out of the trash",
          "gmail.star": "⭐ Starred", "gmail.unstar": "Star removed", "todoist.add": "☑️ Tasks added",
          "todoist.move": "☑️ Tasks moved", "gmail.archive": "📥 Archived", "gmail.mark_read": "Marked as read",
          "gmail.mark_unread": "Marked as unread", "gmail.label": "🏷 Labels changed"}
FULL = {"gmail.trash", "gmail.archive", "gmail.draft", "todoist.add", "todoist.move"}  # every line; of the others the first few
SHOWN = 8
MAX = 3500


def plain(s: str, limit: int) -> str:
    """One line of outside text that Telegram makes no link of."""
    return re.sub(r"([.:@/#。．｡])", "\\1\u2060", clean(s, limit))


def units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2  # as Telegram counts


def _detail(row: dict) -> dict:
    try:
        d = json.loads(row.get("detail") or "null")
    except ValueError:
        d = None
    return d if isinstance(d, dict) else {}


def _as(row: dict) -> str:
    return _detail(row).get("as") or row["op"]


def _lines(row: dict) -> list[str]:
    detail = _detail(row)
    if detail.get("items"):
        change = f" ({plain(detail['change'], 100)})" if detail.get("change") else ""
        return [f"“{plain(str(d.get('subject')), 90)}” {plain(str(d.get('who')), 70)}{change}" for d in detail["items"]]
    body = [x.strip() for x in str(row.get("args_summary") or "").split("\n")[1:] if x.strip()]
    return [plain(" ".join(body).removeprefix("• ") or row["op"], 200)]


def texts(name: str, rows: list[dict]) -> list[str]:
    """Telegram messages (one, unless it is long)."""
    out: list[str] = []
    bad = [r for r in rows if r["status"] != "done"]
    ops = sorted({_as(r) for r in rows if r["status"] == "done"}, key=lambda o: ORDER.index(o) if o in ORDER else 99)
    for op in ops:
        mine = [r for r in rows if _as(r) == op and r["status"] == "done"]
        lines = [x for r in mine for x in _lines(r)]
        n = sum(r.get("items") or 1 for r in mine)
        out.append(f"\n{TITLES.get(op, op)}: {n}")
        shown = lines if op in FULL else lines[:SHOWN]
        out += [f"• {x}" for x in shown]
        if len(lines) > len(shown):
            out.append(f"…and {len(lines) - len(shown)} more (Ampi hub → Activity)")
    for r in bad:
        out.append(f"\n⚠️ {TITLES.get(_as(r), r['op'])}: "
                   + ("not known if it was done" if r["status"] == "unknown" else "failed (perhaps done in part)"))
        out += [f"• {x}" for x in _lines(r)]
    pages, page = [], f"📋 Watcher {name} did this without asking:"
    for line in out:
        if units(page) + units(line) + 1 > MAX:
            pages.append(page)
            page = f"📋 Watcher {name}, continued:"
        page += "\n" + line
    return [*pages, page] if out else pages
