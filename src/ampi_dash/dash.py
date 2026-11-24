"""The dashboard on the web: serves the hub's static pages and passes requests to ampi-tools. User `ampidash`, standard library
only, no data of its own: every request carries the owner's session, which ampi-tools checks. nginx (HTTPS) is the only one
that can connect to its socket."""

from __future__ import annotations

import json
import os
import re
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
PAGES = {"/": "index.html", "/abilities": "abilities.html", "/watchers": "watchers.html", "/activity": "activity.html"}
ASSETS = {"/hub.css": "text/css; charset=utf-8", **{f"/{n}.js": "text/javascript; charset=utf-8" for n in (
    "hub", "overview", "abilities", "watchers", "activity")}}
MAX_BODY = 16 * 1024
WORKERS = 12
COOKIE = "__Host-ampi"
KEY = re.compile(r"[A-Za-z0-9_-]{43}")
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store", "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow",
}


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def ask(request: dict, path: str = "/run/ampi-tools/tools.sock", timeout: float = 20) -> dict:
    raw = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(json.dumps(request).encode() + b"\n")
            while not raw.endswith(b"\n"):
                part = s.recv(65536)
                if not part or len(raw) > 4 * 1024 * 1024:
                    break
                raw += part
        return json.loads(raw)
    except (OSError, ValueError, AttributeError):
        raise Refused(503, "Ampi's tools service isn't reachable; try again") from None


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version, protocol_version = "ampi", "", "HTTP/1.1"
    timeout = 20
    url = ""          # https://HOST, set by make_handler
    static = HERE / "static"
    ask = staticmethod(ask)

    def address_string(self) -> str:
        return "nginx"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        try:
            self._send(code, b"bad request", "text/plain; charset=utf-8")
        except OSError:
            pass

    def _send(self, status: int, body: bytes, ctype: str, cookie: str | None = None) -> None:
        self.send_response(status)
        for k, v in HEADERS.items():
            self.send_header(k, v)
        if cookie is not None:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Secure; HttpOnly; SameSite=Strict"
                             + ("; Max-Age=43200" if cookie else "; Max-Age=0"))
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict, cookie: str | None = None) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8", cookie)

    def _check(self) -> None:
        if self.headers.get_all("Host") != [self.url.removeprefix("https://")]:
            raise Refused(421, "wrong address")
        if self.headers.get("Origin") not in ((self.url,) if self.command == "POST" else (None, self.url)):
            raise Refused(403, "cross-site request refused")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            raise Refused(403, "cross-site request refused")
        if self.headers.get("X-Ampi-Hub") != "web":
            raise Refused(403, "open the page instead")

    def _session(self) -> str:
        found = re.findall(rf"(?:^|;\s*){re.escape(COOKIE)}=([^;\s]*)", "; ".join(self.headers.get_all("Cookie") or []))
        if len(found) != 1 or not KEY.fullmatch(found[0]):
            raise Refused(401, "signed out: send /dashboard to Ampi")
        return found[0]

    def _body(self) -> dict:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json" \
                or self.headers.get("Transfer-Encoding"):
            raise Refused(415, "send JSON")
        n = self.headers.get("Content-Length") or ""
        if not re.fullmatch(r"\d{1,6}", n) or int(n) > MAX_BODY:
            raise Refused(413, "too big")
        try:
            body = json.loads(self.rfile.read(int(n)))
        except ValueError:
            raise Refused(400, "bad JSON") from None
        if not isinstance(body, dict):
            raise Refused(400, "send a JSON object")
        return body

    def _tools(self, request: dict) -> dict:
        r = self.ask(request)
        if not isinstance(r, dict) or not r.get("ok"):
            error = str(r.get("error") or "refused") if isinstance(r, dict) else "refused"
            raise Refused(401 if error.startswith(("signed out", "this link")) else 409, error)
        return r

    def _api(self, path: str) -> None:
        self._check()
        if path == "/api/login" and self.command == "POST":
            r = self._tools({"cmd": "web.login", "key": self._body().get("k")})
            return self._json(200, {"ok": True}, r["session"])
        session = self._session()
        if path == "/api/logout" and self.command == "POST":
            self._body()
            self._tools({"cmd": "web.logout", "session": session})
            return self._json(200, {"ok": True}, "")
        m = re.fullmatch(r"/api/(overview|activity|watchers|abilities)", path)
        if m and self.command == "GET":
            scope = parse_qs(urlsplit(self.path).query).get("scope", ["chat"])[0]
            return self._json(200, self._tools({"cmd": f"hub.{m[1]}", "session": session, "scope": scope}))
        if path == "/api/set" and self.command == "POST":
            b = self._body()
            return self._json(200, self._tools({"cmd": "hub.set", "session": session, "scope": b.get("scope"),
                                                "changes": b.get("changes")}))
        raise Refused(404, "not found")

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path.startswith("/api/"):
                return self._api(path)
            if self.command not in ("GET", "HEAD"):
                raise Refused(405, "not allowed")
            name = PAGES.get(path) or (path[1:] if path in ASSETS else None)
            if name is None:
                raise Refused(404, "not found")
            self._send(200, (self.static / name).read_bytes(), ASSETS.get(path, "text/html; charset=utf-8"))
        except Refused as e:
            self.close_connection = True
            self._json(e.status, {"error": e.message})
        except OSError:
            self.close_connection = True

    do_GET = do_HEAD = do_POST = _handle


class Limited(socketserver.ThreadingMixIn):
    """At most WORKERS connections at a time (the others wait in the socket's queue)."""
    daemon_threads = True

    def process_request(self, request, client_address) -> None:
        self.slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(url: str, ask_fn=ask, static: Path | None = None) -> type[Handler]:
    if not re.fullmatch(r"https://[a-z0-9.-]{1,253}", url):
        sys.exit("AMPI_DASH_URL must be https://HOST")
    return type("Bound", (Handler,), {"url": url, "ask": staticmethod(ask_fn), "static": static or HERE / "static"})


def main() -> None:
    if os.environ.get("LISTEN_FDS") != "1" or os.environ.get("LISTEN_PID") != str(os.getpid()):
        sys.exit("started by ampi-dash.socket only")

    class Server(Limited, socketserver.UnixStreamServer):
        slots = threading.Semaphore(WORKERS)

        def server_bind(self) -> None:
            pass

        def server_activate(self) -> None:
            pass

    server = Server("", make_handler(os.environ.get("AMPI_DASH_URL", "")), bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=3)
    server.serve_forever()


if __name__ == "__main__":
    main()
