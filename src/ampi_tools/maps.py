"""Google Maps through Google's own API for AI agents ("Maps Experience agent tools", mapstools.googleapis.com).

That API is the one Google sanctions for answering with an LLM: it returns a short summary plus Google Maps
links as sources, and its terms ask that every answer shows those sources right after what they support.
Nothing from Google Maps is cached: each command asks Google again.

Needs MAPS_API_KEY (a Google Cloud key with billing on and "Maps Experience agent tools" enabled; the owner caps
it per day in the Cloud Console, see docs/SETUP.md). Without a key only keyless Google Maps links work
(https://developers.google.com/maps/documentation/urls).

Every call goes through a small limiter and a local budget kept on disk (Google's free monthly allowance:
10,000 Essentials and 5,000 Pro requests; we stay well below), so a prompt-injected assistant can't run up a bill.
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
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .gmail import IGNORABLE
from .todoist import ToolError

log = logging.getLogger(__name__)

API = "https://mapstools.googleapis.com"
METHODS = {"searchText", "computeRoutes", "lookupWeather", "resolveNames", "resolveMapsUrls"}
SHORT_LINK_HOSTS = {"maps.app.goo.gl"}
MAPS_HOSTS = {"google.com", "www.google.com", "maps.google.com", "maps.app.goo.gl", "goo.gl"}
NOT_SET_UP = ("Google Maps isn't set up yet (MAPS_API_KEY on the server, see docs/SETUP.md); "
              "until then only `maps link` works")

# Requests a day and a month per SKU. Google's free allowance is 10,000 Essentials and 5,000 Pro a month;
# "free" (name and link resolution) costs nothing but still counts against a cap.
DAILY = {"essentials": 250, "pro": 40, "free": 400}
MONTHLY = {"essentials": 7000, "pro": 1000, "free": 10_000}
PER_MINUTE = 20
BACKOFF = 10 * 60
MAX_REPLY = 2_000_000

TRAVEL_MODES = {"drive": "DRIVE", "walk": "WALK", "bicycle": "BICYCLE", "transit": "TRANSIT"}
LINK_MODES = {"drive": "driving", "walk": "walking", "bicycle": "bicycling", "transit": "transit"}
LATLNG_RE = re.compile(r"\s*(-?\d{1,2}(?:\.\d{1,8})?)\s*,\s*(-?\d{1,3}(?:\.\d{1,8})?)\s*")
PLACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,300}")


# --- arguments ---------------------------------------------------------------------------------


def latlng(v: str) -> dict | None:
    """`60.17,24.94` → {"latitude":…, "longitude":…}; None if v isn't a coordinate pair."""
    m = LATLNG_RE.fullmatch(v or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ToolError(f"coordinates out of range: {v!r}")
    return {"latitude": lat, "longitude": lng}


def place_text(v: Any, what: str = "place") -> str:
    """A place as the owner would write it: an address, a name, a plus code or `lat,lng`. One line, no
    invisible characters, no URLs (links go through `maps place`)."""
    if not isinstance(v, str):
        raise ToolError(f"expected a {what}")
    s = unicodedata.normalize("NFC", v).strip()
    if not 1 <= len(s) <= 200 or "\n" in s or "\r" in s or IGNORABLE.search(s):
        raise ToolError(f"{what}: 1-200 characters on one line")
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in s):
        raise ToolError(f"{what}: contains control characters")
    if re.search(r"https?://|www\.", s, re.I):
        raise ToolError(f"{what}: give an address or name, not a link (for a Google Maps link use `maps place`)")
    return s


def waypoint(v: str) -> dict:
    if v.startswith("place_id:") and PLACE_ID_RE.fullmatch(v[9:]):
        return {"placeId": v[9:]}
    ll = latlng(v)
    return {"latLng": ll} if ll else {"address": v}


def maps_link(v: Any) -> str:
    """A single-place Google Maps link (maps.app.goo.gl/…, google.com/maps/place/…, maps.google.com/…)."""
    if not isinstance(v, str) or len(v) > 2000:
        raise ToolError("expected a Google Maps link")
    s = v.strip()
    if "://" not in s:
        s = "https://" + s
    u = urllib.parse.urlsplit(s)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or u.username or u.port or host not in MAPS_HOSTS:
        raise ToolError(f"not a Google Maps link: {v!r}")
    if host == "goo.gl" and not u.path.startswith("/maps/"):
        raise ToolError(f"not a Google Maps link: {v!r}")
    if host in ("google.com", "www.google.com") and not u.path.startswith("/maps"):
        raise ToolError(f"not a Google Maps link: {v!r}")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in s):
        raise ToolError("the link contains spaces or unusual characters")
    return urllib.parse.urlunsplit(("https", host, u.path, u.query, ""))


def when(v: str, tz: ZoneInfo, now: datetime | None = None) -> str:
    """`HH:MM` (today, or tomorrow if that's past), `YYYY-MM-DD HH:MM` or `YYYY-MM-DDTHH:MM`, in the owner's time
    zone unless an offset is given → RFC 3339 for the Routes API."""
    now = now or datetime.now(tz)
    s = v.strip().replace(" ", "T")
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", s):
            h, m = (int(x) for x in s.split(":"))
            t = now.astimezone(tz).replace(hour=h, minute=m, second=0, microsecond=0)
            if t < now - timedelta(minutes=5):
                t += timedelta(days=1)
        else:
            t = datetime.fromisoformat(s)
            if t.tzinfo is None:
                t = t.replace(tzinfo=tz)
    except ValueError:
        raise ToolError(f"time must be HH:MM or YYYY-MM-DD HH:MM, not {v!r}") from None
    return t.isoformat(timespec="seconds")


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown time zone %r; using UTC", name)
        return ZoneInfo("UTC")


# --- formatting --------------------------------------------------------------------------------


def one_line(s: Any, limit: int = 300) -> str:
    s = IGNORABLE.sub("", re.sub(r"\s+", " ", str(s or ""))).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def safe_url(u: Any) -> str:
    """Links from Google's reply are shown only if they really point at Google Maps."""
    s = str(u or "")
    p = urllib.parse.urlsplit(s)
    host = (p.hostname or "").lower()
    if p.scheme == "https" and (host in ("maps.google.com", "www.google.com", "google.com", "maps.app.goo.gl")
                                or host.endswith(".google.com")) and len(s) < 2000 and " " not in s:
        return s
    return ""


def duration(v: Any) -> str:
    m = re.fullmatch(r"(\d+)(?:\.\d+)?s", str(v or ""))
    if not m:
        return "?"
    minutes = round(int(m.group(1)) / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def distance(meters: Any) -> str:
    try:
        m = int(meters)
    except (TypeError, ValueError):
        return "?"
    return f"{m} m" if m < 1000 else f"{m / 1000:.1f} km".replace(".0 km", " km")


def search_link(query: str, place_id: str | None = None) -> str:
    q = {"api": "1", "query": query}
    if place_id:
        q["query_place_id"] = place_id
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(q)


def directions_link(origin: str, destination: str, mode: str = "drive", via: list[str] | None = None) -> str:
    q = {"api": "1", "origin": origin, "destination": destination, "travelmode": LINK_MODES.get(mode, "driving")}
    if via:
        q["waypoints"] = "|".join(via)
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(q)


def sources(items: list[tuple[str, str]]) -> str:
    """The 'Sources' block Google's terms ask for, right after the content."""
    lines = [f"  [{n}] {one_line(title, 120) or 'Google Maps'} — {url}" for n, (title, url) in enumerate(items) if url]
    return ("Sources (Google Maps):\n" + "\n".join(lines)) if lines else "Source: Google Maps"


# --- limits ------------------------------------------------------------------------------------


class Budget:
    """Requests per SKU today and this month (Pacific time, like Google's billing), kept on disk. Counted
    before each call: Google bills failed calls too. Single-threaded daemon, so no race."""

    def __init__(self, path: Path | None):
        self.path = path
        self.day, self.month = "", ""
        self.used_day: dict[str, int] = {}
        self.used_month: dict[str, int] = {}
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text())
                self.day, self.month = str(raw["day"]), str(raw["month"])
                self.used_day = {str(k): int(v) for k, v in raw["used_day"].items()}
                self.used_month = {str(k): int(v) for k, v in raw["used_month"].items()}
            except (OSError, ValueError, AttributeError, TypeError, KeyError):
                # Unknown usage: assume this month's budget is spent rather than starting from zero.
                log.warning("broken %s; treating this month's Maps budget as used up", path)
                self.day, self.month = self._today()
                self.used_day, self.used_month = dict(DAILY), dict(MONTHLY)

    @staticmethod
    def _today() -> tuple[str, str]:
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def _roll(self) -> None:
        day, month = self._today()
        if self.month != month:
            self.month, self.used_month = month, {}
        if self.day != day:
            self.day, self.used_day = day, {}

    def take(self, sku: str) -> None:
        self._roll()
        if self.used_month.get(sku, 0) + 1 > MONTHLY[sku]:
            raise ToolError(f"this month's Google Maps budget for {sku} requests is used up ({MONTHLY[sku]})")
        if self.used_day.get(sku, 0) + 1 > DAILY[sku]:
            extra = " (detailed searches, more than 5 results, pollen)" if sku == "pro" else ""
            raise ToolError(f"today's Google Maps budget for {sku} requests{extra} is used up "
                            f"({DAILY[sku]} a day; resets at midnight Pacific time)")
        self.used_day[sku] = self.used_day.get(sku, 0) + 1
        self.used_month[sku] = self.used_month.get(sku, 0) + 1
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"day": self.day, "month": self.month, "used_day": self.used_day,
                                       "used_month": self.used_month}))
            os.replace(tmp, self.path)
        except OSError:
            log.warning("couldn't save %s", self.path)

    def summary(self) -> str:
        self._roll()
        return ("today " + ", ".join(f"{k} {self.used_day.get(k, 0)}/{v}" for k, v in DAILY.items())
                + "; this month " + ", ".join(f"{k} {self.used_month.get(k, 0)}/{v}" for k, v in MONTHLY.items()))


class Limiter:
    """At most 2 calls at a time and PER_MINUTE a minute; a 10-minute pause when Google answers 429."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(2)
        self._recent: list[float] = []
        self.paused_until = 0.0

    def check(self) -> None:
        now = time.monotonic()
        if now < self.paused_until:
            raise ToolError(f"Google Maps refused requests; paused for {int((self.paused_until - now) / 60) + 1} "
                            "more minute(s)")
        self._recent = [t for t in self._recent if now - t < 60]
        if len(self._recent) >= PER_MINUTE:
            raise ToolError("too many Google Maps requests in the last minute; try again shortly")

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self.check()
        async with self._sem:
            self.check()
            self._recent.append(time.monotonic())
            yield


# --- client ------------------------------------------------------------------------------------


class Maps:
    def __init__(self, client: httpx.AsyncClient, api_key: str | None, home: Path | None = None, *,
                 language: str = "en", region: str = "", near: str = "", tz: str = "UTC", api: str = API):
        self._client = client
        self.api_key = api_key or None
        self._api = api
        self.language = language if re.fullmatch(r"[a-z]{2,3}([_-][A-Za-z]{2,4})?", language or "") else "en"
        self.region = region.upper() if re.fullmatch(r"[A-Za-z]{2}", region or "") else ""
        self.near = latlng(near) if near else None
        self.tz = zone(tz)
        self.budget = Budget(home / "maps-budget.json" if home else None)
        self.limiter = Limiter()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def status(self) -> str:
        return "API key set" if self.api_key else "no API key (links only)"

    async def call(self, method: str, body: dict, sku: str) -> dict:
        if method not in METHODS:
            raise ToolError(f"refusing odd Maps method {method!r}")
        if not self.api_key:
            raise ToolError(NOT_SET_UP)
        async with self.limiter.slot():
            self.budget.take(sku)
            try:
                resp = await self._client.post(f"{self._api}/v1:{method}", json=body, timeout=30,
                                               follow_redirects=False,
                                               headers={"X-Goog-Api-Key": self.api_key})
            except httpx.TransportError as e:
                raise ToolError(f"Google Maps unreachable ({type(e).__name__})") from e
        if resp.status_code == 429:
            self.limiter.paused_until = time.monotonic() + BACKOFF
            log.warning("mapstools answered 429; pausing for %s minutes", BACKOFF // 60)
        if resp.status_code >= 400:
            raise ToolError(api_error(resp))
        if len(resp.content) > MAX_REPLY:
            raise ToolError("Google Maps sent too big a reply")
        try:
            data = resp.json()
        except ValueError:
            raise ToolError(f"Google Maps sent an unexpected reply (HTTP {resp.status_code})") from None
        if not isinstance(data, dict):
            raise ToolError("Google Maps sent an unexpected reply")
        return data

    # --- the tools -----------------------------------------------------------------------

    async def search(self, query: str, *, near: str | None = None, radius: int | None = None, limit: int = 5,
                     details: bool = False) -> dict:
        body: dict[str, Any] = {"textQuery": query, "languageCode": self.language, "pageSize": max(1, min(limit, 10))}
        if self.region:
            body["regionCode"] = self.region
        center = latlng(near) if near else None
        if near and not center:
            body["textQuery"] = f"{query} near {near}"  # an explicit place in the query wins over any bias anyway
        elif center or self.near:
            circle: dict[str, Any] = {"center": center or self.near}
            if radius:
                circle["radiusMeters"] = max(100, min(radius, 50_000))
            body["locationBias"] = {"circle": circle}
        if details:
            body["includeExtendedDetails"] = True
        sku = "pro" if details or body["pageSize"] > 5 else "essentials"
        return await self.call("searchText", body, sku)

    async def route(self, origin: str, destination: str, *, mode: str = "drive", via: list[str] | None = None,
                    depart: str | None = None, arrive: str | None = None, alternatives: bool = False) -> dict:
        body: dict[str, Any] = {"origin": waypoint(origin), "destination": waypoint(destination),
                                "travelMode": TRAVEL_MODES[mode]}
        if via:
            if mode == "transit":
                raise ToolError("transit routes can't have stops in between")
            body["intermediateWaypoints"] = [waypoint(v) for v in via[:10]]
        if depart and arrive:
            raise ToolError("give a departure or an arrival time, not both")
        if depart:
            body["departureTime"] = when(depart, self.tz)
        if arrive:
            body["arrivalTime"] = when(arrive, self.tz)
        if alternatives:
            body["computeAlternativeRoutes"] = True
        if self.near:
            body["locationBias"] = self.near
        return await self.call("computeRoutes", body, "essentials")

    async def weather(self, place: str, *, date: str | None = None, hour: int | None = None, air: bool = False,
                      pollen: bool = False) -> dict:
        caps = ["WEATHER"] + (["AIR_QUALITY"] if air else []) + (["POLLEN"] if pollen else [])
        body: dict[str, Any] = {"location": waypoint(place), "capabilities": caps, "languageCode": self.language,
                                "weatherOptions": {"unitsSystem": "METRIC"}}
        if date:
            y, m, d = (int(x) for x in date.split("-"))
            body["date"] = {"year": y, "month": m, "day": d}
            if hour is not None:
                body["hour"] = hour
        elif hour is not None:
            raise ToolError("--hour needs --date")
        past_air = air and date is not None and date < datetime.now(self.tz).strftime("%Y-%m-%d")
        return await self.call("lookupWeather", body, "pro" if pollen or past_air else "essentials")

    async def resolve_urls(self, urls: list[str]) -> dict:
        return await self.call("resolveMapsUrls", {"urls": urls[:20]}, "free")

    async def resolve_names(self, names: list[str]) -> dict:
        body: dict[str, Any] = {"queries": [{"text": n} for n in names[:20]]}
        if self.region:
            body["regionCode"] = self.region
        return await self.call("resolveNames", body, "free")

    async def expand_short_link(self, url: str) -> str:
        """Where a maps.app.goo.gl link points (one request, the redirect isn't followed), so the place's name
        and coordinates can be read from it. Google's own short-link service; nothing else is fetched."""
        u = urllib.parse.urlsplit(url)
        if (u.hostname or "") not in SHORT_LINK_HOSTS:
            return url
        async with self.limiter.slot():
            try:
                resp = await self._client.get(url, follow_redirects=False, timeout=10)
            except httpx.TransportError as e:
                raise ToolError(f"couldn't open the short link ({type(e).__name__})") from e
        location = resp.headers.get("location", "")
        if not 300 <= resp.status_code < 400 or not location:
            raise ToolError(f"the short link didn't lead anywhere (HTTP {resp.status_code})")
        try:
            return maps_link(location)
        except ToolError:
            raise ToolError("the short link doesn't lead to a Google Maps place") from None


def name_and_coords(url: str) -> tuple[str, dict | None]:
    """From google.com/maps/place/<Name>/@lat,lng,… (what short links usually expand to)."""
    u = urllib.parse.urlsplit(url)
    name, coords = "", None
    m = re.search(r"/maps/place/([^/@]+)", u.path)
    if m:
        name = one_line(urllib.parse.unquote_plus(m.group(1)), 150)
    m = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+)", u.path) or re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", u.path)
    if m:
        with contextlib.suppress(ToolError):
            coords = latlng(f"{m.group(1)},{m.group(2)}")
    if not name:
        q = urllib.parse.parse_qs(u.query).get("q") or urllib.parse.parse_qs(u.query).get("query") or [""]
        name = one_line(q[0], 150)
    return name, coords


def api_error(resp: httpx.Response) -> str:
    status, message, reason = "", "", ""
    try:
        err = resp.json().get("error", {})
        status, message = str(err.get("status", "")), str(err.get("message", ""))[:300]
        for d in err.get("details") or []:
            reason = reason or str(d.get("reason", ""))
    except (ValueError, AttributeError, TypeError):
        message = resp.text[:300]
    if reason in ("SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED") or "has not been used in project" in message:
        return ("the Maps Experience agent tools API isn't enabled for this key/project "
                "(Cloud Console → APIs & Services → Library, and the key's API restrictions)")
    if reason == "BILLING_DISABLED" or "billing" in message.lower():
        return "billing isn't enabled on the Google Cloud project of the Maps key"
    if reason in ("API_KEY_INVALID", "API_KEY_IP_ADDRESS_BLOCKED") or resp.status_code == 401:
        return f"Google rejected the Maps API key ({reason or resp.status_code}); check MAPS_API_KEY and its restrictions"
    if resp.status_code == 429 or status == "RESOURCE_EXHAUSTED":
        return "Google Maps quota reached (the per-day cap set in the Cloud Console, or per-minute); try later"
    if status == "INVALID_ARGUMENT":
        return f"Google Maps couldn't use that request: {message}"
    if status == "NOT_FOUND" or resp.status_code == 404:
        return f"Google Maps: not found ({message})"
    return f"Google Maps error {resp.status_code}: {status + ': ' if status else ''}{message}"
