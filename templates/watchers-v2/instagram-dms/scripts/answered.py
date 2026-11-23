"""The AI step worked: from here on only failed sending counts against this batch."""

import json
import sys
from pathlib import Path

PENDING = Path("space") / "pending.json"


def main(req):
    try:
        p = json.loads(PENDING.read_text(encoding="utf-8"))
        p.update(ai=0, send=int(p.get("send") or 0) + 1)
        PENDING.write_text(json.dumps(p), encoding="utf-8")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {"ok": True}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout)
