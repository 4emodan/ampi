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

`submit` presses a button that commits to something (book, send, reserve) after the owner approved the site, the
button and everything filled in. Only for that, `bind_submit` reads the page's field values (never of password,
card, code or hidden fields), for the owner's approval message; any change of them before the press refuses it.

No arbitrary JavaScript from the assistant, no other reading of input values, no console/network events forwarded,
no downloads, no file:// pages, no stealth and no captcha solving.
"""

from __future__ import annotations

import base64
import hashlib
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
PAY_FILL_SETTLE = 1.5  # seconds between filling the card and pressing Pay
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
CURRENCY_CODES = {"EUR", "USD", "GBP", "SEK", "NOK", "DKK", "PLN", "CHF", "CZK", "HUF", "ISK", "CAD", "AUD", "NZD",
                  "JPY"}
# "kr" stays "KR" (Swedish, Norwegian, Danish, Icelandic krona all print as kr): the daemon maps it to the card's.
SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "kr": "KR", "zł": "PLN", "CA$": "CAD", "C$": "CAD", "A$": "AUD",
           "AU$": "AUD", "US$": "USD", "NZ$": "NZD"}
_CUR = r"(?:CA|AU|US|NZ|A|C)\$|€|\$|£"
CONFIRMATION = re.compile(r"order|confirm|thank|kiitos|tilaus|vahvist", re.I)
COUNTDOWN = re.compile(r"\b(?:held|hold|left|remaining|expires?( in)?|reserved for|time)\b[^\d\n]{0,15}\d{1,2}:\d{2}(:\d{2})?|"
                       r"\d{1,2}:\d{2}(:\d{2})?\s*(?:min(utes)?\s*)?(?:left|remaining)\b", re.I)
REF_RE = re.compile(r"f\d{1,3}:\d{1,6}")
AMOUNT = re.compile(
    rf"(?P<pre>{_CUR}|\b(?:EUR|USD|GBP|SEK|NOK|DKK|PLN|CHF|CZK|HUF|ISK|CAD|AUD|NZD|JPY|kr)\b)?\s?"
    r"(?P<num>\d{1,3}(?:[  .,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    rf"\s?(?P<post>{_CUR}|\b(?:EUR|USD|GBP|SEK|NOK|DKK|PLN|CHF|CZK|HUF|ISK|CAD|AUD|NZD|JPY|kr|zł)\b)?")
CAPTCHA_FRAME = re.compile(r"hcaptcha\.com/captcha/.*frame=challenge|recaptcha/(api2|enterprise)/bframe|"
                           r"challenges\.cloudflare\.com|captcha-delivery\.com|arkoselabs\.com|funcaptcha", re.I)
THREEDS_FRAME = re.compile(r"3ds|threeds|three-d|/acs\b|acs\.|/challenge|authenticat", re.I)
THREEDS_TEXT = re.compile(r"3-?d ?secure|verified by visa|mastercard (id check|securecode)|verify (your )?(payment|"
                          r"purchase|identity)|authenticat(e|ion) (your )?(payment|purchase)|vahvista (maksu|osto)",
                          re.I)


class Refused(Exception):
    """A request we won't run; the message (never containing request values) goes back to the daemon."""


class CdpError(Exception):
    """The connection to the engine broke; the session is closed."""


class CdpProtocolError(Exception):
    """The engine answered a command with an error (e.g. "Cannot find context" while a page navigates). Routine
    during redirects and frame remounts: the session stays; the caller retries or asks for a new snapshot."""


PAGE_CHANGED = "the page changed; take a new snapshot"


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
                        raise CdpProtocolError(f"{method} failed")
                    return r.get("result") or {}
        except (OSError, ValueError) as e:
            raise CdpError("engine connection failed") from e

    def close(self) -> None:
        self.ws.close()


ENGINE_WAIT = 15  # seconds for the engine's listener to appear after (re)start


def connect_engine(engine_uid: int | None, proc_paths: tuple[str, ...] = ("/proc/net/tcp",),
                   wait: float = ENGINE_WAIT, sleep: Callable[[float], None] = time.sleep) -> Cdp:
    """Only to the engine's own listener: refuse at once if anything else owns the port (a squatter); while
    nothing listens yet (the engine is starting with the controller), wait a little."""
    if engine_uid is None:
        raise Refused("the checkout browser isn't set up (no engine user)")
    waited = 0.0
    while True:
        owner = listener_uid(CDP_PORT, proc_paths)
        if owner == engine_uid:
            break
        if owner is not None or waited >= wait:
            raise Refused("the checkout browser isn't running right now; try again in a minute")
        sleep(0.5)
        waited += 0.5
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
    const shown = (n) => {
      if (n.hidden) return false;
      try { const st = getComputedStyle(n); if (st && (st.display === 'none' || st.visibility === 'hidden')) return false; } catch (e) {}
      return true;
    };
    const visible = (el) => {  // the element and its ancestors (a hidden mega-menu hides its links)
      if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') return false;
      let n = el, depth = 0;
      while (n && n.nodeType === 1 && depth < 40) { if (!shown(n)) return false; n = n.parentElement; depth++; }
      return true;
    };
    const disabledOf = (el) => !!el.disabled || el.getAttribute('aria-disabled') === 'true';
    const kindOf = (el) => {
      const t = el.tagName, ty = (el.type || '').toLowerCase(), role = el.getAttribute('role');
      if (t === 'INPUT') {
        if (ty === 'checkbox') return 'checkbox';
        if (ty === 'radio') return 'radio';
        if (['submit', 'button', 'image', 'reset'].includes(ty)) return 'button';
        return 'input';
      }
      if (t === 'TEXTAREA') return 'textarea';
      if (t === 'SELECT') return 'select';
      if (role === 'switch' || role === 'checkbox') return 'checkbox';
      if (['option', 'gridcell', 'tab', 'menuitem', 'radio'].includes(role) && t !== 'A') return 'button';
      if (t === 'BUTTON' || role === 'button') return 'button';
      if (t === 'A' && el.hasAttribute('href')) return 'link';
      return null;
    };
    const isChecked = (el) => el.tagName === 'INPUT' ? !!el.checked : el.getAttribute('aria-checked') === 'true';
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
        id: txt(el.id, 80), label: labelOf(el, kind), required: !!el.required, checked: null, filled: false,
        disabled: disabledOf(el),
        role: txt(el.getAttribute('role'), 30) };
      for (const k of ['aria-selected', 'aria-pressed', 'aria-checked'])
        if (kind === 'button' && el.hasAttribute(k)) d.checked = el.getAttribute(k) === 'true' || !!d.checked;
      if (kind === 'checkbox' || kind === 'radio') d.checked = isChecked(el);
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
        // Form controls first, links after: on big shops the cap must not cut off the inputs and buttons.
        for (const sel of ['input,textarea,select,button,[role=button],[role=switch],[role=checkbox],[role=option],[role=gridcell],[role=tab],[role=menuitem],[role=radio]', 'a[href]']) {
          for (const el of document.querySelectorAll(sel)) {
            if (list.length >= 400) break;
            if (kindOf(el) && visible(el)) list.push(describe(el));
          }
        }
        // Every checked box, shown or not (a hidden one is still sent with the form), for the risky check.
        const checked = [];
        for (const el of document.querySelectorAll('input[type=checkbox],[role=switch],[role=checkbox]')) {
          if (checked.length >= 100) break;
          if (isChecked(el)) checked.push(labelOf(el, 'checkbox'));
        }
        const out = { href: String(location.href), ready: document.readyState, elements: list, checked: checked };
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
      if (a.op === 'form') {  // what would be sent (bind_submit/submit). Secret values never leave the page.
        const out = [], hidden = []; let problem = '';
        const sel = 'input,textarea,select,[contenteditable=""],[contenteditable=true],[role=switch],[role=checkbox],[role=radio],' +
          '[role=option],[role=gridcell],[role=tab],[aria-selected=true],[aria-pressed=true],[aria-checked=true]';
        for (const n of document.querySelectorAll('*'))
          if (n.shadowRoot && n.shadowRoot.querySelector('input,textarea,select')) problem = 'a form inside a web component';
        for (const el of document.querySelectorAll(sel)) {
          const ty = (el.type || '').toLowerCase(), tag = el.tagName;
          if (tag === 'INPUT' && ['submit', 'button', 'image', 'reset'].includes(ty)) continue;
          if (tag === 'INPUT' && ty === 'file') { if (el.files && el.files.length) problem = 'a file to upload'; continue; }
          const name = txt(el.getAttribute('name'), 80);
          if (tag === 'INPUT' && ty === 'hidden') { hidden.push([name, String(el.value || '').slice(0, 20000)]); continue; }
          const kind = kindOf(el) || 'text', ac = (el.getAttribute('autocomplete') || '').toLowerCase();
          const secret = ty === 'password' || /(^| )(cc-|one-time-code|current-password|new-password)/.test(ac) ||
            /cvc|cvv|csc|iban|passw|\botp\b/i.test(name + ' ' + el.id);
          let v = null;
          if (tag === 'SELECT') v = Array.from(el.options).filter((o, i) => o.selected || (!el.multiple && i === el.selectedIndex)).map(o => txt(o.text, 200) + (txt(o.text, 200) === txt(o.value, 200) ? '' : ' (' + txt(o.value, 200) + ')')).join('; ');
          else if (tag === 'INPUT' && (ty === 'checkbox' || ty === 'radio')) { if (el.checked) v = true; }
          else if (tag === 'INPUT' || tag === 'TEXTAREA') v = String(el.value || '');
          else if (['aria-selected', 'aria-pressed', 'aria-checked'].some(k => el.getAttribute(k) === 'true')) v = true;
          else if (el.hasAttribute('contenteditable')) v = String(el.textContent || '').trim();
          if (v === null || v === '') continue;
          if (secret) v = '(secret)';
          if (v !== true && v.length > 1000) problem = 'a very long text';
          if (out.length >= 60) { problem = 'too many filled fields'; break; }
          out.push({ label: labelOf(el, v === true ? 'button' : kind), kind: kind, value: v, shown: visible(el), name: name, type: ty });
        }
        return { fields: out, hidden: hidden, problem: problem };
      }
      if (a.op === 'gate') {  // what a press on this element would really press
        const el = get(a.n); if (!el) return { connected: false };
        const lab = el.closest('label');
        const c = el.closest('button,input,a[href],[role=button]') || (lab && lab.control) || el;
        const texts = [];
        for (const n of (c === el ? [el] : [el, c])) {
          for (const s of [n.getAttribute('aria-label'), n.tagName === 'INPUT' ? (['submit', 'button', 'image', 'reset'].includes((n.type || '').toLowerCase()) ? n.value : '') : n.textContent, n.getAttribute('title'), n.getAttribute('alt')]) {
            const t = txt(s, 200); if (t && !texts.includes(t)) texts.push(t);
          }
          n.querySelectorAll && n.querySelectorAll('img[alt]').forEach(i => { const t = txt(i.getAttribute('alt'), 100); if (t && texts.length < 12 && !texts.includes(t)) texts.push(t); });
        }
        const owner = c.form instanceof HTMLFormElement ? c.form : (c.closest ? c.closest('form') : null);
        const cty = c.tagName === 'BUTTON' ? (c.getAttribute('type') || 'submit').toLowerCase() : (c.type || '').toLowerCase();
        const presses = (c.tagName === 'BUTTON' || c.tagName === 'INPUT') && ['submit', 'image'].includes(cty);
        const submits = !!owner && presses;
        let search = false;
        if (owner) {
          const fs = Array.from(owner.querySelectorAll('input,textarea,select')).filter(i => !['hidden', 'submit', 'button', 'image', 'reset'].includes((i.type || '').toLowerCase()));
          search = fs.length <= 1 && ((owner.getAttribute('role') || '') === 'search' || !!owner.closest('[role=search]') || (fs.length === 1 && (fs[0].type === 'search' || /^(q|s|query|search|keyword)$/i.test(fs[0].getAttribute('name') || ''))));
        }
        const resolve = (u) => { try { return String(new URL(u || '', document.baseURI || location.href)); } catch (e) { return 'unreadable'; } };
        let dest = '', method = '';
        if (c.tagName === 'A') { dest = resolve(c.getAttribute('href')); method = 'link'; }
        else if (owner) { dest = resolve(c.getAttribute('formaction') || owner.getAttribute('action'));
          method = (c.getAttribute('formmethod') || owner.getAttribute('method') || 'get').toLowerCase(); }
        let box = owner || c.closest('[role=dialog],dialog,section,article,main') || c.parentElement;
        return { connected: true, tag: c.tagName.toLowerCase(), texts: texts.slice(0, 12), ident: txt((c.getAttribute('name') || '') + ' ' + c.id, 120),
          submits: submits, search: search, dest: dest.slice(0, 1000), method: method, href: String(location.href),
          around: txt(box ? (box.innerText || box.textContent) : '', 600) };
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
        self.gen = 0         # counts everything done in the session: an approved press needs it unchanged
        self.filled = False  # the owner's details were filled in
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
        # True once a session paid or lost its engine connection: when no session is left, the controller exits,
        # systemd stops the engine with it (BindsTo=), and the next request starts both fresh (no card details
        # left in a long-lived engine process).
        self.retire = False
        # Where a fill/pay got to, reported with every error: "verify" (nothing written: a definite refusal),
        # "filled" (values written, Pay not pressed), "sent" (Pay was pressed: outcome unknown).
        self._phase: str | None = None

    CHANGES = ("open", "click", "type", "choose", "back", "fill", "pay")

    # --- request entry -------------------------------------------------------------------

    def _error(self, text: str) -> dict:
        out: dict[str, Any] = {"ok": False, "error": text}
        if self._phase:
            out["phase"] = self._phase
        return out

    def handle(self, req: Any) -> dict:
        self._phase = None
        try:
            self.expire()
            if not isinstance(req, dict) or not isinstance(req.get("cmd"), str):
                raise Refused("bad request")
            cmd = req["cmd"]
            fn = {"status": self.status, "open": self.open, "snapshot": self.snapshot_cmd, "click": self.click,
                  "type": self.type_text, "choose": self.choose, "back": self.back, "close": self.close,
                  "bind": self.bind, "gate": self.gate, "bind_submit": self.bind_submit, "submit": self.submit, "fill": self.fill, "pay": self.pay, "hold": self.hold}.get(cmd)
            if fn is None:
                raise Refused("unknown command")
            if cmd in self.CHANGES and self.session is not None:
                self.session.gen += 1
            return fn(req)
        except Refused as e:
            return self._error(str(e))
        except br.Refused as e:  # check_url's messages name no values
            return self._error(str(e))
        except CdpProtocolError:  # the page was changing under us; the session stays
            return self._error(PAGE_CHANGED)
        except CdpError:
            self.retire = True
            self.drop()
            return self._error("the checkout browser failed; the session was closed")
        except Exception:  # never echo request contents
            self.retire = True
            self.drop()
            return self._error("internal error in the checkout browser; the session was closed")

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
        if s.readonly:
            self.retire = True  # it paid: start over with a fresh engine
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

    ELEMENT_OPS = ("inspect", "gate", "set", "click", "choose")

    def _eval(self, s: Session, frame: dict, op: dict) -> dict | None:
        ctx = self._world(s, frame)
        r = s.cdp.call("Runtime.evaluate", {"expression": page_call(op), "contextId": ctx, "returnByValue": True},
                       s.cdp_session)
        value = (r.get("result") or {}).get("value")
        try:
            data = json.loads(value) if isinstance(value, str) else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _run(self, s: Session, frame: dict, op: dict, quiet: bool = False) -> dict | None:
        """One page op in the frame's isolated world. An engine error (the context went away while the page
        navigated or a frame remounted) is retried once in a fresh world, but an element op only if the frame
        still holds the same document (its elements are gone otherwise). Connection loss raises CdpError."""
        try:
            return self._eval(s, frame, op)
        except CdpProtocolError:
            s.worlds.pop(frame["frame_id"], None)
        try:
            fresh = next((f for f in self._frames(s) if f["frame_id"] == frame["frame_id"]), None)
            if fresh is None or (op.get("op") in self.ELEMENT_OPS and fresh["loader"] != frame["loader"]):
                raise CdpProtocolError("document changed")
            frame.update(loader=fresh["loader"], url=fresh["url"], origin=fresh["origin"])
            return self._eval(s, frame, op)
        except CdpProtocolError:
            s.worlds.pop(frame["frame_id"], None)
            if quiet:
                return None
            raise Refused(PAGE_CHANGED) from None

    # --- snapshot ------------------------------------------------------------------------

    def snapshot(self, s: Session) -> dict:
        frames = self._frames(s)
        top = frames[0]
        snap: dict[str, Any] = {"url": top["url"][:500], "origin": top["origin"], "title": "", "text": "",
                                "frames": [], "elements": [], "readonly": s.readonly}
        frame_urls = []
        texts = []
        checked_labels: list[str] = []
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
            checked_labels += [br.MARKERS.sub("", br.clean(str(x)))[:120] for x in (data.get("checked") or [])[:100]
                               if isinstance(x, str)]
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
                item["disabled"] = bool(e.get("disabled"))
                if e["kind"] == "select":
                    item["options"] = [br.MARKERS.sub("", br.clean(str(o)))[:60] for o in (e.get("options") or [])[:30]]
                    item["selected"] = br.clean(str(e.get("selected") or ""))[:60] or None
                snap["elements"].append(item)
        snap["totals"] = extract_totals(snap["text"])
        # Every checked box counts, also hidden ones and role=switch toggles (they're sent with the form too).
        labels = [el["label"] for el in snap["elements"] if el["kind"] == "checkbox" and el["checked"]] + checked_labels
        snap["risky_checked"] = list(dict.fromkeys(x for x in labels if RISKY.search(x or "")))
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
        if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
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
        if d.get("disabled"):
            raise Refused("that element is disabled right now (the page may want something filled first)")
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
        if d.get("disabled"):
            raise Refused("that field is disabled right now")
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
                    "main": frame is frames[0], "kind": d.get("kind"), "disabled": bool(d.get("disabled"))}
            for k in self.BOUND + ("label",):
                item[k] = str(d.get(k) or "")[:120]
            item["role"] = str(d.get("role") or "")[:30]
            out.append(item)
        snap = self.snapshot(s)
        return {"ok": True, "session": s.id, "origin": snap["origin"], "bindings": out, "totals": snap["totals"],
                "risky_checked": snap["risky_checked"]}

    # --- submit: a button that commits to something, after approval ---------------------------

    def _form(self, s: Session, frames: list[dict], bframe: dict) -> tuple[list[dict], str]:
        """(what is filled in on the page and in the button's frame, a fingerprint that also covers hidden fields).
        Anything that can't be read or shown in full is refused, never left out."""
        fields: list[dict] = []
        raw = []
        for f in ([frames[0]] if bframe["frame_id"] == frames[0]["frame_id"] else [frames[0], bframe]):
            data = self._run(s, f, {"op": "form"})
            if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("hidden"), list):
                raise Refused("the form on this page can't be read; the owner has to do this")
            if data.get("problem"):
                raise Refused(f"the page has {str(data['problem'])[:60]}, which can't be shown to the owner; they have to do this")
            raw.append([f["frame_id"], data["fields"], data["hidden"]])
            for e in data["fields"]:
                value = e.get("value")
                shown = True if value is True else br.MARKERS.sub("", br.clean(str(value or "")))
                if isinstance(shown, str) and has_card_number(shown):
                    shown = "(secret)"
                fields.append({"label": br.MARKERS.sub("", br.clean(str(e.get("label") or "")))[:120],
                               "kind": str(e.get("kind") or "")[:20], "value": shown, "shown": bool(e.get("shown"))})
        return fields, hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()

    def _gate(self, s: Session, frame: dict, n: int) -> dict:
        g = self._run(s, frame, {"op": "gate", "n": n}) or {}
        if not g.get("connected"):
            raise Refused(PAGE_CHANGED)
        return {"tag": str(g.get("tag") or "")[:20], "texts": [br.MARKERS.sub("", br.clean(str(t)))[:200] for t in (g.get("texts") or [])[:12]],
                "ident": br.clean(str(g.get("ident") or ""))[:120], "submits": bool(g.get("submits")), "search": bool(g.get("search")),
                "dest": br.clean(str(g.get("dest") or ""))[:1000], "method": str(g.get("method") or "")[:10],
                "around": br.MARKERS.sub("", br.clean(str(g.get("around") or "")))[:600]}

    def gate(self, req: dict) -> dict:
        """What pressing an element would really press, so the daemon can decide whether it needs the owner."""
        s = self._session(req)
        frame, d, n = self._element(s, req.get("ref"))
        return {"ok": True, "session": s.id, "kind": d.get("kind"), "frame_origin": frame["origin"], "filled": s.filled,
                **self._gate(s, frame, n)}

    @staticmethod
    def _seen(g: dict, title: str) -> str:
        """A fingerprint of the text the owner is shown next to the button (a running countdown doesn't count)."""
        text = COUNTDOWN.sub(" ", f"{title}\n{g['around']}")
        return hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest()

    @staticmethod
    def _press(g: dict) -> dict:
        return {k: g[k] for k in ("tag", "texts", "dest", "method", "submits")}

    def bind_submit(self, req: dict) -> dict:
        b = self.bind({**req, "refs": [req.get("ref")]})
        s = self._session(req)
        frames = self._frames(s)
        bframe, d, n = self._element(s, req.get("ref"), frames)
        if d.get("kind") not in ("button", "link") or d.get("disabled"):
            raise Refused("give the ref of an enabled button")
        snap = self.snapshot(s)
        g = self._gate(s, bframe, n)
        fields, digest = self._form(s, frames, bframe)
        return {"ok": True, "session": s.id, "origin": b["origin"], "url": frames[0]["url"], "button": b["bindings"][0],
                "totals": snap["totals"], "risky_checked": snap["risky_checked"], "captcha": snap["captcha"],
                "title": snap["title"], "form": fields, "digest": digest, "gen": s.gen, "press": self._press(g),
                "seen": self._seen(g, snap["title"]),
                "around": g["around"], "frames": sorted({f["origin"] for f in frames})}

    def submit(self, req: dict) -> dict:
        s = self._session(req)
        self._phase = "verify"
        changed = Refused("the page changed since the approval; nothing was pressed")
        b = req.get("button")
        if not isinstance(b, dict):
            raise Refused("bad button")
        frames = self._frames(s)
        if frames[0]["origin"] != req.get("origin") or frames[0]["origin"] == "opaque" or frames[0]["url"] != req.get("url"):
            raise changed
        try:
            bframe, d, n = self._element(s, b.get("ref"), frames)
        except Refused:
            raise changed from None
        if (bframe["frame_id"] != b.get("frame_id") or bframe["origin"] != b.get("frame_origin")
                or d.get("kind") not in ("button", "link") or d.get("disabled")
                or any(str(d.get(k) or "")[:120] != str(b.get(k) or "") for k in self.BOUND + ("label",))):
            raise changed
        if req.get("gen") != s.gen:
            raise Refused("something else was done in the browser since the approval; nothing was pressed")
        g = self._gate(s, bframe, n)
        if self._press(g) != req.get("press"):
            raise changed
        snap = self.snapshot(s)
        if self._seen(g, snap["title"]) != req.get("seen"):
            raise changed
        if totals_key(snap["totals"]) != totals_key(req.get("expect_totals") or []) or snap["captcha"] \
                or snap["risky_checked"] or sorted({f["origin"] for f in frames}) != req.get("frames"):
            raise changed
        if self._form(s, frames, bframe)[1] != req.get("digest"):
            raise changed
        # Whatever happens from here on, the press may have happened: never a definite refusal.
        s.gen += 1  # an approval is good for one press
        self._phase = "sent"
        try:
            r = self._run(s, bframe, {"op": "click", "n": n}) or {}
            if not r.get("ok"):
                return self._error("the button vanished while being pressed; it MAY have gone through")
            return {**self._after_action(s), "phase": "sent"}
        except (Refused, CdpProtocolError):
            return self._error("after pressing, the page changed or went somewhere this browser doesn't open; it MAY "
                               "have gone through")
        except Exception:
            self.retire = True
            self.drop()
            return self._error("the browser failed after pressing; it MAY have gone through")

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
        self._phase = "verify"
        _, plan = self._verify(s, req, req.get("fields"), allow_card=False)
        self._phase = "filled"
        s.filled = True
        self._set_all(s, plan)
        self._phase = None  # done: later problems (settling, the snapshot) are ordinary errors
        return self._after_action(s)

    def _set_all(self, s: Session, plan: list[tuple]) -> None:
        for frame, n, value in plan:
            r = self._run(s, frame, {"op": "set", "n": n, "value": value}) or {}
            if not r.get("ok"):
                raise Refused("a field disappeared while filling; check the page before trying again")

    def _pay_button(self, s: Session, button: Any, frames: list[dict]) -> tuple[dict, int]:
        changed = Refused("the page changed since the approval; nothing was filled")
        if not isinstance(button, dict) or not isinstance(button.get("binding"), dict):
            raise Refused("bad pay button")
        b = button["binding"]
        try:
            bframe, bd, bn = self._element(s, b.get("ref"), frames)
        except Refused:
            raise changed from None
        if (button.get("ref") != b.get("ref") or bframe["frame_id"] != b.get("frame_id")
                or bframe["origin"] != b.get("frame_origin") or bd.get("kind") != "button"
                or any(str(bd.get(k) or "")[:120] != str(b.get(k) or "") for k in self.BOUND)):
            raise changed
        return bframe, bn

    def pay(self, req: dict) -> dict:
        s = self._session(req)
        # 1. verify: nothing has been written; any refusal here is definite.
        self._phase = "verify"
        frames, plan = self._verify(s, req, req.get("fields"), allow_card=True)
        bframe, bn = self._pay_button(s, req.get("button"), frames)
        snap = self.snapshot(s)
        if totals_key(snap["totals"]) != totals_key(req.get("expect_totals") or []):
            raise Refused("the totals on the page changed since the approval; nothing was filled or paid")
        if snap["risky_checked"]:
            raise Refused("a subscription / save-card / Link box is ticked; nothing was filled or paid")
        # 2. filled: from the first value on, this session only looks (no click or type can follow a half fill).
        s.readonly = True
        self._phase = "filled"
        self._set_all(s, plan)
        self.sleep(PAY_FILL_SETTLE)  # let the provider's frame report the card as complete before pressing Pay
        cur = self._run(s, bframe, {"op": "inspect", "n": bn}) or {}
        if not cur.get("connected") or cur.get("disabled"):
            raise Refused("the card was filled but the pay button is gone or still disabled; Pay was NOT pressed. "
                          "Check the page")
        # 3. sent: Pay is pressed. Whatever happens after this, the outcome is unknown, never a definite refusal.
        self._phase = "sent"
        try:
            r = self._run(s, bframe, {"op": "click", "n": bn}) or {}
            if not r.get("ok"):
                return self._error("the pay button vanished while being pressed; the payment MAY have gone through")
            return self._after_pay(s, frames[0]["url"])
        except Refused:
            self.drop()
            return self._error("after pressing Pay the page went somewhere the checkout browser doesn't open (or "
                               "changed); the payment MAY have gone through")
        except Exception:
            self.retire = True
            self.drop()
            return self._error("the checkout browser failed after pressing Pay; the payment MAY have gone through")

    def _after_pay(self, s: Session, before: str) -> dict:
        start = self.clock()
        self.sleep(2)
        while self.clock() - start < PAY_SETTLE:
            main = self._frames(s)[0]
            st = self._run(s, main, {"op": "state"}, quiet=True) or {}
            if st.get("ready") == "complete" and main["url"] != before:
                break
            self.sleep(1)
        self._check_top(s)
        snap = self.snapshot(s)
        m = CONFIRMATION.search(snap["text"])
        snap["confirmation"] = snap["text"][max(0, m.start() - 150):m.start() + 250].strip()[:400] if m else ""
        return {"ok": True, "session": s.id, "snapshot": snap}


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
        if should_exit(controller):
            # A session paid or lost the engine: exit, so systemd stops the engine too (BindsTo=) and the next
            # request on the socket starts both fresh. The socket (Accept=no) keeps listening meanwhile.
            print("ampi-checkout controller: session over after a payment or engine failure; restarting fresh",
                  file=sys.stderr)
            return 0


def should_exit(controller: Controller) -> bool:
    return controller.retire and controller.session is None


if __name__ == "__main__":
    sys.exit(main())
