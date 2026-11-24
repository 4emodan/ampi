"""Signing in to the dashboard on the web (/ampi). The owner sends /dashboard to Ampi in the private chat; the bridge (only
its main process may ask) gets a one-time link. The page trades the link's key for a session. Only hashes are kept, in
memory: a restart of this service signs everyone out."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import socket
import ssl
import time
from pathlib import Path
from typing import Any

from .todoist import ToolError

log = logging.getLogger("ampi-tools")
LINK_TTL = 300
IDLE = 3600
LIFE = 12 * 3600
MAX_SESSIONS = 3
KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}")
CERT_WARN = 36 * 3600


def load_url(raw: Any) -> str:
    """[web] url in config.toml (root-owned), e.g. https://192.0.2.1; "" = the dashboard is off."""
    url = raw.get("url", "") if isinstance(raw, dict) else ""
    if url and (not isinstance(url, str) or not re.fullmatch(r"https://[a-z0-9.-]{1,253}", url)):
        raise ValueError("[web] url must be https://HOST")
    return url


def _h(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


class WebSessions:
    def __init__(self, url: str = "", clock=time.monotonic):
        self.url = url
        self.clock = clock
        self.link: tuple[bytes, float] | None = None
        self.sessions: dict[bytes, list[float]] = {}  # hash -> [made, last used]

    def new_link(self) -> str:
        if not self.url:
            raise ToolError("the dashboard isn't set up on the web (deploy/install-dash.sh); it opens over SSH")
        key = secrets.token_urlsafe(32)
        self.link = (_h(key), self.clock() + LINK_TTL)  # (the one before stops working)
        return f"{self.url}/ampi/#k={key}"

    def login(self, key: Any) -> str:
        link = self.link
        if not (isinstance(key, str) and KEY_RE.fullmatch(key)) or link is None or self.clock() > link[1] \
                or not secrets.compare_digest(_h(key), link[0]):
            raise ToolError("this link was used or is too old: send /dashboard to Ampi again")
        self.link = None
        session = secrets.token_urlsafe(32)
        self._drop()
        while len(self.sessions) >= MAX_SESSIONS:
            del self.sessions[min(self.sessions, key=lambda h: self.sessions[h][1])]
        self.sessions[_h(session)] = [self.clock(), self.clock()]
        return session

    def _drop(self) -> None:
        now = self.clock()
        self.sessions = {h: t for h, t in self.sessions.items() if now - t[1] < IDLE and now - t[0] < LIFE}

    def check(self, session: Any) -> None:
        self._drop()
        t = self.sessions.get(_h(session)) if isinstance(session, str) and KEY_RE.fullmatch(session) else None
        if t is None:
            raise ToolError("signed out: send /dashboard to Ampi")
        t[1] = self.clock()

    def logout(self, session: Any = None) -> None:
        """One session, or (None) all of them and the open link."""
        if session is None:
            self.sessions.clear()
            self.link = None
        elif isinstance(session, str):
            self.sessions.pop(_h(session), None)


def cert_left(host: str) -> float:
    """Seconds the certificate on this server is still good for; raises if it isn't."""
    with socket.create_connection(("127.0.0.1", 443), 10) as raw, \
            ssl.create_default_context().wrap_socket(raw, server_hostname=host) as s:
        return ssl.cert_time_to_seconds(s.getpeercert()["notAfter"]) - time.time()


async def watch_cert(url: str, notice, left=cert_left) -> None:
    """Tells the owner (once a day) when the dashboard's certificate isn't being renewed."""
    host, told = url.removeprefix("https://"), 0.0
    await asyncio.sleep(600)
    while True:
        try:
            secs = await asyncio.to_thread(left, host)
            bad = f"runs out in {int(secs // 3600)} hours and wasn't renewed" if secs < CERT_WARN else ""
        except ssl.SSLError as e:
            bad = f"isn't valid ({str(getattr(e, 'verify_message', '') or 'refused')[:80]})"
        except OSError:
            bad = "couldn't be checked (the web server doesn't answer)"
        if bad and time.time() - told > 86400:
            notice(f"⚠️ The dashboard's HTTPS certificate {bad}. On the server: systemctl status ampi-cert")
            told = time.time()
        await asyncio.sleep(3 * 3600)


def home_url(path: Path) -> str:
    import tomllib
    return load_url(tomllib.loads(path.read_text(encoding="utf-8")).get("web")) if path.is_file() else ""
