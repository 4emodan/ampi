#!/usr/bin/python3 -I
"""ampi-pkg: installs Ubuntu packages for the assistant, as root, only when the owner approved.

Listens on /run/ampi-pkg/pkg.sock (root:assistant 0660). It answers only the bridge's main process
(claude-assistant.service MainPID, checked with SO_PEERCRED): the bridge sends a request only after the
owner tapped Approve in Telegram, and `claude` runs (same Unix user, but other processes) can't
pretend to be it. Requests: {"cmd": "plan"|"install", "packages": [...]}, one JSON line each.

Only installs from the configured apt sources, never removes anything (a plan that would remove
packages is refused), no recommends, no options. Standard library only; runs with the system Python.
"""

from __future__ import annotations

import grp
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

SOCKET = os.environ.get("AMPI_PKG_SOCKET", "/run/ampi-pkg/pkg.sock")
GROUP = os.environ.get("AMPI_PKG_GROUP", "assistant")
UNIT = os.environ.get("AMPI_PKG_TRUSTED_UNIT", "claude-assistant.service")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,62}$")
MAX_PACKAGES = 10
UPDATE_AFTER = 6 * 3600  # refresh the package lists if older than this
APT_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"}


def log(msg: str) -> None:
    print(msg, flush=True)


def peer_pid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[0]


def trusted(pid: int) -> bool:
    out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    return pid > 0 and out.isdigit() and int(out) == pid


def valid(packages: object) -> list[str] | None:
    if (isinstance(packages, list) and 1 <= len(packages) <= MAX_PACKAGES
            and all(isinstance(p, str) and PACKAGE_RE.match(p) for p in packages)):
        return list(dict.fromkeys(packages))
    return None


def apt(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["apt-get", *args], capture_output=True, text=True, timeout=timeout, env=APT_ENV)


def tail(text: str, n: int = 1500) -> str:
    return text.strip()[-n:]


def plan(packages: list[str]) -> dict:
    r = apt(["-s", "-q", "install", "--no-install-recommends", "--", *packages], 120)
    if r.returncode != 0:
        return {"ok": False, "error": tail(r.stderr or r.stdout, 600)}
    new = [line.split()[1] for line in r.stdout.splitlines() if line.startswith("Inst ")]
    removed = [line.split()[1] for line in r.stdout.splitlines() if line.startswith("Remv ")]
    if removed:
        return {"ok": False, "error": "this would remove installed packages: " + " ".join(removed)}
    return {"ok": True, "new": new}


def lists_age() -> float:
    try:
        return time.time() - os.stat("/var/lib/apt/lists").st_mtime
    except OSError:
        return float("inf")


def install(packages: list[str]) -> dict:
    if lists_age() > UPDATE_AFTER:
        u = apt(["update", "-q"], 300)
        if u.returncode != 0:
            log(f"apt-get update failed: {tail(u.stderr, 300)}")
    checked = plan(packages)  # again, right before installing: never remove anything
    if not checked.get("ok"):
        return checked
    r = apt(["install", "-y", "-q", "--no-install-recommends", "--", *packages], 1100)
    if r.returncode != 0:
        return {"ok": False, "error": tail(r.stderr or r.stdout)}
    return {"ok": True, "text": tail(r.stdout, 800)}


def handle(conn: socket.socket) -> dict:
    pid = peer_pid(conn)
    if not trusted(pid):
        log(f"refused a request from pid {pid} (not the bridge)")
        return {"ok": False, "error": "only the assistant bridge may ask for installs"}
    conn.settimeout(10)
    data = b""
    while not data.endswith(b"\n") and len(data) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    try:
        req = json.loads(data)
    except ValueError:
        return {"ok": False, "error": "bad request"}
    if not isinstance(req, dict):
        return {"ok": False, "error": "bad request"}
    packages = valid(req.get("packages"))
    if packages is None:
        return {"ok": False, "error": f"give 1-{MAX_PACKAGES} package names"}
    if req.get("cmd") == "plan":
        return plan(packages)
    if req.get("cmd") == "install":
        log(f"installing (approved by the owner): {' '.join(packages)}")
        result = install(packages)
        log(f"install {'done' if result.get('ok') else 'failed'}: {' '.join(packages)}")
        return result
    return {"ok": False, "error": "unknown command"}


def main() -> None:
    os.umask(0o077)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET)
    os.chown(SOCKET, 0, grp.getgrnam(GROUP).gr_gid)
    os.chmod(SOCKET, 0o660)
    srv.listen(8)
    log(f"listening on {SOCKET}")
    while True:  # one request at a time: apt can't run twice anyway
        conn, _ = srv.accept()
        with conn:
            try:
                reply = handle(conn)
            except Exception as e:  # keep serving
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
