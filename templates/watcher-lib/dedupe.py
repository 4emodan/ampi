"""Remembers keys it has seen (in a space file) and stops the run on a repeat.

Args:
  field  the key, e.g. "item.id" or "steps.offer.url" (default "item.id")
  file   space file for the seen keys (default "seen.txt")
  keep   how many keys to remember (default 5000; oldest are dropped)
  stop   true (default): stop the run when the key was seen before
Returns: {"new": bool, "key": "..."}
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
    key = get(req, args.get("field", "item.id"))
    key = key if isinstance(key, str) else json.dumps(key, sort_keys=True, ensure_ascii=False)
    key = key.replace("\n", " ").strip()
    f = space_file(args.get("file", "seen.txt"))
    seen = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
    new = key not in set(seen)
    if new:
        seen = (seen + [key])[-int(args.get("keep", 5000)):]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(seen) + "\n", encoding="utf-8")
    out = {"new": new, "key": key}
    if not new and args.get("stop", True):
        out["stop"] = True
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
