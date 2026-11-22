"""Reads or changes a list kept in a space file (one entry per line), e.g. known sender addresses.

Args:
  file   space file (required), e.g. "pascal-addresses.txt"
  op     "list" (default), "has", "add", "remove"
  field  for has/add/remove: where the value is, e.g. "steps.sender.address"; or give "value" directly
  value  the value itself
  stop   for has: true stops the run when the value is not in the list (default false)
Returns: list: {"entries": [...]}; has: {"has": bool}; add/remove: {"changed": bool, "count": n}
"""

import json
import sys
from pathlib import Path


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
    f = space_file(args["file"])
    entries = [line.strip() for line in f.read_text(encoding="utf-8").splitlines() if line.strip()] if f.is_file() else []
    op = args.get("op", "list")
    if op == "list":
        return {"entries": entries}
    value = args.get("value") if "value" in args else get(req, str(args.get("field", "input")))
    value = " ".join(str(value or "").split())
    if op == "has":
        has = value.lower() in {e.lower() for e in entries}
        return {"has": has, **({"stop": True} if not has and args.get("stop") else {})}
    changed = False
    if op == "add" and value and value.lower() not in {e.lower() for e in entries}:
        entries.append(value)
        changed = True
    elif op == "remove":
        kept = [e for e in entries if e.lower() != value.lower()]
        changed, entries = len(kept) != len(entries), kept
    elif op not in ("add", "remove"):
        raise SystemExit(f"unknown op {op!r}")
    if changed:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    return {"changed": changed, "count": len(entries)}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
