"""Wolt through the endpoints wolt.com's own web app uses (Wolt has no public consumer API).

The owner chose this knowing it's unofficial and outside Wolt's terms for automated use, so it stays small and
polite: one request at a time, about one a second, a few hundred an hour, an honest user agent, and a long pause
as soon as Wolt answers 403/429 or anything that looks like a block. Nothing here tries to look like a browser.

What it does: find venues and dishes near the owner, show a venue (hours, open now, delivery estimate, minimum
order, fees) and its menu, and with the owner's Wolt login: their orders and order status, and filling a basket
in their account. **It never places or pays for an order**: Wolt requires a fraud-check device fingerprint and
signed fields for that, which we won't fake. The owner checks out in the Wolt app, where the basket is waiting.

The login is a refresh token the owner copies from their own browser (`ampi-tools wolt-login`); Wolt rotates it
on every use, so the newest one is saved each time (/home/ampitools/wolt-token.json, mode 600).
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
from pathlib import Path
from typing import Any

import httpx

from .gmail import IGNORABLE
from .meta import register_secret
from .todoist import ToolError

log = logging.getLogger(__name__)

CONSUMER = "https://consumer-api.wolt.com"
RESTAURANT = "https://restaurant-api.wolt.com"
AUTH = "https://authentication.wolt.com"
USER_AGENT = "AmpiAssistant/1.0 (personal assistant; one user)"
NOT_LOGGED_IN = "the owner's Wolt account isn't connected (`ampi-tools wolt-login` on the server)"

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,120}")
ID_RE = re.compile(r"[0-9a-f]{24}")
GAP = 1.1                 # seconds between requests
HOURLY = 300
BACKOFF = 60 * 60         # after 403/429 or a block page
MAX_REPLY = 6_000_000     # the nearby-venues page alone is ~2 MB
TOKEN_TIMEOUT = 15


def slug(v: Any) -> str:
    """A venue slug, or a wolt.com venue link (…/restaurant/<slug>, …/venue/<slug>)."""
    if not isinstance(v, str) or len(v) > 500:
        raise ToolError("expected a Wolt venue (slug or wolt.com link)")
    s = v.strip()
    if SLUG_RE.fullmatch(s):
        return s
    if "://" not in s and s.lower().startswith("wolt.com/"):
        s = "https://" + s
    u = urllib.parse.urlsplit(s)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if u.scheme == "https" and host in ("wolt.com", "www.wolt.com") and len(parts) >= 2 \
            and parts[-2] in ("restaurant", "venue") and SLUG_RE.fullmatch(parts[-1]):
        return parts[-1]
    raise ToolError(f"not a Wolt venue: {v!r} (use the slug from `wolt venues` or a wolt.com venue link)")


def item_id(v: Any) -> str:
    if not isinstance(v, str) or not ID_RE.fullmatch(v):
        raise ToolError(f"bad Wolt item id {v!r}")
    return v


def text(v: Any, limit: int = 120) -> str:
    """Searches and names: one line, no invisible characters."""
    if not isinstance(v, str):
        raise ToolError("expected text")
    s = unicodedata.normalize("NFC", v).strip()
    if not 1 <= len(s) <= limit or IGNORABLE.search(s) or any(unicodedata.category(c) in ("Cc", "Cf") for c in s):
        raise ToolError(f"1-{limit} characters on one line")
    return s


def money(cents: Any, currency: str = "EUR") -> str:
    try:
        c = int(cents)
    except (TypeError, ValueError):
        return "?"
    sign = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency)
    amount = f"{c / 100:.2f}"
    return f"{amount} {sign}" if sign else f"{amount} {currency}"


def one_line(s: Any, limit: int = 200) -> str:
    s = IGNORABLE.sub("", re.sub(r"\s+", " ", str(s or ""))).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def share_url(u: Any) -> str:
    s = str(u or "")
    p = urllib.parse.urlsplit(s)
    if p.scheme == "https" and (p.hostname or "") in ("wolt.com", "www.wolt.com") and len(s) < 500 and " " not in s:
        return s
    return ""


def looks_blocked(resp: httpx.Response) -> bool:
    if resp.status_code in (403, 429):
        return True
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype and resp.status_code >= 400:
        return True
    body = resp.content[:4000].lower()
    return b"captcha" in body or b"cf-chl" in body or b"are you a robot" in body


# --- limits and login -------------------------------------------------------------------------


class Limiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._hour: list[float] = []
        self.paused_until = 0.0

    def check(self) -> None:
        now = time.monotonic()
        if now < self.paused_until:
            raise ToolError(f"Wolt refused requests from the server; paused for "
                            f"{int((self.paused_until - now) / 60) + 1} more minute(s). Don't try another way.")
        self._hour = [t for t in self._hour if now - t < 3600]
        if len(self._hour) >= HOURLY:
            raise ToolError("too many Wolt requests in the last hour; try again later")

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self.check()
        async with self._lock:  # one request at a time
            wait = self._last + GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.check()
            self._last = time.monotonic()
            self._hour.append(self._last)
            yield

    def block(self) -> None:
        self.paused_until = time.monotonic() + BACKOFF
        log.warning("Wolt refused or blocked a request; pausing for %s minutes", BACKOFF // 60)


class WoltAuth:
    """The owner's refresh token (rotated by Wolt on every use) and a short-lived access token."""

    def __init__(self, path: Path, client: httpx.AsyncClient, auth_url: str = AUTH):
        self.path = path
        self._client = client
        self._auth_url = auth_url
        self._access: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        register_secret(self._refresh_token())

    def configured(self) -> bool:
        return bool(self._refresh_token())

    def _refresh_token(self) -> str | None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        tok = data.get("refresh_token") if isinstance(data, dict) else None
        return tok if isinstance(tok, str) and tok else None

    def save(self, refresh_token: str) -> None:
        if not re.fullmatch(r"[\x21-\x7e]{20,4096}", refresh_token or ""):
            raise ToolError("that doesn't look like a Wolt token")
        register_secret(refresh_token)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"refresh_token": refresh_token, "saved": int(time.time())}, f)
        os.replace(tmp, self.path)

    async def token(self) -> str:
        async with self._lock:
            if self._access and time.monotonic() < self._expires:
                return self._access
            refresh = self._refresh_token()
            if not refresh:
                raise ToolError(NOT_LOGGED_IN)
            try:
                resp = await self._client.post(
                    f"{self._auth_url}/v1/wauth2/access_token", timeout=TOKEN_TIMEOUT, follow_redirects=False,
                    data={"grant_type": "refresh_token", "refresh_token": refresh},
                    headers={"Accept": "application/json", "Origin": "https://wolt.com", "User-Agent": USER_AGENT})
            except httpx.TransportError as e:
                raise ToolError(f"Wolt login service unreachable ({type(e).__name__})") from e
            if resp.status_code in (400, 401):
                raise ToolError("Wolt no longer accepts the saved login (it expires, or was used elsewhere); "
                                "run `ampi-tools wolt-login` again")
            if resp.status_code != 200:
                raise ToolError(f"Wolt login service answered {resp.status_code}")
            try:
                data = resp.json()
                access, new_refresh, lifetime = data["access_token"], data["refresh_token"], float(data["expires_in"])
            except (ValueError, KeyError, TypeError):
                raise ToolError("Wolt's login service sent an unexpected reply") from None
            register_secret(access)
            self.save(new_refresh)  # before anything else: the old one is already invalid
            self._access, self._expires = access, time.monotonic() + max(30.0, lifetime - 60)
            return access


# --- client -------------------------------------------------------------------------------------


class Wolt:
    def __init__(self, client: httpx.AsyncClient, home: Path | None = None, *, near: dict | None = None,
                 language: str = "en", consumer: str = CONSUMER, restaurant: str = RESTAURANT, auth_url: str = AUTH):
        self._client = client
        self._consumer, self._restaurant = consumer, restaurant
        self.near = near
        self.language = language if re.fullmatch(r"[a-z]{2}", language or "") else "en"
        self.limiter = Limiter()
        self.auth = WoltAuth(home / "wolt-token.json", client, auth_url) if home else None

    @property
    def connected(self) -> bool:
        return bool(self.auth and self.auth.configured())

    def status(self) -> str:
        where = "home area set" if self.near else "no home area (WOLT_NEAR)"
        return f"{'account connected' if self.connected else 'not logged in (read-only lookups)'}, {where}"

    def location(self, near: dict | None = None) -> dict:
        loc = near or self.near
        if not loc:
            raise ToolError("no location: give --near lat,lng, or set WOLT_NEAR on the server")
        return loc

    async def request(self, method: str, base: str, path: str, *, params: dict | None = None, body: Any = None,
                      account: bool = False) -> Any:
        if base not in (self._consumer, self._restaurant) or not path.startswith("/") or ".." in path:
            raise ToolError("refusing odd Wolt request")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "platform": "Web",
                   "app-language": self.language}
        if account:
            if not self.connected:
                raise ToolError(NOT_LOGGED_IN)
            headers["Authorization"] = f"Bearer {await self.auth.token()}"
        async with self.limiter.slot():
            try:
                resp = await self._client.request(method, base + path, params=params, json=body, headers=headers,
                                                  timeout=30, follow_redirects=False)
            except httpx.TransportError as e:
                raise ToolError(f"Wolt unreachable ({type(e).__name__})") from e
        if looks_blocked(resp):
            self.limiter.block()
            raise ToolError(f"Wolt refused the request ({resp.status_code}); Wolt requests are paused for an hour. "
                            "Tell the owner; don't try another way.")
        if resp.status_code == 401 and account:
            raise ToolError("Wolt rejected the login; run `ampi-tools wolt-login` again")
        if resp.status_code == 404:
            raise ToolError("Wolt: not found (wrong venue or item?)")
        if resp.status_code >= 400:
            raise ToolError(f"Wolt error {resp.status_code}")
        if len(resp.content) > MAX_REPLY:
            raise ToolError("Wolt sent too big a reply")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise ToolError("Wolt sent an unexpected reply") from None

    # --- public data -----------------------------------------------------------------------

    async def search(self, query: str, target: str, near: dict | None = None) -> dict:
        loc = self.location(near)
        return await self.request("POST", self._restaurant, "/v1/pages/search", body={
            "q": query, "target": target, "lat": loc["latitude"], "lon": loc["longitude"]})

    async def nearby(self, near: dict | None = None) -> list[dict]:
        loc = self.location(near)
        data = await self.request("GET", self._consumer, "/v1/pages/restaurants",
                                  params={"lat": loc["latitude"], "lon": loc["longitude"]})
        out = []
        for sec in data.get("sections") or []:
            for it in sec.get("items") or []:
                if isinstance(it, dict) and isinstance(it.get("venue"), dict):
                    out.append(it["venue"])
        return out

    async def venue_static(self, venue: str) -> dict:
        return await self.request("GET", self._consumer, f"/order-xp/web/v1/pages/venue/slug/{slug(venue)}/static")

    async def venue_dynamic(self, venue: str, near: dict | None = None) -> dict:
        loc = self.location(near)
        return await self.request("GET", self._consumer, f"/order-xp/web/v1/venue/slug/{slug(venue)}/dynamic/",
                                  params={"lat": loc["latitude"], "lon": loc["longitude"],
                                          "selected_delivery_method": "homedelivery"})

    async def assortment(self, venue: str) -> dict:
        return await self.request("GET", self._consumer,
                                  f"/consumer-api/consumer-assortment/v1/venues/slug/{slug(venue)}/assortment",
                                  params={"language": self.language})

    async def assortment_search(self, venue: str, query: str) -> dict:
        return await self.request("POST", self._consumer,
                                  f"/consumer-api/consumer-assortment/v1/venues/slug/{slug(venue)}/assortment/items/search",
                                  params={"language": self.language}, body={"q": query})

    async def assortment_items(self, venue: str, ids: list[str]) -> dict:
        return await self.request("POST", self._consumer,
                                  f"/consumer-api/consumer-assortment/v1/venues/slug/{slug(venue)}/assortment/items",
                                  params={"language": self.language}, body={"item_ids": [item_id(i) for i in ids]})

    # --- the owner's account ---------------------------------------------------------------

    async def orders(self) -> dict:
        return await self.request("GET", self._consumer, "/order-tracking-api/v1/order_history/",
                                  params={"limit": 20}, account=True)

    async def order_status(self, purchase_id: str) -> dict:
        if not ID_RE.fullmatch(purchase_id):
            raise ToolError(f"bad Wolt order id {purchase_id!r}")
        return await self.request("GET", self._restaurant, f"/v2/order_details/purchase_tracking/{purchase_id}",
                                  account=True)

    async def baskets(self, near: dict | None = None) -> dict:
        loc = self.location(near)
        return await self.request("GET", self._consumer, "/order-xp/web/v1/pages/baskets",
                                  params={"lat": loc["latitude"], "lon": loc["longitude"]}, account=True)

    async def save_basket(self, payload: dict) -> dict:
        return await self.request("POST", self._consumer, "/order-xp/v1/baskets", body=payload, account=True)


# --- baskets: turning "2 × pad thai, extra tofu" into exactly what Wolt expects ------------------


def _by_id(records: Any) -> dict[str, dict]:
    return {r["id"]: r for r in records or [] if isinstance(r, dict) and isinstance(r.get("id"), str)}


def _match(name: str, candidates: dict[str, dict]) -> str:
    """An option value by id, or by name (exact, then a unique case-insensitive match)."""
    if name in candidates:
        return name
    low = name.casefold()
    exact = [k for k, v in candidates.items() if str(v.get("name", "")).casefold() == low]
    if len(exact) == 1:
        return exact[0]
    part = [k for k, v in candidates.items() if low in str(v.get("name", "")).casefold()]
    if len(part) == 1:
        return part[0]
    names = ", ".join(one_line(v.get("name"), 40) for v in candidates.values())
    raise ToolError(f"{name!r} is {'ambiguous' if part else 'not a choice'}; choices: {names}")


def build_basket(assortment: dict, venue: dict, wanted: list[dict]) -> tuple[dict, list[str], int]:
    """Checks every line against the menu and returns (Wolt basket payload, readable lines, subtotal in cents).

    wanted: [{"id": item id, "count": n, "options": {option name or id: value name/id or [names]}}]."""
    items, options = _by_id(assortment.get("items")), _by_id(assortment.get("options"))
    currency = str(venue.get("currency") or "EUR")
    out, lines, total = [], [], 0
    seen: set[str] = set()
    for w in wanted:
        iid = item_id(w.get("id"))
        if iid in seen:
            raise ToolError("each item once; use count for more")
        seen.add(iid)
        item = items.get(iid)
        if item is None:
            raise ToolError(f"item {iid} isn't on this venue's menu")
        if item.get("disabled_info"):
            raise ToolError(f"{one_line(item.get('name'), 60)} isn't available right now")
        count = w.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise ToolError("count must be 1-20")
        hi = item.get("max_quantity_per_purchase")
        if isinstance(hi, int) and count > hi:
            raise ToolError(f"at most {hi} × {one_line(item.get('name'), 60)}")
        chosen = w.get("options") or {}
        if not isinstance(chosen, dict):
            raise ToolError("options must be an object {option: choice}")
        configs = [c for c in item.get("options") or [] if isinstance(c, dict)]
        unit = int(item.get("price") or 0)
        opt_payload, opt_text = [], []
        used: set[str] = set()
        for cfg in configs:
            root = options.get(str(cfg.get("option_id")))
            if root is None:
                raise ToolError(f"Wolt's menu data for {one_line(item.get('name'), 60)} is incomplete")
            key = next((k for k in chosen if k == cfg["id"] or k == root["id"]
                        or str(k).casefold() == str(cfg.get("name") or root.get("name", "")).casefold()), None)
            picks = chosen.get(key, []) if key is not None else []
            if key is not None:
                used.add(key)
            picks = [picks] if isinstance(picks, str) else picks
            if not isinstance(picks, list) or not all(isinstance(p, str) for p in picks):
                raise ToolError("option choices must be names or lists of names")
            values = _by_id(root.get("values"))
            vids = [_match(p, values) for p in picks]
            rng = ((cfg.get("multi_choice_config") or {}).get("total_range") or {})
            lo, hi_ = int(rng.get("min") or 0), int(rng.get("max") or max(len(vids), 1))
            cname = one_line(cfg.get("name") or root.get("name"), 60)
            if not lo <= len(vids) <= hi_:
                choices = ", ".join(one_line(v.get("name"), 40) for v in values.values())
                need = f"exactly {lo}" if lo == hi_ else f"{lo}-{hi_}"
                raise ToolError(f"{one_line(item.get('name'), 60)}: choose {need} for {cname!r} ({choices})")
            if len(set(vids)) != len(vids):
                raise ToolError(f"{cname}: each choice once")
            unit += sum(int(values[v].get("price") or 0) for v in vids)
            opt_payload.append({"id": cfg["id"], "values": [{"id": v, "count": 1} for v in vids]})
            if vids:
                opt_text.append(f"{cname}: " + ", ".join(one_line(values[v].get("name"), 40) for v in vids))
        unknown = [k for k in chosen if k not in used]
        if unknown:
            names = ", ".join(one_line(c.get("name"), 40) for c in configs) or "none"
            raise ToolError(f"{one_line(item.get('name'), 60)} has no option {unknown[0]!r} (options: {names})")
        line = unit * count
        total += line
        name = one_line(item.get("name"), 80)
        out.append({"id": iid, "count": count, "name": name, "price": line, "options": opt_payload,
                    "substitution_settings": {"is_allowed": False}})
        lines.append(f"{count} × {name}" + (f" ({'; '.join(opt_text)})" if opt_text else "") + f" — {money(line, currency)}")
    if not out:
        raise ToolError("the basket is empty")
    return {"venue_id": venue["id"], "currency": currency, "items": out}, lines, total
