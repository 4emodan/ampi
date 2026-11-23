"""`ampi-phone` (root, over the owner's SSH): pairing and removing phones, for the gateway and for the sign-in service.
See docs/COMPANION.md.

    ampi-phone offer                    what the phone needs to know (public data), for scripts/companion.ps1
    ampi-phone code   < certificate     the code to compare with the phone's screen
    ampi-phone add NAME --code CODE < certificate
    ampi-phone devices | remove NAME | status
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
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
SIGNIN = Path("/etc/ampi-signin")


def run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def read_cert() -> bytes:
    return trust.parse_cert(sys.stdin.read(3 * trust.MAX_CERT).lstrip("\ufeff"))  # (PowerShell may add a BOM)


def signin_der(signin: Path) -> bytes:
    """The sign-in service's certificate; empty when it isn't installed."""
    return trust.server_der(signin) if (signin / "server.crt").exists() else b""


def write_device(conf: Path, name: str, der: bytes, code: str, signin: bytes) -> None:
    folder = conf / "devices"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".new-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"name": name, "cert": base64.b64encode(der).decode(), "added": time.strftime("%Y-%m-%d %H:%M"),
                       "code": code, "signin": hashlib.sha256(signin).hexdigest() if signin else ""}, f)
        if hasattr(os, "chown"):
            os.chown(tmp, 0, folder.stat().st_gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, folder / f"{name}.json")
    except BaseException:
        os.unlink(tmp)
        raise


def add(conf: Path, name: str, der: bytes, code: str, signin: Path = SIGNIN) -> None:
    if not trust.NAME_RE.fullmatch(name):
        raise ValueError("the name may have a-z, 0-9 and -")
    key = signin_der(signin)
    right = trust.pair_code(trust.server_der(conf), der, key)
    if not trust.same_code(code, right):
        raise ValueError("the code doesn't match this certificate: nothing was added")
    devices = trust.load_devices(conf)
    if any(d.name == name for d in devices):
        raise ValueError(f"{name} is already paired (ampi-phone remove {name})")
    if any(d.der == der for d in devices):
        raise ValueError("that phone is already paired")
    places = [conf, signin] if key else [conf]
    try:
        for c in places:
            write_device(c, name, der, right, key)
            trust.make_context(trust.load_devices(c), c)  # it must be usable, or say so now
        apply(conf)
    except BaseException:
        # Either paired and running, or not paired at all.
        for c in places:
            (c / "devices" / f"{name}.json").unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            apply(conf)
        raise


def remove(conf: Path, name: str, signin: Path = SIGNIN) -> None:
    files = [c / "devices" / f"{name}.json" for c in (conf, signin)]
    if not trust.NAME_RE.fullmatch(name) or not any(f.exists() for f in files):
        raise ValueError(f"no phone {name}")
    # First the connection: whatever goes wrong after this, a lost phone isn't connected any more.
    try:
        run("systemctl", "stop", "ampi-phone.service")
    finally:
        for f in files:
            f.unlink(missing_ok=True)
        try:
            apply(conf)
        finally:  # after the file is gone: it reads the list for every request
            run("systemctl", "stop", "ampi-signin.service", check=False)


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


def describe(f: Path, conf: Path, signin: Path) -> str:
    d = json.loads(f.read_text(encoding="utf-8"))
    code = d.get("code") or trust.pair_code(trust.server_der(conf), trust.parse_cert(d["cert"]), version=1)
    key = signin_der(signin)
    with_signin = key and d.get("signin") == hashlib.sha256(key).hexdigest() and (signin / "devices" / f.name).exists()
    return f"{d['name']}  added {d['added']}  {code}  saved logins: {'yes' if with_signin else 'no (pair again to use them)'}"


def main(argv: list[str] | None = None, conf: Path = trust.CONF, signin: Path = SIGNIN) -> int:
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
            print(json.dumps({"v": 2, "host": c["host"], "port": c["port"],
                              "cert": base64.b64encode(trust.server_der(conf)).decode(),
                              "signin": base64.b64encode(signin_der(signin)).decode()}))
        elif a.cmd == "code":
            print(trust.pair_code(trust.server_der(conf), read_cert(), signin_der(signin)))
        elif a.cmd == "add":
            add(conf, a.name, read_cert(), a.code, signin)
            print(f"{a.name} is paired")
        elif a.cmd == "devices":
            for f in sorted((conf / "devices").glob("*.json")):
                print(describe(f, conf, signin))
        elif a.cmd == "remove":
            remove(conf, a.name, signin)
            print(f"{a.name} was removed and disconnected")
        elif a.cmd == "status":
            try:
                print(json.dumps(status(), indent=2))
            except FileNotFoundError:
                print("nothing is running: no phone is paired")
    except (ValueError, OSError) as e:
        print(f"ampi-phone: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"ampi-phone: {' '.join(e.cmd)} failed: {(e.stderr or e.stdout or '').strip()[:300]}", file=sys.stderr)
        return 1
    return 0
