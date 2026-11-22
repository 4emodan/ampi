"""Builds a new object from fields of the item and earlier steps (rename, reshape, defaults).

Args:
  fields   {"new_name": "path.to.value", ...}, e.g. {"subject": "item.subject", "summary": "steps.llm.summary"}
  defaults {"new_name": value} used when a path is missing
Returns: the new object
"""

import json
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
    defaults = args.get("defaults") or {}
    return {name: get(req, str(path), defaults.get(name)) for name, path in (args.get("fields") or {}).items()}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
