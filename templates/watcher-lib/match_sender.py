"""Checks whether an email's sender matches patterns or a list of addresses.

Args:
  field     where the sender is (default "item.from"), e.g. 'Anna <anna@pascal.edu>'
  patterns  substrings matched case-insensitively against the whole sender, e.g. ["pascal"]
  addresses exact addresses, e.g. ["office@pascal.edu"]
  list      a file in space/ with one address (or @domain) per line; missing file = empty list
  stop      true (default): stop the run when nothing matches
Returns: {"matched": bool, "address": "anna@pascal.edu", "why": "pattern pascal" | "list" | ""}
"""

import json
import re
import sys
from pathlib import Path

ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


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
    sender = str(get(req, args.get("field", "item.from"), "") or "")
    found = ADDRESS.findall(sender)
    address = found[-1].lower() if found else ""
    why = ""
    low = sender.lower()
    for p in args.get("patterns") or []:
        if str(p).lower() and str(p).lower() in low:
            why = f"pattern {p}"
            break
    if not why and address:
        known = {str(a).strip().lower() for a in args.get("addresses") or []}
        if args.get("list"):
            f = space_file(args["list"])
            if f.is_file():
                known |= {line.strip().lower() for line in f.read_text(encoding="utf-8").splitlines()
                          if line.strip() and not line.startswith("#")}
        domain = "@" + address.split("@", 1)[1]
        if address in known or domain in known:
            why = "list"
    out = {"matched": bool(why), "address": address, "why": why}
    if not why and args.get("stop", True):
        out["stop"] = True
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
