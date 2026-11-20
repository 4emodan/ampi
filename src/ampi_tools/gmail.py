"""Gmail API over httpx, with the gmail.modify scope (read, label, draft, send, trash; never permanent delete)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
from html.parser import HTMLParser
import json
import os
import re
import secrets
import time
import unicodedata
import urllib.parse
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from pathlib import Path
from typing import Any

import httpx

from .todoist import ToolError

API = "https://gmail.googleapis.com/gmail/v1/users/me"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
REDIRECT = "http://localhost:8765/"
MAX_BODY = 20_000
MAX_HTML = 300_000  # longer HTML is cut before parsing (the regexes would get slow)
SAFE_PATH = re.compile(r"(/[A-Za-z0-9_-]{1,128})+")
SYSTEM_LABELS = {
    "INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "SENT", "DRAFT",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


class GoogleAuth:
    """OAuth client (installed app) with a stored refresh token, for one Google API scope."""

    def __init__(self, client_id: str, client_secret: str, token_file: Path, client: httpx.AsyncClient,
                 scope: str = SCOPE, service: str = "Gmail", login_command: str = "gmail-login"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self._client = client
        self._access: str | None = None
        self._expires = 0.0
        self.scope = scope
        self.service = service
        self.login_command = login_command

    def login_url(self) -> tuple[str, str, str]:
        """Returns (url, state, code_verifier) for the one-time login."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        query = {
            "client_id": self.client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(query)}", state, verifier

    async def finish_login(self, redirected_url: str, state: str, verifier: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(redirected_url.strip()).query)
        if q.get("error"):
            raise ToolError(f"Google said: {q['error'][0]}")
        if q.get("state", [""])[0] != state:
            raise ToolError("that URL is from a different login attempt; start again")
        code = q.get("code", [""])[0]
        if not code:
            raise ToolError("no code in that URL; paste the whole address from the browser")
        resp = await self._client.post(TOKEN_URL, data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
            "redirect_uri": REDIRECT, "grant_type": "authorization_code", "code_verifier": verifier,
        })
        data = resp.json()
        if "refresh_token" not in data:
            raise ToolError(f"login failed: {data}")
        tmp = self.token_file.with_name(self.token_file.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            # What Google actually granted (the owner can untick permissions), not what was asked for.
            f.write(json.dumps({"refresh_token": data["refresh_token"], "scope": str(data.get("scope", ""))}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_file)
        self._access = None

    def configured(self) -> bool:
        return self.token_file.is_file()

    def scopes(self) -> set[str]:
        try:
            return set(str(json.loads(self.token_file.read_text()).get("scope", "")).split())
        except (OSError, ValueError, AttributeError):
            return set()

    async def token(self) -> str:
        if self._access and time.monotonic() < self._expires - 60:
            return self._access
        try:
            refresh = json.loads(self.token_file.read_text())["refresh_token"]
        except (OSError, ValueError, KeyError):
            raise ToolError(
                f"{self.service} isn't connected yet (run `ampi-tools {self.login_command}` on the server)"
            ) from None
        try:
            resp = await self._client.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token",
            }, timeout=30)
        except httpx.TransportError as e:
            raise ToolError(f"Google unreachable ({type(e).__name__})") from e
        try:
            data = resp.json()
        except ValueError:
            raise ToolError(f"Google sent an unexpected reply (HTTP {resp.status_code})") from None
        if "access_token" not in data:
            raise ToolError(f"{self.service} login expired or was revoked ({data.get('error')}); "
                            f"run `ampi-tools {self.login_command}`")
        self._access = data["access_token"]
        self._expires = time.monotonic() + float(data.get("expires_in", 3600))
        return self._access


GmailAuth = GoogleAuth  # the defaults are Gmail's


class Gmail:
    def __init__(self, auth: GoogleAuth, client: httpx.AsyncClient, base: str = API):
        self.auth = auth
        self._client = client
        self._base = base
        self._labels: tuple[float, list[dict]] | None = None

    async def _req(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        if not SAFE_PATH.fullmatch(path):  # ids are validated in ops.py too; this is the last line of defence
            raise ToolError(f"refusing odd API path {path!r}")
        headers = {"Authorization": f"Bearer {await self.auth.token()}"}
        try:
            resp = await self._client.request(
                method, f"{self._base}{path}", params=params, json=body, headers=headers, timeout=60
            )
        except httpx.TransportError as e:
            raise ToolError(f"Gmail unreachable: {e!r}") from e
        if resp.status_code == 404:
            raise ToolError(f"Gmail: not found ({path})")
        if resp.status_code >= 400:
            raise ToolError(f"Gmail error {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json() if resp.content else None
        except ValueError:
            raise ToolError(f"Gmail sent an unexpected reply (HTTP {resp.status_code})") from None

    # --- labels -----------------------------------------------------------------------

    async def labels(self) -> list[dict]:
        if self._labels and time.monotonic() - self._labels[0] < 120:
            return self._labels[1]
        data = await self._req("GET", "/labels")
        labels = data.get("labels", [])
        self._labels = (time.monotonic(), labels)
        return labels

    async def label_ids(self, names: list[str], create: bool = False) -> list[str]:
        labels = await self.labels()
        by_name = {label["name"].lower(): label["id"] for label in labels}
        ids = []
        for name in names:
            if name.upper() in SYSTEM_LABELS:
                ids.append(name.upper())
            elif name.lower() in by_name:
                ids.append(by_name[name.lower()])
            elif create:
                made = await self._req("POST", "/labels", body={"name": name})
                self._labels = None
                ids.append(made["id"])
            else:
                raise ToolError(f"no Gmail label {name!r}")
        return ids

    async def label_names(self) -> dict[str, str]:
        return {label["id"]: label["name"] for label in await self.labels()}

    async def delete_label(self, name: str) -> None:
        (label_id,) = await self.label_ids([name])
        await self._req("DELETE", f"/labels/{label_id}")
        self._labels = None

    # --- reading ----------------------------------------------------------------------

    async def search_ids(self, query: str, limit: int = 20) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            params = {"q": query, "maxResults": min(500, limit - len(ids)), "pageToken": token}
            data = await self._req("GET", "/messages", {k: v for k, v in params.items() if v})
            ids += [m["id"] for m in data.get("messages", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    async def metadata(self, ids: list[str]) -> list[dict]:
        sem = asyncio.Semaphore(8)

        async def one(mid: str) -> dict:
            async with sem:
                return await self._req("GET", f"/messages/{mid}", {
                    "format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date", "List-Unsubscribe"],
                })

        return list(await asyncio.gather(*(one(i) for i in ids)))

    async def summaries(self, ids: list[str]) -> list[str]:
        if not ids:
            return []
        names = await self.label_names()
        return [format_summary(m, names) for m in await self.metadata(ids)]

    async def message(self, mid: str) -> dict:
        return await self._req("GET", f"/messages/{mid}", {"format": "full"})

    async def read(self, mid: str) -> str:
        names = await self.label_names()
        msg = await self.message(mid)
        # Parsing hostile HTML runs in a thread, so a slow parse never blocks approvals or other requests.
        return await asyncio.wait_for(asyncio.to_thread(format_full, msg, names), 30)

    async def thread(self, tid: str) -> str:
        names = await self.label_names()
        data = await self._req("GET", f"/threads/{tid}", {"format": "full"})

        def render() -> str:
            return "\n\n".join(format_full(m, names) for m in data.get("messages", []))

        return await asyncio.wait_for(asyncio.to_thread(render), 60)

    # --- changing ---------------------------------------------------------------------

    async def modify(self, ids: list[str], add: list[str] = (), remove: list[str] = ()) -> None:
        body = {
            "ids": ids,
            "addLabelIds": await self.label_ids(list(add), create=True),
            "removeLabelIds": await self.label_ids(list(remove)),
        }
        await self._req("POST", "/messages/batchModify", body=body)

    async def trash(self, ids: list[str]) -> None:
        for mid in ids:
            await self._req("POST", f"/messages/{mid}/trash")

    async def untrash(self, ids: list[str]) -> None:
        for mid in ids:
            await self._req("POST", f"/messages/{mid}/untrash")

    async def resolve_mail(self, fields: dict) -> dict:
        """Turns {to?, cc?, subject?, body, reply_to?} into the exact fields that will be sent:
        recipients, subject and threading headers are fixed here, so what the owner approves is what goes out."""
        out = {k: fields.get(k) or "" for k in ("to", "cc", "subject", "body")}
        out.update(thread_id="", in_reply_to="", references="")
        reply_to = fields.get("reply_to")
        if reply_to:
            orig = await self._req("GET", f"/messages/{reply_to}", {
                "format": "metadata", "metadataHeaders": ["Message-ID", "References", "Subject", "From", "Reply-To"],
            })
            h = headers(orig)
            out["thread_id"] = orig.get("threadId") or ""
            if h.get("message-id"):
                out["in_reply_to"] = h["message-id"]
                out["references"] = (h.get("references", "") + " " + h["message-id"]).strip()
            if not out["subject"]:
                s = h.get("subject", "")
                out["subject"] = s if s.lower().startswith("re:") else f"Re: {s}"
            if not out["to"]:
                out["to"] = h.get("reply-to") or h.get("from", "")
        # Parse the addresses once, strictly, and rebuild the headers from the result: the summary and
        # the sent email then name exactly the same recipients (lenient header parsing can't add any).
        out["to"] = strict_addresses(out["to"], "to")
        out["cc"] = strict_addresses(out["cc"], "cc") if out["cc"].strip() else ""
        if not out["to"]:
            raise ToolError("who to? (--to)")
        out["subject"] = normalize_text(out["subject"]).replace("\n", " ").strip()
        out["body"] = normalize_text(out["body"])
        return out

    @staticmethod
    def compose(f: dict) -> dict:
        msg = EmailMessage()
        try:
            msg["To"] = f["to"]
            if f.get("cc"):
                msg["Cc"] = f["cc"]
            msg["Subject"] = f.get("subject") or ""
            if f.get("in_reply_to"):
                msg["In-Reply-To"] = f["in_reply_to"]
                msg["References"] = f.get("references") or f["in_reply_to"]
            msg.set_content(f["body"])
        except (ValueError, TypeError) as e:
            raise ToolError(f"can't build that email: {e}") from None
        out: dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if f.get("thread_id"):
            out["threadId"] = f["thread_id"]
        return out

    async def draft(self, fields: dict) -> dict:
        message = self.compose(await self.resolve_mail(fields))
        return await self._req("POST", "/drafts", body={"message": message})

    async def send_resolved(self, resolved: dict) -> dict:
        """Sends exactly the fields fixed by resolve_mail when the owner was asked."""
        return await self._req("POST", "/messages/send", body=self.compose(resolved))

    async def get_draft(self, draft_id: str) -> dict:
        return await self._req("GET", f"/drafts/{draft_id}", {"format": "full"})

    async def send_draft(self, draft_id: str) -> dict:
        return await self._req("POST", "/drafts/send", body={"id": draft_id})

    async def drafts(self, limit: int = 20) -> list[dict]:
        data = await self._req("GET", "/drafts", {"maxResults": limit})
        return data.get("drafts", [])


# --- formatting -----------------------------------------------------------------------


def headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}


def _when(msg: dict) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(msg["internalDate"]) / 1000))
    except (KeyError, ValueError):
        return "?"


def _labels(msg: dict, names: dict[str, str]) -> str:
    return ", ".join(names.get(i, i) for i in msg.get("labelIds", []))


def format_summary(msg: dict, names: dict[str, str]) -> str:
    h = headers(msg)
    sender = h.get("from", "?")
    line = f"- [{msg['id']}] {_when(msg)} · {sender} · {h.get('subject', '(no subject)')}"
    line += f"\n  labels: {_labels(msg, names) or '-'}"
    if h.get("list-unsubscribe"):
        line += " · newsletter/list"
    snippet = html.unescape(msg.get("snippet", ""))
    if snippet:
        line += f"\n  {snippet[:200]}"
    return line


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    """Linear-time HTML to text (regexes over hostile HTML can take minutes)."""

    BLOCK = {"p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.href: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "a":
            self.href.append(dict(attrs).get("href"))
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            href = self.href.pop()
            if href and href.startswith(("http://", "https://")) and not self.skip:
                self.out.append(f" ({href})")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(s: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(s[:MAX_HTML])
        parser.close()
    except Exception:  # malformed HTML: keep what was parsed
        pass
    s = "".join(parser.out)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _walk(part: dict, plain: list[str], htmls: list[str], files: list[str]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body") or {}
    if part.get("filename"):
        files.append(f"{part['filename']} ({mime}, {body.get('size', 0) // 1024} KB)")
    elif mime == "text/plain" and body.get("data"):
        plain.append(_b64(body["data"]))
    elif mime == "text/html" and body.get("data"):
        htmls.append(_b64(body["data"]))
    for sub in part.get("parts", []) or []:
        _walk(sub, plain, htmls, files)


def format_full(msg: dict, names: dict[str, str]) -> str:
    h = headers(msg)
    plain: list[str] = []
    htmls: list[str] = []
    files: list[str] = []
    _walk(msg.get("payload") or {}, plain, htmls, files)
    text = "\n".join(plain).strip() or html_to_text("\n".join(htmls))
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY] + f"\n[... cut, {len(text) - MAX_BODY} more characters]"
    lines = [
        f"### [{msg['id']}] {h.get('subject', '(no subject)')}",
        f"From: {h.get('from', '?')}",
        f"To: {h.get('to', '?')}" + (f" · Cc: {h['cc']}" if h.get("cc") else ""),
        f"Date: {_when(msg)} · thread {msg.get('threadId')} · labels: {_labels(msg, names) or '-'}",
    ]
    if h.get("list-unsubscribe"):
        lines.append(f"List-Unsubscribe: {h['list-unsubscribe'][:300]}")
    if files:
        lines.append("Attachments: " + "; ".join(files))
    lines += ["", text or "(no text)"]
    return "\n".join(lines)


ADDRESS_RE = re.compile(r"[^@\s<>(),;:\"\[\]\\]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def strict_addresses(value: str, field: str) -> str:
    """Parses a To/Cc value; refuses anything ambiguous; returns a cleanly rebuilt header value."""
    if "\n" in value or "\r" in value:
        raise ToolError(f"{field}: line breaks aren't allowed")
    pairs = getaddresses([value], strict=True) if "strict" in getaddresses.__code__.co_varnames else getaddresses([value])
    # Every "@" in the value must belong to one parsed address, so nothing is silently dropped.
    if not pairs or value.count("@") != len(pairs) or any(not ADDRESS_RE.fullmatch(a) for _, a in pairs):
        raise ToolError(f"{field}: can't read {value!r} as a list of email addresses")
    return ", ".join(formataddr((normalize_text(n).replace("\n", " ").strip(), a)) for n, a in pairs)


_SPACES = re.compile(r"[\t   -   　]")


def normalize_text(s: str) -> str:
    """What's approved must be what's sent: no invisible or look-alike characters to hide data in."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _SPACES.sub(" ", s)
    s = IGNORABLE.sub("", s)
    s = "".join(ch for ch in s if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs"))
    return "\n".join(line.rstrip() for line in s.split("\n")).strip("\n")


# Characters that render as nothing but aren't in the control/format categories (variation selectors,
# fillers, tag characters, ...): they could hide data inside text the owner approves.
IGNORABLE = re.compile(
    "[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮⁠-⁯"
    "ㅤ︀-️﻿ﾠ\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)


def sender_address(value: str) -> str:
    return parseaddr(value)[1].lower()


def recipients(value: str) -> list[str]:
    return [addr for _, addr in getaddresses([value]) if addr]
