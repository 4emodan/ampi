"""`tg-read`: the assistant's read-only view of the owner's Telegram (standard library only).

    tg-read chats [-n 50] [-q NAME]
    tg-read history CHAT [-n 100] [--since DATE] [--until DATE] [-s TEXT] [-o FILE]
    tg-read search TEXT [-n 50] [-o FILE]

Long results are saved to a file under ./telegram/ and only the path is printed, because the
Bash tool cuts long output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOCKET = "/run/tg-reader/tg-reader.sock"
PRINT_LIMIT = 20_000  # characters; more than this goes to a file
SAVE_DIR = Path("telegram")


def request(payload: dict, path: str | None = None, timeout: float = 300) -> dict:
    path = path or os.environ.get("TG_READER_SOCKET") or DEFAULT_SOCKET
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            return {"ok": False, "error": "the Telegram reader isn't running (service tg-reader on the server)"}
        except PermissionError:
            return {"ok": False, "error": f"no permission to use {path} (is this user in the tgread group?)"}
        s.sendall(json.dumps(payload).encode() + b"\n")
        chunks = []
        while chunk := s.recv(65536):
            chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        return {"ok": False, "error": "the Telegram reader sent a broken reply"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "chat"


def deliver(text: str, out: str | None, label: str, now: datetime | None = None) -> str:
    """Returns what to print: the text itself, or where it was saved."""
    if out is None and len(text) <= PRINT_LIMIT:
        return text
    if out is None:
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        target = SAVE_DIR / f"{stamp}-{_slug(label)}.md"
    else:
        target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    first_line = text.split("\n", 1)[0]
    return f"{first_line}\nSaved {len(text):,} characters to {target} — read it with the Read tool."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg-read", description="Read the owner's Telegram chats (read-only).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("chats", help="list chats, groups and channels, most recent first")
    c.add_argument("-n", "--limit", type=int, default=50)
    c.add_argument("-q", "--query", help="only chats whose name contains this")

    h = sub.add_parser("history", help="messages of one chat, oldest first")
    h.add_argument("chat", help='name (or part of it), @username, id, or "me" for Saved Messages')
    h.add_argument("-n", "--limit", type=int, default=100, help="newest N messages (max 2000)")
    h.add_argument("--since", help="YYYY-MM-DD[THH:MM] UTC, inclusive")
    h.add_argument("--until", help="YYYY-MM-DD[THH:MM] UTC, inclusive for a bare date")
    h.add_argument("-s", "--search", help="only messages containing this")
    h.add_argument("-o", "--out", help="save to this file instead of printing")

    s = sub.add_parser("search", help="search all chats")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("-o", "--out", help="save to this file instead of printing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {k: v for k, v in vars(args).items() if k != "out" and v is not None}
    response = request(payload)
    if not response.get("ok"):
        print(f"tg-read: {response.get('error', 'failed')}", file=sys.stderr)
        return 1
    label = getattr(args, "chat", None) or getattr(args, "query", None) or args.cmd
    print(deliver(response.get("text", ""), getattr(args, "out", None), label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
