"""Collects items into a digest (a space file) and hands them all over when it's time.

Use one watcher step with mode "add" on each new item, and a time-triggered run with mode "flush".

Args:
  mode    "add" (default) or "flush"
  fields  for add: {"name": "path"} to store, e.g. {"subject": "item.subject", "from": "item.from"}
  file    space file (default "digest.json")
  max     keep at most this many items (default 500; oldest dropped)
  stop    for add: true (default) stops the run after adding; for flush: true stops when empty
Returns: add: {"count": n}; flush: {"items": [...], "count": n}
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
    f = space_file(args.get("file", "digest.json"))
    items = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
    f.parent.mkdir(parents=True, exist_ok=True)
    if args.get("mode", "add") == "flush":
        f.write_text("[]", encoding="utf-8")
        out = {"items": items, "count": len(items)}
        if not items and args.get("stop", True):
            out["stop"] = True
        return out
    fields = args.get("fields") or {"item": "item"}
    items.append({name: get(req, str(path)) for name, path in fields.items()})
    items = items[-int(args.get("max", 500)):]
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    out = {"count": len(items)}
    if args.get("stop", True):
        out["stop"] = True
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
