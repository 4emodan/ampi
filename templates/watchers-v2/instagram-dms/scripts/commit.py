"""Runs after the message was sent: remembers the last reported message of each conversation (times and ids only)."""

import json
import sys
from pathlib import Path

CURSORS = Path("space") / "cursors.json"
KEEP = 500


def main(req):
    try:
        state = json.loads(CURSORS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state = {k: v for k, v in state.items() if isinstance(v, dict) and isinstance(v.get("ts"), int)
             and isinstance(v.get("ids"), list)}
    state.update((req.get("args") or {}).get("commit") or {})
    newest = sorted(state.items(), key=lambda kv: kv[1]["ts"])[-KEEP:]
    CURSORS.parent.mkdir(parents=True, exist_ok=True)
    CURSORS.write_text(json.dumps(dict(newest)), encoding="utf-8")
    return {"saved": True}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout)
