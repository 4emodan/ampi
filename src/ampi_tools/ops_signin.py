"""Sign-in with the logins saved on the owner's phone (docs/COMPANION.md; registered into ops.OPS).

The logins are only on the phone; the owner unlocks one with a fingerprint there each time. ampi-signin (its own user) does
the asking over a channel of its own and fills the login into the checkout browser itself, for the site that browser is
really on: no password passes through here, and Ampi only learns the outcome.

Only the owner's private chat may ask: no group, watcher, forwarded message or unknown process.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .ops import FIELDS, OPS, Op, Result, Services, clean, need
from .todoist import ToolError

SOCKET = "/run/ampi-signin.sock"
TIMEOUT = 106  # the service ends by itself after 100 s; Claude Code's commands after 120 s
FILL_TIMEOUT, CODE_TIMEOUT = 200, 250  # the phone, and the browser before and after it (the skill runs it with a longer timeout)
REF_RE = re.compile(r"f\d{1,3}:\d{1,6}")
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
    "paused": "the owner refused one a short while ago. Don't ask again unless they say so",
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
    "no_browser": "the checkout browser isn't running",
    "browser_busy": "the checkout browser is busy; " + AGAIN,
    "other_target": "that page sends the login to another site than the ones the owner saved it for",
    "other_sites": "this browser session was on a site that isn't part of that login: close it, and open the sign-in page in a new "
                   "one (a sign-in over two sites: the owner saves both addresses with the login)",
    "refused_site": "saved logins aren't used on that site (mail, sign-in providers, banks and the like): the owner signs in there",
    "no_signin": "the phone only gives a code for a login it unlocked in the last 10 minutes, for a site of that login (at most three "
                 "times): start again with `signin fill`",
    "no_session": "no session of that site was given: the owner signs in there in Firefox, taps the Ampi button there (Give, then Send to the "
                  "Ampi app), and asks again within 10 minutes",
    "unknown": "the browser stopped answering while the login was being filled in: it MAY have been sent. Look at the page (`checkout "
               "snapshot`) before trying again",
}


class SigninClient:
    """One JSON line to ampi-signin's socket, one JSON line back."""

    def __init__(self, path: str = SOCKET, timeout: float = TIMEOUT):
        self.path, self.timeout = path, timeout

    async def call(self, req: dict, timeout: float | None = None) -> dict:
        try:
            rd, wr = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 5)
        except (FileNotFoundError, ConnectionRefusedError, AttributeError):
            raise ToolError("saved logins aren't set up on the server (docs/SETUP.md, companion app)") from None
        except (OSError, asyncio.TimeoutError):
            raise ToolError("the sign-in service can't be reached") from None
        try:
            wr.write(json.dumps(req).encode() + b"\n")
            await wr.drain()
            resp = json.loads(await asyncio.wait_for(rd.readline(), timeout or self.timeout))
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


def _ref(v: Any) -> str:
    if not isinstance(v, str) or not REF_RE.fullmatch(v):
        raise ToolError("give the element's ref as the snapshot shows it, like f0:3")
    return v


FIELDS.update({"site_host": _site, "user_ref": _ref, "password_ref": _ref, "press_ref": _ref, "code_ref": _ref})


async def si_test(s: Services, a: dict) -> Result:
    host, = need(a, "site_host")
    r = await _client(s).call({"cmd": "test", "host": host})
    if r.get("ok") is True:
        return f"The owner unlocked the login saved for {host} with a fingerprint. It was a test: nothing was sent or filled in."
    raise ToolError("not unlocked: " + _why(r))


def _client(s: Services) -> Any:
    if s.signin_client is None:
        raise ToolError("saved logins aren't set up on the server (docs/SETUP.md, companion app)")
    return s.signin_client


def _why(r: dict, what: str = "login") -> str:
    code = r.get("code")
    if code == "page":  # the controller's own words about the form
        said = clean(r.get("error"), 200) or "the page isn't as expected"
        return said + {"sent": f". The {what} WAS sent, and it may have worked: look with `checkout snapshot`, don't just try again",
                       "filled": ". Something was filled in already"}.get(r.get("phase"), "")
    return WORDS.get(code, "something went wrong") if isinstance(code, str) else "something went wrong"


async def si_fill(s: Services, a: dict) -> Result:
    from .ops_checkout import cs

    refs = {k: a.get(f"{k}_ref") for k in ("user", "password", "code", "press")}
    if not refs["user"] and not refs["password"]:
        raise ToolError("give the user name field (--user), the password field (--password), or both")
    client = _client(s)
    side = cs(s)
    st = await side.client.call({"cmd": "status"})
    if not st.get("session"):
        raise ToolError("no checkout session; open the site's sign-in page with `checkout open URL`")
    r = await client.call({"cmd": "fill", "session": st["session"], **{k: v for k, v in refs.items() if v}},
                          timeout=CODE_TIMEOUT if refs["code"] else FILL_TIMEOUT)
    if r.get("ok") is not True:
        raise ToolError(("unclear: " if r.get("phase") == "sent" or r.get("code") == "unknown" else "not signed in: ") + _why(r))
    if not r.get("pressed"):
        done = "The login was filled in; its button was not pressed."
    elif r.get("password"):
        done = "The login was filled in and sent. Check below whether the site accepted it."
    else:
        done = "The user name was filled in and sent. For the password page, run `signin fill` again within 3 minutes."
    if r.get("pressed") and not r.get("moved", True):
        done += " The page hasn't changed yet: take a snapshot in a few seconds, and don't sign in again before that."
    if r.get("password"):
        done += (" If it asks for a one-time code now: `signin code --code REF`, right away (doing anything else first, even a "
                 "click, ends that).")
    return await _then(s, side, st, done)


async def _then(s: Services, side: Any, st: dict, done: str) -> str:
    from .ops_checkout import _show

    try:
        page = await side.client.call({"cmd": "snapshot", "session": st["session"]}, timeout=60)
        shown = await _show(s, page)
    except ToolError as e:
        shown = f"(The page can't be read right now: {e}. Don't sign in again; look with `checkout snapshot`.)"
    return done + "\n\n" + shown


async def si_code(s: Services, a: dict) -> Result:
    from .ops_checkout import cs

    ref, = need(a, "code_ref")
    client = _client(s)
    side = cs(s)
    st = await side.client.call({"cmd": "status"})
    if not st.get("session"):
        raise ToolError("no checkout session; a code goes in right after `signin fill`")
    r = await client.call({"cmd": "code", "session": st["session"], "code": ref,
                           **({"press": a["press_ref"]} if a.get("press_ref") else {})}, timeout=CODE_TIMEOUT)
    if r.get("ok") is not True:
        raise ToolError(("unclear: " if r.get("phase") == "sent" or r.get("code") == "unknown" else "no code was filled in: ") + _why(r, "code"))
    done = ("The code was filled in" + (" and its button pressed" if r.get("pressed") else "") + ". Check below whether the site accepted it"
            + ("" if r.get("moved") else " (the page hasn't changed yet: if nothing happens, look again in a few seconds)") + ".")
    return await _then(s, side, st, done)


async def si_session(s: Services, a: dict) -> Result:
    from .ops_checkout import cs

    host, = need(a, "site_host")
    client = _client(s)
    side = cs(s)
    if (await side.client.call({"cmd": "status"})).get("session"):
        raise ToolError("a checkout session is open: close it first (`checkout close`)")
    r = await client.call({"cmd": "session", "host": host, **({"engine": a["engine"]} if a.get("engine") else {})}, timeout=FILL_TIMEOUT)
    if r.get("ok") is not True:
        raise ToolError("no session: " + _why(r, "session"))
    st = await side.client.call({"cmd": "status"})
    if not st.get("session"):
        raise ToolError("the browser closed the session again; try again")
    done = (f"The owner's session is in the browser ({r.get('kept')} of {r.get('of')} cookies). Check below whether the site took it: some don't "
            "accept a session in another browser, and then that's it (don't try to get around it).")
    return await _then(s, side, st, done)


OPS["signin.session"] = Op("signin.session", False, si_session)
OPS["signin.test"] = Op("signin.test", False, si_test)
OPS["signin.code"] = Op("signin.code", False, si_code)
OPS["signin.fill"] = Op("signin.fill", False, si_fill)
