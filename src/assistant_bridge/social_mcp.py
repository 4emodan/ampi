"""The only tools a social-mode run has: the `instagram` and `threads` commands as MCP tools (stdio).

Why not `Bash(instagram:*)`: a Bash rule is a prefix check by Claude Code, and Claude Code also lets read-only commands
(cat, grep, ls…) through without any rule, so a run with Bash can read the workspace (other chats, notes). Here there is
no shell at all: each call is a list of arguments given to the command's own parser, in this process, so there is
nothing to chain, substitute or redirect.

ampi-tools decides what the run may do from the process tree (this server is a child of the claude run), not from
anything said here. Nothing is written to disk.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys

from .permission_mcp import PROTOCOL

COMMANDS = ("instagram", "threads")
MAX_ARGS, MAX_ARG = 30, 5000


def offered() -> list[str]:
    chosen = os.environ.get("AMPI_SOCIAL_TOOLS", "").split(",")
    return [c for c in COMMANDS if c in chosen]


def tool(name: str) -> dict:
    return {
        "name": name,
        "description": f"Runs the `{name}` command with these arguments (one per list item, no shell), e.g. "
                       '["feed", "-n", "10"]; ["--help"] lists what it can do.',
        "inputSchema": {"type": "object", "required": ["args"], "additionalProperties": False, "properties": {
            "args": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_ARGS}}},
    }


def call(name: str, args: object) -> tuple[str, bool]:
    """(text, is_error)."""
    if name not in offered():
        return f"unknown tool {name}", True
    if not isinstance(args, list) or not 1 <= len(args) <= MAX_ARGS or not all(
            isinstance(a, str) and len(a) <= MAX_ARG for a in args):
        return "args must be a list of texts", True
    from ampi_tools import client
    out, err = io.StringIO(), io.StringIO()
    code: object = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = (client.instagram_main if name == "instagram" else client.threads_main)(list(args))
        except SystemExit as e:
            code = e.code
            if isinstance(code, str):
                err.write(code)
        except Exception as e:  # noqa: BLE001 - never the exception's text
            return f"{name} failed ({type(e).__name__})", True
    failed = code not in (0, None)
    return (out.getvalue() + (err.getvalue() if failed else "")).strip() or ("failed" if failed else "Done."), failed


def answer(msg: dict) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if mid is None:
        return None
    if method == "initialize":
        result: dict = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                        "serverInfo": {"name": "social", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [tool(n) for n in offered()]}
    elif method == "tools/call":
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        text, failed = call(str(params.get("name", "")), arguments.get("args"))
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping" or str(method or "").endswith("/list"):
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    os.environ["AMPI_ORIGIN"] = "forwarded:social"  # the client's hints; the daemon checks the process tree itself
    real = sys.stdout
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = answer(msg) if isinstance(msg, dict) else None
        if reply is not None:
            real.write(json.dumps(reply) + "\n")
            real.flush()


if __name__ == "__main__":
    main()
