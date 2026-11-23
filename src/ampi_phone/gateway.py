"""The phone gateway: keeps the owner's phone connected over mutual TLS and passes requests from ampi-tools to it.

    python3 -I /usr/local/lib/ampi-phone/run.py      (ampi-phone.service; both sockets come from systemd)

It holds no other secret than its TLS key, stores nothing, starts nothing and opens no connection. What a phone may be
asked is decided in ampi-tools; this only knows which kinds exist and how many per minute. Journal: kinds and outcomes,
never texts.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import trust

log = logging.getLogger("ampi-phone")

PROTO = 1
MAX_FRAME = 64 * 1024
MAX_BODY = 8 * 1024
KINDS = frozenset({"notify", "alarm.set", "timer.set", "alarm.list", "alarm.cancel", "status"})
PING_EVERY = 170.0
SILENT = 390.0  # nothing from the phone for this long (two pings unanswered): the connection is dead
HELLO_WAIT = 15.0
WAIT = 10.0  # how long a request waits for the phone's answer
PER_MINUTE, PER_HOUR = 20, 120  # requests to the phone
FRAMES_PER_MINUTE = 120  # from the phone
HANDSHAKE_S, HANDSHAKES, HANDSHAKES_PER_ADDR, ADDR_PER_MINUTE = 5.0, 64, 2, 30
# For addresses a paired phone connected from in the last day: slots nobody else can take, and wider limits.
RESERVED, KNOWN_PER_ADDR, KNOWN_PER_MINUTE, KNOWN_S, KNOWN_MAX = 16, 8, 120, 24 * 3600, 200
CONTROLS = 16  # connections to the control socket at once
MAX_CONTROL = 64 * 1024


class Closed(Exception):
    pass


class Window:
    """At most n events in the last `seconds`."""

    def __init__(self, n: int, seconds: float, clock=time.monotonic):
        self.n, self.seconds, self.clock = n, seconds, clock
        self.times: deque[float] = deque()

    def full(self) -> bool:
        now = self.clock()
        while self.times and self.times[0] <= now - self.seconds:
            self.times.popleft()
        return len(self.times) >= self.n

    def add(self) -> None:
        self.times.append(self.clock())


class Gate:
    """Who may start a TLS handshake: limits before any of OpenSSL's work."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.running: dict[str, int] = {}
        self.recent: dict[str, Window] = {}
        self.known: dict[str, float] = {}  # where a paired phone connected from, and when
        self.total = 0

    def authenticated(self, key: str) -> None:
        self.known.pop(key, None)
        self.known[key] = self.clock()
        while len(self.known) > KNOWN_MAX:
            del self.known[next(iter(self.known))]

    def _is_known(self, key: str) -> bool:
        return key != "local" and self.clock() - self.known.get(key, -1e12) < KNOWN_S

    @staticmethod
    def key(address: str) -> str:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return "?"
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped:
                return Gate.key(str(ip.ipv4_mapped))
            if ip.is_loopback:
                return "local"
            return str(ipaddress.ip_network((ip, 64), strict=False))
        return "local" if ip.is_loopback else str(ip)  # every 127.x.y.z is the same someone

    def enter(self, key: str) -> bool:
        known = self._is_known(key)
        if self.total >= HANDSHAKES - (0 if known else RESERVED):
            return False
        if self.running.get(key, 0) >= (KNOWN_PER_ADDR if known else HANDSHAKES_PER_ADDR):
            return False
        w = self.recent.get(key)
        if w is None:
            if len(self.recent) > 5000 and not known:
                self.recent = {k: v for k, v in self.recent.items() if v.full() or v.times}
                if len(self.recent) > 5000:
                    return False
            w = self.recent[key] = Window(ADDR_PER_MINUTE, 60, self.clock)
        w.n = KNOWN_PER_MINUTE if known else ADDR_PER_MINUTE
        if w.full():
            return False
        w.add()
        self.total += 1
        self.running[key] = self.running.get(key, 0) + 1
        return True

    def leave(self, key: str) -> None:
        self.total -= 1
        n = self.running.get(key, 0) - 1
        if n > 0:
            self.running[key] = n
        else:
            self.running.pop(key, None)


async def read_frame(reader: asyncio.StreamReader) -> dict:
    try:
        n = int.from_bytes(await reader.readexactly(4), "big")
        if not 0 < n <= MAX_FRAME:
            raise Closed("bad frame length")
        msg = json.loads((await reader.readexactly(n)).decode("utf-8"))
    except (asyncio.IncompleteReadError, OSError):
        raise Closed("closed") from None
    except (ValueError, RecursionError):
        raise Closed("bad frame") from None
    if not isinstance(msg, dict):
        raise Closed("bad frame")
    return msg


def frame(msg: dict) -> bytes:
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > MAX_FRAME:
        raise ValueError("frame too big")
    return struct.pack(">I", len(body)) + body


def clean_hello(h: dict) -> dict:
    """What the phone says about itself, cut to a known shape."""
    def text(k: str, n: int) -> str:
        v = h.get(k)
        return "".join(c for c in v if c.isprintable())[:n] if isinstance(v, str) else ""

    warn = h.get("warn")
    return {"app": text("app", 20), "sdk": text("sdk", 20), "model": text("model", 60), "zone": text("zone", 60),
            "warn": [w[:40] for w in warn if isinstance(w, str)][:10] if isinstance(warn, list) else []}


@dataclass
class Link:
    device: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    since: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    hello: dict | None = None  # None until the phone has said hello: not ready for requests

    def send(self, msg: dict) -> None:
        if self.writer.is_closing():
            raise Closed("closed")
        # Small frames to one peer: if it stops reading, the answer wait, TCP_USER_TIMEOUT or the silence limit ends it.
        if self.writer.transport.get_write_buffer_size() > 4 * MAX_FRAME:
            self.abort()
            raise Closed("the phone doesn't read")
        self.writer.write(frame(msg))

    def abort(self) -> None:
        self.writer.transport.abort()


class Gateway:
    def __init__(self, conf: Path = trust.CONF):
        self.devices = trust.load_devices(conf)
        self.ctx = trust.make_context(self.devices, conf) if self.devices else None
        self.links: dict[str, Link] = {}
        self.last: dict[str, dict] = {}  # device -> last contact, kept after it disconnects
        self.pending: dict[tuple[str, str], tuple[asyncio.Future, dict]] = {}
        self.minute, self.hour = Window(PER_MINUTE, 60), Window(PER_HOUR, 3600)
        self.gate = Gate()
        self._tasks: set[asyncio.Task] = set()
        self._controls = 0

    # --- phones ------------------------------------------------------------------------------------

    async def accept(self, sock: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, addr = await loop.sock_accept(sock)
            except OSError:  # e.g. out of file descriptors
                await asyncio.sleep(0.5)
                continue
            key = self.gate.key(str(addr[0]) if addr else "?")
            if self.ctx is None or not self.gate.enter(key):
                conn.close()
                continue
            task = loop.create_task(self._connection(conn, key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _connection(self, conn: socket.socket, key: str) -> None:
        # The TLS transport is made directly on the accepted socket: StreamWriter.start_tls() after an await loses the
        # phone's first bytes on Python 3.12.
        loop = asyncio.get_running_loop()
        writer = None
        try:
            try:
                conn.setblocking(False)
                with contextlib.suppress(OSError):
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    if hasattr(socket, "TCP_USER_TIMEOUT"):  # longer than a short outage, which the phone sleeps through
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 150_000)
                reader = asyncio.StreamReader(limit=MAX_FRAME + 4)
                protocol = asyncio.StreamReaderProtocol(reader)
                transport, _ = await loop.connect_accepted_socket(
                    lambda: protocol, conn, ssl=self.ctx, ssl_handshake_timeout=HANDSHAKE_S, ssl_shutdown_timeout=3)
                writer = asyncio.StreamWriter(transport, protocol, reader, loop)
            except (OSError, ssl.SSLError, asyncio.TimeoutError, ConnectionError):
                conn.close()
                return
            finally:
                self.gate.leave(key)
            der = transport.get_extra_info("ssl_object").getpeercert(True)
            device = next((d for d in self.devices if der and d.der == der), None)
            if device is None:
                log.warning("a certificate that isn't a paired phone's got through the handshake")
                return
            self.gate.authenticated(key)
            try:
                await self.session(device.name, reader, writer)
            finally:
                self.gate.authenticated(key)  # online for days: still known when it comes back
        except Exception:
            log.exception("connection failed")
        finally:
            if writer is not None:
                writer.transport.abort()

    async def session(self, device: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        link = Link(device, reader, writer)
        frames = Window(FRAMES_PER_MINUTE, 60)
        pinger = None
        try:
            link.send({"t": "welcome", "proto": PROTO, "now": time.time()})
            hello = await asyncio.wait_for(read_frame(reader), HELLO_WAIT)
            if hello.get("t") != "hello":
                raise Closed("no hello")
            link.hello = clean_hello(hello)
            old = self.links.get(device)
            self.links[device] = link
            if old is not None:
                old.abort()
            self._seen(link)
            log.info("%s connected", device)
            pinger = asyncio.create_task(self._ping(link))
            for (dev, _), (fut, msg) in list(self.pending.items()):
                if dev == device and not fut.done():
                    link.send(msg)  # asked on the connection before this one; the phone does an id only once
            while True:
                msg = await asyncio.wait_for(read_frame(reader), SILENT)
                if frames.full():
                    raise Closed("too many frames")
                frames.add()
                self._seen(link)
                if msg.get("t") == "res":
                    self._answer(device, msg)
        except (Closed, asyncio.TimeoutError) as e:
            log.info("%s disconnected (%s)", device, e or "silent")
        finally:
            if pinger is not None:
                pinger.cancel()
            if self.links.get(device) is link:
                del self.links[device]
            link.abort()

    def _seen(self, link: Link) -> None:
        link.last_seen = time.time()
        self.last[link.device] = {"last_seen": link.last_seen, "hello": link.hello}

    async def _ping(self, link: Link) -> None:
        with contextlib.suppress(Closed):
            while True:
                await asyncio.sleep(PING_EVERY)
                link.send({"t": "ping", "now": time.time()})

    def _answer(self, device: str, msg: dict) -> None:
        waiting = self.pending.get((device, str(msg.get("id"))))
        if waiting is None or waiting[0].done():
            return
        ok, data, error = msg.get("ok") is True, msg.get("data"), msg.get("error")
        waiting[0].set_result({"ok": ok, "data": data if isinstance(data, dict) else {},
                               "error": str(error)[:60] if error is not None else None})

    # --- ampi-tools --------------------------------------------------------------------------------

    async def ask(self, kind: str, body: dict) -> dict:
        link = max(self.links.values(), key=lambda x: x.since, default=None)
        if link is None:
            seen = max((v["last_seen"] for v in self.last.values()), default=None)
            return {"ok": False, "code": "offline", "last_seen": seen, "paired": bool(self.devices)}
        if self.minute.full() or self.hour.full():
            return {"ok": False, "code": "limit", "error": "too many requests to the phone; try later"}
        self.minute.add()
        self.hour.add()
        rid = secrets.token_hex(8)
        now = time.time()
        msg = {"t": "req", "id": rid, "kind": kind, "body": body, "exp": now + WAIT, "now": now}
        key = (link.device, rid)
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = (fut, msg)
        try:
            with contextlib.suppress(Closed):  # it may come back within the wait: session() sends it again
                link.send(msg)
            answer = await asyncio.wait_for(fut, WAIT)
        except asyncio.TimeoutError:
            log.info("%s: no answer", kind)
            if self.links.get(link.device) is link:
                link.abort()  # half-open: don't go on believing in it
            return {"ok": False, "code": "unknown"}
        finally:
            del self.pending[key]
        log.info("%s: %s", kind, "ok" if answer["ok"] else "refused")
        return {"ok": True, "answer": answer, "hello": link.hello, "now": time.time()}

    def status(self) -> dict:
        out = []
        for d in self.devices:
            link, last = self.links.get(d.name), self.last.get(d.name) or {}
            out.append({"name": d.name, "connected": link is not None, "since": link.since if link else None,
                        "last_seen": last.get("last_seen"), "hello": last.get("hello")})
        return {"ok": True, "devices": out, "now": time.time()}

    async def control(self, req: Any) -> dict:
        """One request from ampi-tools (or root)."""
        if not isinstance(req, dict):
            return {"ok": False, "error": "send a JSON object"}
        if req.get("cmd") == "status":
            return self.status()
        if req.get("cmd") == "ask":
            kind, body = req.get("kind"), req.get("body")
            if kind not in KINDS or not isinstance(body, dict):
                return {"ok": False, "error": "unknown kind"}
            if len(json.dumps(body, ensure_ascii=False).encode("utf-8", "replace")) > MAX_BODY:
                return {"ok": False, "error": "too big"}
            return await self.ask(kind, body)
        return {"ok": False, "error": "unknown command"}

    async def control_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                 allowed: frozenset[int]) -> None:
        self._controls += 1
        try:
            if peer_uid(writer) not in allowed or self._controls > CONTROLS:
                return
            line = await asyncio.wait_for(reader.readline(), 10)
            try:
                req = json.loads(line)
            except (ValueError, RecursionError):
                req = None
            try:
                resp = await self.control(req)
            except Exception:
                log.exception("control request failed")
                resp = {"ok": False, "error": "the gateway failed"}
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except (OSError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            self._controls -= 1
            writer.close()


def peer_uid(writer: asyncio.StreamWriter) -> int:
    try:
        creds = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except (AttributeError, OSError):
        return -1


def systemd_sockets() -> tuple[list[socket.socket], socket.socket]:
    """(public listeners, control socket) handed over by systemd."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        raise SystemExit("run me through ampi-phone.socket")
    public, control = [], None
    for fd in range(3, 3 + int(os.environ.get("LISTEN_FDS") or 0)):
        s = socket.socket(fileno=fd)
        s.setblocking(False)
        if s.family == socket.AF_UNIX:
            control = s
        else:
            public.append(s)
    if control is None or not public:
        raise SystemExit("ampi-phone.socket must pass one public and one control socket")
    return public, control


async def serve(conf: Path = trust.CONF) -> None:
    allowed = frozenset({0, *(int(u) for u in os.environ.get("AMPI_PHONE_CLIENT_UIDS", "").split(",") if u)})
    public, control = systemd_sockets()
    gw = Gateway(conf)
    log.info("%d paired phone(s)", len(gw.devices))
    server = await asyncio.start_unix_server(lambda r, w: gw.control_connection(r, w, allowed), sock=control,
                                             limit=MAX_CONTROL)
    async with server:
        await asyncio.gather(*(gw.accept(s) for s in public))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve())
