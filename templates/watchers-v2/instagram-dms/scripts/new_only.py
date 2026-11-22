"""Keeps only messages newer than the last one already reported in each conversation (space/cursors.json),
so an old unread message is summarized once, not again with every new one. Stops the run when nothing is new."""

import json
import sys
from pathlib import Path

CURSORS = Path("space") / "cursors.json"
KEEP = 500  # conversations remembered


def main(req):
    threads = (req.get("args") or {}).get("threads") or []
    try:
        cursors = json.loads(CURSORS.read_text(encoding="utf-8")) if CURSORS.is_file() else {}
    except ValueError:
        cursors = {}
    lines, count = [], 0
    for t in threads:
        last = int(cursors.get(t["thread"], 0))
        new = [i for i in t["items"] if int(i["ts"]) > last]
        if not new:
            continue
        cursors[t["thread"]] = max(int(i["ts"]) for i in new)
        who = ", ".join("@" + u for u in t["with"]) or "?"
        lines.append(f"[conversation] {('group ' + t['title'] + ' (' + who + ')') if t['group'] else who}")
        lines += [f"  - @{i['from']} · {i['time']}: {i['text']}" for i in new]
        count += len(new)
    if not count:
        return {"stop": True}
    CURSORS.parent.mkdir(parents=True, exist_ok=True)
    CURSORS.write_text(json.dumps(dict(list(cursors.items())[-KEEP:])), encoding="utf-8")
    if (req.get("args") or {}).get("test"):
        lines.insert(0, "(test data, not your real messages)")
    return {"text": "\n".join(lines), "count": count}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
