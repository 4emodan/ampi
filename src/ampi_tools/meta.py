"""Threads and Instagram (Meta's official APIs): logins, tokens, the request client, and checks for text that
gets published.

- Threads API: any Threads account that is a *Threads Tester* of the owner's own Meta app. Own posts, replies
  under them, insights; publishing, replying, deleting and hiding replies (each approved by the owner).
- Instagram API with Instagram Login: professional (Business/Creator) accounts only. Own posts, comments on
  them, insights; replying to, hiding and deleting comments (approved by the owner).

Neither API lets an app read other people's content without Meta's App Review, and nothing here fetches
threads.com or instagram.com pages.

Tokens are long-lived (60 days) and refreshed by the daemon in the background. They never appear in what the
assistant sees or in the log: every error from the HTTP layer is reduced to its type, and `redact()` is a
backstop over every text the daemon returns or logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .gmail import IGNORABLE
from .todoist import ToolError

log = logging.getLogger(__name__)

DAY = 24 * 3600
REFRESH_AFTER = 7 * DAY
WARN_BEFORE = 7 * DAY
MAX_REPLY = 2_000_000
ID_RE = re.compile(r"[0-9]{1,30}")
DEFAULT_REDIRECT = "https://localhost:8765/"

# --- secrets that must never leave the daemon --------------------------------------------------

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: Any) -> str:
    s = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in s:
            s = s.replace(secret, "[redacted]")
    # Anything shaped like a Meta token or code in a URL, even if it isn't registered (yet).
    return re.sub(r"(access_token|client_secret|code|fb_exchange_token)=[^&\s\"']+", r"\1=[redacted]", s)


class RedactingFormatter(logging.Formatter):
    """Applied to the whole formatted record, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


# --- services --------------------------------------------------------------------------------


@dataclass(frozen=True)
class MetaService:
    name: str
    login_command: str
    authorize_url: str
    code_url: str
    exchange_url: str
    exchange_grant: str
    refresh_url: str
    refresh_grant: str
    api: str
    bearer: bool  # token in an Authorization header (Instagram) or as access_token (Threads, as Meta documents)
    grant_days: int | None  # Threads: permissions of private profiles end 90 days after login
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]


THREADS = MetaService(
    name="Threads", login_command="threads-login",
    authorize_url="https://threads.com/oauth/authorize",
    code_url="https://graph.threads.com/oauth/access_token",
    exchange_url="https://graph.threads.com/access_token", exchange_grant="th_exchange_token",
    refresh_url="https://graph.threads.com/refresh_access_token", refresh_grant="th_refresh_token",
    api="https://graph.threads.com/v1.0", bearer=False, grant_days=90,
    read_scopes=("threads_basic", "threads_read_replies", "threads_manage_insights"),
    write_scopes=("threads_content_publish", "threads_manage_replies", "threads_delete"),
)
INSTAGRAM = MetaService(
    name="Instagram", login_command="instagram-login",
    authorize_url="https://www.instagram.com/oauth/authorize",
    code_url="https://api.instagram.com/oauth/access_token",
    exchange_url="https://graph.instagram.com/access_token", exchange_grant="ig_exchange_token",
    refresh_url="https://graph.instagram.com/refresh_access_token", refresh_grant="ig_refresh_token",
    api="https://graph.instagram.com", bearer=True, grant_days=None,
    # Reading comments needs the same permission as replying to them, so there's no read-only login.
    read_scopes=("instagram_business_basic", "instagram_business_manage_comments",
                 "instagram_business_manage_insights"),
    write_scopes=(),
)


class MetaAuth:
    """The owner's long-lived token for one service, kept in a 0600 file in the daemon's home."""

    def __init__(self, service: MetaService, app_id: str, secret: str, token_file: Path,
                 client: httpx.AsyncClient, redirect: str = DEFAULT_REDIRECT):
        self.s = service
        self.app_id, self.secret, self.redirect = app_id, secret, redirect
        self.token_file = token_file
        self._client = client
        self._lock = asyncio.Lock()
        register_secret(secret)
        data = self.load()
        register_secret(data.get("access_token"))

    # --- storage -------------------------------------------------------------------------

    def load(self) -> dict:
        try:
            data = json.loads(self.token_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        tmp = self.token_file.with_name(self.token_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_file)

    def configured(self) -> bool:
        return bool(self.load().get("access_token"))

    @property
    def user(self) -> dict:
        d = self.load()
        return {"id": str(d.get("user_id", "")), "username": str(d.get("username", ""))}

    def scopes(self) -> set[str]:
        return set(self.load().get("scopes", []))

    # --- login (interactive, run by the owner on the server) ------------------------------

    def login_url(self, write: bool) -> tuple[str, str]:
        state = secrets.token_urlsafe(16)
        scopes = self.s.read_scopes + (self.s.write_scopes if write else ())
        query = {"client_id": self.app_id, "redirect_uri": self.redirect, "scope": ",".join(scopes),
                 "response_type": "code", "state": state}
        return f"{self.s.authorize_url}?{urllib.parse.urlencode(query)}", state

    async def finish_login(self, redirected_url: str, state: str, write: bool) -> dict:
        u = urllib.parse.urlsplit(redirected_url.strip())
        q = urllib.parse.parse_qs(u.query)
        if q.get("error"):
            raise ToolError(f"{self.s.name} said: {q.get('error_description', q['error'])[0]}")
        if q.get("state", [""])[0] != state:
            raise ToolError("that address is from a different login attempt; start again")
        code = q.get("code", [""])[0].split("#")[0]  # Meta appends "#_"
        if not code:
            raise ToolError("no code in that address; paste the whole address from the browser")
        register_secret(code)
        short = await self._post_form(self.s.code_url, {
            "client_id": self.app_id, "client_secret": self.secret, "grant_type": "authorization_code",
            "redirect_uri": self.redirect, "code": code})
        if isinstance(short.get("data"), list) and short["data"]:  # Instagram sometimes wraps it
            short = short["data"][0]
        token = short.get("access_token")
        if not token:
            raise ToolError(f"{self.s.name} login failed ({redact(short.get('error_message') or short.get('error'))})")
        register_secret(token)
        long = await self._get(self.s.exchange_url, {"grant_type": self.s.exchange_grant,
                                                    "client_secret": self.secret, "access_token": token})
        if not long.get("access_token"):
            raise ToolError(f"{self.s.name}: couldn't get a long-lived token ({redact(long.get('error'))})")
        register_secret(long["access_token"])
        granted = short.get("permissions")
        scopes = (granted.split(",") if isinstance(granted, str) else
                  list(self.s.read_scopes + (self.s.write_scopes if write else ())))
        now = time.time()
        data = {"access_token": long["access_token"], "scopes": scopes, "obtained_at": now,
                "expires_at": now + float(long.get("expires_in", 60 * DAY)), "login_at": now}
        self.save(data)
        me = await MetaClient(self.s, self, self._client).call("GET", "/me", {
            "fields": "user_id,username" if self.s is INSTAGRAM else "id,username"})
        data["user_id"] = str(me.get("user_id") or me.get("id") or "")
        data["username"] = str(me.get("username", ""))
        self.save(data)
        return data

    # --- tokens ----------------------------------------------------------------------------

    async def token(self) -> str:
        data = self.load()
        tok = data.get("access_token")
        if not tok:
            raise ToolError(f"{self.s.name} isn't set up yet (`ampi-tools {self.s.login_command}` on the server)")
        if time.time() >= float(data.get("expires_at", 0)):
            raise ToolError(f"the {self.s.name} login expired: run `ampi-tools {self.s.login_command}` on the server")
        register_secret(tok)
        return tok

    async def refresh_if_due(self) -> bool:
        """Refreshes a token older than 7 days (Meta needs it ≥ 24 h old). The old one stays if it fails."""
        async with self._lock:
            data = self.load()
            if not data.get("access_token"):
                return False
            age = time.time() - float(data.get("obtained_at", 0))
            if age < REFRESH_AFTER or time.time() >= float(data.get("expires_at", 0)):
                return False
            try:
                new = await self._get(self.s.refresh_url, {"grant_type": self.s.refresh_grant,
                                                          "access_token": data["access_token"]})
            except ToolError as e:
                log.warning("%s token refresh failed: %s", self.s.name, redact(e))
                return False
            if not new.get("access_token"):
                log.warning("%s token refresh failed: %s", self.s.name, redact(new.get("error")))
                return False
            register_secret(new["access_token"])
            now = time.time()
            data.update(access_token=new["access_token"], obtained_at=now,
                        expires_at=now + float(new.get("expires_in", 60 * DAY)))
            self.save(data)
            log.info("%s token refreshed", self.s.name)
            return True

    def expiry_note(self) -> str:
        data = self.load()
        if not data.get("access_token"):
            return ""
        now = time.time()
        left = float(data.get("expires_at", 0)) - now
        if left < WARN_BEFORE:
            return (f"⚠ The {self.s.name} login expires in {max(0, int(left // DAY))} day(s): run "
                    f"`ampi-tools {self.s.login_command}` on the server.")
        if self.s.grant_days:
            grant_left = float(data.get("login_at", now)) + self.s.grant_days * DAY - now
            if grant_left < WARN_BEFORE:
                return (f"⚠ If the {self.s.name} profile is private, its permissions end in "
                        f"{max(0, int(grant_left // DAY))} day(s): then run `ampi-tools {self.s.login_command}`.")
        return ""

    # --- token endpoints (the only requests with secrets in the URL) ----------------------

    async def _get(self, url: str, params: dict) -> dict:
        try:
            resp = await self._client.get(url, params=params, timeout=30, follow_redirects=False)
            data = resp.json()
        except Exception as e:  # never the exception text: it can hold the URL with the secret
            raise ToolError(f"{self.s.name} unreachable ({type(e).__name__})") from None
        return data if isinstance(data, dict) else {}

    async def _post_form(self, url: str, form: dict) -> dict:
        try:
            resp = await self._client.post(url, data=form, timeout=30, follow_redirects=False)
            data = resp.json()
        except Exception as e:
            raise ToolError(f"{self.s.name} unreachable ({type(e).__name__})") from None
        return data if isinstance(data, dict) else {}


# --- requests ----------------------------------------------------------------------------------

PATH_RE = re.compile(r"/(me|[0-9]{1,30})(?:/([a-z_]{1,40}))?")
EDGES = {"threads", "insights", "conversation", "replies", "threads_insights", "manage_reply", "media",
         "comments"}
HOURLY = {"normal": 300, "watch": 200}


class MetaClient:
    def __init__(self, service: MetaService, auth: MetaAuth, client: httpx.AsyncClient, api: str | None = None):
        self.s = service
        self.auth = auth
        self._client = client
        self._api = api or service.api
        self._hour: dict[str, list[float]] = {p: [] for p in HOURLY}

    def _count(self, priority: str) -> None:
        now = time.monotonic()
        self._hour[priority] = [t for t in self._hour[priority] if now - t < 3600]
        if len(self._hour[priority]) >= HOURLY[priority]:
            raise ToolError(f"too many {self.s.name} requests in the last hour; try again later")
        self._hour[priority].append(now)

    async def call(self, method: str, path: str, params: dict | None = None, *, priority: str = "normal") -> Any:
        m = PATH_RE.fullmatch(path)
        if not m or (m.group(2) and m.group(2) not in EDGES):
            raise ToolError(f"refusing odd {self.s.name} path {path!r}")
        token = await self.auth.token()
        self._count(priority)
        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        headers = {}
        if self.s.bearer:
            headers["Authorization"] = f"Bearer {token}"
        else:
            params["access_token"] = token
        try:
            resp = await self._client.request(method, f"{self._api}{path}", params=params, headers=headers,
                                              timeout=30, follow_redirects=False)
            body = resp.content
        except Exception as e:  # the exception text could contain the URL, and with it the token
            raise ToolError(f"{self.s.name} unreachable ({type(e).__name__})") from None
        if len(body) > MAX_REPLY:
            raise ToolError(f"{self.s.name}: reply too big")
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            raise ToolError(f"{self.s.name} sent an unexpected reply (HTTP {resp.status_code})") from None
        if resp.status_code >= 400 or (isinstance(data, dict) and "error" in data):
            raise ToolError(meta_error(self.s, data, resp.status_code))
        return data

    async def pages(self, path: str, params: dict, limit: int, *, priority: str = "normal",
                    page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        after = None
        while len(out) < limit:
            data = await self.call("GET", path, {**params, "limit": min(page_size, limit - len(out)), "after": after},
                                   priority=priority)
            items = data.get("data", []) if isinstance(data, dict) else []
            out += [i for i in items if isinstance(i, dict)]
            after = ((data.get("paging") or {}).get("cursors") or {}).get("after") if data.get("paging", {}).get("next") else None
            if not after or not items:
                break
        return out[:limit]


def meta_error(s: MetaService, data: Any, status: int) -> str:
    err = data.get("error", {}) if isinstance(data, dict) else {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    code, msg = err.get("code"), redact(str(err.get("message", "")))[:300]
    if code == 190 or status == 401:
        return f"the {s.name} login is no longer valid: run `ampi-tools {s.login_command}` on the server"
    if code in (10, 200, 3) or "permission" in msg.lower():
        extra = " --write" if s.write_scopes else ""
        return f"{s.name} doesn't allow this with the current login ({msg}); `ampi-tools {s.login_command}{extra}`"
    if code in (4, 17, 32, 613) or status == 429:
        return f"{s.name} rate limit reached; try again later"
    return f"{s.name} error {status}{f' ({code})' if code else ''}: {msg}"


# --- text that gets published ------------------------------------------------------------------

_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x2300, 0x23FF), (0x2190, 0x21FF),
                 (0x3030, 0x303D), (0x3297, 0x3299), (0x1FC00, 0x1FFFD))
_SNEAKY = set("͏ᅟᅠㅤﾠ᠎")


def is_emoji(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _EMOJI_RANGES)


def check_text(text: str, limit: int, emoji_bytes: bool) -> str:
    """Text to publish must be exactly what the owner sees in the approval: no invisible characters (they could
    hide data inside a public post), except the joiner and emoji selector inside emoji. Returns the text."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError("the text is empty")
    for i, ch in enumerate(text):
        if ch == "\n":
            continue
        prev, nxt = text[i - 1] if i else "", text[i + 1] if i + 1 < len(text) else ""
        if ch == "️":  # emoji presentation, once, right after an emoji or symbol
            if prev and (is_emoji(prev) or unicodedata.category(prev) == "So") and nxt != "️":
                continue
        elif ch == "‍":  # joins emoji (👩‍💻), nothing else
            if prev and nxt and (is_emoji(prev) or prev == "️") and is_emoji(nxt):
                continue
        elif (unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp", "Co", "Cn", "Cs")
              and not IGNORABLE.match(ch) and ch not in _SNEAKY
              and not 0xFE00 <= ord(ch) <= 0xFE0F and not 0xE0100 <= ord(ch) <= 0xE01EF):
            continue
        raise ToolError(f"the text contains an invisible or control character (U+{ord(ch):04X}); remove it")
    size = sum(len(ch.encode()) if is_emoji(ch) or ch in "‍️" else 1 for ch in text)
    if size > limit:
        raise ToolError(f"the text is too long ({size} of {limit}" + (", emoji count as several" if emoji_bytes else "")
                        + ")")
    return text


def text_extras(text: str) -> list[str]:
    """Who and what a post points at, listed separately in the approval."""
    out = []
    mentions = sorted(set(re.findall(r"(?<![\w.])@([A-Za-z0-9._]{1,30})", text)))
    if mentions:
        out.append("will notify: " + ", ".join(f"@{m}" for m in mentions))
    links = re.findall(r"(?:https?://|www\.)[^\s]+|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/[^\s]*)?", text, re.I)
    if links:
        shown = []
        for link in dict.fromkeys(links):
            host = urllib.parse.urlsplit(link if "://" in link else "https://" + link).hostname or link
            try:
                ascii_host = host.encode("idna").decode()
            except UnicodeError:
                ascii_host = ascii(host)
            shown.append(link if ascii_host == host else f"{link} (= {ascii_host})")
        out.append("links: " + ", ".join(shown))
    tags = sorted(set(re.findall(r"(?<![\w&])#(\w{1,50})", text)))
    if tags:
        out.append("tags: " + ", ".join(f"#{t}" for t in tags))
    return out


# --- links -------------------------------------------------------------------------------------


def social_ref(v: Any) -> str:
    """A numeric id, or a Threads/Instagram post link, normalized to `id:N`, `threads:user:CODE` or `instagram:CODE`."""
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or len(v) > 500:
        raise ToolError("expected a post id or link")
    s = v.strip()
    if ID_RE.fullmatch(s):
        return f"id:{s}"
    u = urllib.parse.urlsplit(s if "://" in s else "https://" + s)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if u.scheme in ("http", "https") and not u.username and not u.port:
        if host in ("threads.com", "www.threads.com", "threads.net", "www.threads.net"):
            if len(parts) >= 3 and re.fullmatch(r"@[A-Za-z0-9._]{1,30}", parts[0]) and parts[1] == "post" \
                    and re.fullmatch(r"[A-Za-z0-9_-]{5,40}", parts[2]):
                return f"threads:{parts[0][1:].lower()}:{parts[2]}"
        if host in ("instagram.com", "www.instagram.com"):
            if len(parts) >= 2 and parts[0] in ("p", "reel", "reels", "tv") and re.fullmatch(r"[A-Za-z0-9_-]{5,40}", parts[1]):
                return f"instagram:{parts[1]}"
    raise ToolError(f"not a Threads/Instagram post id or link: {v!r}")
