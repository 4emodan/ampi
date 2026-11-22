"""Checks text for keywords or a regular expression.

Args:
  field     where the text is, e.g. "item.subject" or "steps.page.text" (default "input")
  any       keywords; matches if any appears (case-insensitive)
  all       keywords; matches only if all appear
  none      keywords; never matches if any appears
  regex     a Python regular expression (case-insensitive)
  stop      true (default): stop the run when it doesn't match
Returns: {"matched": bool, "hits": ["keyword", ...], "groups": [...regex groups of the first match]}
"""

import json
import re
import sys


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


def main(req):
    args = req.get("args") or {}
    value = get(req, args.get("field", "input"), "")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    low = text.lower()
    hits = [k for k in (args.get("any") or []) + (args.get("all") or []) if str(k).lower() in low]
    ok = True
    if args.get("any"):
        ok = any(str(k).lower() in low for k in args["any"])
    if ok and args.get("all"):
        ok = all(str(k).lower() in low for k in args["all"])
    if ok and args.get("none"):
        ok = not any(str(k).lower() in low for k in args["none"])
    groups = []
    if ok and args.get("regex"):
        m = re.search(args["regex"], text, re.I | re.S)
        ok = m is not None
        groups = list(m.groups()) if m else []
    out = {"matched": ok, "hits": hits, "groups": groups}
    if not ok and args.get("stop", True):
        out["stop"] = True
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
