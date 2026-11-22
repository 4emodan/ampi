"""Compares text with the version saved last time (in a space file) and keeps the new one.

Args:
  field    where the text is (default "input")
  file     space file for the last version (default "last.txt")
  ignore   regular expressions; matching lines are ignored (dates, counters, ads)
  stop     true (default): stop the run when nothing changed (the first run only saves and stops)
Returns: {"changed": bool, "added": ["line", ...], "removed": ["line", ...], "first": bool}
"""

import json
import re
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


def lines(text, ignore):
    out = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and not any(re.search(p, line, re.I) for p in ignore):
            out.append(line)
    return out


def main(req):
    args = req.get("args") or {}
    value = get(req, args.get("field", "input"), "")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    ignore = args.get("ignore") or []
    f = space_file(args.get("file", "last.txt"))
    first = not f.is_file()
    old = lines(f.read_text(encoding="utf-8"), ignore) if not first else []
    new = lines(text, ignore)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    old_set, new_set = set(old), set(new)
    added = [line for line in new if line not in old_set][:200]
    removed = [line for line in old if line not in new_set][:200]
    changed = not first and bool(added or removed)
    out = {"changed": changed, "added": added, "removed": removed, "first": first}
    if not changed and args.get("stop", True):
        out["stop"] = True
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
