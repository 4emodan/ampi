"""`watchers`: the command Ampi (and the watcher builder) uses to see and manage watchers. Standard library only;
it talks to the bridge, which decides from the process tree who is calling."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

DEFAULT_SOCKET = "/run/claude-assistant/watchers.sock"
TIMEOUT = 590  # tests run AI steps; stay under Claude Code's 10-minute Bash limit


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="watchers", description="See and manage Ampi's watchers (automations).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("help")
    sub.add_parser("list")
    sub.add_parser("lib")
    for c in ("show", "space", "validate", "propose", "pause", "run"):
        sub.add_parser(c).add_argument("name")
    r = sub.add_parser("runs")
    r.add_argument("name")
    r.add_argument("--limit", type=int, default=15)
    a = sub.add_parser("audit")
    a.add_argument("name")
    a.add_argument("--limit", type=int, default=20)
    lg = sub.add_parser("logs")
    lg.add_argument("run")
    rt = sub.add_parser("retry")
    rt.add_argument("run")
    t = sub.add_parser("test")
    t.add_argument("name")
    t.add_argument("--items", type=int, default=3)
    b = sub.add_parser("build")
    b.add_argument("name")
    b.add_argument("text", nargs="?", default="")
    b.add_argument("--change", metavar="TEXT")
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    req = {k: v for k, v in vars(a).items() if v is not None}
    if a.cmd == "build" and a.change:
        req.update(text=a.change, change=True)
    path = os.environ.get("AMPI_WATCHERS_SOCKET") or DEFAULT_SOCKET
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(path)
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as e:
        print(f"watchers: the bridge isn't reachable ({type(e).__name__})", file=sys.stderr)
        return 1
    try:
        reply = json.loads(buf)
    except ValueError:
        print("watchers: broken reply", file=sys.stderr)
        return 1
    print(reply.get("text", ""))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
