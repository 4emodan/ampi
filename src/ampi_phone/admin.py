"""`ampi-phone` (root, over the owner's SSH): pairing and removing phones. See docs/COMPANION.md.

    ampi-phone offer                    what the phone needs to know (public data), for scripts/companion.ps1
    ampi-phone code   < certificate     the code to compare with the phone's screen
    ampi-phone add NAME --code CODE < certificate
    ampi-phone devices | remove NAME | status
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import trust

SOCKET = "/run/ampi-phone.sock"


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def read_cert() -> bytes:
    return trust.parse_cert(sys.stdin.read(3 * trust.MAX_CERT).lstrip("\ufeff"))  # (PowerShell may add a BOM)


def write_device(conf: Path, name: str, der: bytes) -> None:
    folder = conf / "devices"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".new-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"name": name, "cert": base64.b64encode(der).decode(), "added": time.strftime("%Y-%m-%d %H:%M")}, f)
        if hasattr(os, "chown"):
            os.chown(tmp, 0, folder.stat().st_gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, folder / f"{name}.json")
    except BaseException:
        os.unlink(tmp)
        raise


def add(conf: Path, name: str, der: bytes, code: str) -> None:
    if not trust.NAME_RE.fullmatch(name):
        raise ValueError("the name may have a-z, 0-9 and -")
    if not trust.same_code(code, trust.pair_code(trust.server_der(conf), der)):
        raise ValueError("the code doesn't match this certificate: nothing was added")
    devices = trust.load_devices(conf)
    if any(d.name == name for d in devices):
        raise ValueError(f"{name} is already paired (ampi-phone remove {name})")
    if any(d.der == der for d in devices):
        raise ValueError("that phone is already paired")
    write_device(conf, name, der)
    try:
        trust.make_context(trust.load_devices(conf), conf)  # it must be usable, or say so now
        apply(conf)
    except BaseException:
        # Either paired and running, or not paired at all.
        (conf / "devices" / f"{name}.json").unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            apply(conf)
        raise


def remove(conf: Path, name: str) -> None:
    if not trust.NAME_RE.fullmatch(name) or not (conf / "devices" / f"{name}.json").exists():
        raise ValueError(f"no phone {name}")
    # First the connection: whatever goes wrong after this, a lost phone isn't connected any more.
    try:
        run("systemctl", "stop", "ampi-phone.service")
    finally:
        (conf / "devices" / f"{name}.json").unlink()
    apply(conf)


def apply(conf: Path) -> None:
    """Restarting the service drops every connection and reads the list again (the socket stays, so nobody else can take
    the port meanwhile); without a phone the port closes."""
    if trust.load_devices(conf):
        run("systemctl", "enable", "--now", "ampi-phone.socket")
        run("systemctl", "restart", "ampi-phone.service")
        time.sleep(1)
        run("systemctl", "is-active", "ampi-phone.service")
        firewall(conf, True)
    else:
        firewall(conf, False)
        run("systemctl", "disable", "--now", "ampi-phone.socket")
        run("systemctl", "stop", "ampi-phone.service")


def firewall(conf: Path, opened: bool) -> None:
    port = f"{trust.load_config(conf)['port']}/tcp"
    try:
        if opened:
            run("ufw", "allow", port, "comment", "ampi-phone")
        else:
            run("ufw", "--force", "delete", "allow", port, check=False)
    except FileNotFoundError:
        pass  # no ufw: the port is open as soon as systemd listens


def status() -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(SOCKET)
        s.sendall(b'{"cmd": "status"}\n')
        return json.loads(s.makefile().readline())


def main(argv: list[str] | None = None, conf: Path = trust.CONF) -> int:
    p = argparse.ArgumentParser(prog="ampi-phone", description="Pair and remove the owner's phone (root).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("offer")
    sub.add_parser("code")
    s = sub.add_parser("add")
    s.add_argument("name")
    s.add_argument("--code", required=True)
    sub.add_parser("devices")
    sub.add_parser("remove").add_argument("name")
    sub.add_parser("status")
    a = p.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("run me as root", file=sys.stderr)
        return 1
    try:
        if a.cmd == "offer":
            c = trust.load_config(conf)
            print(json.dumps({"v": 1, "host": c["host"], "port": c["port"],
                              "cert": base64.b64encode(trust.server_der(conf)).decode()}))
        elif a.cmd == "code":
            print(trust.pair_code(trust.server_der(conf), read_cert()))
        elif a.cmd == "add":
            add(conf, a.name, read_cert(), a.code)
            print(f"{a.name} is paired")
        elif a.cmd == "devices":
            for f in sorted((conf / "devices").glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                print(f"{d['name']}  added {d['added']}  {trust.pair_code(trust.server_der(conf), trust.parse_cert(d['cert']))}")
        elif a.cmd == "remove":
            remove(conf, a.name)
            print(f"{a.name} was removed and disconnected")
        elif a.cmd == "status":
            print(json.dumps(status(), indent=2))
    except (ValueError, OSError) as e:
        print(f"ampi-phone: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"ampi-phone: {' '.join(e.cmd)} failed: {(e.stderr or e.stdout or '').strip()[:300]}", file=sys.stderr)
        return 1
    return 0
