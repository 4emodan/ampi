"""ampi-signin: asks the owner's phone to unlock a saved login (docs/COMPANION.md; standard library, user `ampisignin`).

    python3 -I /usr/local/lib/ampi-signin/run.py      (ampi-signin.service, started by ampi-signin.socket)

The logins are only on the phone, locked by a key in its hardware that needs a fingerprint every time. This service has no
key to them: its own key only proves to the phone who is asking. It reaches the phone through the gateway's vault socket,
with TLS 1.3 inside: both certificates were fixed while pairing, so the gateway can neither read nor ask anything.

`test`: the phone unlocks the login and says so; nothing of it is sent. `fill`: the checkout browser's controller says which
site the sign-in form is on, the owner unlocks the login for exactly that site, and it goes straight to the controller, which
checks everything again and fills it in. No answer of this service ever has a login in it.

One JSON line in, one out, only for ampi-tools. One request at a time. A login is kept in memory only until it was filled in, or
(a sign-in over two pages) 3 minutes for the same browser session and site; then the process ends, and its memory with it. It also
ends when it has been idle. Journal: outcomes, never sites.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import trust

log = logging.getLogger("ampi-signin")

CONF = Path("/etc/ampi-signin")
VAULT = "/run/ampi-phone-vault/vault.sock"
WAIT = 75  # seconds the owner has on the phone
TOTAL = 100.0  # for everything (Ampi's commands end after two minutes)
IDLE = 30.0
HOLD = 180.0  # an unlocked login waits this long for the second page of a sign-in (or a page that changed)
CHECKOUT = "/run/ampi-checkout.sock"
SESSION_RE = re.compile(r"[0-9a-f]{16}")
REF_RE = re.compile(r"f\d{1,3}:\d{1,6}")
MAX_LINE, MAX_FRAME, MAX_CALLS = 4096, 8192, 4
HOST_RE = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])")
# What the phone and the gateway may answer; anything else is "failed".
PHONE_CODES = frozenset({"no_login", "busy", "paused", "limit", "ringing", "notifications_off", "no_fingerprint", "key_gone",
                         "denied", "expired", "interrupted", "unsupported", "bad_request", "failed"})
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


@dataclass(repr=False)
class Login:
    """What the phone sent. Never printed."""
    user: str
    secret: str
    hosts: frozenset[str]


def login_of(r: dict, host: str) -> Login | None:
    user, secret, hosts = r.get("user"), r.get("secret"), r.get("hosts")
    for v in (user, secret):
        if not isinstance(v, str) or not 0 < len(v) <= 200 or any(ord(c) < 32 for c in v):
            return None
    if not isinstance(hosts, list) or len(hosts) > 10 or any(not isinstance(h, str) or not HOST_RE.fullmatch(h) for h in hosts) \
            or host not in hosts:
        return None
    return Login(user, secret, frozenset(hosts))


def ask(host: str, caller: socket.socket, conf: Path = CONF, vault: Callable[[], socket.socket] = open_vault,
        purpose: str = "test") -> tuple[str, Login | None]:
    """Has the owner unlock the login for `host` on the phone; ("ok" or why not, the login when it is to be filled in)."""
    deadline = time.monotonic() + TOTAL
    try:
        devices = trust.load_devices(conf)
        if not devices:
            return "not_paired", None
        ctx = trust.make_context(devices, conf)
    except (OSError, ValueError):  # a broken device file, an unreadable key
        log.exception("the pairing can't be read")
        return "failed", None
    try:
        sock = vault()
    except OSError:
        return "offline", None  # the gateway isn't running, or is just starting
    with sock, sock.dup() as stop:
        watch = Watch(caller, stop, deadline)
        watch.start()
        try:
            return _ask(host, sock, ctx, devices, deadline, purpose)
        except ssl.SSLEOFError:
            return "expired" if watch.late else "lost", None
        except ssl.SSLError as e:
            reason = str(e.reason or "")
            log.warning("sign-in channel: %s", reason)
            # A certificate that isn't the paired one, on either side; anything else broke on the way.
            return "untrusted" if "CERT" in reason or "UNKNOWN_CA" in reason else "lost", None
        except (Lost, OSError, ValueError):
            return "expired" if watch.late else "lost", None
        finally:
            watch.done.set()
            watch.join()


def _ask(host: str, sock: socket.socket, ctx: ssl.SSLContext, devices: list[trust.Device], deadline: float,
         purpose: str) -> tuple[str, Login | None]:
    sock.sendall(b'{"cmd": "open"}\n')
    r = json.loads(read_line(sock, deadline))
    if not isinstance(r, dict) or r.get("ok") is not True:
        return known(r.get("code") if isinstance(r, dict) else None, GATEWAY_CODES), None
    sock.settimeout(TOTAL)  # (the watch ends it at the deadline)
    with ctx.wrap_socket(sock, server_side=True, suppress_ragged_eofs=False) as tls:
        der = tls.getpeercert(True)
        if not any(der == d.der for d in devices):
            return "untrusted", None
        send_frame(tls, {"v": 1, "op": "get", "purpose": purpose, "host": host, "wait": WAIT})
        r = read_frame(tls)
        if r.get("asking") is True:
            r = read_frame(tls)
        if r.get("ok") is not True:
            return known(r.get("error"), PHONE_CODES), None
        login = login_of(r, host) if purpose == "fill" else None
        return ("ok", login) if login is not None or purpose == "test" else ("failed", None)


def controller(req: dict, timeout: float = 60) -> dict:
    """One request to the checkout browser's controller (which lets this user ask for nothing but bind_login and
    fill_login)."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(CHECKOUT)
        s.sendall(json.dumps(req).encode() + b"\n")
        out = b""
        while not out.endswith(b"\n"):
            part = s.recv(65536)
            if not part or len(out) > 262_144:
                raise Lost
            out += part
    r = json.loads(out)
    if not isinstance(r, dict):
        raise Lost
    return r


def words(error: Any) -> str:
    """The controller's own fixed sentences."""
    return "".join(c for c in error if c.isascii() and c.isprintable())[:200] if isinstance(error, str) else "it didn't work"


# --- ampi-tools -----------------------------------------------------------------------------------


@dataclass(repr=False)
class Held:
    """An unlocked login that isn't used up yet: for one browser session, a few minutes."""
    session: str
    login: Login
    until: float


class Service:
    def __init__(self, allowed: frozenset[int], ask: Callable[..., tuple[str, Login | None]] = ask,
                 controller: Callable[[dict], dict] = controller):
        self.allowed, self.ask, self.controller = allowed, ask, controller
        self.asking = threading.Lock()
        self.calls = 0
        self.held: Held | None = None
        self.spent = False  # a login was used up: end, so that nothing of it stays in memory
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
        req = req if isinstance(req, dict) else {}
        cmd = req.get("cmd")
        if cmd not in ("test", "fill") or not (self._good_test(req) if cmd == "test" else self._good_fill(req)):
            return {"ok": False, "code": "bad_request"}
        if not self.asking.acquire(blocking=False):  # never in a queue: it would ask later, for someone who has gone
            return {"ok": False, "code": "busy"}
        try:
            if cmd == "fill":
                resp = self._fill(req, conn)
            else:
                code, _ = self.ask(req["host"], conn)
                resp = {"ok": code == "ok", "code": code}
        except Exception:
            log.exception("asking failed")
            resp = {"ok": False, "code": "failed"}
        finally:
            self.asking.release()
        log.info("%s: %s", cmd, resp["code"])
        return resp

    @staticmethod
    def _good_test(req: dict) -> bool:
        return isinstance(req.get("host"), str) and bool(HOST_RE.fullmatch(req["host"]))

    @staticmethod
    def _good_fill(req: dict) -> bool:
        refs = [req[k] for k in ("user", "password", "press") if req.get(k) is not None]
        return (isinstance(req.get("session"), str) and bool(SESSION_RE.fullmatch(req["session"]))
                and all(isinstance(r, str) and REF_RE.fullmatch(r) for r in refs)
                and (req.get("user") is not None or req.get("password") is not None))

    def _fill(self, req: dict, conn: socket.socket) -> dict:
        refs = {k: req[k] for k in ("user", "password", "press") if req.get(k) is not None}
        try:
            # Which site it is: what the browser is really on, never what the caller says.
            b = self.controller({"cmd": "bind_login", "session": req["session"], **refs})
        except (Lost, OSError, ValueError):
            return {"ok": False, "code": "no_browser"}
        host = b.get("host")
        if b.get("ok") is not True or not isinstance(host, str) or not HOST_RE.fullmatch(host) or not isinstance(b.get("bound"), dict):
            return {"ok": False, "code": "page", "error": words(b.get("error"))}
        login = self._held_for(req["session"], host)
        if login is None:
            code, login = self.ask(host, conn, purpose="fill")
            if login is None:
                return {"ok": False, "code": code if code != "ok" else "failed"}
        values = {"user": login.user} if "user" in refs else {}
        if "password" in refs:
            values["password"] = login.secret
        try:
            r = self.controller({"cmd": "fill_login", "session": req["session"], "host": host, "bound": b["bound"], **values})
        except (Lost, OSError, ValueError):
            self._forget()
            return {"ok": False, "code": "unknown"}
        if "password" in refs and (r.get("ok") is True or r.get("phase") != "verify"):
            self._forget()  # it is in the page now
        else:
            self.held = Held(req["session"], login, time.monotonic() + HOLD)
        if r.get("ok") is not True:
            return {"ok": False, "code": "page", "error": words(r.get("error"))}
        return {"ok": True, "code": "ok", "pressed": r.get("pressed") is True, "password": "password" in refs}

    def _held_for(self, session: str, host: str) -> Login | None:
        h = self.held
        if h is None or h.until <= time.monotonic() or h.session != session or host not in h.login.hosts:
            if h is not None:
                self._forget()
            return None
        return h.login

    def _forget(self) -> None:
        self.held = None
        self.spent = True

    def over(self, idle: float) -> bool:
        """Nothing is going on, and nothing has to be kept (or something is in memory that has to go)."""
        if self.held is not None and self.held.until <= time.monotonic():
            self._forget()
        return self.calls == 0 and self.held is None and (self.spent or idle >= IDLE)

    def run(self, listener: socket.socket) -> None:
        listener.settimeout(min(0.5, IDLE))
        last = time.monotonic()
        while True:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                if self.calls:
                    last = time.monotonic()
                if self.over(time.monotonic() - last):
                    return  # a connection arriving right now waits in the socket, and systemd starts this again
                continue
            last = time.monotonic()
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
