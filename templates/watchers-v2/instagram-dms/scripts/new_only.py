"""Picks the messages to report now: those after the last one already reported in each conversation, oldest first,
at most BATCH_MESSAGES / BATCH_CHARS (the rest stays for the next run). Saves nothing: commit.py moves the cursor
once the message to the owner was sent, so a failed run repeats messages rather than losing them."""

import json
import sys
from pathlib import Path

CURSORS = Path("space") / "cursors.json"
BATCH_MESSAGES = 40
BATCH_CHARS = 10000


def cursors():
    """{conversation: (timestamp, {ids reported at that timestamp})}; anything unreadable counts as not reported."""
    try:
        raw = json.loads(CURSORS.read_text(encoding="utf-8"))
        items = raw.items()
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for k, v in items:
        try:
            out[k] = (int(v["ts"]), {str(i) for i in list(v["ids"])})
        except (TypeError, ValueError, KeyError):
            pass
    return out


def main(req):
    args = req.get("args") or {}
    seen = cursors()
    waiting, notes = [], []
    for t in args.get("threads") or []:
        ts, ids = seen.get(t["thread"], (0, set()))
        new = [i for i in t["items"] if int(i["ts"]) > ts or (int(i["ts"]) == ts and i["id"] not in ids)]
        who = ", ".join("@" + u for u in t["with"]) or "?"
        head = f"group {t['title']} ({who})" if t["group"] else who
        if new and t.get("has_older") and int(t.get("oldest") or 0) > max(ts, int(t.get("seen") or 0)):
            notes.append("Instagram didn't send the earlier messages of a conversation: see the app.")
        waiting += [(int(i["ts"]), i["id"], t["thread"], head, i) for i in new]
    if not waiting:
        return {"stop": True}
    waiting.sort(key=lambda m: m[:3])
    picked, size = [], 0
    for m in waiting:
        n = len(m[3]) + len(m[4]["text"]) + 80
        if picked and (len(picked) >= BATCH_MESSAGES or size + n > BATCH_CHARS):
            break
        picked.append(m)
        size += n
    commit, groups = {}, {}
    for ts, mid, thread, head, i in picked:
        old_ts, old_ids = seen.get(thread, (0, set()))
        c = commit.setdefault(thread, {"ts": old_ts, "ids": sorted(old_ids)})
        commit[thread] = {"ts": ts, "ids": c["ids"] + [mid] if ts == c["ts"] else [mid]}
        groups.setdefault(thread, [f"[conversation] {head}"]).append(f"  - @{i['from']} - {i['time']}: {i['text']}")
    left = len(waiting) - len(picked)
    if left:
        notes.append(f"{left} more new message(s) follow in the next check.")
    if args.get("more_threads"):
        notes.append("There may be more unread conversations: see the app.")
    text = "\n".join(line for g in groups.values() for line in g)
    if args.get("test"):
        text = "(test data, not your real messages)\n" + text
    return {"text": text, "count": len(picked), "note": "\n".join(dict.fromkeys(notes)), "commit": commit}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
