"""Finds dates in text: 2026-03-12, 12.03.2026, 12/03/2026 (day first), "12 March 2026", "March 12, 2026".

Args:
  field  where the text is (default "input")
  after  keep only dates on or after this ISO date (e.g. today's date from "item.time")
Returns: {"dates": ["2026-03-12", ...], "first": "2026-03-12" | null}
"""

import json
import re
import sys
from datetime import date

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DMY = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
D_MON_Y = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
MON_D_Y = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


def get(req, path, default=None):
    cur = req
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
        if cur is None:
            return default
    return cur


def make(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def main(req):
    args = req.get("args") or {}
    text = str(get(req, args.get("field", "input"), "") or "")
    found = []
    for m in ISO.finditer(text):
        found.append((m.start(), make(*m.groups())))
    for m in DMY.finditer(text):
        found.append((m.start(), make(m.group(3), m.group(2), m.group(1))))
    for m in D_MON_Y.finditer(text):
        mon = MONTHS.get(m.group(2)[:3].lower())
        found.append((m.start(), make(m.group(3), mon, m.group(1)) if mon else None))
    for m in MON_D_Y.finditer(text):
        mon = MONTHS.get(m.group(1)[:3].lower())
        found.append((m.start(), make(m.group(3), mon, m.group(2)) if mon else None))
    dates = []
    for _, d in sorted(found, key=lambda x: x[0]):
        if d and d not in dates and (not args.get("after") or d >= str(args["after"])[:10]):
            dates.append(d)
    return {"dates": dates[:100], "first": dates[0] if dates else None}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
