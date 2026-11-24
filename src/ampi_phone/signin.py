"""ampi-signin: asks the owner's phone to unlock a saved login (docs/COMPANION.md; standard library, user `ampisignin`).

    python3 -I /usr/local/lib/ampi-signin/run.py      (ampi-signin.service, started by ampi-signin.socket)

The logins are only on the phone, locked by a key in its hardware that needs a fingerprint every time. This service has no
key to them: its own key only proves to the phone who is asking. It reaches the phone through the gateway's vault socket,
with TLS 1.3 inside: both certificates were fixed while pairing, so the gateway can neither read nor ask anything.

`test`: the phone unlocks the login and says so; nothing of it is sent. `fill`: the checkout browser's controller says which
site the sign-in form is on, the owner unlocks the login for exactly that site, and it goes straight to the controller, which
checks everything again and fills it in. No answer of this service ever has a login in it. `code`: the same with the one-time
code a site asks for next: the owner types it on the phone, or the phone works it out from a key saved with the login (it
stays there, like everything else).

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
CODE_WAIT = 120  # where they may have to look up a code first
TOTAL, CODE_TOTAL = 100.0, 145.0  # for everything with the phone
IDLE = 30.0
HOLD = 180.0  # an unlocked login waits this long for the second page of a sign-in (or a page that changed)
FILL_TOTAL, CODE_FILL_TOTAL = 175.0, 225.0  # a whole sign-in: the browser, the phone, the browser
CHECKOUT = "/run/ampi-checkout.sock"
SESSION_RE = re.compile(r"[0-9a-f]{16}")
REF_RE = re.compile(r"f\d{1,3}:\d{1,6}")
MAX_LINE, MAX_FRAME, MAX_CALLS = 4096, 8192, 4
HOST_RE = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])")
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{2,14}[A-Za-z0-9]")
# Sites no login is used for (the app saves none for them either, Hosts.java): one opens much more than one site (mail, sign-in
# providers), is how Ampi itself is controlled, or is about money.
NOT = frozenset("""
    google.com gmail.com googlemail.com youtube.com apple.com icloud.com facebook.com instagram.com
    threads.net threads.com messenger.com whatsapp.com meta.com microsoft.com microsoftonline.com live.com
    outlook.com hotmail.com office.com office365.com azure.com yahoo.com linkedin.com x.com
    twitter.com telegram.org t.me telegram.me web.telegram.org fragment.com proton.me protonmail.com
    tuta.com fastmail.com zoho.com gmx.com gmx.net gmx.de web.de mail.com
    mail.ru vk.com vk.ru yandex.ru yandex.com ya.ru github.com gitlab.com
    bitbucket.org npmjs.com pypi.org docker.com hetzner.com hetzner.cloud hetzner.de digitalocean.com
    aws.amazon.com amazonaws.com awsapps.com cloudflare.com ovh.com ovhcloud.com linode.com akamai.com
    namecheap.com godaddy.com gandi.net porkbun.com netlify.com vercel.com heroku.com anthropic.com
    claude.ai claude.com openai.com chatgpt.com mozilla.org firefox.com 1password.com 1password.eu
    1password.ca bitwarden.com bitwarden.eu lastpass.com dashlane.com nordpass.com keepersecurity.com okta.com
    auth0.com onelogin.com duosecurity.com paypal.com stripe.com klarna.com klarna.se revolut.com
    wise.com n26.com monzo.com skrill.com neteller.com payoneer.com venmo.com cash.app
    curve.com viva.com binance.com coinbase.com kraken.com bybit.com okx.com bitstamp.net
    crypto.com metamask.io interactivebrokers.com interactivebrokers.ie degiro.eu degiro.nl nordnet.fi nordnet.se
    etoro.com trading212.com nordea.fi nordea.com op.fi aktia.fi handelsbanken.se handelsbanken.fi
    swedbank.se seb.se zoho.eu tutanota.com hey.com mailbox.org posteo.de aol.com
    bunq.com suomi.fi""".split())
NOT_LABELS = frozenset("google yahoo yandex hotmail gov govt gob gouv".split())  # under any ending
NOT_WORDS = re.compile(r"bank|banque|banco|pankki|sparkasse")
# What the phone and the gateway may answer; anything else is "failed".
PHONE_CODES = frozenset({"no_login", "busy", "paused", "limit", "ringing", "notifications_off", "no_fingerprint", "key_gone",
                         "denied", "expired", "interrupted", "unsupported", "bad_request", "failed", "no_signin"})
GATEWAY_CODES = frozenset({"offline", "no_vault", "busy", "limit", "no_answer"})


def refused(host: str) -> bool:
    labels = host.split(".")
    return (any(host == n or host.endswith("." + n) for n in NOT) or any(NOT_WORDS.search(x) for x in labels[:-1])
            or any(x in NOT_LABELS for x in labels))


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
            if self.late or gone(self.caller):
                try:
                    self.stop.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return


def gone(conn: socket.socket | None) -> bool:
    """The caller has left (or says something more, which it never does)."""
    try:
        return conn is not None and bool(select.select([conn], [], [], 0)[0])
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
    code: str = ""


def login_of(r: dict, host: str, purpose: str = "fill") -> Login | None:
    """What was asked for: the login ("fill"), with a code ("fill+code"), or only the code ("code")."""
    user, secret, hosts, code = r.get("user"), r.get("secret"), r.get("hosts"), r.get("code")
    if purpose == "code":
        user = secret = "-"
    for v in (user, secret):
        if not isinstance(v, str) or not 0 < len(v) <= 200 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in v):
            return None
    if not isinstance(hosts, list) or len(hosts) > 10 or any(not isinstance(h, str) or not HOST_RE.fullmatch(h) for h in hosts) \
            or host not in hosts:
        return None
    if purpose == "fill":
        return Login(user, secret, frozenset(hosts))
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        return None
    return Login("", "", frozenset(hosts), code) if purpose == "code" else Login(user, secret, frozenset(hosts), code)


def ask(host: str, caller: socket.socket, conf: Path = CONF, vault: Callable[[], socket.socket] = open_vault,
        purpose: str = "test") -> tuple[str, Login | None]:
    """Has the owner unlock the login for `host` on the phone, or give the code it asks for; ("ok" or why not, the login when it
    is to be filled in). `purpose`: test, fill, fill+code, code."""
    deadline = time.monotonic() + (CODE_TOTAL if "code" in purpose else TOTAL)
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
    sock.settimeout(CODE_TOTAL)  # (the watch ends it at the deadline)
    with ctx.wrap_socket(sock, server_side=True, suppress_ragged_eofs=False) as tls:
        der = tls.getpeercert(True)
        if not any(der == d.der for d in devices):
            return "untrusted", None
        wait = CODE_WAIT if "code" in purpose else WAIT
        send_frame(tls, {"v": 1, "op": "code", "host": host, "wait": wait} if purpose == "code" else
                   {"v": 1, "op": "get", "purpose": purpose.split("+")[0], "host": host, "wait": wait,
                    **({"code": True} if purpose == "fill+code" else {})})
        r = read_frame(tls)
        if r.get("asking") is True:
            r = read_frame(tls)
        if r.get("ok") is not True:
            return known(r.get("error"), PHONE_CODES), None
        if purpose == "test":
            return "ok", None
        if purpose == "fill+code" and "code" not in r:
            return "unsupported", None  # an older app
        login = login_of(r, host, purpose)
        return ("ok", login) if login is not None else ("failed", None)


class Busy(Exception):
    """The controller didn't answer in time."""


def controller(req: dict, timeout: float = 60) -> dict:
    """One request to the checkout browser's controller (which lets this user ask for nothing but bind_login, fill_login,
    bind_code and fill_code)."""
    with socket.socket(socket.AF_UNIX) as s:
        deadline = time.monotonic() + timeout
        s.settimeout(timeout)
        s.connect(CHECKOUT)
        s.sendall(json.dumps(req).encode() + b"\n")
        out = b""
        while not out.endswith(b"\n"):
            s.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                part = s.recv(65536)
            except TimeoutError:
                raise Busy from None
            if not part or len(out) > 262_144:
                raise Lost
            out += part
    r = json.loads(out)
    if not isinstance(r, dict):
        raise Lost
    return r


def words(error: Any) -> str:
    """The controller's own fixed sentences."""
    return "".join(c for c in error if c.isascii() and c.isprintable())[:300] if isinstance(error, str) else "it didn't work"


# --- ampi-tools -----------------------------------------------------------------------------------


@dataclass(repr=False)
class Held:
    """An unlocked login that isn't used up yet: for one browser session, a few minutes from the fingerprint on."""
    session: str
    login: Login
    until: float


def related(site: str, host: str) -> bool:
    """`site` is the login's host, or a site above it (club.example for accounts.club.example). Not one below: those can
    belong to anybody (someone.blogs.example for blogs.example)."""
    return site == host or (site.count(".") >= 1 and host.endswith("." + site))


class Service:
    def __init__(self, allowed: frozenset[int], ask: Callable[..., tuple[str, Login | None]] = ask,
                 controller: Callable[..., dict] = controller):
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
        good = {"test": self._good_test, "fill": self._good_fill, "code": self._good_code}.get(cmd) if isinstance(cmd, str) else None
        if good is None or not good(req):
            return {"ok": False, "code": "bad_request"}
        if not self.asking.acquire(blocking=False):  # never in a queue: it would ask later, for someone who has gone
            return {"ok": False, "code": "busy"}
        try:
            if cmd == "fill":
                resp = self._fill(req, conn)
            elif cmd == "code":
                resp = self._code(req, conn)
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
        return (Service._good_refs(req, ("user", "password", "code", "press"))
                and (req.get("user") is not None or req.get("password") is not None))

    @staticmethod
    def _good_code(req: dict) -> bool:
        return Service._good_refs(req, ("code", "press")) and req.get("code") is not None

    @staticmethod
    def _good_refs(req: dict, keys: tuple[str, ...]) -> bool:
        refs = [req[k] for k in keys if req.get(k) is not None]
        return (isinstance(req.get("session"), str) and bool(SESSION_RE.fullmatch(req["session"]))
                and all(isinstance(r, str) and REF_RE.fullmatch(r) for r in refs))

    def _bind(self, cmd: str, req: dict, refs: dict) -> tuple[dict, str, dict | None]:
        """(what the controller bound, the site, or why not). Which site it is: what the browser is really on, never what
        the caller says."""
        try:
            b = self.controller({"cmd": cmd, "session": req["session"], **refs}, timeout=40)
        except Busy:
            return {}, "", {"ok": False, "code": "browser_busy"}
        except (Lost, OSError, ValueError):
            return {}, "", {"ok": False, "code": "no_browser"}
        host = b.get("host")
        if b.get("ok") is not True or not isinstance(host, str) or not HOST_RE.fullmatch(host) or not isinstance(b.get("bound"), dict):
            return {}, "", {"ok": False, "code": "page", "error": words(b.get("error"))}
        if refused(host):
            return {}, "", {"ok": False, "code": "refused_site"}
        return b, host, None

    @staticmethod
    def _elsewhere(b: dict, host: str, hosts: frozenset[str]) -> str:
        """Why what the owner unlocked doesn't go into this session, or "" (it does)."""
        # A session that was on some other site is not one to put a login into: that site's page may still have a hand in it.
        visited, targets = b.get("visited"), b.get("targets")
        if not isinstance(visited, list) or any(not isinstance(v, str) or not any(related(v, h) for h in hosts) for v in visited):
            return "other_sites"
        # The form may send it to the page's own site, or to another address the owner saved with this login.
        if not isinstance(targets, list) or any(t not in hosts and t != host or refused(str(t)) for t in targets):
            return "other_target"
        return ""

    def _fill(self, req: dict, conn: socket.socket) -> dict:
        refs = {k: req[k] for k in ("user", "password", "code", "press") if req.get(k) is not None}
        end = time.monotonic() + (CODE_FILL_TOTAL if "code" in refs else FILL_TOTAL)
        b, host, no = self._bind("bind_login", req, refs)
        if no is not None:
            return no
        held = self._held_for(req["session"], host)
        if held is None:
            code, login = self.ask(host, conn, purpose="fill+code" if "code" in refs else "fill")
            if login is None:
                return {"ok": False, "code": code if code != "ok" else "failed"}
            held = Held(req["session"], login, time.monotonic() + HOLD)  # from here on, whatever happens
            one_time = login.code
        else:
            one_time = ""
            if "code" in refs:  # the second page of a sign-in asks for both
                code, extra = self.ask(host, conn, purpose="code")
                if extra is None:
                    return {"ok": False, "code": code if code != "ok" else "failed"}
                one_time = extra.code
        login = held.login
        if why := self._elsewhere(b, host, login.hosts):
            self._forget()
            return {"ok": False, "code": why}
        if end - time.monotonic() < 45 or gone(conn):  # nobody is waiting for it any more
            self._forget()
            return {"ok": False, "code": "lost"}
        values = {"user": login.user} if "user" in refs else {}
        if "password" in refs:
            values["password"] = login.secret
        if "code" in refs:
            values["code"] = one_time
        try:
            r = self.controller({"cmd": "fill_login", "session": req["session"], "host": host, "bound": b["bound"], "name": login.user,
                                 **values}, timeout=max(45, end - time.monotonic()))
        except (Busy, Lost, OSError, ValueError):
            self._forget()
            return {"ok": False, "code": "unknown"}
        if (r.get("phase") == "verify" or (r.get("ok") is True and "password" not in refs)) and "code" not in refs:
            self.held = held  # not used yet, or only its first half
        else:
            self._forget()  # it is in the page now, or there is no page any more
        if r.get("ok") is not True:
            phase = r.get("phase")
            return {"ok": False, "code": "page", "error": words(r.get("error")), "phase": phase if phase in ("filled", "sent") else ""}
        return {"ok": True, "code": "ok", "pressed": r.get("pressed") is True, "moved": r.get("moved") is not False,
                "password": "password" in refs}

    def _code(self, req: dict, conn: socket.socket) -> dict:
        """The one-time code a site asks for after `fill`."""
        refs = {k: req[k] for k in ("code", "press") if req.get(k) is not None}
        end = time.monotonic() + CODE_FILL_TOTAL
        b, host, no = self._bind("bind_code", req, refs)
        if no is not None:
            return no
        # The phone gives one only for the login it unlocked a moment ago, for a site of that login.
        code, got = self.ask(host, conn, purpose="code")
        if got is None:
            return {"ok": False, "code": code if code != "ok" else "failed"}
        self._forget()  # whatever happens: nothing of this process stays
        if why := self._elsewhere(b, host, got.hosts):
            return {"ok": False, "code": why}
        if end - time.monotonic() < 45 or gone(conn):
            return {"ok": False, "code": "lost"}
        try:
            r = self.controller({"cmd": "fill_code", "session": req["session"], "host": host, "bound": b["bound"], "code": got.code},
                                timeout=max(45, end - time.monotonic()))
        except (Busy, Lost, OSError, ValueError):
            return {"ok": False, "code": "unknown"}
        if r.get("ok") is not True:
            phase = r.get("phase")
            return {"ok": False, "code": "page", "error": words(r.get("error")), "phase": "sent" if phase == "sent" else ""}
        return {"ok": True, "code": "ok", "pressed": r.get("pressed") is True, "moved": r.get("moved") is True}

    def _held_for(self, session: str, host: str) -> Held | None:
        h = self.held
        if h is None or h.until <= time.monotonic() or h.session != session or host not in h.login.hosts:
            if h is not None:
                self._forget()
            return None
        return h

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
