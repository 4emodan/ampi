"""A tiny MCP server (stdio) that `claude` starts for each owner conversation turn.

- `approve` is Claude Code's permission prompt tool (`--permission-prompt-tool mcp__ampi__approve`):
  whenever Claude wants to use a tool its rules don't already allow (running a script, installing a
  Python package...), Claude Code calls it and waits. The request goes to the bridge, which asks the
  owner with Approve / Reject buttons in Telegram.
- `install_system_packages` asks the owner to install Ubuntu packages (apt); only the bridge can make
  the root helper (ampi-pkg) install anything, and only after the owner approved.

The bridge's socket and this run's token come from the environment (AMPI_PERMISSION_SOCKET,
AMPI_RUN). Standard library only: it runs from the bridge's virtualenv but needs nothing from it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

PROTOCOL = "2025-06-18"
WAIT = 1800  # the bridge answers before this (it times requests out itself)

TOOLS = [
    {
        "name": "approve",
        "description": "Permission prompt for Claude Code (used automatically; don't call it yourself).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
                "tool_use_id": {"type": "string"},
            },
            "required": ["tool_name", "input"],
        },
    },
    {
        "name": "install_system_packages",
        "description": (
            "Ask the owner to install Ubuntu packages on the server with apt (e.g. librsvg2-bin, ffmpeg). "
            "The owner sees the packages, what apt would add with them, and your reason, and approves or "
            "rejects in Telegram; this call waits for the answer. Use it only for programs or system "
            "libraries; install Python packages into a virtualenv in your workspace instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "packages": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10,
                             "description": "Ubuntu package names"},
                "reason": {"type": "string", "description": "One sentence for the owner: what you need them for"},
            },
            "required": ["packages", "reason"],
        },
    },
]


def ask_bridge(payload: dict) -> dict:
    path = os.environ.get("AMPI_PERMISSION_SOCKET", "")
    payload = {**payload, "run": os.environ.get("AMPI_RUN", "")}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(WAIT)
            s.connect(path)
            s.sendall((json.dumps(payload, ensure_ascii=True) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data or b"{}")
        return reply if isinstance(reply, dict) else {}
    except (OSError, ValueError, AttributeError) as e:  # AttributeError: no Unix sockets (Windows)
        return {"error": f"the bridge didn't answer ({e})"}


def call_tool(name: str, args: dict) -> tuple[str, bool]:
    """Returns (text, is_error)."""
    if name == "approve":
        tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
        reply = ask_bridge({"cmd": "permission", "tool": str(args.get("tool_name", "")), "input": tool_input})
        if reply.get("allow"):
            decision = {"behavior": "allow", "updatedInput": tool_input}
        else:
            decision = {"behavior": "deny", "message": reply.get("message") or reply.get("error") or "Not approved."}
        return json.dumps(decision), False
    if name == "install_system_packages":
        packages = args.get("packages")
        if not isinstance(packages, list) or not all(isinstance(p, str) for p in packages):
            return "packages must be a list of package names", True
        reply = ask_bridge({"cmd": "packages", "packages": packages, "reason": str(args.get("reason", ""))})
        if reply.get("error"):
            return reply["error"], True
        return reply.get("text", ""), not reply.get("ok")
    return f"unknown tool {name}", True


class Server:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.lock = threading.Lock()

    def send(self, msg: dict) -> None:
        with self.lock:
            self.out.write(json.dumps(msg) + "\n")
            self.out.flush()

    def handle(self, msg: dict) -> None:
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            version = params.get("protocolVersion") or PROTOCOL
            self.send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ampi", "version": "1"},
            }})
        elif method == "tools/list":
            self.send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            # Waiting for the owner can take minutes: answer in a thread so pings etc. still get replies.
            threading.Thread(target=self._call, args=(mid, params), daemon=True).start()
        elif mid is not None:
            if method == "ping" or (method or "").endswith("/list") or method == "server/discover":
                self.send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})

    def _call(self, mid, params: dict) -> None:
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            text, is_error = call_tool(str(params.get("name", "")), args)
        except Exception as e:  # never leave Claude Code waiting
            text, is_error = f"internal error: {e}", True
        self.send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error,
        }})


def main() -> None:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            server.handle(msg)


if __name__ == "__main__":
    main()
