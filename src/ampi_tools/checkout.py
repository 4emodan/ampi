"""ampi-checkout: the checkout browser's controller (standard library only; user `ampicheckoutctl`).

It drives the Obscura engine (user `ampicheckout`, `obscura serve` on 127.0.0.1:19333, see
deploy/ampi-checkout-engine.service) over the Chrome DevTools Protocol, and answers the ampi-tools daemon on
/run/ampi-checkout.sock (one JSON line in, one JSON line out). Only the daemon may connect (SO_PEERCRED).

What the assistant gets from here is a *snapshot*: the page's text and a numbered list of the things it can
use (inputs, buttons, links, selects, checkboxes), never what is typed into a field. Card and personal values
arrive only in `fill`/`pay` requests, which the daemon sends after the owner approved them, and are set into
exactly the elements that were approved:

- Every frame gets its own isolated JavaScript world (Page.createIsolatedWorld). The script there numbers the
  elements and keeps them in a Map that lives only in that world: page scripts can't see it, renumber it or
  swap an element in it. A ref is (frame, number); an approval binds the element's frame, the frame's origin
  and its tag/type/autocomplete/name/id, and all of it is checked again right before a value is set.
- Values are set through the element's native value setter plus input/change/blur events in that world, never
  by focusing and typing (a page could move the focus).
- Frames with an opaque origin (about:, data:, blob:, srcdoc, javascript:) are never actionable.
- After `pay` the session is read-only: the assistant can look, not click or type (3-D Secure is the owner's).

No arbitrary JavaScript from the assistant, no reading of input values, no console/network events forwarded,
no downloads, no file:// pages, no stealth and no captcha solving.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import secrets
import select
import socket
import struct
import sys
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

if __name__ == "__main__":  # deployed next to browser.py in /usr/local/lib/ampi-checkout (-I drops the script dir)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from . import browser as br
except ImportError:  # pragma: no cover - deployed layout
    import browser as br  # type: ignore[no-redef]

CDP_HOST = "127.0.0.1"   # literal: never "localhost", which could resolve to ::1
CDP_PORT = 19333
SOCKET_PATH = os.environ.get("AMPI_CHECKOUT_SOCKET", "/run/ampi-checkout.sock")
IDLE = 10 * 60
HARD_MAX = 30 * 60
MIN_MEM_MB = 900
NAV_TIMEOUT = 25
PAY_SETTLE = 30
MAX_REQUEST = 262_144
MAX_TEXT = 60_000
MAX_FRAMES = 20
MAX_ELEMENTS = 400
CALL_TIMEOUT = 30

# Field kinds that only `fill`/`pay` may set (never `type`): card, CVC, passwords, one-time codes, bank accounts.
SECRET_FIELD = re.compile(r"cc-|card|cvc|cvv|csc|password|passcode|passwd|otp|one-time|onetime|\bpin\b|pincode|"
                          r"iban|account|security.?code", re.I)
TEXT_TYPES = {"", "text", "email", "tel", "search", "url", "number"}
RISKY = re.compile(r"subscri|recurring|membership|auto-?renew|save (my )?card|remember (this |my )?card|\blink\b|"
                   r"tilaus|jäsenyys|kestotilaus", re.I)
TOTAL_WORDS = re.compile(r"\b(grand total|order total|total|subtotal|to pay|amount due|summa|yhteensä|maksettava|"
                         r"totalt|att betala|gesamt|summe)\b", re.I)
CURRENCY_CODES = {"EUR", "USD", "GBP", "SEK", "NOK", "DKK", "PLN", "CHF", "CZK", "HUF", "ISK", "CAD", "AUD", "JPY"}
SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "kr": "KR", "zł": "PLN"}
AMOUNT = re.compile(
    r"(?P<pre>€|\$|£|\b(?:EUR|USD|GBP|SEK|NOK|DKK|PLN|CHF|CZK|HUF|ISK|CAD|AUD|JPY|kr)\b)?\s?"
    r"(?P<num>\d{1,3}(?:[  .,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s?(?P<post>€|\$|£|\b(?:EUR|USD|GBP|SEK|NOK|DKK|PLN|CHF|CZK|HUF|ISK|CAD|AUD|JPY|kr|zł)\b)?")
CAPTCHA_FRAME = re.compile(r"hcaptcha\.com/captcha/.*frame=challenge|recaptcha/(api2|enterprise)/bframe|"
                           r"challenges\.cloudflare\.com|captcha-delivery\.com|arkoselabs\.com|funcaptcha", re.I)
THREEDS_FRAME = re.compile(r"3ds|threeds|three-d|/acs\b|acs\.|/challenge|authenticat", re.I)
THREEDS_TEXT = re.compile(r"3-?d ?secure|verified by visa|mastercard (id check|securecode)|verify (your )?(payment|"
                          r"purchase|identity)|authenticat(e|ion) (your )?(payment|purchase)|vahvista (maksu|osto)",
                          re.I)


class Refused(Exception):
    """A request we won't run; the message (never containing request values) goes back to the daemon."""


class CdpError(Exception):
    """The engine failed or the connection broke; the session is closed."""


# --- small helpers ------------------------------------------------------------------------------


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    """A Luhn-valid run of 13-19 digits, allowing spaces, dots and dashes between them."""
    for m in re.finditer(r"\d(?:[ .\- ]?\d){12,22}", text):
        digits = re.sub(r"\D", "", m.group())
        for n in range(13, 20):
            for i in range(0, len(digits) - n + 1):
                if luhn_ok(digits[i:i + n]):
                    return True
    return False


def norm_amount(num: str) -> str | None:
    s = num.replace(" ", " ").strip()
    m = re.fullmatch(r"(.*?)([.,](\d{1,2}))?", s)
    whole, frac = (m.group(1), m.group(3)) if m else (s, None)
    if frac is not None and re.search(r"[.,]\d{3}$", s):  # "1.234" / "1,234" read as thousands, not decimals
        whole, frac = s, None
    whole = re.sub(r"[ .,]", "", whole)
    if not whole.isdigit():
        return None
    return f"{int(whole)}.{(frac or '0').ljust(2, '0')}"


def extract_totals(text: str) -> list[dict]:
    """Amounts on lines that say total/summa/…, with their currency (best effort; for the approval's 'page says')."""
    out: list[dict] = []
    lines = text.splitlines()[:3000]
    for i, line in enumerate(lines):
        word = TOTAL_WORDS.search(line)
        if not word:
            continue
        for candidate in (line[word.end():], lines[i + 1] if i + 1 < len(lines) else ""):
            found = False
            for m in AMOUNT.finditer(candidate):
                cur = m.group("pre") or m.group("post")
                if not cur:
                    continue
                amount = norm_amount(m.group("num"))
                if amount is None:
                    continue
                cur = SYMBOLS.get(cur, cur.upper())
                item = {"amount": amount, "currency": cur, "label": word.group(1).lower()}
                if item not in out:
                    out.append(item)
                found = True
            if found:
                break
        if len(out) >= 10:
            break
    return out


def totals_key(totals: list[dict]) -> set[tuple[str, str]]:
    return {(str(t.get("amount")), str(t.get("currency"))) for t in totals or [] if isinstance(t, dict)}


def origin_of(url: str, security_origin: str | None = None) -> str:
    """'https://shop.example' or 'opaque' (about:, data:, blob:, javascript:, srcdoc, 'null')."""
    u = (url or "").strip().lower()
    if security_origin in ("null", "") or u.startswith(("about:", "data:", "blob:", "javascript:", "filesystem:")):
        return "opaque"
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "opaque"
    default = {"http": 80, "https": 443}[p.scheme]
    try:
        port = p.port
    except ValueError:
        return "opaque"
    host = p.hostname if ":" not in p.hostname else f"[{p.hostname}]"
    origin = f"{p.scheme}://{host}" + (f":{port}" if port and port != default else "")
    if security_origin and security_origin.rstrip("/").lower() != origin:
        return "opaque"  # the engine says the document's origin isn't its URL's (sandboxed, inherited…): don't trust it
    return origin


def listener_uid(port: int, paths: tuple[str, ...] = ("/proc/net/tcp",)) -> int | None:
    """The uid owning the LISTEN socket on 127.0.0.1:port (or 0.0.0.0:port), from /proc/net/tcp."""
    want = {f"0100007F:{port:04X}", f"00000000:{port:04X}"}
    owners = set()
    for path in paths:
        try:
            with open(path, encoding="ascii") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) > 7 and parts[1].upper() in want and parts[3] == "0A":
                        owners.add(int(parts[7]))
        except OSError:
            continue
    return owners.pop() if len(owners) == 1 else None


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 0


# --- CDP over a minimal WebSocket client -----------------------------------------------------------


class WebSocket:
    def __init__(self, host: str, port: int, path: str, timeout: float = CALL_TIMEOUT):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk or len(buf) > 65536:
                raise CdpError("handshake failed")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CdpError("handshake refused")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 64 * 1024 * 1024:
                raise CdpError("frame too big")
            payload = self._read(n)
            op = b1 & 0x0F
            if op == 8:
                raise CdpError("connection closed")
            if op == 9:  # ping
                continue
            if op in (0, 1, 2):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Cdp:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.n = 0

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        self.n += 1
        msg: dict[str, Any] = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.sock.settimeout(timeout)
        try:
            self.ws.send(json.dumps(msg))
            while True:
                r = json.loads(self.ws.recv())
                if r.get("id") == self.n:  # events (console, network, …) are dropped unread
                    if "error" in r:
                        raise CdpError(f"{method} failed")
                    return r.get("result") or {}
        except (OSError, ValueError) as e:
            raise CdpError("engine connection failed") from e

    def close(self) -> None:
        self.ws.close()


def connect_engine(engine_uid: int | None, proc_paths: tuple[str, ...] = ("/proc/net/tcp",)) -> Cdp:
    """Only to the engine's own listener: refuse if anything else owns the port (a squatter)."""
    owner = listener_uid(CDP_PORT, proc_paths)
    if engine_uid is None or owner != engine_uid:
        raise Refused("the checkout browser isn't running right now; try again in a minute")
    conn = http.client.HTTPConnection(CDP_HOST, CDP_PORT, timeout=5)
    try:
        conn.request("GET", "/json/version")
        info = json.loads(conn.getresponse().read(65536) or b"{}")
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise CdpError("engine unreachable") from e
    finally:
        conn.close()
    path = urllib.parse.urlsplit(str(info.get("webSocketDebuggerUrl", ""))).path or "/"
    return Cdp(WebSocket(CDP_HOST, CDP_PORT, path))


# --- the page-side script (runs in each frame's isolated world) ------------------------------------

# One op per call: __ampi.run({...}) returns a JSON string. Elements live in a Map in this world only.
PAGE_JS = r"""
(function () {
  const G = globalThis;
  if (!G.__ampi) {
    const els = new Map(), ids = new WeakMap();
    let next = 1;
    const txt = (s, n) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, n || 120);
    const visible = (el) => {
      if (el.hidden || el.disabled) return false;
      if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') return false;
      try { const st = getComputedStyle(el); if (st && (st.display === 'none' || st.visibility === 'hidden')) return false; } catch (e) {}
      return true;
    };
    const kindOf = (el) => {
      const t = el.tagName, ty = (el.type || '').toLowerCase();
      if (t === 'INPUT') {
        if (ty === 'checkbox') return 'checkbox';
        if (ty === 'radio') return 'radio';
        if (['submit', 'button', 'image', 'reset'].includes(ty)) return 'button';
        return 'input';
      }
      if (t === 'TEXTAREA') return 'textarea';
      if (t === 'SELECT') return 'select';
      if (t === 'BUTTON' || el.getAttribute('role') === 'button') return 'button';
      if (t === 'A' && el.hasAttribute('href')) return 'link';
      return null;
    };
    const labelOf = (el, kind) => {
      let s = el.getAttribute('aria-label') || '';
      const lb = el.getAttribute('aria-labelledby');
      if (!s && lb) s = lb.split(/\s+/).map(i => { const n = document.getElementById(i); return n ? n.textContent : ''; }).join(' ');
      if (!s && el.id) { try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) s = l.textContent; } catch (e) {} }
      if (!s) { const l = el.closest && el.closest('label'); if (l) s = l.textContent.replace(el.textContent || '', ''); }
      if (!s && kind === 'button' && el.tagName === 'INPUT') s = el.value;  // a button's caption, not user input
      if (!s && (kind === 'button' || kind === 'link')) s = el.textContent;
      if (!s) s = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
      return txt(s);
    };
    const idOf = (el) => { let i = ids.get(el); if (!i) { i = next++; ids.set(el, i); els.set(i, el); } return i; };
    const describe = (el) => {
      const kind = kindOf(el);
      const d = { n: idOf(el), kind: kind, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
        autocomplete: txt(el.getAttribute('autocomplete'), 60), name: txt(el.getAttribute('name'), 80),
        id: txt(el.id, 80), label: labelOf(el, kind), required: !!el.required, checked: null, filled: false };
      if (kind === 'checkbox' || kind === 'radio') d.checked = !!el.checked;
      if (kind === 'input' || kind === 'textarea') d.filled = (el.value || '') !== '';
      if (kind === 'select') { d.options = Array.from(el.options).slice(0, 30).map(o => txt(o.text, 60));
        const o = el.options[el.selectedIndex]; d.selected = o ? txt(o.text, 60) : null; }
      return d;
    };
    const setValue = (el, v) => {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      try { el.dispatchEvent(new FocusEvent('focus')); } catch (e) {}
      setter.call(el, v);
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: v, inputType: 'insertText' }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      try { el.dispatchEvent(new FocusEvent('blur')); } catch (e) {}
    };
    G.__ampi = { run: function (a) {
      const get = (n) => { const el = els.get(n); return el && el.isConnected ? el : null; };
      if (a.op === 'enumerate') {
        const list = [];
        const all = document.querySelectorAll('input,textarea,select,button,a[href],[role=button]');
        for (const el of all) { if (list.length >= 400) break; if (kindOf(el) && visible(el)) list.push(describe(el)); }
        const out = { href: String(location.href), ready: document.readyState, elements: list };
        let body = '';
        if (document.body) {  // without script/style text (the engine's innerText includes it)
          body = String(document.body.innerText || '');
          document.body.querySelectorAll('script,style,noscript,template').forEach(n => {
            const t = (n.textContent || '').trim(); if (t) body = body.split(t).join('');
          });
        }
        if (a.main) { out.title = txt(document.title, 300); out.text = body.slice(0, 200000); }
        else { out.text = body.slice(0, 4000); }
        return out;
      }
      if (a.op === 'inspect') { const el = get(a.n); return el ? Object.assign({ connected: true }, describe(el)) : { connected: false }; }
      if (a.op === 'set') { const el = get(a.n); if (!el) return { ok: false }; setValue(el, a.value); return { ok: true }; }
      if (a.op === 'click') { const el = get(a.n); if (!el) return { ok: false };
        try { el.scrollIntoView && el.scrollIntoView({ block: 'center' }); } catch (e) {} el.click(); return { ok: true }; }
      if (a.op === 'choose') { const el = get(a.n); if (!el || el.tagName !== 'SELECT') return { ok: false };
        const opts = Array.from(el.options); let i = opts.findIndex(o => txt(o.text, 200) === a.option);
        if (i < 0) { const lo = a.option.toLowerCase(); const m = opts.map((o, k) => [k, txt(o.text, 200).toLowerCase()]).filter(x => x[1] === lo);
          if (m.length === 1) i = m[0][0]; }
        if (i < 0) return { ok: false, why: 'option' };
        el.selectedIndex = i; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
        return { ok: true }; }
      if (a.op === 'state') return { href: String(location.href), ready: document.readyState };
      if (a.op === 'back') { history.back(); return { ok: true }; }
      return { error: 'op' };
    } };
  }
})();
"""


def page_call(op: dict) -> str:
    return PAGE_JS + "\nJSON.stringify(globalThis.__ampi.run(" + json.dumps(op) + "));"


# --- sessions ------------------------------------------------------------------------------------


class Session:
    def __init__(self, sid: str, cdp: Cdp, context: str, target: str, cdp_session: str, now: float):
        self.id, self.cdp, self.context, self.target, self.cdp_session = sid, cdp, context, target, cdp_session
        self.started = self.last = now
        self.hold_until = 0.0
        self.readonly = False
        self.frame_names: dict[str, str] = {}      # CDP frameId -> "f0", "f1", …
        self.worlds: dict[str, tuple[str, int]] = {}  # CDP frameId -> (loaderId, isolated world context id)

    def name(self, frame_id: str) -> str:
        if frame_id not in self.frame_names:
            self.frame_names[frame_id] = f"f{len(self.frame_names)}"
        return self.frame_names[frame_id]

    def frame_id(self, name: str) -> str | None:
        return next((k for k, v in self.frame_names.items() if v == name), None)


class Controller:
    def __init__(self, connect: Callable[[], Cdp] | None = None, clock: Callable[[], float] = time.time,
                 mem_mb: Callable[[], int] = mem_available_mb, check_url: Callable[[str], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, engine_uid: int | None = None):
        self._connect = connect or (lambda: connect_engine(engine_uid))
        self.clock, self.mem_mb, self.sleep = clock, mem_mb, sleep
        self.check_url = check_url or br.check_url
        self.session: Session | None = None

    # --- request entry -------------------------------------------------------------------

    def handle(self, req: Any) -> dict:
        try:
            self.expire()
            if not isinstance(req, dict) or not isinstance(req.get("cmd"), str):
                raise Refused("bad request")
            cmd = req["cmd"]
            fn = {"status": self.status, "open": self.open, "snapshot": self.snapshot_cmd, "click": self.click,
                  "type": self.type_text, "choose": self.choose, "back": self.back, "close": self.close,
                  "bind": self.bind, "fill": self.fill, "pay": self.pay, "hold": self.hold}.get(cmd)
            if fn is None:
                raise Refused("unknown command")
            return fn(req)
        except Refused as e:
            return {"ok": False, "error": str(e)}
        except br.Refused as e:  # check_url's messages name no values
            return {"ok": False, "error": str(e)}
        except CdpError:
            self.drop()
            return {"ok": False, "error": "the checkout browser failed; the session was closed"}
        except Exception:  # never echo request contents
            self.drop()
            return {"ok": False, "error": "internal error in the checkout browser; the session was closed"}

    # --- lifecycle -----------------------------------------------------------------------

    def expire(self) -> None:
        s = self.session
        if s is None:
            return
        now = self.clock()
        if now - s.started > HARD_MAX or now > max(s.last + IDLE, s.hold_until):
            self.drop()

    def drop(self) -> None:
        s, self.session = self.session, None
        if s is None:
            return
        try:
            s.cdp.call("Target.disposeBrowserContext", {"browserContextId": s.context}, timeout=5)
        except Exception:
            pass
        try:
            s.cdp.close()
        except Exception:
            pass

    def _session(self, req: dict, writable: bool = True) -> Session:
        s = self.session
        if s is None or not isinstance(req.get("session"), str) or req["session"] != s.id:
            raise Refused("no such session")
        if writable and s.readonly:
            raise Refused("this session is finished (after paying only snapshot and close work)")
        s.last = self.clock()
        return s

    def status(self, req: dict) -> dict:
        s = self.session
        if s is None:
            return {"ok": True, "session": None, "state": "idle"}
        return {"ok": True, "session": s.id, "state": "readonly" if s.readonly else "active",
                "started": s.started, "idle_for": round(self.clock() - s.last)}

    def hold(self, req: dict) -> dict:
        s = self._session(req, writable=False)
        until = req.get("until")
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise Refused("bad hold")
        s.hold_until = min(float(until), s.started + HARD_MAX)
        return {"ok": True, "session": s.id, "until": s.hold_until}

    def close(self, req: dict) -> dict:
        self._session(req, writable=False)
        self.drop()
        return {"ok": True}

    def _new_session(self) -> Session:
        if self.mem_mb() < MIN_MEM_MB:
            raise Refused("the server is short of memory right now; try again later")
        cdp = self._connect()
        try:
            ctx = cdp.call("Target.createBrowserContext", {"disposeOnDetach": True}).get("browserContextId")
            target = cdp.call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx}).get("targetId")
            sess = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True}).get("sessionId")
            if not (ctx and target and sess):
                raise CdpError("no target")
            for m in ("Page.enable", "Runtime.enable"):
                cdp.call(m, {}, sess)
        except Exception:
            cdp.close()
            raise
        s = Session(secrets.token_hex(8), cdp, ctx, target, sess, self.clock())
        self.session = s
        return s

    # --- navigation ----------------------------------------------------------------------

    def open(self, req: dict) -> dict:
        url = req.get("url")
        if not isinstance(url, str):
            raise Refused("give a link (https://…)")
        url = self.check_url(url)
        if req.get("session") is not None:
            s = self._session(req)
        elif self.session is not None:
            raise Refused("another checkout session is open; close it first")
        else:
            s = self._new_session()
        s.cdp.call("Page.navigate", {"url": url}, s.cdp_session)
        self._settle(s, NAV_TIMEOUT)
        return self._reply(s)

    def back(self, req: dict) -> dict:
        s = self._session(req)
        main = self._frames(s)[0]
        self._run(s, main, {"op": "back"})
        self._settle(s, NAV_TIMEOUT)
        return self._reply(s)

    def _settle(self, s: Session, limit: float) -> None:
        """Waits for the main frame to finish loading, then checks where it ended up."""
        deadline = self.clock() + limit
        self.sleep(0.5)
        while True:
            main = self._frames(s)[0]
            state = self._run(s, main, {"op": "state"}, quiet=True) or {}
            if state.get("ready") == "complete" or self.clock() >= deadline:
                break
            self.sleep(0.5)
        self.sleep(0.5)
        self._check_top(s)

    def _check_top(self, s: Session) -> None:
        top = self._frames(s)[0]
        if top["url"].startswith("about:blank"):
            return
        try:
            self.check_url(top["url"])
        except br.Refused:
            self.drop()
            raise Refused("the page went somewhere the checkout browser doesn't open; the session was closed") from None

    # --- frames, worlds, page calls ------------------------------------------------------

    def _frames(self, s: Session) -> list[dict]:
        tree = s.cdp.call("Page.getFrameTree", {}, s.cdp_session).get("frameTree") or {}
        out: list[dict] = []

        def walk(node: dict, parent: str | None) -> None:
            f = node.get("frame") or {}
            if f.get("id") and len(out) < MAX_FRAMES:
                url = str(f.get("url") or "")
                out.append({"frame_id": f["id"], "name": s.name(f["id"]), "url": url, "loader": f.get("loaderId", ""),
                            "origin": origin_of(url, f.get("securityOrigin")), "parent": parent})
            for c in node.get("childFrames") or []:
                walk(c, f.get("id"))

        walk(tree, None)
        if not out:
            raise CdpError("no frames")
        return out

    def _world(self, s: Session, frame: dict) -> int:
        cached = s.worlds.get(frame["frame_id"])
        if cached and cached[0] == frame["loader"]:
            return cached[1]
        ctx = s.cdp.call("Page.createIsolatedWorld", {"frameId": frame["frame_id"], "worldName": "ampi"},
                         s.cdp_session).get("executionContextId")
        if not isinstance(ctx, int):
            raise CdpError("no world")
        s.worlds[frame["frame_id"]] = (frame["loader"], ctx)
        return ctx

    def _run(self, s: Session, frame: dict, op: dict, quiet: bool = False) -> dict | None:
        try:
            ctx = self._world(s, frame)
            r = s.cdp.call("Runtime.evaluate", {"expression": page_call(op), "contextId": ctx, "returnByValue": True},
                           s.cdp_session)
            value = (r.get("result") or {}).get("value")
            data = json.loads(value) if isinstance(value, str) else None
            return data if isinstance(data, dict) else None
        except CdpError:
            if quiet:
                return None
            raise
        except ValueError:
            return None

    # --- snapshot ------------------------------------------------------------------------

    def snapshot(self, s: Session) -> dict:
        frames = self._frames(s)
        top = frames[0]
        snap: dict[str, Any] = {"url": top["url"][:500], "origin": top["origin"], "title": "", "text": "",
                                "frames": [], "elements": [], "readonly": s.readonly}
        frame_urls = []
        texts = []
        for f in frames:
            snap["frames"].append({"id": f["name"], "origin": f["origin"], "url": f["url"][:200]})
            frame_urls.append(f["url"])
            if f["origin"] == "opaque" and f is not top:
                continue  # never actionable
            data = self._run(s, f, {"op": "enumerate", "main": f is top}, quiet=f is not top)
            if not data:
                continue
            if f is top:
                snap["title"] = br.clean(str(data.get("title") or ""))[:300]
                snap["text"] = br.MARKERS.sub("", br.clean(str(data.get("text") or "")))[:MAX_TEXT]
            else:
                texts.append(str(data.get("text") or ""))
            if f["origin"] == "opaque":
                continue
            for e in (data.get("elements") or [])[:MAX_ELEMENTS]:
                if not isinstance(e, dict) or not isinstance(e.get("n"), int) or e.get("kind") not in (
                        "input", "textarea", "select", "button", "link", "checkbox", "radio"):
                    continue
                item = {"ref": f"{f['name']}:{e['n']}", "frame": f["name"], "frame_origin": f["origin"],
                        "kind": e["kind"]}
                for key, limit in (("type", 30), ("autocomplete", 60), ("name", 80), ("id", 80), ("label", 120)):
                    item[key] = br.MARKERS.sub("", br.clean(str(e.get(key) or "")))[:limit]
                item["required"] = bool(e.get("required"))
                item["checked"] = e.get("checked") if isinstance(e.get("checked"), bool) else None
                item["filled"] = bool(e.get("filled"))
                if e["kind"] == "select":
                    item["options"] = [br.MARKERS.sub("", br.clean(str(o)))[:60] for o in (e.get("options") or [])[:30]]
                    item["selected"] = br.clean(str(e.get("selected") or ""))[:60] or None
                snap["elements"].append(item)
        snap["totals"] = extract_totals(snap["text"])
        snap["risky_checked"] = [el["label"] for el in snap["elements"]
                                 if el["kind"] == "checkbox" and el["checked"] and RISKY.search(el["label"] or "")]
        snap["captcha"] = any(CAPTCHA_FRAME.search(u) for u in frame_urls) or br.looks_like_bot_check(
            snap["text"], snap["title"])
        snap["threeds"] = any(THREEDS_FRAME.search(urllib.parse.urlsplit(u).netloc + urllib.parse.urlsplit(u).path)
                              for u in frame_urls[1:]) or bool(THREEDS_TEXT.search(snap["text"] + " ".join(texts)))
        return snap

    def _reply(self, s: Session) -> dict:
        return {"ok": True, "session": s.id, "snapshot": self.snapshot(s)}

    def snapshot_cmd(self, req: dict) -> dict:
        return self._reply(self._session(req, writable=False))

    # --- elements --------------------------------------------------------------------------

    def _element(self, s: Session, ref: Any, frames: list[dict] | None = None) -> tuple[dict, dict, int]:
        """(frame, current description, number) for a ref like 'f1:12'; refuses anything not actionable."""
        if not isinstance(ref, str) or not re.fullmatch(r"f\d{1,2}:\d{1,6}", ref):
            raise Refused("unknown element")
        name, num = ref.split(":")
        fid = s.frame_id(name)
        frames = frames or self._frames(s)
        frame = next((f for f in frames if f["frame_id"] == fid), None)
        if frame is None or frame["origin"] == "opaque":
            raise Refused("that element is gone (the page changed); take a new snapshot")
        cached = s.worlds.get(frame["frame_id"])
        if not cached or cached[0] != frame["loader"]:
            raise Refused("that element is gone (the page changed); take a new snapshot")
        d = self._run(s, frame, {"op": "inspect", "n": int(num)}) or {}
        if not d.get("connected"):
            raise Refused("that element is gone (the page changed); take a new snapshot")
        return frame, d, int(num)

    def _after_action(self, s: Session) -> dict:
        self._settle(s, 8)
        return self._reply(s)

    def click(self, req: dict) -> dict:
        s = self._session(req)
        frame, d, n = self._element(s, req.get("ref"))
        if d.get("kind") not in ("button", "link", "checkbox", "radio", "input"):
            raise Refused("that element can't be clicked")
        r = self._run(s, frame, {"op": "click", "n": n}) or {}
        if not r.get("ok"):
            raise Refused("that element is gone (the page changed); take a new snapshot")
        return self._after_action(s)

    def type_text(self, req: dict) -> dict:
        s = self._session(req)
        text = req.get("text")
        if not isinstance(text, str) or len(text) > 500 or any(ord(c) < 32 and c not in "\n" for c in text):
            raise Refused("text must be up to 500 ordinary characters")
        if has_card_number(text):
            raise Refused("that looks like a card number; card details are filled only with the owner's approval")
        frame, d, n = self._element(s, req.get("ref"))
        if d.get("kind") not in ("input", "textarea") or (d.get("kind") == "input" and d.get("type") not in TEXT_TYPES):
            raise Refused("that element isn't a text field")
        if any(SECRET_FIELD.search(str(d.get(k) or "")) for k in ("type", "autocomplete", "name", "id")):
            raise Refused("that field is for card, password or code details; those are only filled with approval")
        r = self._run(s, frame, {"op": "set", "n": n, "value": text}) or {}
        if not r.get("ok"):
            raise Refused("that element is gone (the page changed); take a new snapshot")
        return self._after_action(s)

    def choose(self, req: dict) -> dict:
        s = self._session(req)
        option = req.get("option")
        if not isinstance(option, str) or not 0 < len(option) <= 200:
            raise Refused("give the option's text")
        frame, d, n = self._element(s, req.get("ref"))
        if d.get("kind") != "select":
            raise Refused("that element isn't a list to choose from")
        r = self._run(s, frame, {"op": "choose", "n": n, "option": option}) or {}
        if not r.get("ok"):
            raise Refused("no such option in that list" if r.get("why") == "option" else
                          "that element is gone (the page changed); take a new snapshot")
        return self._after_action(s)

    # --- approvals: bind, fill, pay ------------------------------------------------------

    BOUND = ("tag", "type", "autocomplete", "name", "id")

    def bind(self, req: dict) -> dict:
        s = self._session(req)
        refs = req.get("refs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 12:
            raise Refused("give 1-12 elements")
        frames = self._frames(s)
        out = []
        for ref in refs:
            frame, d, _ = self._element(s, ref, frames)
            item = {"ref": ref, "frame": frame["name"], "frame_id": frame["frame_id"], "frame_origin": frame["origin"],
                    "main": frame is frames[0], "kind": d.get("kind")}
            for k in self.BOUND + ("label",):
                item[k] = str(d.get(k) or "")[:120]
            out.append(item)
        snap = self.snapshot(s)
        return {"ok": True, "session": s.id, "origin": snap["origin"], "bindings": out, "totals": snap["totals"],
                "risky_checked": snap["risky_checked"]}

    def _verify(self, s: Session, req: dict, fields: Any, allow_card: bool) -> tuple[list[dict], list[tuple]]:
        """All-or-nothing: every field must still be exactly the element that was approved."""
        changed = Refused("the page changed since the approval; nothing was filled")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 12:
            raise Refused("bad fields")
        frames = self._frames(s)
        if frames[0]["origin"] != req.get("origin") or frames[0]["origin"] == "opaque":
            raise changed
        psp = {o for o in req.get("psp_origins") or [] if isinstance(o, str)}
        plan = []
        for f in fields:
            if not isinstance(f, dict) or not isinstance(f.get("binding"), dict) or not isinstance(f.get("value"), str):
                raise Refused("bad fields")
            b = f["binding"]
            if f.get("ref") != b.get("ref"):
                raise changed
            try:
                frame, d, n = self._element(s, b.get("ref"), frames)
            except Refused:
                raise changed from None
            if frame["frame_id"] != b.get("frame_id") or frame["origin"] != b.get("frame_origin"):
                raise changed
            if any(str(d.get(k) or "")[:120] != str(b.get(k) or "") for k in self.BOUND):
                raise changed
            if d.get("kind") not in ("input", "textarea"):
                raise changed
            if f.get("card"):
                if not allow_card:
                    raise Refused("card details are only filled as part of a payment")
                in_psp_frame = frame is not frames[0] and frame["origin"] in psp
                hosted = frame is frames[0] and frames[0]["origin"] in psp
                if not (in_psp_frame or hosted):
                    raise Refused("card details go only into a payment provider's field; this one isn't")
            plan.append((frame, n, f["value"]))
        return frames, plan

    def fill(self, req: dict) -> dict:
        s = self._session(req)
        _, plan = self._verify(s, req, req.get("fields"), allow_card=False)
        self._set_all(s, plan)
        return self._after_action(s)

    def _set_all(self, s: Session, plan: list[tuple]) -> None:
        for frame, n, value in plan:
            r = self._run(s, frame, {"op": "set", "n": n, "value": value}) or {}
            if not r.get("ok"):
                raise Refused("a field disappeared while filling; check the page before trying again")

    def pay(self, req: dict) -> dict:
        s = self._session(req)
        frames, plan = self._verify(s, req, req.get("fields"), allow_card=True)
        button = req.get("button")
        if not isinstance(button, dict) or not isinstance(button.get("binding"), dict):
            raise Refused("bad pay button")
        b = button["binding"]
        try:
            bframe, bd, bn = self._element(s, b.get("ref"), frames)
        except Refused:
            raise Refused("the page changed since the approval; nothing was filled") from None
        if (button.get("ref") != b.get("ref") or bframe["frame_id"] != b.get("frame_id")
                or bframe["origin"] != b.get("frame_origin") or bd.get("kind") != "button"
                or any(str(bd.get(k) or "")[:120] != str(b.get(k) or "") for k in self.BOUND)):
            raise Refused("the page changed since the approval; nothing was filled")
        snap = self.snapshot(s)
        if totals_key(snap["totals"]) != totals_key(req.get("expect_totals") or []):
            raise Refused("the totals on the page changed since the approval; nothing was filled or paid")
        if snap["risky_checked"]:
            raise Refused("a subscription / save-card / Link box is ticked; nothing was filled or paid")
        self._set_all(s, plan)
        r = self._run(s, bframe, {"op": "click", "n": bn}) or {}
        s.readonly = True  # from here on: look, don't touch (3-D Secure belongs to the owner)
        if not r.get("ok"):
            raise Refused("the pay button disappeared after filling; check the page (it may not have been sent)")
        start = self.clock()
        before = frames[0]["url"]
        self.sleep(2)
        while self.clock() - start < PAY_SETTLE:
            main = self._frames(s)[0]
            st = self._run(s, main, {"op": "state"}, quiet=True) or {}
            if st.get("ready") == "complete" and main["url"] != before:
                break
            self.sleep(1)
        self._check_top(s)
        return self._reply(s)


# --- the socket server -------------------------------------------------------------------------


def peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def read_line(conn: socket.socket) -> bytes:
    conn.settimeout(10)
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_REQUEST:
            raise Refused("request too big")
    return buf.split(b"\n", 1)[0]


def serve_one(controller: Controller, conn: socket.socket, allowed_uids: set[int],
              uid_of: Callable[[socket.socket], int] = peer_uid) -> None:
    try:
        try:
            line = read_line(conn)  # read first (bounded), so the peer always gets an answer, not a reset
        except (Refused, OSError):
            line = b""
        if uid_of(conn) not in allowed_uids:
            resp = {"ok": False, "error": "not allowed"}
        else:
            try:
                req = json.loads(line or b"null")
            except ValueError:
                req = None
            resp = controller.handle(req)
        conn.settimeout(30)
        conn.sendall(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
    except OSError:
        pass
    finally:
        conn.close()


def listening_socket() -> socket.socket:
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        return socket.socket(fileno=3)  # from ampi-checkout.socket: root owns the path, we can't re-bind it
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    s.listen(4)
    return s


def _uids(var: str) -> set[int]:
    return {int(x) for x in os.environ.get(var, "").split(",") if x.strip().isdigit()}


def main() -> int:
    if sys.argv[1:] != ["serve"]:
        print("usage: checkout.py serve", file=sys.stderr)
        return 2
    allowed = _uids("AMPI_CHECKOUT_CLIENT_UIDS")
    engine = _uids("AMPI_CHECKOUT_ENGINE_UID")
    if not allowed or len(engine) != 1:
        print("AMPI_CHECKOUT_CLIENT_UIDS and AMPI_CHECKOUT_ENGINE_UID must be set", file=sys.stderr)
        return 2
    controller = Controller(engine_uid=engine.pop())
    srv = listening_socket()
    print("ampi-checkout controller ready", file=sys.stderr)
    while True:
        ready, _, _ = select.select([srv], [], [], 5)
        controller.expire()
        if ready:
            conn, _ = srv.accept()
            serve_one(controller, conn, allowed)


if __name__ == "__main__":
    sys.exit(main())
