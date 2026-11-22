"""Finds http(s) links in text, optionally only on some domains.

Args:
  field    where the text is (default "input")
  domains  keep only links on these domains or their subdomains, e.g. ["tickets.example.com"]
  contains keep only links containing one of these substrings
  limit    maximum links (default 50)
Returns: {"links": ["https://...", ...], "count": n}
"""

import json
import re
import sys
from urllib.parse import urlsplit

URL = re.compile(r"https?://[^\s<>\"'()\[\]]+")


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
    domains = [d.lower().lstrip(".") for d in args.get("domains") or []]
    contains = args.get("contains") or []
    out = []
    for url in URL.findall(text):
        url = url.rstrip(".,;:!?")
        host = (urlsplit(url).hostname or "").lower()
        if domains and not any(host == d or host.endswith("." + d) for d in domains):
            continue
        if contains and not any(c in url for c in contains):
            continue
        if url not in out:
            out.append(url)
    out = out[: int(args.get("limit", 50))]
    return {"links": out, "count": len(out)}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
