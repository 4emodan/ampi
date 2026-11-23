"""ampi-signin: asks the owner's phone to unlock a saved login (docs/COMPANION.md; standard library, user `ampisignin`).

    python3 -I /usr/local/lib/ampi-signin/run.py      (ampi-signin.service, started by ampi-signin.socket)

The logins are only on the phone, locked by a key in its hardware that needs a fingerprint every time. This service has no
key to them: its own key only proves to the phone who is asking. It reaches the phone through the gateway's vault socket,
with TLS 1.3 inside: both certificates were fixed while pairing, so the gateway can neither read nor ask anything.

For now it can only test (the phone unlocks the login and says so; nothing of it is sent). One JSON line in, one out, only
for ampi-tools. One request at a time; it exits when it has been idle. Journal: outcomes, never sites.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import trust

log = logging.getLogger("ampi-signin")

CONF = Path("/etc/ampi-signin")
VAULT = "/run/ampi-phone-vault/vault.sock"
WAIT = 75  # seconds the owner has on the phone
TOTAL = 100.0  # for everything (Ampi's commands end after two minutes)
IDLE = 30.0
MAX_LINE, MAX_FRAME, MAX_CALLS = 4096, 8192, 4
HOST_RE = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])")
# What the phone and the gateway may answer; anything else is "failed".
PHONE_CODES = frozenset({"no_login", "busy", "paused", "limit", "ringing", "notifications_off", "no_fingerprint", "key_gone",
                         "other_server", "denied", "expired", "interrupted", "unsupported", "bad_request", "failed"})
GATEWAY_CODES = frozenset({"offline", "no_vault", "busy", "limit", "no_answer"})


def known(code: Any, codes: frozenset[str]) -> str:
    return code if isinstance(code, str) and code in codes else "failed"


class Lost(Exception):
    """The channel ended before an answer."""


def read_line(conn: socket.socket, deadline: float) -> bytes:
    """Byte by byte: what comes after the line belongs to someone else."""
    out = b""
    while not out.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or len(out) >= MAX_LINE:
            raise Lost
        conn.settimeout(left)
        b = conn.recv(1)
        if not b:
            raise Lost
        out += b
    return out


def send_frame(tls: ssl.SSLSocket, msg: dict) -> None:
    body = json.dumps(msg).encode()
    tls.sendall(struct.pack(">I", len(body)) + body)


def read_frame(tls: ssl.SSLSocket) -> dict:
    def exactly(n: int) -> bytes:
        out = b""
        while len(out) < n:
            part = tls.recv(n - len(out))
            if not part:
                raise Lost
            out += part
        return out

    n = int.from_bytes(exactly(4), "big")
    if not 0 < n <= MAX_FRAME:
        raise Lost
    try:
        msg = json.loads(exactly(n).decode("utf-8"))
    except (ValueError, RecursionError):
        raise Lost from None
    if not isinstance(msg, dict):
        raise Lost
    return msg


class Watch(threading.Thread):
    """Ends the channel (`stop` is a copy of its socket) when the caller has gone or the time is up."""

    def __init__(self, caller: socket.socket, stop: socket.socket, deadline: float):
        super().__init__(daemon=True)
        self.caller, self.stop, self.deadline = caller, stop, deadline
        self.done, self.late = threading.Event(), False

    def run(self) -> None:
        while not self.done.wait(0.2):
            self.late = time.monotonic() >= self.deadline
            if self.late or self._gone():
                try:
                    self.stop.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    def _gone(self) -> bool:
        try:
            return bool(select.select([self.caller], [], [], 0)[0])  # it has nothing more to say: the end, or noise
        except (OSError, ValueError):
            return True


def open_vault() -> socket.socket:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.settimeout(5)
        s.connect(VAULT)
    except BaseException:
        s.close()
        raise
    return s


def ask(host: str, caller: socket.socket, conf: Path = CONF, vault: Callable[[], socket.socket] = open_vault) -> str:
    """Has the owner unlock the login for `host` on the phone; "ok" or why not."""
    deadline = time.monotonic() + TOTAL
    devices = trust.load_devices(conf)
    if not devices:
        return "not_paired"
    ctx = trust.make_context(devices, conf)
    try:
        sock = vault()
    except OSError:
        return "offline"  # the gateway isn't running, or is just starting
    with sock, sock.dup() as stop:
        watch = Watch(caller, stop, deadline)
        watch.start()
        try:
            return _ask(host, sock, ctx, devices, deadline)
        except ssl.SSLEOFError:
            return "expired" if watch.late else "lost"
        except ssl.SSLError as e:
            log.warning("sign-in channel: %s", e.reason)
            return "untrusted"
        except (Lost, OSError, ValueError):
            return "expired" if watch.late else "lost"
        finally:
            watch.done.set()
            watch.join()


def _ask(host: str, sock: socket.socket, ctx: ssl.SSLContext, devices: list[trust.Device], deadline: float) -> str:
    sock.sendall(b'{"cmd": "open"}\n')
    r = json.loads(read_line(sock, deadline))
    if not isinstance(r, dict) or r.get("ok") is not True:
        return known(r.get("code") if isinstance(r, dict) else None, GATEWAY_CODES)
    sock.settimeout(TOTAL)  # (the watch ends it at the deadline)
    with ctx.wrap_socket(sock, server_side=True, suppress_ragged_eofs=False) as tls:
        der = tls.getpeercert(True)
        if not any(der == d.der for d in devices):
            return "untrusted"
        send_frame(tls, {"v": 1, "op": "get", "purpose": "test", "host": host, "wait": WAIT})
        r = read_frame(tls)
        if r.get("asking") is True:
            r = read_frame(tls)
        return "ok" if r.get("ok") is True else known(r.get("error"), PHONE_CODES)


# --- ampi-tools -----------------------------------------------------------------------------------


class Service:
    def __init__(self, allowed: frozenset[int], ask: Callable[[str, socket.socket], str] = ask):
        self.allowed, self.ask = allowed, ask
        self.asking = threading.Lock()
        self.calls = 0
        self._lock = threading.Lock()

    def serve(self, conn: socket.socket) -> None:
        with conn:
            try:
                if peer_uid(conn) not in self.allowed:
                    return
                resp = self.handle(json.loads(read_line(conn, time.monotonic() + 5)), conn)
                conn.settimeout(5)
                conn.sendall(json.dumps(resp).encode() + b"\n")
            except (Lost, OSError, ValueError, RecursionError):
                pass
            except Exception:
                log.exception("request failed")

    def handle(self, req: Any, conn: socket.socket) -> dict:
        host = req.get("host") if isinstance(req, dict) and req.get("cmd") == "test" else None
        if not isinstance(host, str) or not HOST_RE.fullmatch(host):
            return {"ok": False, "code": "bad_request"}
        if not self.asking.acquire(blocking=False):  # never in a queue: it would ask later, for someone who has gone
            return {"ok": False, "code": "busy"}
        try:
            code = self.ask(host, conn)
        finally:
            self.asking.release()
        log.info("test: %s", code)
        return {"ok": code == "ok", "code": code}

    def run(self, listener: socket.socket) -> None:
        listener.settimeout(IDLE)
        while True:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                if self.calls == 0:
                    return  # a connection arriving right now waits in the socket, and systemd starts this again
                continue
            with self._lock:
                if self.calls >= MAX_CALLS:
                    conn.close()
                    continue
                self.calls += 1
            threading.Thread(target=self._thread, args=(conn,), daemon=True).start()

    def _thread(self, conn: socket.socket) -> None:
        try:
            self.serve(conn)
        finally:
            with self._lock:
                self.calls -= 1


def peer_uid(conn: socket.socket) -> int:
    try:
        return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
    except (AttributeError, OSError):
        return -1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("run me through ampi-signin.socket")
    allowed = frozenset(int(u) for u in os.environ.get("AMPI_SIGNIN_CLIENT_UIDS", "").split(",") if u)
    Service(allowed).run(socket.socket(fileno=3))
