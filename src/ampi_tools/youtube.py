"""YouTube: the Data API v3 (OAuth or an API key) and YouTube's public RSS feeds and oEmbed.

From the server's datacenter IP, YouTube's video pages ask for a sign-in ("confirm you're not a bot"), so
video pages, transcripts and yt-dlp don't work there, and we don't try to get around that. What works
without any setup: the public RSS feeds (a channel's 15 newest uploads, playlists), oEmbed (a video's
title and channel) and looking up a channel's id on its public page. Everything else uses the official
API once the owner sets it up (YOUTUBE_API_KEY for public data, `ampi-tools youtube-login` for their
account; `--write` for playlist/subscription changes).

Every request to YouTube goes through one limiter (few at a time, a few per second, and a 30-minute pause
when YouTube starts refusing), and API calls through a daily quota budget, so a prompt-injected assistant
can't burn the quota or get the server's IP blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .gmail import IGNORABLE, GoogleAuth
from .todoist import ToolError

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
SITE = "https://www.youtube.com"
SCOPE_READ = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_WRITE = "https://www.googleapis.com/auth/youtube"
NOT_SET_UP = ("the YouTube API isn't set up yet (YOUTUBE_API_KEY, or `ampi-tools youtube-login`); "
              "without it: `youtube uploads`, `youtube feed --channel` and basic `youtube video` work")
NOT_CONNECTED = "the owner's YouTube account isn't set up yet (`ampi-tools youtube-login` on the server)"
NO_WRITE = "write access to YouTube isn't set up yet (`ampi-tools youtube-login --write` on the server)"

VIDEO_RE = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
LEGACY_RE = re.compile(r"(c|user)/((?=[A-Za-z0-9._-]*[A-Za-z0-9])[A-Za-z0-9._-]{1,100})")  # old /c/name and /user/name channel URLs
PLAYLIST_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")
SPECIAL_PLAYLIST = re.compile(r"(LL|WL|HL|FL|UU|RD|OL|UL|LM|TL)")  # likes, watch later, uploads, mixes, albums...
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
         "youtube-nocookie.com", "www.youtube-nocookie.com"}

# Every API path we call; anything else is refused (the last line of defence after ops.py).
API_PATHS = {"/search", "/videos", "/channels", "/playlistItems", "/playlists", "/subscriptions", "/commentThreads"}
MAX_FEED, MAX_PAGE, MAX_OEMBED = 2_000_000, 3_000_000, 64_000
FEED_TTL = 15 * 60
SUBS_TTL = 6 * 3600
MAX_CHANNELS = 200  # per fan-out: what fits the deadline at the request rate below
FAN_OUT_DEADLINE = 120  # seconds; below the 300 s socket timeouts of the bridge and the CLI
BACKOFF = 30 * 60
HOURLY = {"normal": 600, "watch": 900}  # uncached youtube.com requests per hour (watchers get a reserve)
HANDLE_TTL = 7 * 24 * 3600  # handles can change hands; critical changes always look them up fresh

# Daily quota (Google gives 10,000 units, reset at midnight Pacific time). Every kind has its own
# budget, so no kind of use (e.g. a loop of cheap reads) can starve the others.
COST = {"search": 100, "write": 50, "read": 1, "watch": 1}
BUDGET = {"search": 3000, "write": 2000, "read": 3000, "watch": 1000}

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


# --- references: ids and URLs ----------------------------------------------------------------


def _url(v: str) -> urllib.parse.SplitResult | None:
    """A youtube.com / youtu.be URL, or None if v isn't a URL at all (URLs of other sites raise)."""
    s = v.strip()
    if not re.match(r"(https?://)?([A-Za-z0-9-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com)(/|$|\?)", s, re.I):
        if "://" in s or s.lower().startswith("www.") or re.match(r"[\w.-]+\.[a-z]{2,}/", s, re.I):
            raise ToolError(f"not a YouTube link: {v!r}")
        return None
    if "://" not in s:
        s = "https://" + s
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https") or (u.hostname or "") not in HOSTS or u.username or u.port:
        raise ToolError(f"not a YouTube link: {v!r}")
    return u


def video_id(v: Any) -> str:
    """A video id from an id or any usual YouTube link (watch, youtu.be, shorts, live, embed)."""
    if not isinstance(v, str) or len(v) > 500:
        raise ToolError("expected a video id or YouTube link")
    if VIDEO_RE.fullmatch(v):
        return v
    u = _url(v)
    candidate = ""
    if u is not None:
        parts = [p for p in u.path.split("/") if p]
        if u.hostname == "youtu.be":
            candidate = parts[0] if parts else ""
        elif u.path.rstrip("/") == "/watch":
            candidate = (urllib.parse.parse_qs(u.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            candidate = parts[1]
    if not VIDEO_RE.fullmatch(candidate):
        raise ToolError(f"not a YouTube video id or link: {v!r}")
    return candidate


def _handle(s: str) -> str | None:
    """`@name` with letters/digits of any script (and their combining marks), `_`, `.`, `-`: no spaces,
    bidi or zero-width characters."""
    h = unicodedata.normalize("NFKC", s)
    body = h[1:]
    if not h.startswith("@") or not 3 <= len(body) <= 30 or IGNORABLE.search(h):
        return None
    if all(ch.isalnum() or ch in "_.-" or unicodedata.category(ch) in ("Mn", "Mc") for ch in body):
        return h
    return None


def channel_ref(v: Any) -> str:
    """Normalizes a channel reference to `UC…`, `@handle`, `c/name` or `user/name`."""
    if not isinstance(v, str) or len(v) > 500:
        raise ToolError("expected a channel")
    s = v.strip()
    if CHANNEL_RE.fullmatch(s):
        return s
    if s.startswith("@") and _handle(s):
        return _handle(s)
    u = _url(s)
    if u is not None and u.hostname != "youtu.be":
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_RE.fullmatch(parts[1]):
            return parts[1]
        if parts and parts[0].startswith("@") and _handle(parts[0]):
            return _handle(parts[0])
        if len(parts) >= 2 and LEGACY_RE.fullmatch(f"{parts[0]}/{parts[1]}"):
            return f"{parts[0]}/{parts[1]}"
    raise ToolError(f"not a YouTube channel: {v!r} (use @handle, a channel link, or the UC… id)")


def playlist_id(v: Any) -> str:
    """A playlist id, given as an id or a link with `list=`. (Names of the owner's playlists: playlist_name.)"""
    if not isinstance(v, str) or len(v) > 500:
        raise ToolError("expected a playlist id or link")
    s = v.strip()
    if PLAYLIST_RE.fullmatch(s) and len(s) >= 10:
        return s
    u = _url(s)
    pid = (urllib.parse.parse_qs(u.query).get("list") or [""])[0] if u else ""
    if not PLAYLIST_RE.fullmatch(pid):
        raise ToolError(f"not a playlist id or link: {v!r} (for one of the owner's playlists by name, "
                        "use playlist_name / --name)")
    return pid


def source_ref(v: Any) -> str:
    """Watcher/feed source: `subscriptions`, or comma-separated channels (normalized)."""
    if not isinstance(v, str) or len(v) > 5000:
        raise ToolError("expected `subscriptions` or a comma-separated list of channels")
    if v.strip().lower() == "subscriptions":
        return "subscriptions"
    refs = [channel_ref(x) for x in v.split(",") if x.strip()]
    if not 1 <= len(refs) <= MAX_CHANNELS:
        raise ToolError(f"give 1-{MAX_CHANNELS} channels, or `subscriptions`")
    return ",".join(dict.fromkeys(refs))


# --- formatting --------------------------------------------------------------------------------


def duration(iso: str | None) -> str:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m or not iso or iso in ("P0D", "PT0S"):
        return ""
    d, h, mi, s = (int(x or 0) for x in m.groups())
    h += 24 * d
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi}:{s:02d}"


def seconds(iso: str | None) -> int:
    parts = [int(x) for x in duration(iso).split(":") if x]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def count(n: Any) -> str:
    try:
        x = int(n)
    except (TypeError, ValueError):
        return "?"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if x >= limit:
            return f"{x / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(x)


def chapters(description: str) -> list[str]:
    """Timestamped lines (`0:00 Intro`) of a description, if they start at 0:00 like YouTube's chapters."""
    out = []
    for line in description.splitlines():
        m = re.match(r"\s*[(\[]?((?:\d{1,2}:)?\d{1,2}:\d{2})[)\]]?\s*[-–—:|]?\s*(.{1,120})$", line)
        if m:
            out.append(f"{m.group(1)} {m.group(2).strip()}")
    if len(out) < 2 or not re.fullmatch(r"(0{1,2}:)?0{1,2}:00", out[0].split()[0]):
        return []
    return out[:100]


def watch_url(vid: str) -> str:
    return f"https://youtu.be/{vid}"


def day(ts: str | None) -> str:
    return (ts or "")[:10] or "?"


def one_line(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def format_video(v: dict, full: bool = True) -> str:
    sn, cd, st = v.get("snippet", {}), v.get("contentDetails", {}), v.get("statistics", {})
    vid = v.get("id", "?")
    meta = [f"{one_line(sn.get('channelTitle', '?'), 100)} ({sn.get('channelId', '?')})", day(sn.get("publishedAt"))]
    if duration(cd.get("duration")):
        meta.append(duration(cd.get("duration")))
    if sn.get("liveBroadcastContent") in ("live", "upcoming"):
        meta.append(sn["liveBroadcastContent"].upper())
    meta.append(f"{count(st.get('viewCount'))} views")
    if "likeCount" in st:
        meta.append(f"{count(st['likeCount'])} likes")
    if "commentCount" in st:
        meta.append(f"{count(st['commentCount'])} comments")
    lines = [f"[{vid}] {one_line(sn.get('title', '?'), 200)}", "  " + " · ".join(meta), f"  {watch_url(vid)}"]
    desc = sn.get("description") or ""
    if not full:
        if desc.strip():
            lines.append("  " + one_line(desc, 300))
        return "\n".join(lines)
    extra = [f"captions: {'yes' if cd.get('caption') == 'true' else 'no'} (transcripts can't be fetched)"]
    if sn.get("tags"):
        extra.append("tags: " + one_line(", ".join(sn["tags"][:15]), 400))
    lines.append("  " + " · ".join(extra))
    ch = chapters(desc)
    if ch:
        lines.append("  Chapters:")
        lines += [f"    {c}" for c in ch]
    if desc.strip():
        cut = desc if len(desc) <= 5000 else desc[:5000] + "… (description cut)"
        lines.append("  Description:")
        lines.append("    " + cut.strip().replace("\n", "\n    "))
    return "\n".join(lines)


def format_entry(e: dict, full: bool = False) -> str:
    """One RSS entry (no API): title, channel, date and the description (one line unless full)."""
    kind = " · Short" if e.get("short") else ""
    views = f" · {count(e['views'])} views" if e.get("views") is not None else ""
    line = (f"[{e['id']}] {one_line(e['title'], 200)}\n  {one_line(e['channel'], 100)} · {day(e['published'])}"
            f"{kind}{views} · {watch_url(e['id'])}")
    desc = e.get("description") or ""
    if not desc.strip():
        return line
    if not full:
        return line + "\n  " + one_line(desc, 300)
    ch = chapters(desc)
    out = line + ("\n  Chapters:\n" + "\n".join(f"    {c}" for c in ch) if ch else "")
    return out + "\n  Description:\n    " + desc[:5000].strip().replace("\n", "\n    ")


# --- RSS ---------------------------------------------------------------------------------------


def parse_feed(data: bytes) -> tuple[str, list[dict]]:
    """Returns (feed title, entries). Refuses anything that isn't plain UTF-8 XML without a DTD, so entity
    tricks can't blow up the parser."""
    if len(data) > MAX_FEED:
        raise ToolError("YouTube feed too big")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError("unexpected YouTube feed (not UTF-8)") from None
    if "<!doctype" in text.lower() or "<!entity" in text.lower():
        raise ToolError("unexpected YouTube feed (has a DTD)")
    try:
        root = ET.fromstring(text.lstrip("﻿"))
    except ET.ParseError:
        raise ToolError("YouTube sent a broken feed") from None
    title = root.findtext("a:title", "", NS)
    entries = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", "", NS)
        if not VIDEO_RE.fullmatch(vid):
            continue
        link_el = e.find("a:link", NS)
        link = link_el.get("href", "") if link_el is not None else ""
        stats = e.find("media:group/media:community/media:statistics", NS)
        views = int(stats.get("views")) if stats is not None and (stats.get("views") or "").isdigit() else None
        cid = e.findtext("yt:channelId", "", NS)
        entries.append({
            "id": vid,
            "title": e.findtext("a:title", "", NS),
            "channel": e.findtext("a:author/a:name", "", NS),
            "channel_id": cid if CHANNEL_RE.fullmatch(cid) else "",
            "published": e.findtext("a:published", "", NS),
            "short": "/shorts/" in link,
            "description": e.findtext("media:group/media:description", "", NS),
            "views": views,
        })
    return title, entries


def published_at(e: dict) -> datetime:
    try:
        return datetime.fromisoformat(e["published"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return datetime.fromtimestamp(0, timezone.utc)


# --- limits ------------------------------------------------------------------------------------


class Limiter:
    """All requests to youtube.com: at most 2 at a time and 3 per second; a 30-minute pause after YouTube
    answers 429/403 (being too eager from one IP could get the feeds blocked as well)."""

    def __init__(self, concurrent: int = 2, per_second: float = 3.0):
        self._sem = asyncio.Semaphore(concurrent)
        self._gap = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.paused_until = 0.0
        self._forbidden: dict[str, float] = {}
        # Start times of the requests in the last hour, per priority (watchers and the assistant can't
        # use up each other's allowance).
        self._hour: dict[str, list[float]] = {p: [] for p in HOURLY}
        self.sent = 0

    def check(self, priority: str = "normal") -> None:
        if time.monotonic() < self.paused_until:
            minutes = int((self.paused_until - time.monotonic()) / 60) + 1
            raise ToolError(f"YouTube refused requests from the server; paused for {minutes} more minute(s)")
        now = time.monotonic()
        self._hour[priority] = [t for t in self._hour[priority] if now - t < 3600]
        if len(self._hour[priority]) >= HOURLY[priority]:
            raise ToolError("too many YouTube requests in the last hour; try again later")

    @contextlib.asynccontextmanager
    async def slot(self, priority: str = "normal") -> AsyncIterator[None]:
        self.check(priority)
        async with self._sem:
            async with self._lock:
                wait = self._next - time.monotonic()
                self._next = max(time.monotonic(), self._next) + self._gap
            if wait > 0:
                await asyncio.sleep(wait)
            # Checked again right before sending: a request queued behind one that YouTube refused
            # must not go out (a fan-out queues up to 200 of them).
            self.check(priority)
            self._hour[priority].append(time.monotonic())
            self.sent += 1
            yield

    def refusal(self, status: int, path: str) -> bool:
        """Returns True if this answer pauses everything: a 429 at once; 403s only from 3 different paths
        within 10 minutes (one forbidden feed is about that feed, not about the server)."""
        now = time.monotonic()
        if status == 403:
            self._forbidden = {p: t for p, t in self._forbidden.items() if now - t < 600}
            self._forbidden[path] = now
            if len(self._forbidden) < 3:
                return False
        self.paused_until = now + BACKOFF
        self._forbidden = {}
        log.warning("youtube.com refused requests; pausing for %s minutes", BACKOFF // 60)
        return True


class Quota:
    """Units of the Data API used today (Pacific time, when Google resets it), kept on disk across restarts.
    Units are taken before each call (the daemon is single-threaded, so there's no race)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.day = ""
        self.used: dict[str, int] = {}
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text())
                self.day, self.used = str(raw["day"]), {str(k): int(v) for k, v in raw["used"].items()}
            except (OSError, ValueError, AttributeError, TypeError, KeyError):
                # Unknown usage: assume today's budget is spent rather than starting from zero.
                log.warning("broken %s; treating today's YouTube quota as used up", path)
                self.day, self.used = self.today(), dict(BUDGET)

    @staticmethod
    def today() -> str:
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

    def take(self, kind: str, units: int) -> None:
        """Counts the units before the call (Google charges failed calls too); no await in here."""
        if self.day != self.today():
            self.day, self.used = self.today(), {}
        if self.used.get(kind, 0) + units > BUDGET[kind]:
            what = {"search": "searches", "write": "changes", "read": "lookups", "watch": "watcher lookups"}[kind]
            raise ToolError(f"today's YouTube API budget for {what} is used up ({BUDGET[kind] // COST[kind]} a day; "
                            "it resets at midnight Pacific time)")
        self.used[kind] = self.used.get(kind, 0) + units
        self._save()

    def left(self, kind: str) -> int:
        if self.day != self.today():
            return BUDGET[kind]
        return max(0, BUDGET[kind] - self.used.get(kind, 0))

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"day": self.day, "used": self.used}))
            os.replace(tmp, self.path)
        except OSError:
            log.warning("couldn't save %s", self.path)

    def summary(self) -> str:
        if self.day != self.today():
            return "0 units used today"
        return f"{sum(self.used.values())} units used today ({', '.join(f'{k} {v}' for k, v in self.used.items())})"


# --- client ------------------------------------------------------------------------------------


class YouTube:
    def __init__(self, client: httpx.AsyncClient, auth: GoogleAuth | None = None, api_key: str | None = None,
                 home: Path | None = None, api: str = API, site: str = SITE):
        self._client = client
        self.auth = auth
        self.api_key = api_key or None
        self._api_base, self._site = api, site
        self.limiter = Limiter()
        self.quota = Quota(home / "youtube-quota.json" if home else None)
        self._channels_file = home / "youtube-channels.json" if home else None
        self._channels: dict[str, tuple[str, float]] = {}  # handle / legacy name -> (UC id, when looked up)
        if self._channels_file and self._channels_file.is_file():
            try:
                raw = json.loads(self._channels_file.read_text())
                self._channels = {k: (v[0], float(v[1])) for k, v in raw.items()
                                  if isinstance(v, list) and len(v) == 2 and CHANNEL_RE.fullmatch(str(v[0]))}
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        self._feeds: dict[str, tuple[float, str, list[dict]]] = {}
        self._fetched: dict[str, float] = {}  # feed key -> when last fetched (for the fan-out order)
        self._subs: tuple[float, list[dict]] | None = None
        self.entries: dict[str, dict] = {}  # video id -> last RSS entry seen (for youtube.show without the API)

    # --- state ---------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self.auth and self.auth.configured())

    @property
    def can_write(self) -> bool:
        return self.connected and SCOPE_WRITE in self.auth.scopes()

    @property
    def has_api(self) -> bool:
        return self.connected or bool(self.api_key)

    def status(self) -> str:
        parts = []
        if self.connected:
            parts.append("the owner's account (" + ("read and write" if self.can_write else "read-only") + ")")
        if self.api_key:
            parts.append("API key")
        return ("API via " + " + ".join(parts)) if parts else "RSS and oEmbed only (no API)"

    # --- youtube.com (feeds, oEmbed, channel pages) ---------------------------------------

    async def _site_get(self, path: str, params: dict | None, limit: int, what: str, *,
                        priority: str = "normal", blocked: tuple[int, ...] = (429, 403)) -> httpx.Response:
        async with self.limiter.slot(priority):
            try:
                async with self._client.stream("GET", f"{self._site}{path}", params=params, timeout=10,
                                               follow_redirects=False, headers={"Accept-Language": "en"}) as resp:
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ToolError(f"{what}: reply too big")
                        chunks.append(chunk)
                    status, location = resp.status_code, resp.headers.get("location", "")
            except httpx.TransportError as e:
                raise ToolError(f"YouTube unreachable ({type(e).__name__})") from e
        if 300 <= status < 400 and is_block_page(location):
            status = 429  # Google's "unusual traffic" / consent wall: the same as being refused
        what_key = path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))  # e.g. one per channel feed
        if status in blocked and self.limiter.refusal(status, what_key):
            raise ToolError(f"YouTube refused the request ({status}); YouTube requests are paused for 30 minutes")
        return httpx.Response(status, content=b"".join(chunks))

    async def feed(self, *, channel_id: str | None = None, playlist: str | None = None,
                   priority: str = "normal") -> tuple[str, list[dict]]:
        if channel_id:
            if not CHANNEL_RE.fullmatch(channel_id):
                raise ToolError(f"bad channel id {channel_id!r}")
            key, params = f"c:{channel_id}", {"channel_id": channel_id}
        else:
            if not playlist or not PLAYLIST_RE.fullmatch(playlist):
                raise ToolError(f"bad playlist id {playlist!r}")
            key, params = f"p:{playlist}", {"playlist_id": playlist}
        hit = self._feeds.get(key)
        if hit and time.monotonic() - hit[0] < FEED_TTL:
            return hit[1], hit[2]
        resp = await self._site_get("/feeds/videos.xml", params, MAX_FEED, "YouTube feed", priority=priority)
        if resp.status_code == 404:
            raise ToolError(f"YouTube has no feed for {channel_id or playlist} (wrong id, or private)")
        if resp.status_code != 200:
            raise ToolError(f"YouTube feed error {resp.status_code}")
        title, entries = parse_feed(resp.content)
        self._fetched[key] = time.time()  # only on success: channels that failed or timed out go first next time
        if len(self._feeds) > 3000:
            self._feeds.clear()
            self._fetched.clear()
        self._feeds[key] = (time.monotonic(), title, entries)
        if len(self.entries) > 10_000:
            self.entries.clear()
        self.entries.update({e["id"]: e for e in entries})
        return title, entries

    def _last_fetch(self, ref: str) -> float:
        cid = ref if CHANNEL_RE.fullmatch(ref) else (self._channels.get(ref) or ("", 0.0))[0]
        return self._fetched.get(f"c:{cid}", 0.0)

    async def recent(self, refs: list[str], days: float, shorts: bool,
                     priority: str = "normal") -> tuple[list[dict], list[str], int, int]:
        """Uploads of these channels (UC… ids or other channel references) from the last `days` days, newest
        first, each with "src" (its channel id). Returns (entries, channel ids read, channels that failed or
        ran out of time, channels left for a later run).

        At most MAX_CHANNELS per run, those read longest ago first, so with more channels (or a deadline)
        every channel still gets its turn. Handles are looked up inside the deadline too, and a channel that
        can't be found only counts as failed."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        unique_refs = list(dict.fromkeys(refs))
        unique_refs.sort(key=self._last_fetch)  # stable: never-read channels keep their order
        wanted, skipped = unique_refs[:MAX_CHANNELS], max(0, len(unique_refs) - MAX_CHANNELS)
        self.limiter.check(priority)

        async def one(ref: str) -> tuple[str, list[dict]]:
            cid = await self.channel_id(ref, priority=priority)
            return cid, (await self.feed(channel_id=cid, priority=priority))[1]

        tasks = [asyncio.ensure_future(one(r)) for r in wanted]
        done, pending = await asyncio.wait(tasks, timeout=FAN_OUT_DEADLINE) if tasks else (set(), set())
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending)  # let cancelled requests release the limiter before returning
        entries, ok = [], []
        for t in done:
            if t.cancelled() or t.exception() is not None:
                continue
            cid, es = t.result()
            ok.append(cid)
            entries += [{**e, "src": cid} for e in es
                        if (shorts or not e["short"]) and published_at(e) >= cutoff]
        entries.sort(key=published_at, reverse=True)
        unique = list({e["id"]: e for e in entries}.values())
        if not ok and wanted:
            self.limiter.check(priority)  # explains a pause, if that's why
        return unique, ok, len(wanted) - len(ok), skipped

    async def oembed(self, vid: str) -> dict:
        """Title and channel of any public video, without the API (YouTube's official embed info)."""
        if not VIDEO_RE.fullmatch(vid):
            raise ToolError(f"bad video id {vid!r}")
        resp = await self._site_get("/oembed", {"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                                    MAX_OEMBED, "YouTube oEmbed", blocked=(429,))
        if resp.status_code in (401, 403, 404):
            raise ToolError(f"video {vid} doesn't exist, is private, or can't be embedded")
        if resp.status_code != 200:
            raise ToolError(f"YouTube oEmbed error {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise ToolError("YouTube oEmbed sent a broken reply") from None
        if not isinstance(data, dict):
            raise ToolError("YouTube oEmbed sent a broken reply")
        return {k: str(data.get(k, "")) for k in ("title", "author_name", "author_url")}

    # --- channels ------------------------------------------------------------------------

    async def channel_id(self, ref: str, fresh: bool = False, priority: str = "normal") -> str:
        """`UC…` from any normalized channel reference (see channel_ref). `fresh`: don't use the cache (for
        changes the owner approves: a handle can belong to someone else by now)."""
        if CHANNEL_RE.fullmatch(ref):
            return ref
        hit = self._channels.get(ref)
        if hit and not fresh and time.time() - hit[1] < HANDLE_TTL:
            return hit[0]
        cid = None
        if self.has_api and (ref.startswith("@") or ref.startswith("user/")):
            param = {"forHandle": ref} if ref.startswith("@") else {"forUsername": ref.split("/", 1)[1]}
            kind = "watch" if priority == "watch" else "read"
            items = (await self.api("GET", "/channels", {"part": "id", **param}, kind=kind) or {}).get("items", [])
            cid = items[0]["id"] if items else None
        if cid is None:
            cid = await self._channel_id_from_page(ref, priority)
        if not cid or not CHANNEL_RE.fullmatch(cid):
            raise ToolError(f"no YouTube channel {ref}")
        if len(self._channels) > 5000:
            self._channels.clear()
        self._channels[ref] = (cid, time.time())
        if self._channels_file:
            try:
                tmp = self._channels_file.with_name(self._channels_file.name + ".tmp")
                tmp.write_text(json.dumps(self._channels))
                os.replace(tmp, self._channels_file)
            except OSError:
                pass
        return cid

    async def _channel_id_from_page(self, ref: str, priority: str = "normal") -> str | None:
        """The channel's own public page names its id in the canonical link (no API needed)."""
        if not (_handle(ref) == ref or LEGACY_RE.fullmatch(ref)):
            raise ToolError(f"bad channel reference {ref!r}")
        resp = await self._site_get("/" + urllib.parse.quote(ref, safe="@/._-"), None, MAX_PAGE, "YouTube channel page",
                                    priority=priority)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ToolError(f"couldn't look up {ref} (YouTube answered {resp.status_code}); use the channel's "
                            "UC… id, or set up the YouTube API")
        page = resp.text
        for pattern in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"',
                        r'<meta property="og:url" content="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'):
            m = re.search(pattern, page)
            if m:
                return m.group(1)
        raise ToolError(f"couldn't find the channel id of {ref}; use the channel's UC… id")

    # --- Data API ------------------------------------------------------------------------

    async def api(self, method: str, path: str, params: dict | None = None, body: Any = None, *,
                  account: bool = False, kind: str = "read", prefer_key: bool = False) -> Any:
        """One Data API call. `account`: needs the owner's login (a key only reads public data).
        `kind`: what the quota counts it as (search / write / read / watch)."""
        if path not in API_PATHS:
            raise ToolError(f"refusing odd API path {path!r}")
        if kind == "write" and not self.can_write:
            raise ToolError(NO_WRITE if self.connected else NOT_CONNECTED)
        headers = {}
        if self.api_key and prefer_key and not account:
            headers["X-Goog-Api-Key"] = self.api_key  # a header, so the key never appears in a URL or log
        elif self.connected:
            headers["Authorization"] = f"Bearer {await self.auth.token()}"
        elif account:
            raise ToolError(NOT_CONNECTED)
        elif self.api_key:
            headers["X-Goog-Api-Key"] = self.api_key
        else:
            raise ToolError(NOT_SET_UP)
        self.quota.take(kind, COST[kind])
        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        try:
            resp = await self._client.request(method, f"{self._api_base}{path}", params=params, json=body,
                                              headers=headers, timeout=30, follow_redirects=False)
        except httpx.TransportError as e:
            raise ToolError(f"YouTube API unreachable ({type(e).__name__})") from e
        if resp.status_code >= 400:
            raise ToolError(api_error(resp))
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ToolError(f"YouTube sent an unexpected reply (HTTP {resp.status_code})") from None

    async def _pages(self, path: str, params: dict, limit: int, *, account: bool = False, kind: str = "read") -> list[dict]:
        out: list[dict] = []
        token = None
        while len(out) < limit:
            data = await self.api("GET", path, {**params, "maxResults": min(50, limit - len(out)),
                                                "pageToken": token}, account=account, kind=kind) or {}
            out += data.get("items", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out[:limit]

    async def channels(self, ids: list[str], kind: str = "read") -> list[dict]:
        if not ids:
            return []
        data = await self.api("GET", "/channels", {"part": "snippet,statistics", "id": ",".join(ids[:50])},
                              kind=kind) or {}
        return data.get("items", [])

    async def videos(self, ids: list[str], kind: str = "read") -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = await self.api("GET", "/videos", {"part": "snippet,contentDetails,statistics",
                                                     "id": ",".join(ids[i:i + 50])}, kind=kind) or {}
            out += data.get("items", [])
        order = {v: n for n, v in enumerate(ids)}
        return sorted(out, key=lambda v: order.get(v.get("id"), 0))

    async def search(self, query: str, kind: str, order: str, limit: int, after: str | None,
                     channel_id: str | None) -> list[dict]:
        data = await self.api("GET", "/search", {
            "part": "snippet", "q": query, "type": kind, "order": order, "maxResults": limit,
            "publishedAfter": after, "channelId": channel_id,
        }, kind="search") or {}
        return data.get("items", [])

    async def comments(self, vid: str, limit: int, order: str) -> list[dict]:
        return await self._pages_key("/commentThreads", {"part": "snippet", "videoId": vid, "order": order,
                                                         "textFormat": "plainText"}, limit)

    async def _pages_key(self, path: str, params: dict, limit: int) -> list[dict]:
        out: list[dict] = []
        token = None
        while len(out) < limit:
            data = await self.api("GET", path, {**params, "maxResults": min(100, limit - len(out)),
                                                "pageToken": token}, prefer_key=True) or {}
            out += data.get("items", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out[:limit]

    async def playlist_items(self, pid: str, limit: int, *, account: bool = False) -> list[dict]:
        return await self._pages("/playlistItems", {"part": "snippet,contentDetails", "playlistId": pid},
                                 limit, account=account)

    async def playlists(self, ids: list[str]) -> list[dict]:
        data = await self.api("GET", "/playlists", {"part": "snippet,status,contentDetails",
                                                    "id": ",".join(ids[:50])}) or {}
        return data.get("items", [])

    # --- the owner's account -------------------------------------------------------------

    async def my_playlists(self) -> list[dict]:
        """Always fresh: whether a playlist is private decides whether a change needs approval."""
        return await self._pages("/playlists", {"part": "snippet,status,contentDetails", "mine": "true"}, 500,
                                 account=True)

    async def subscriptions(self, fresh: bool = False, kind: str = "read") -> list[dict]:
        if not fresh and self._subs and time.monotonic() - self._subs[0] < SUBS_TTL:
            return self._subs[1]
        subs = await self._pages("/subscriptions", {"part": "snippet", "mine": "true", "order": "alphabetical"},
                                 1000, account=True, kind=kind)
        self._subs = (time.monotonic(), subs)
        return subs

    async def subscription_to(self, cid: str) -> dict | None:
        data = await self.api("GET", "/subscriptions", {"part": "snippet", "mine": "true", "forChannelId": cid},
                              account=True) or {}
        items = data.get("items", [])
        return items[0] if items else None

    async def liked(self, limit: int) -> list[dict]:
        return await self._pages("/videos", {"part": "snippet,contentDetails,statistics", "myRating": "like"},
                                 limit, account=True)

    async def add_to_playlist(self, pid: str, vid: str) -> dict:
        return await self.api("POST", "/playlistItems", {"part": "snippet"}, {"snippet": {
            "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}, account=True, kind="write")

    async def remove_playlist_item(self, item_id: str) -> None:
        await self.api("DELETE", "/playlistItems", {"id": item_id}, account=True, kind="write")

    async def new_playlist(self, title: str) -> dict:
        made = await self.api("POST", "/playlists", {"part": "snippet,status"}, {
            "snippet": {"title": title}, "status": {"privacyStatus": "private"}}, account=True, kind="write") or {}
        if made.get("status", {}).get("privacyStatus") != "private":
            raise ToolError(f"YouTube made the playlist {made.get('id')} but didn't confirm it's private; "
                            "the owner should check it")
        return made

    async def subscribe(self, cid: str) -> dict:
        self._subs = None
        return await self.api("POST", "/subscriptions", {"part": "snippet"}, {"snippet": {
            "resourceId": {"kind": "youtube#channel", "channelId": cid}}}, account=True, kind="write")

    async def unsubscribe(self, sub_id: str) -> None:
        self._subs = None
        await self.api("DELETE", "/subscriptions", {"id": sub_id}, account=True, kind="write")


def is_block_page(location: str) -> bool:
    """A redirect away from YouTube (google.com/sorry, consent.youtube.com, a sign-in page)."""
    host = (urllib.parse.urlsplit(urllib.parse.urljoin(SITE + "/", location)).hostname or "").lower()
    return "/sorry/" in location or host not in ("www.youtube.com", "youtube.com", "m.youtube.com")


def api_error(resp: httpx.Response) -> str:
    reason, message = "", ""
    try:
        err = resp.json().get("error", {})
        message = str(err.get("message", ""))[:300]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
    except (ValueError, AttributeError, IndexError):
        message = resp.text[:300]
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return "the YouTube API quota for today is used up (it resets at midnight Pacific time)"
    if reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used in project" in message:
        return "YouTube Data API v3 isn't enabled in the Google Cloud project (APIs & Services → Library)"
    if reason == "commentsDisabled":
        return "comments are turned off for this video"
    if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
        return "the YouTube login doesn't allow this (add YOUTUBE_API_KEY, or `ampi-tools youtube-login --write`)"
    if resp.status_code == 401:
        return "YouTube rejected the login; run `ampi-tools youtube-login` again"
    if resp.status_code == 404:
        return f"YouTube: not found ({message or reason})"
    return f"YouTube error {resp.status_code}: {reason + ': ' if reason else ''}{message}"
