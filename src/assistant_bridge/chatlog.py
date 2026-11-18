"""Plain-text logs of every conversation, so any session can read what was said elsewhere.

The Bot API can't fetch chat history, so the bridge records messages as they arrive:
<workspace>/chats/<conversation>/<YYYY-MM-DD>.md, one Markdown entry per message or reply.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CHATS_DIR = "chats"


def conversation_dir(workspace: Path, chat_id: int, thread_id: int, group_name: str | None, title: str | None) -> Path:
    root = workspace / CHATS_DIR
    if group_name:
        base = root / group_name
        return base if thread_id == 0 else base / f"topic-{thread_id}"
    if thread_id == 0:
        return root / "private" / "main"
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40]
    return root / "private" / (f"topic-{thread_id}-{slug}" if slug else f"topic-{thread_id}")


def append(folder: Path, who: str, body: str, when: datetime | None = None) -> None:
    """Add one entry to today's log file. Never raises: logging must not break a reply."""
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = folder / f"{when:%Y-%m-%d}.md"
    entry = f"### {when:%H:%M} UTC · {who}\n{body.strip()}\n\n"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        log.warning("could not write chat log %s: %s", path, e)


def message_time(msg: dict) -> datetime | None:
    ts = msg.get("date")
    return datetime.fromtimestamp(ts, timezone.utc) if isinstance(ts, int) else None
