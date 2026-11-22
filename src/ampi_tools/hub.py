"""Ampi hub: the owner's configuration pages (first: the address book), served by `ampi-tools hub` as user
ampitools on a Unix socket (/run/ampi-hub/hub.sock, mode 0600). There is no network port: the owner reaches it
through `ssh -L 127.0.0.1:PORT:/run/ampi-hub/hub.sock` (scripts/ampi-hub.ps1), which logs in as root.

Who may use it:
- Only root and ampitools can open the socket (Ampi runs as `assistant` and can't).
- Every API request carries the token in /run/ampi-hub/token. The owner's script has root's helper
  (/usr/local/sbin/ampi-hub-token) write a fresh one at each session and reads it over SSH and opens the page with it in the URL fragment, which never reaches a server; the
  page keeps it in sessionStorage (per origin, so not shared with other ports on 127.0.0.1) and sends it as the
  X-Ampi-Hub header. No cookies: a site open in the owner's browser can't send that header without our CORS
  approval (never given), and can't read the token.
- The Host header must be 127.0.0.1/localhost/[::1] (DNS rebinding); Origin, when sent, must be that same
  origin; Sec-Fetch-Site, when sent, same-origin.
The pages themselves are static (no data in them), so they are served without the token.

The journal gets entry names only, never values.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import socketserver
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .todoist import ToolError
from .vault import ADDRESS_FIELDS, LATLNG, Conflict, Vault, check_name, entry_rev

log = logging.getLogger("ampi-hub")

STATIC = Path(__file__).with_name("hub_static")
PAGES = {"/": "index.html", "/address-book": "address-book.html"}
ASSETS = {"/hub.css": ("hub.css", "text/css; charset=utf-8"), "/hub.js": ("hub.js", "text/javascript; charset=utf-8"),
          "/address-book.js": ("address-book.js", "text/javascript; charset=utf-8")}
HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 64 * 1024
GEOCODES_PER_DAY = 30
ADDRESS_KEYS = tuple(f.split(".", 1)[1] for f in ADDRESS_FIELDS if f != "address.full")
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# --- the address book ---------------------------------------------------------------------------


def place_view(e: dict) -> dict:
    f = e.get("fields") or {}
    return {"name": e["name"], "rev": entry_rev({k: v for k, v in e.items() if k != "id"}),
            "company": f.get("company", ""),
            "address": {k: (f.get("address") or {}).get(k, "") for k in ADDRESS_KEYS},
            "location": f.get("location", ""), "timezone": f.get("timezone", ""),
            "search": bool(e.get("search", False)), "revealable": bool(e.get("revealable", False))}


def _text(v: Any, what: str, limit: int = 200) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise HttpError(400, f"{what} must be text")
    v = v.strip()
    if len(v) > limit:
        raise HttpError(400, f"{what} is too long")
    return v


def place_fields(body: dict) -> tuple[dict, dict]:
    """The page's form → (fields for Vault.replace_entry, switches)."""
    a = body.get("address") or {}
    if not isinstance(a, dict):
        raise HttpError(400, "address must be an object")
    unknown = set(a) - set(ADDRESS_KEYS)
    if unknown:
        raise HttpError(400, f"unknown address fields: {', '.join(sorted(unknown))[:80]}")
    fields: dict[str, Any] = {}
    address = {k: _text(a.get(k), k, 120) for k in ADDRESS_KEYS}
    if any(address.values()):
        fields["address"] = {k: v for k, v in address.items() if v}
    for key in ("company", "location", "timezone"):
        value = _text(body.get(key), key, 120)
        if value:
            fields[key] = value
    flags = {}
    for flag in ("search", "revealable"):
        if flag in body:
            if not isinstance(body[flag], bool):
                raise HttpError(400, f"{flag} must be true or false")
            flags[flag] = body[flag]
    return fields, flags


class AddressBook:
    def __init__(self, vault: Vault, geocode: Callable[[str], dict] | None):
        self.vault = vault
        self._geocode = geocode
        self._geo_day, self._geo_count = "", 0
        self._geo_lock = threading.Lock()

    def places(self) -> list[dict]:
        rows = [place_view(e) for e in self.vault.entries() if e.get("type") == "place"]
        order = {"home": 0, "work": 1}
        return sorted(rows, key=lambda r: (order.get(r["name"], 2), r["name"]))

    def _one(self, name: str) -> dict:
        e = self.vault.entry(name)
        if not e or e.get("type") != "place":
            raise HttpError(404, f"no place {name}")
        return place_view(e)

    def create(self, body: dict) -> dict:
        name = check_name(_text(body.get("name"), "name", 40))
        existing = self.vault.entry(name)
        if existing:
            raise HttpError(409, f"there is already an entry {name}"
                            + ("" if existing.get("type") == "place" else f" (a {existing['type']})"))
        fields, flags = place_fields(body)
        self.vault.replace_entry(name, "place", fields, flags=flags, create=True)
        log.info("hub: added place %s", name)
        return self._one(name)

    def update(self, name: str, body: dict) -> dict:
        check_name(name)
        current = self.vault.entry(name)
        if not current or current.get("type") != "place":
            raise HttpError(404, f"no place {name}")
        rev = body.get("rev")
        if not isinstance(rev, str):
            raise HttpError(400, "rev is missing (reload the page)")
        new_name = _text(body.get("name"), "name", 40) or name
        fields, flags = place_fields(body)
        self.vault.replace_entry(name, "place", fields, new_name=new_name, flags=flags, rev=rev)
        log.info("hub: changed place %s%s", name, f" (now {new_name})" if new_name != name else "")
        return self._one(new_name)

    def delete(self, name: str, rev: Any) -> None:
        self._one(check_name(name))
        if not isinstance(rev, str):
            raise HttpError(400, "rev is missing (reload the page)")
        self.vault.delete(name, rev=rev)
        log.info("hub: deleted place %s", name)

    def geocode(self, body: dict) -> dict:
        query = _text(body.get("address"), "address", 300)
        if len(query) < 3:
            raise HttpError(400, "type an address first")
        if self._geocode is None:
            raise HttpError(503, "looking up addresses needs MAPS_API_KEY in /home/ampitools/env; or type "
                                 "latitude,longitude")
        with self._geo_lock:
            today = time.strftime("%Y-%m-%d")
            if self._geo_day != today:
                self._geo_day, self._geo_count = today, 0
            if self._geo_count >= GEOCODES_PER_DAY:
                raise HttpError(429, f"{GEOCODES_PER_DAY} lookups a day from the hub; type latitude,longitude")
            self._geo_count += 1
        found = self._geocode(query)
        if not found:
            raise HttpError(404, "Google Maps didn't find that address; try a fuller one or type latitude,longitude")
        log.info("hub: looked up an address")
        return found


def maps_geocoder(home: Path) -> Callable[[str], dict] | None:
    """One Google Maps text search per lookup (the owner presses "Find"); None without MAPS_API_KEY."""
    key = os.environ.get("MAPS_API_KEY", "").strip()
    if not key:
        return None
    import httpx

    from .maps import Maps
    from .ops_maps import safe_url

    def geocode(address: str) -> dict:
        async def lookup() -> dict:
            async with httpx.AsyncClient() as client:
                # No budget file: ampi-tools keeps its own; the hub has its own small daily cap above.
                m = Maps(client, key, None, language=os.environ.get("MAPS_LANGUAGE", "en").strip(),
                         region=os.environ.get("MAPS_REGION", "").strip())
                data = await m.search(address, limit=1)
            for p in data.get("places") or []:
                loc = (p.get("location") or {}) if isinstance(p, dict) else {}
                if loc.get("latitude") is not None and loc.get("longitude") is not None:
                    lat, lng = float(loc["latitude"]), float(loc["longitude"])
                    links = p.get("googleMapsLinks") or {}
                    return {"location": f"{lat:.6f},{lng:.6f}",
                            "title": str((p.get("attribution") or {}).get("title") or "")[:200],
                            "url": safe_url(links.get("placeUrl")) or ""}
            return {}

        try:
            return asyncio.run(lookup())
        except ToolError as e:
            raise HttpError(502, f"Google Maps: {e}") from None
        except (ValueError, TypeError):
            raise HttpError(502, "Google Maps sent an unexpected reply") from None

    return geocode


# --- HTTP ------------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ampi-hub"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    hub: Hub  # set on the subclass made by Hub.handler()

    def address_string(self) -> str:  # a Unix socket has no client address
        return "ssh"

    def log_message(self, fmt: str, *args: Any) -> None:  # the request line only (paths hold names, not values)
        log.info("hub: " + fmt, *args)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        path = urlsplit(self.path).path if hasattr(self, "path") else "?"
        log.info("hub: %s %s %s", getattr(self, "command", "?"), path[:80], code)

    # --- responses ---------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # Our own minimal error body (the default one echoes parts of the request into HTML).
        self.close_connection = True
        try:
            self._send(code, (message or HTTPStatus(code).phrase).encode(), "text/plain; charset=utf-8")
        except OSError:
            pass

    # --- checks ------------------------------------------------------------------------

    def _host(self) -> str:
        host = (self.headers.get("Host") or "").strip().lower()
        m = re.fullmatch(r"(\[::1\]|127\.0\.0\.1|localhost)(?::(\d{1,5}))?", host)
        if not m:
            raise HttpError(421, "open the hub at http://127.0.0.1:PORT (through scripts/ampi-hub.ps1)")
        return host

    def _check_api(self) -> None:
        host = self._host()
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise HttpError(403, "cross-site request refused")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise HttpError(403, "cross-site request refused")
        token = self.headers.get("X-Ampi-Hub") or ""
        expected = self.hub.current_token()
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HttpError(401, "the hub was restarted or the page was opened without the script: run "
                                 "scripts/ampi-hub.ps1 again")

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "send JSON")
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "send a Content-Length")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise HttpError(400, "bad Content-Length") from None
        if length < 0 or length > MAX_BODY:
            raise HttpError(413, "too big")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HttpError(400, "bad JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "send a JSON object")
        return body

    # --- routing -----------------------------------------------------------------------

    def _handle(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                self._host()
                if method not in ("GET", "HEAD"):
                    raise HttpError(405, "not allowed")
                return self._static(path)
            self._check_api()
            book = self.hub.book
            if path == "/api/places":
                if method == "GET":
                    return self._json(200, {"places": book.places()})
                if method == "POST":
                    return self._json(201, {"place": book.create(self._body())})
            elif path == "/api/geocode" and method == "POST":
                return self._json(200, book.geocode(self._body()))
            elif m := re.fullmatch(r"/api/places/([^/]{1,100})", path):
                name = unquote(m.group(1))
                if method == "PUT":
                    return self._json(200, {"place": book.update(name, self._body())})
                if method == "DELETE":
                    book.delete(name, self._body().get("rev"))
                    return self._json(200, {"deleted": name})
            raise HttpError(404, "not found")
        except HttpError as e:
            self._error(e.status, e.message)
        except Conflict as e:
            self._error(409, str(e))
        except ToolError as e:
            self._error(400, str(e))
        except Exception:
            log.exception("hub: request failed")
            self._error(500, "something went wrong on the server (see journalctl -u ampi-hub)")

    def _error(self, status: int, message: str) -> None:
        # An unread body would be parsed as the next request on a kept-alive connection (its headers chosen by
        # whoever sent it): after an error the connection always closes.
        self.close_connection = True
        if urlsplit(self.path).path.startswith("/api/"):
            self._json(status, {"error": message})
        else:
            self._send(status, message.encode(), "text/plain; charset=utf-8")

    def _static(self, path: str) -> None:
        if path in PAGES:
            name, ctype = PAGES[path], "text/html; charset=utf-8"
        elif path in ASSETS:
            name, ctype = ASSETS[path]
        else:
            raise HttpError(404, "not found")
        self._send(200, (STATIC / name).read_bytes(), ctype)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


if hasattr(socketserver, "UnixStreamServer"):  # (not on Windows, where only the tests run)
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        request_queue_size = 16


class Hub:
    """token_file: the access token as root's helper (ampi-hub-token, run by the owner's script) last wrote it;
    read again when it changes, so every session of the script gets a new token. token: a fixed one (tests)."""

    def __init__(self, home: Path, token: str | None = None, geocode: Callable[[str], dict] | None = None,
                 token_file: Path | None = None):
        self.token = token
        self.token_file = token_file
        self._token_stamp: tuple | None = None
        self._token_lock = threading.Lock()
        self.book = AddressBook(Vault(home), geocode)

    def current_token(self) -> str:
        if self.token_file is None:
            return self.token or ""
        with self._token_lock:
            try:
                st = self.token_file.stat()
                stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
                if stamp != self._token_stamp:
                    value = self.token_file.read_text().strip()
                    self.token = value if re.fullmatch(r"[A-Za-z0-9_-]{32,100}", value) else ""
                    self._token_stamp = stamp
            except OSError:
                self.token, self._token_stamp = "", None
            return self.token or ""

    def handler(self) -> type[Handler]:
        return type("BoundHandler", (Handler,), {"hub": self, "timeout": 30})


def serve(home: Path, run_dir: Path) -> None:
    os.umask(0o077)
    sock_path, token_path = run_dir / "hub.sock", run_dir / "token"
    # A random token nobody knows until the owner's script asks root's helper for a fresh one.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32) + "\n")
    hub = Hub(home, geocode=maps_geocoder(home), token_file=token_path)
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass
    with Server(str(sock_path), hub.handler()) as server:
        os.chmod(sock_path, 0o600)
        log.info("hub: listening on %s", sock_path)
        try:
            server.serve_forever()
        finally:
            for p in (sock_path, token_path):
                try:
                    p.unlink()
                except OSError:
                    pass


def main(home: Path) -> None:
    run_dir = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/ampi-hub").split(":")[0])
    serve(home, run_dir)

