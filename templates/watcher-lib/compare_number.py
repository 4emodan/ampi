"""Compares a number with a threshold, or with its value last time (kept in a space file).

Args:
  field      where the number is, e.g. "steps.price.lowest"
  op         "<", "<=", ">", ">=", "==", "!=", "dropped" (lower than last time), "rose", "changed"
  threshold  the number to compare with (for < <= > >= == !=)
  file       space file remembering the last value (default "last_<field>.json"); used by dropped/rose/changed
  stop       true (default): stop the run when the comparison fails (or there is no number)
Returns: {"passed": bool, "value": 12.5, "previous": 14.0 | null}
"""

import json
import operator
import re
import sys
from pathlib import Path

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq, "!=": operator.ne}


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


def space_file(name):
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        raise SystemExit("space file names must be relative, without ..")
    return Path("space") / (p.relative_to("space") if p.parts and p.parts[0] == "space" else p)


def main(req):
    args = req.get("args") or {}
    field = args.get("field", "input")
    raw = get(req, field)
    try:
        value = float(raw) if raw is not None and not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        value = None
    op = args.get("op", "<")
    f = space_file(args.get("file") or "last_" + re.sub(r"[^\w]+", "_", field) + ".json")
    previous = None
    if f.is_file():
        try:
            previous = json.loads(f.read_text(encoding="utf-8")).get("value")
        except ValueError:
            previous = None
    if value is None:
        passed = False
    elif op in OPS:
        passed = OPS[op](value, float(args.get("threshold", 0)))
    elif op == "dropped":
        passed = previous is not None and value < previous
    elif op == "rose":
        passed = previous is not None and value > previous
    elif op == "changed":
        passed = previous is not None and value != previous
    else:
        raise SystemExit(f"unknown op {op!r}")
    if value is not None and op in ("dropped", "rose", "changed"):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"value": value}), encoding="utf-8")
    out = {"passed": passed, "value": value, "previous": previous}
    if not passed and args.get("stop", True):
        out["stop"] = True
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
