"""Runs after the message was sent: remembers which messages were reported (times and ids only)."""

import json
import sys
from pathlib import Path

CURSORS, PENDING = Path("space") / "cursors.json", Path("space") / "pending.json"
IDS, RECENT, KEEP = 100, 50, 500  # ids kept per conversation, conversations with ids, conversations in all


def entry(v):
    try:
        if isinstance(v, int):
            return v, []
        if "floor" in v:
            return int(v["floor"]), [[int(t), str(i)] for t, i in v["ids"]]
        return int(v["ts"]) - 1, [[int(v["ts"]), str(i)] for i in list(v["ids"])]
    except (TypeError, ValueError, KeyError):
        return 0, []


def main(req):
    try:
        state = json.loads(CURSORS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state = {k: entry(v) for k, v in state.items()} if isinstance(state, dict) else {}
    for thread, ids in ((req.get("args") or {}).get("commit") or {}).items():
        floor, old = state.get(thread, (0, []))
        ids = sorted(old + ids)
        if len(ids) > IDS:  # older ones count as reported by their time
            cut = ids[-IDS][0]
            floor = max([floor] + [t for t, _ in ids if t < cut])
            ids = [x for x in ids if x[0] >= cut]
        state[thread] = (floor, ids)
    order = sorted(state, key=lambda k: max([state[k][0]] + [t for t, _ in state[k][1]]), reverse=True)[:KEEP]
    out = {}
    for n, k in enumerate(order):
        floor, ids = state[k]
        out[k] = {"floor": floor, "ids": ids} if n < RECENT else \
            {"floor": max([floor] + [t for t, _ in ids]), "ids": []}
    CURSORS.parent.mkdir(parents=True, exist_ok=True)
    CURSORS.write_text(json.dumps(out), encoding="utf-8")
    PENDING.unlink(missing_ok=True)
    return {"saved": True}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout)
