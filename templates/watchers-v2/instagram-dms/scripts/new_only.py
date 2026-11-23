"""Picks the messages to report now: those not reported yet, oldest first, at most BATCH_MESSAGES / BATCH_CHARS. The
batch stays the same until it was reported; after 2 failures of the AI step (or 3 of sending) that are about the messages
(not outages or limits: the engine says in failure.json) it becomes a plain count, so one bad message can't hold up the
others. commit.py marks it as reported, only after the message was sent."""

import json
import sys
from pathlib import Path

CURSORS, PENDING = Path("space") / "cursors.json", Path("space") / "pending.json"
FAILURE = Path("space") / "failure.json"
BATCH_MESSAGES, BATCH_CHARS = 40, 10000


def load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reported(v):
    """(time up to which everything counts as reported, ids reported after it, newest reported time); also from older
    versions' files."""
    try:
        if isinstance(v, int):
            return v, set(), v
        if "floor" in v:
            floor = int(v["floor"])
            return floor, {str(i) for _, i in v["ids"]}, max([floor] + [int(t) for t, _ in v["ids"]])
        return int(v["ts"]) - 1, {str(i) for i in list(v["ids"])}, int(v["ts"])
    except (TypeError, ValueError, KeyError):
        return 0, set(), 0


def count(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1000 else 0


def pairs(v):
    return {(b[0], b[1]) for b in v if isinstance(b, list) and len(b) == 2 and all(isinstance(x, str) for x in b)} \
        if isinstance(v, list) else set()


def main(req):
    args = req.get("args") or {}
    seen, pending, failure = load(CURSORS), load(PENDING), load(FAILURE)
    waiting, notes = [], []
    for t in args.get("threads") or []:
        try:
            thread, (floor, ids, newest) = t["thread"], reported(seen.get(t["thread"], 0))
            who = ", ".join("@" + u for u in t["with"]) or "?"
            head = f"group {t['title']} ({who})" if t["group"] else who
            items = t["items"]
        except (TypeError, KeyError):
            continue
        found = 0
        for i in items:
            try:
                ts, mid, text = int(i["ts"]), str(i["id"]), f"  - @{i['from']} - {i['time']}: {i['text']}"
            except (TypeError, ValueError, KeyError):
                continue
            if ts > floor and mid not in ids:
                waiting.append((ts, mid, thread, head, text))
                found += 1
        # Nothing sent reaches back to what was reported or seen. (Their own message in it means they saw what's before.)
        if found and not t.get("own") and t.get("has_older") and \
                int(t.get("oldest") or 0) > max(newest, int(t.get("seen") or 0)):
            notes.append("There may be earlier messages that Instagram didn't send: see the app.")
    waiting.sort()
    batch = pairs(pending.get("ids"))
    picked = [m for m in waiting if (m[2], m[1]) in batch]
    if not picked:
        pending, failure, size = {}, {}, 0
        for m in waiting:
            n = len(m[3]) + len(m[4]) + 20
            if picked and (len(picked) >= BATCH_MESSAGES or size + n > BATCH_CHARS):
                break
            picked.append(m)
            size += n
    if not picked:
        return {"stop": True}
    ai, send = count(pending.get("ai")), count(pending.get("send"))
    if failure.get("transient") is False:
        ai += failure.get("step") == "summary"
        send += failure.get("step") == "send"
    plain = ai >= 2 or send >= 3
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({"ids": [[m[2], m[1]] for m in picked], "ai": ai, "send": send}), encoding="utf-8")
    FAILURE.unlink(missing_ok=True)
    commit, groups = {}, {}
    for ts, mid, thread, head, text in picked:
        commit.setdefault(thread, []).append([ts, mid])
        groups.setdefault(thread, [f"[conversation] {head}"]).append(text)
    left = len(waiting) - len(picked)
    if left:
        notes.append(f"{left} more new message(s) follow in the next check.")
    if args.get("more_threads"):
        notes.append("There may be more unread conversations: see the app.")
    out = {"plain": plain, "count": len(picked), "note": "\n".join(dict.fromkeys(notes)), "commit": commit, "text": ""}
    if not plain:
        out["text"] = ("(test data, not your real messages)\n" if args.get("test") else "") + \
            "\n".join(line for g in groups.values() for line in g)
    return out


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
