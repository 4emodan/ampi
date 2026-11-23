"""Sign-in with the logins saved on the owner's phone (docs/COMPANION.md; registered into ops.OPS).

The logins are only on the phone; the owner unlocks one with a fingerprint there each time. ampi-signin (its own user) does
the asking over a channel of its own: no password passes through here, and Ampi only learns the outcome. For now there is
only a test; filling them into a page comes with the checkout browser's next engine.

Only the owner's private chat may ask: no group, watcher, forwarded message or unknown process.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .ops import FIELDS, OPS, Op, Result, Services, need
from .todoist import ToolError

SOCKET = "/run/ampi-signin.sock"
TIMEOUT = 106  # the service ends by itself after 100 s; Claude Code's commands after 120 s
HOST_RE = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])")
AGAIN = "try again later"
# Only these words reach Ampi, whatever the phone or a service in between says.
WORDS = {
    "no_login": "no login is saved for it on the phone (the owner adds one in the Ampi app, Saved logins)",
    "denied": "the owner refused on the phone. Don't ask again unless they say so",
    "expired": "the owner didn't answer on the phone in time",
    "interrupted": "something on the phone came in between (such as an alarm); " + AGAIN,
    "ringing": "an alarm is ringing on the phone; " + AGAIN,
    "busy": "another sign-in request is open",
    "paused": "the owner refused one less than a minute ago. Don't ask again unless they say so",
    "limit": "too many sign-in requests lately; " + AGAIN,
    "notifications_off": "sign-in notifications are switched off for Ampi on the phone",
    "no_fingerprint": "the phone can't check a fingerprint now",
    "key_gone": "the phone's fingerprints changed, so its saved logins can't be opened any more (the owner deletes them in the "
                "Ampi app and adds them again)",
    "unsupported": "the app on the phone is too old for this",
    "offline": "the phone isn't reachable",
    "no_vault": "the phone is paired without saved logins (docs/SETUP.md, companion app: pair it again)",
    "not_paired": "no phone is paired for saved logins (docs/SETUP.md, companion app)",
    "no_answer": "the phone didn't open the channel",
    "untrusted": "the phone and the sign-in service don't trust each other: the phone has to be paired again",
    "lost": "the connection to the phone broke",
}


class SigninClient:
    """One JSON line to ampi-signin's socket, one JSON line back."""

    def __init__(self, path: str = SOCKET, timeout: float = TIMEOUT):
        self.path, self.timeout = path, timeout

    async def call(self, req: dict) -> dict:
        try:
            rd, wr = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 5)
        except (FileNotFoundError, ConnectionRefusedError, AttributeError):
            raise ToolError("saved logins aren't set up on the server (docs/SETUP.md, companion app)") from None
        except (OSError, asyncio.TimeoutError):
            raise ToolError("the sign-in service can't be reached") from None
        try:
            wr.write(json.dumps(req).encode() + b"\n")
            await wr.drain()
            resp = json.loads(await asyncio.wait_for(rd.readline(), self.timeout))
        except (asyncio.TimeoutError, OSError, ValueError):
            raise ToolError("the sign-in service gave no answer") from None
        finally:
            wr.close()
        return resp if isinstance(resp, dict) else {}


def check(ops: list[dict], origin: str) -> None:
    """Refuses sign-ins from anyone but the owner's own chat."""
    if not any(o["op"].startswith("signin.") for o in ops):
        return
    if origin != "private":
        raise ToolError("saved logins are only for the owner's own chat")
    if len(ops) > 1:
        raise ToolError("one sign-in action per request")


def _site(v: Any) -> str:
    """A host, or a link to a page on it."""
    s = v.strip().lower() if isinstance(v, str) else ""
    if "://" in s:
        try:
            u = urlsplit(s)
            s = u.hostname or "" if u.scheme == "https" and u.port in (None, 443) and not u.username else ""
        except ValueError:
            s = ""
    s = s.removeprefix("www.")
    if not HOST_RE.fullmatch(s):
        raise ToolError("give the site like example.com")
    return s


FIELDS["site_host"] = _site


async def si_test(s: Services, a: dict) -> Result:
    host, = need(a, "site_host")
    if s.signin_client is None:
        raise ToolError("saved logins aren't set up on the server (docs/SETUP.md, companion app)")
    r = await s.signin_client.call({"cmd": "test", "host": host})
    if r.get("ok") is True:
        return f"The owner unlocked the login saved for {host} with a fingerprint. It was a test: nothing was sent or filled in."
    code = r.get("code")
    raise ToolError(f"not unlocked: {WORDS.get(code) if isinstance(code, str) and code in WORDS else 'something went wrong'}")


OPS["signin.test"] = Op("signin.test", False, si_test)
