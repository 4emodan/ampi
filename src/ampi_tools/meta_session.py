"""Instagram and Threads through the owner's own logged-in web session (cookies the owner copies from their browser).

The owner chose this knowing it is against Meta's terms and the account may be checkpointed or banned. It stays
small and honest:

- **The owner logs in, never Ampi.** No passwords. The owner pastes the session cookies on the server
  (`ampi-tools instagram-session` / `threads-session`, prompts don't echo). Claude never sees them.
- **No detection evasion.** Only the endpoints instagram.com / threads.com themselves call, with the static headers
  they need and one fixed, honest User-Agent. No device ids, signatures, private mobile API, proxies, jitter, or
  challenge solving. The owner's own device cookies (mid, ig_did, datr) are sent only if they pasted them.
- **Stop at the first sign of trouble.** A redirect, 401/403, checkpoint/challenge/login_required, a deleted
  session cookie or a refused user agent pauses the session (the file says `paused`, shared with the watchers):
  every call then fails with a fixed message until the owner runs `--resume`, and one Telegram notice is sent.
  A 429 or feedback_required pauses for an hour (on a write: until resumed).
- **Allow-list.** Every request is one of the (method, host, path[, GraphQL doc]) entries in ENDPOINTS / TH_DOCS
  below; `/accounts/` and `/challenge/` are refused outright, as is anything else. Nothing marks anything seen.
- **Budgets** (account safety, not evasion): reads ≤ 120/hour and ≤ 600/day, writes ≤ 20/hour and ≤ 60/day,
  follows ≤ 20/day, watchers their own 30 reads/hour; a fixed 1 s between requests.

None of the endpoints or GraphQL doc ids below could be checked against the real sites when this was written:
they come from public knowledge of the web apps. The first real session will show what needs adjusting; every
one of them lives in the tables in this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .meta import redact, register_secret
from .todoist import ToolError

log = logging.getLogger(__name__)

USER_AGENT = "AmpiAssistant/1.0 (personal assistant; one user)"  # the same honest agent as Wolt; never a browser's
GAP = 1.0                  # seconds between two requests of one service (fixed, no jitter)
MAX_REPLY = 5_000_000      # threads.com's logged-in page is large; API replies are far smaller
TIMEOUT = 30
TOKEN_AGE = 30 * 60        # Threads page tokens (fb_dtsg, lsd) are fetched again after this
COOLDOWN = 3600            # after a 429 / feedback_required on a read
ID_RE = re.compile(r"[0-9]{1,40}")
COOKIE_RE = re.compile(r"[\x21-\x2b\x2d-\x3a\x3c-\x7e]{1,4096}")  # printable, no space, comma or semicolon
USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")


@dataclass(frozen=True)
class WebService:
    key: str        # instagram | threads
    name: str
    host: str
    command: str    # the owner's command on the server

    @property
    def paused_text(self) -> str:
        return (f"{self.name} wants you to confirm it's you: open the app, then run "
                f"`ampi-tools {self.command} --resume`")


IG = WebService("instagram", "Instagram", "www.instagram.com", "instagram-session")
TH = WebService("threads", "Threads", "www.threads.com", "threads-session")
SERVICES = {IG.key: IG, TH.key: TH}

# Cookies the owner pastes. The first three are required; the others are the owner's own device cookies, sent only
# if pasted (never generated).
REQUIRED_COOKIES = ("sessionid", "csrftoken", "ds_user_id")
OPTIONAL_COOKIES = ("mid", "ig_did", "datr")
ROTATED_COOKIES = ("sessionid", "csrftoken", "rur")  # replaced from Set-Cookie and saved at once

# --- the allow-list ----------------------------------------------------------------------------------------
#
# Every request either service may make. {placeholders} are numeric ids only. `cls` decides the budget: read,
# write, follow (a write that also counts against the daily follow limit). Comments mark what is least certain.

IG_APP_ID = "936619743392459"     # the instagram.com web app's X-IG-App-ID
TH_APP_ID = "238260118697367"     # threads.com web app's X-IG-App-ID (GUESS: from 2023 public write-ups)
ASBD_ID = "129477"                # X-ASBD-ID the web apps send (GUESS: it changes now and then)


@dataclass(frozen=True)
class Endpoint:
    service: str
    method: str
    host: str
    path: str
    cls: str        # read | write | follow
    headers: str    # a profile in HEADERS
    kind: str = "json"  # json | html | graphql


ENDPOINTS: dict[str, Endpoint] = {
    # Instagram (www.instagram.com, REST endpoints its web app calls)
    # The logged-in page, for the page tokens Instagram's own GraphQL requests carry (fb_dtsg, lsd).
    "ig.home": Endpoint("instagram", "GET", "www.instagram.com", "/", "read", "html", "html"),
    "ig.user_info": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/users/{user}/info/", "read", "ig"),
    "ig.web_profile_info": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/users/web_profile_info/",
                                    "read", "ig"),                           # ?username=
    # The home feed comes from GraphQL on the web (IG_DOCS["feed"]); /api/v1/feed/timeline/ is the mobile app's and
    # is deliberately not listed.
    "ig.graphql": Endpoint("instagram", "POST", "www.instagram.com", "/graphql/query", "read", "ig_gql", "graphql"),
    "ig.user_feed": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/feed/user/{user}/", "read", "ig"),
    "ig.media_info": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/media/{media}/info/", "read", "ig"),
    "ig.media_comments": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/media/{media}/comments/", "read",
                                  "ig"),                                     # ?min_id= (next page)
    # GUESS: replies under one comment (the web app's "View replies").
    "ig.child_comments": Endpoint("instagram", "GET", "www.instagram.com",
                                  "/api/v1/media/{media}/comments/{comment}/child_comments/", "read", "ig"),
    "ig.reels_tray": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/feed/reels_tray/", "read", "ig"),
    "ig.reels_media": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/feed/reels_media/", "read", "ig"),
    "ig.inbox": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/direct_v2/inbox/", "read", "ig"),
    "ig.thread": Endpoint("instagram", "GET", "www.instagram.com", "/api/v1/direct_v2/threads/{thread}/", "read",
                          "ig"),
    "ig.comment_add": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/web/comments/{media}/add/", "write",
                               "ig_post"),
    "ig.comment_delete": Endpoint("instagram", "POST", "www.instagram.com",
                                  "/api/v1/web/comments/{media}/delete/{comment}/", "write", "ig_post"),
    "ig.like": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/web/likes/{media}/like/", "write",
                        "ig_post"),
    "ig.unlike": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/web/likes/{media}/unlike/", "write",
                          "ig_post"),
    # GUESS: comment likes on the web; older web builds used /web/comments/like/{id}/ (no api/v1).
    "ig.comment_like": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/web/comments/like/{comment}/",
                                "write", "ig_post"),
    "ig.comment_unlike": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/web/comments/unlike/{comment}/",
                                  "write", "ig_post"),
    # GUESS: the current web app's Follow button posts here (older web builds: /web/friendships/{id}/follow/).
    "ig.follow": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/friendships/create/{user}/",
                          "follow", "ig_post"),
    "ig.unfollow": Endpoint("instagram", "POST", "www.instagram.com", "/api/v1/friendships/destroy/{user}/",
                            "write", "ig_post"),
    # Threads (www.threads.com): the logged-in page (for fb_dtsg / lsd) and its GraphQL endpoint.
    "th.home": Endpoint("threads", "GET", "www.threads.com", "/", "read", "html", "html"),
    # GUESS: threads.net used /api/graphql; threads.com's app posts to /graphql/query.
    "th.graphql": Endpoint("threads", "POST", "www.threads.com", "/graphql/query", "read", "th", "graphql"),
}


@dataclass(frozen=True)
class Doc:
    doc_id: str
    name: str       # fb_api_req_friendly_name the web app sends with it
    root: str       # the key under "data" a good answer has ("" = any data)
    cls: str        # read | write | follow
    status: str = "known"  # known | stale (real but old: sent, may fail) | placeholder (never sent)

    success: tuple[str, ...] = ("status",)  # a change: the result's field that must say ok (GUESS until seen)

    @property
    def sendable(self) -> bool:
        return self.status != "placeholder" and bool(re.fullmatch(r"[1-9][0-9]{8,24}", self.doc_id))


# GraphQL queries and mutations (doc_id, operation name). Meta changes doc_ids with new web builds: a doc that stops
# answering fails with "… changed its web app; the `X` query needs updating", never with a guess. Placeholders are
# refused locally (nothing is sent) until the real doc_id is copied from DevTools (SETUP §8c-2).
TH_DOCS: dict[str, Doc] = {
    # STALE: from 2023 public threads.net clients; the names are what to look for in DevTools.
    "profile": Doc("23996318473300828", "BarcelonaProfileRootQuery", "user", "read", "stale"),           # {userID}
    "posts": Doc("6232751443445612", "BarcelonaProfileThreadsTabQuery", "mediaData", "read", "stale"),     # {userID}
    "post": Doc("5587632691339264", "BarcelonaPostPageQuery", "data", "read", "stale"),                   # {postID}
    # PLACEHOLDERS: unknown doc_ids, operation names guessed. Never sent.
    "profile_by_name": Doc("0", "BarcelonaUsernameHovercardImplQuery", "user", "read", "placeholder"),   # {username}
    "feed": Doc("0", "BarcelonaFeedTimelineQuery", "feedData", "read", "placeholder"),
    "like": Doc("0", "useTHLikeMutationLikeMutation", "", "write", "placeholder"),                       # {mediaID}
    "unlike": Doc("0", "useTHLikeMutationUnlikeMutation", "", "write", "placeholder"),                   # {mediaID}
    "follow": Doc("0", "useTHFollowMutationFollowMutation", "", "follow", "placeholder"),     # {target_user_id}
    "unfollow": Doc("0", "useTHUnfollowMutationUnfollowMutation", "", "write", "placeholder"),  # {target_user_id}
}
IG_DOCS: dict[str, Doc] = {
    # PLACEHOLDER: the web home feed query (answer under data.xdt_api__v1__feed__timeline__connection). Never sent.
    "feed": Doc("0", "PolarisFeedRootPaginationCachedQuery_subscribe", "xdt_api__v1__feed__timeline__connection",
                "read", "placeholder"),
}
DOCS = {"instagram": IG_DOCS, "threads": TH_DOCS}
IG_ENDPOINTS = {k: v for k, v in ENDPOINTS.items() if v.service == "instagram"}
TH_ENDPOINTS = {k: v for k, v in ENDPOINTS.items() if v.service == "threads"}

# Per-profile static headers; "{csrf}", "{lsd}" and "{friendly}" are filled per request.
HEADERS: dict[str, dict[str, str]] = {
    "html": {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"},
    "ig": {"Accept": "*/*", "X-IG-App-ID": IG_APP_ID, "X-ASBD-ID": ASBD_ID, "X-Requested-With": "XMLHttpRequest",
           "X-CSRFToken": "{csrf}", "Referer": "https://www.instagram.com/"},
    "ig_post": {"Accept": "*/*", "X-IG-App-ID": IG_APP_ID, "X-ASBD-ID": ASBD_ID, "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": "{csrf}", "Referer": "https://www.instagram.com/", "Origin": "https://www.instagram.com",
                "Content-Type": "application/x-www-form-urlencoded"},
    "ig_gql": {"Accept": "*/*", "X-IG-App-ID": IG_APP_ID, "X-ASBD-ID": ASBD_ID, "X-CSRFToken": "{csrf}",
               "X-FB-LSD": "{lsd}", "X-FB-Friendly-Name": "{friendly}", "Referer": "https://www.instagram.com/",
               "Origin": "https://www.instagram.com", "Content-Type": "application/x-www-form-urlencoded"},
    "th": {"Accept": "*/*", "X-IG-App-ID": TH_APP_ID, "X-ASBD-ID": ASBD_ID, "X-CSRFToken": "{csrf}",
           "X-FB-LSD": "{lsd}", "X-FB-Friendly-Name": "{friendly}", "Referer": "https://www.threads.com/",
           "Origin": "https://www.threads.com", "Content-Type": "application/x-www-form-urlencoded"},
}

BANNED = ("/accounts/", "/challenge/")
_PATHS = {name: re.compile("^" + re.sub(r"\\\{[a-z]+\\\}", "[0-9]{1,40}", re.escape(ep.path)) + "$")
          for name, ep in ENDPOINTS.items()}


def allowed(method: str, host: str, path: str, doc: str | None = None, friendly: str | None = None) -> str | None:
    """The endpoint name this exact request is, or None if it isn't allowed."""
    if any(b in path for b in BANNED) or "seen" in path.lower():
        return None
    for name, ep in ENDPOINTS.items():
        if ep.method == method and ep.host == host and _PATHS[name].fullmatch(path):
            if ep.kind == "graphql":
                d = next((d for d in DOCS[ep.service].values() if d.doc_id == doc and d.sendable), None)
                if d is None or d.name != friendly:
                    return None
            elif doc is not None:
                return None
            return name
    return None


# --- the session file ---------------------------------------------------------------------------------------


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """One process at a time changes `path` (the daemon and the owner's commands share these files). Not reentrant.
    Waits a while, then gives up; in the daemon it runs outside the event loop."""
    deadline = time.monotonic() + LOCK_WAIT
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:  # Windows (development)
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ToolError("the session file is busy; try again in a moment") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # releases the lock


def identity(d: dict) -> tuple:
    """Which saved session this is (a new one is saved under a new id)."""
    return d.get("sid"), d.get("saved_at")


class SessionAuth:
    """{cookies, user_id, username, saved_at, state} in /home/ampitools/<service>-session.json (0600)."""

    def __init__(self, service: WebService, path: Path, notify: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.s = service
        self.path = path
        self.notify = notify
        self.clock = clock
        for v in (self.load().get("cookies") or {}).values():
            register_secret(v if isinstance(v, str) else None)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def configured(self) -> bool:
        c = self.load().get("cookies")
        return isinstance(c, dict) and all(isinstance(c.get(k), str) and c.get(k) for k in REQUIRED_COOKIES)

    def snapshot(self) -> tuple[dict[str, str], tuple]:
        """The cookies and which session they belong to, from one read of the file."""
        d = self.load()
        c = d.get("cookies")
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) and c.get(k) for k in REQUIRED_COOKIES):
            raise ToolError(f"the owner's {self.s.name} session isn't set up (`ampi-tools {self.s.command}` on the "
                            "server)")
        out = {k: c[k] for k in (*REQUIRED_COOKIES, *OPTIONAL_COOKIES, "rur") if isinstance(c.get(k), str) and c[k]}
        for v in out.values():
            register_secret(v)
        return out, identity(d)

    def cookies(self) -> dict[str, str]:
        return self.snapshot()[0]

    def same(self, session: tuple) -> bool:
        return identity(self.load()) == session

    def _change(self, session: tuple | None, change: Callable[[dict], bool]) -> bool:
        """Changes the file under its lock, unless it is another session's by now (a late reply to the old one)."""
        with file_lock(self.path):
            d = self.load()
            if not d or (session is not None and identity(d) != session) or not change(d):
                return False
            self.save(d)
            return True

    @property
    def user(self) -> dict:
        d = self.load()
        uid, name = str(d.get("user_id", "")), str(d.get("username", ""))
        if not ID_RE.fullmatch(uid) or not USERNAME_RE.fullmatch(name):
            raise ToolError(f"the {self.s.name} session doesn't say whose it is: run `ampi-tools {self.s.command}` "
                            "again")
        return {"id": uid, "username": name}

    def state(self) -> str:
        d = self.load()
        if d.get("state") == "paused":
            return "paused"
        if float(d.get("cooldown_until") or 0) > self.clock():
            return "cooldown"
        return "ok" if self.configured() else "missing"

    def check(self) -> None:
        """Raises while paused or cooling down (the file is shared: the watchers stop too)."""
        d = self.load()
        if d.get("state") == "paused":
            raise ToolError(self.paused_text())
        left = float(d.get("cooldown_until") or 0) - self.clock()
        if left > 0:
            raise ToolError(f"{self.s.name} asked to slow down; paused for {int(left // 60) + 1} more minute(s). "
                            "Don't try another way.")

    def paused_text(self) -> str:
        """Why it's paused, as far as that's known (never text from the service)."""
        reason = self.load().get("paused_reason")
        text = UA_TEXT if reason == "user_agent" else NEUTRAL_TEXT if reason in NEUTRAL else None
        return text.format(name=self.s.name, command=self.s.command) if text else self.s.paused_text

    def pause(self, reason: str, session: tuple | None = None) -> None:
        """Until the owner resumes. One Telegram notice, on the change only."""
        if self.mark_paused(reason, session):
            self.paused_notice(reason)

    def mark_paused(self, reason: str, session: tuple | None = None) -> bool:
        def change(d: dict) -> bool:
            if d.get("state") == "paused":
                return False
            d.update(state="paused", paused_reason=reason, paused_at=self.clock())
            return True

        return self._change(session, change)

    def paused_notice(self, reason: str) -> None:
        log.warning("%s session paused (%s)", self.s.name, reason)
        if self.notify is not None:
            notice = {"user_agent": UA_NOTICE}.get(reason, NEUTRAL_NOTICE if reason in NEUTRAL else PAUSE_NOTICE)
            self.notify(notice.format(name=self.s.name, command=self.s.command))

    def cool_down(self, session: tuple | None = None) -> None:
        if self.mark_cooling(session):
            self.cooling_notice()

    def mark_cooling(self, session: tuple | None = None) -> bool:
        """True when it wasn't cooling down already."""
        already = []

        def change(d: dict) -> bool:
            if d.get("state") == "paused":
                return False
            if float(d.get("cooldown_until") or 0) > self.clock():
                already.append(True)
            d["cooldown_until"] = self.clock() + COOLDOWN
            return True

        return self._change(session, change) and not already

    def cooling_notice(self) -> None:
        log.warning("%s asked to slow down; pausing reads for an hour", self.s.name)
        if self.notify is not None:
            self.notify(COOLDOWN_NOTICE.format(name=self.s.name))

    def rotate(self, name: str, value: str, session: tuple | None = None) -> None:
        if name not in ROTATED_COOKIES or not COOKIE_RE.fullmatch(value or ""):
            return
        register_secret(value)

        def change(d: dict) -> bool:
            cookies = d.get("cookies")
            if not isinstance(cookies, dict) or cookies.get(name) == value:
                return False
            cookies[name] = value
            return True

        self._change(session, change)

    def resume(self) -> None:
        def change(d: dict) -> bool:
            d.update(state="ok", cooldown_until=0, resumed_at=self.clock())  # page tokens are fetched again
            d.pop("paused_reason", None)
            return True

        if not self._change(None, change):
            raise ToolError(f"no {self.s.name} session saved")

    def forget(self) -> bool:
        with file_lock(self.path):
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False

    def status(self) -> str:
        d = self.load()
        st = self.state()
        if st == "missing":
            return f"{self.s.name} web session: not set up"
        who = f"@{d.get('username', '?')}"
        saved = time.strftime("%Y-%m-%d", time.gmtime(float(d.get("saved_at") or 0)))
        if st == "paused":
            return f"{self.s.name} web session: PAUSED ({d.get('paused_reason', '?')}), {who}, saved {saved}: " \
                   f"`ampi-tools {self.s.command} --resume`"
        return f"{self.s.name} web session: {'cooling down (rate limit)' if st == 'cooldown' else 'on'}, {who}, " \
               f"saved {saved}"


LOCK_WAIT = 10.0  # seconds
NEUTRAL = ("no_page_tokens", "stale_tokens", "not_json")
MAYBE = ". It MAY have happened anyway: check it yourself before asking again; it isn't retried"
PAUSE_NOTICE = ("⚠️ Ampi stopped using your {name} session: {name} asked to confirm it's you, or the session was logged "
                "out. Open the {name} app (or website) and confirm, then on the server run `ampi-tools {command} "
                "--resume` (or save fresh cookies with `ampi-tools {command}`). Nothing more is sent until then.")
COOLDOWN_NOTICE = "⚠️ {name} asked Ampi to slow down: no {name} requests for an hour."
UA_TEXT = ("{name}'s web endpoints refuse non-browser clients (they refused the assistant's honest user agent), and "
           "Ampi doesn't pretend to be a browser, so the {name} session is stopped (`ampi-tools {command} --resume` "
           "only if that changes)")
NEUTRAL_TEXT = ("{name} didn't give a usable logged-in page, so the {name} session is stopped; the reason isn't known "
                "(logged out, or the site changed). Check {name} in your browser, then `ampi-tools {command} --resume`")
NEUTRAL_NOTICE = ("⚠️ {name} didn't give Ampi a usable logged-in page (logged out, or the site changed), so the {name} "
                  "session is stopped. Check it in your browser, then run `ampi-tools {command} --resume` on the server.")
UA_NOTICE = ("⚠️ {name}'s web endpoints refuse non-browser clients: they refused Ampi's honest user agent, so the "
             "{name} session is stopped. Ampi won't pretend to be a browser.")


# --- budgets ------------------------------------------------------------------------------------------------

LIMITS = {"read": ((3600, 120),), "watch": ((3600, 30),), "write": ((3600, 20), (86400, 60)),
          "follow": ((86400, 20),)}
READS_PER_DAY = 600  # chat and watcher reads together


class Budget:
    """Request counts per kind over the last day, kept in a small file so a restart doesn't reset them."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self.hits: dict[str, list[float]] = {k: [] for k in LIMITS}
        self._read()

    def _read(self) -> None:
        if self.path is not None:
            try:
                raw = json.loads(self.path.read_text())
                self.hits.update({k: [float(t) for t in v] for k, v in raw.items() if k in LIMITS})
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def _count(self, kind: str, window: float) -> int:
        now = self.clock()
        return sum(1 for t in self.hits[kind] if now - t < window)

    def take(self, cls: str, watch: bool, name: str) -> None:
        if self.path is None:
            return self._take(cls, watch, name)
        with file_lock(self.path):  # the owner's commands count in the same file
            self._read()
            self._take(cls, watch, name)

    def _take(self, cls: str, watch: bool, name: str) -> None:
        now = self.clock()
        self.hits = {k: [t for t in v if now - t < 86400] for k, v in self.hits.items()}
        kinds = ["write", "follow"] if cls == "follow" else ["write"] if cls == "write" else \
            ["watch" if watch else "read"]
        for kind in kinds:
            for window, cap in LIMITS[kind]:
                if self._count(kind, window) >= cap:
                    what = {"read": "reads", "watch": "watcher reads", "write": "changes", "follow": "follows"}[kind]
                    span = "hour" if window == 3600 else "day"
                    raise ToolError(f"the {name} limit for {what} is used up ({cap} per {span}, for the account's "
                                    "safety); try again later")
        if cls == "read" and len(self.hits["read"]) + len(self.hits["watch"]) >= READS_PER_DAY:
            raise ToolError(f"the {name} limit for reads is used up ({READS_PER_DAY} a day); try again tomorrow")
        for kind in kinds:
            self.hits[kind].append(now)
        self._save()  # every request sent counts, refused or not

    def _save(self) -> None:
        if self.path is not None:
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(self.hits))
                os.replace(tmp, self.path)
            except OSError:
                log.warning("couldn't save the request counts (%s)", self.path.name)


# --- the client ---------------------------------------------------------------------------------------------

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_pk(code: str) -> str:
    """The media id behind an instagram.com/p/CODE (or threads.com/@u/post/CODE) link; computed, no request.
    Longer codes (private posts) carry extra characters after the first 11 (GUESS for Threads codes)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,40}", code or ""):
        raise ToolError("bad post code")
    n = 0
    for ch in code[:11]:
        n = n * 64 + SHORTCODE_ALPHABET.index(ch)
    return str(n)


def pk_to_shortcode(pk: str | int) -> str:
    n = int(pk)
    out = ""
    while n:
        n, r = divmod(n, 64)
        out = SHORTCODE_ALPHABET[r] + out
    return out or "A"


_CHALLENGES = ("checkpoint_required", "challenge_required", "login_required", "consent_required", "sentry_block")
_LOGIN_MARKERS = ("/accounts/login/\"", "/accounts/login/'", "/accounts/login/?", '"is_logged_in":false', 'id="loginForm"')
# A login page even if it carries page tokens (a plain mention of /accounts/login can be on logged-in pages too).
_LOGIN_PAGE = re.compile(r'''"is_logged_in":false|id="loginForm"|<(?:a|form)\b[^>]*(?:href|action)=["'][^"']*'''
                         r'''(?:/accounts/login/?["'?#]|/challenge/)''')
# Whole phrases only: "This link has expired" or "Blog in maintenance" are not about the login.
_LOGIN_RE = re.compile(r"\bnot logged in\b|\blog ?in (?:is )?required\b|\bplease log ?in\b|\b(?:login|session) (?:has )?"
                       r"expired\b|\byou must log ?in\b|\blog in to continue\b", re.I)
HOME = {"instagram": "ig.home", "threads": "th.home"}


_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


class _StaleTokens(Exception):
    """The page tokens (fb_dtsg/lsd) were refused (Meta error 1357004 on a GraphQL request)."""

    def __init__(self, cls: str, tokens: dict):
        super().__init__(cls)
        self.cls = cls
        self.tokens = tokens
_SLOW_DOWN = re.compile(r"feedback_required|please wait a few minutes|rate[ _-]*limit|too[ _-]+many[ _-]+requests")
_UA_RE = re.compile(r"user[ _-]?agent[ _-]?(?:mismatch|not (?:supported|allowed)|blocked|refused)|"
                    r"(?:unsupported|invalid|blocked|bad) user[ _-]?agent", re.I)
_AUTH_WORDS = re.compile(r"log ?in|session|authenti|authori[sz]|checkpoint|challenge|forbidden")


class WebClient:
    """One service's requests: allow-listed, budgeted, one at a time, cookies only in a header to that host."""

    def __init__(self, service: WebService, auth: SessionAuth, client: httpx.AsyncClient, *, gap: float = GAP,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, usage: Path | None = None):
        self.s = service
        self.auth = auth
        self._client = client
        self.gap = gap
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last = -1e9
        self.budget = Budget(usage, auth.clock)
        self._tokens: dict[str, Any] = {}
        self._token_lock = asyncio.Lock()  # one token fetch at a time; waiting callers share its result
        self._stale_key: tuple = ()
        self._empty = 0  # empty 200 replies in a row
        self._stale: list[dict] = []  # token sets refused in a row (several callers may have used the same set)

    def configured(self) -> bool:
        return self.auth.configured()

    async def call(self, name: str, *, ids: dict[str, str] | None = None, params: dict | None = None,
                   form: dict | None = None, doc: str | None = None, variables: dict | None = None,
                   watch: bool = False) -> Any:
        for attempt in (1, 2):
            try:
                return await self._call(name, ids, params, form, doc, variables, watch)
            except _StaleTokens as e:
                if not any(t is e.tokens for t in self._stale):
                    self._stale.append(e.tokens)
                if self._tokens is e.tokens:  # not if another caller already fetched newer ones
                    self._tokens = {}
                if len(self._stale) >= 2:  # fresh tokens refused too
                    self._stale.clear()
                    if await asyncio.to_thread(self.auth.mark_paused, "stale_tokens"):
                        self.auth.paused_notice("stale_tokens")
                    raise ToolError(self.auth.paused_text()) from None
                if e.cls != "read":  # a change is never sent twice
                    raise ToolError(f"{self.s.name} refused the page tokens; nothing was changed. Ask again in a "
                                    "moment.") from None
                if attempt == 2:
                    raise ToolError(f"{self.s.name} refused the page tokens; try again later") from None

    async def _call(self, name: str, ids: dict[str, str] | None, params: dict | None, form: dict | None,
                    doc: str | None, variables: dict | None, watch: bool) -> Any:
        ep = ENDPOINTS.get(name)
        if ep is None or ep.service != self.s.key:
            raise ToolError(f"refusing an unlisted {self.s.name} request")
        values = {}
        for k, v in (ids or {}).items():
            if not isinstance(v, str) or not ID_RE.fullmatch(v):
                raise ToolError(f"bad {self.s.name} id")
            values[k] = v
        try:
            path = ep.path.format(**values)
        except (KeyError, IndexError):
            raise ToolError(f"refusing an incomplete {self.s.name} request") from None
        cls = ep.cls
        friendly = None
        d = None
        if ep.kind == "graphql":
            d = DOCS[self.s.key].get(doc or "")
            if d is None:
                raise ToolError(f"refusing an unlisted {self.s.name} query")
            if not d.sendable:  # nothing is sent with a made-up doc_id
                raise ToolError(f"this {self.s.name} query ({d.name}) isn't known yet: copy its doc_id from "
                                "DevTools (SETUP §8c-2)")
            cls, friendly = d.cls, d.name
        if allowed(ep.method, ep.host, path, d.doc_id if d else None, friendly) != name:
            raise ToolError(f"refusing {self.s.name} request outside the allow-list")
        self.auth.check()
        cookies, session = self.auth.snapshot()
        tokens: dict = {}
        if ep.kind == "graphql":
            tokens = await self._page_tokens()
            cookies, session = self.auth.snapshot()  # fetching the page may have rotated some
            if tokens["key"] != self._token_key():
                raise ToolError(f"the {self.s.name} session was replaced meanwhile; ask again")
            form = {**_token_form(cookies, tokens), "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": d.name, "variables": json.dumps(variables or {}, separators=(",", ":")),
                    "server_timestamps": "true", "doc_id": d.doc_id}
        headers = {"User-Agent": USER_AGENT, "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
        for k, v in HEADERS[ep.headers].items():
            v = v.replace("{csrf}", cookies["csrftoken"]).replace("{friendly}", friendly or "")
            if "{lsd}" in v:
                v = v.replace("{lsd}", tokens.get("lsd", ""))
            headers[k] = v
        async with self._lock:  # one request at a time, a fixed gap between them
            wait = self._last + self.gap - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self.auth.check()
            if not self.auth.same(session):
                raise ToolError(f"the {self.s.name} session was replaced meanwhile; nothing was sent, ask again")
            await asyncio.to_thread(self.budget.take, cls, watch, self.s.name)
            self._last = self._clock()
            try:
                request = self._client.build_request(ep.method, f"https://{ep.host}{path}", params=params or None,
                                                     data=form or None, headers=headers, timeout=TIMEOUT)
                resp = await self._client.send(request, stream=True, follow_redirects=False)
            except Exception as e:  # never the exception text: it could carry the cookie header
                raise ToolError(f"{self.s.name} unreachable ({type(e).__name__})" + (
                    "" if cls == "read" or isinstance(e, _NOT_SENT) else MAYBE)) from None
            finally:
                self._client.cookies.clear()  # no shared cookie jar: cookies only go in our own header
            try:
                # Status and headers first: a stop signal counts whatever the body is (or can't be read as).
                await self._set_cookies(resp, session)
                status = resp.status_code
                if 300 <= status < 400:
                    await self._stop("redirect", session, cls)  # to /accounts/login, /challenge or anywhere: never followed
                if status in (401, 403):
                    await self._stop(f"http_{status}", session, cls)
                body = await self._body(resp)
            finally:
                await resp.aclose()
        return await self._answer(ep, status, body, cls, d, tokens, session)

    async def _pause(self, reason: str, session: tuple) -> None:
        try:
            if await asyncio.to_thread(self.auth.mark_paused, reason, session):
                self.auth.paused_notice(reason)
        except ToolError:
            log.warning("%s session should be paused (%s), but its file is busy", self.s.name, reason)

    async def _stop(self, reason: str, session: tuple, cls: str = "read") -> None:
        await self._pause(reason, session)
        raise ToolError(self.auth.paused_text() + ("" if cls == "read" else MAYBE))

    async def _body(self, resp: httpx.Response) -> bytes | str:
        """The body, or why there is none (read in pieces: never more than the limit)."""
        out = bytearray()
        try:
            async for chunk in resp.aiter_bytes():
                out += chunk
                if len(out) > MAX_REPLY:
                    return "reply too big"
        except Exception as e:  # noqa: BLE001 - undecodable, or the connection ended
            return f"unreadable reply ({type(e).__name__})"
        return bytes(out)

    async def _set_cookies(self, resp: httpx.Response, session: tuple) -> None:
        for raw in resp.headers.get_list("set-cookie"):
            first, _, attrs = raw.partition(";")
            cname, _, value = first.strip().partition("=")
            value = value.strip().strip('"')
            gone = not value or value == "deleted" or re.search(r"max-age=0\b", attrs, re.I) is not None \
                or re.search(r"expires=thu, 01[- ]jan[- ]1970", attrs, re.I) is not None
            if cname == "sessionid" and gone:
                await self._pause("logged_out", session)
            elif not gone:
                try:
                    await asyncio.to_thread(self.auth.rotate, cname, value, session)
                except ToolError:
                    log.warning("couldn't save a changed %s cookie: its file is busy", self.s.name)

    async def _answer(self, ep: Endpoint, status: int, body: bytes | str, cls: str, d: Doc | None, tokens: dict,
                session: tuple) -> Any:
        name = self.s.name
        maybe = "" if cls == "read" else MAYBE
        if self.auth.same(session) and self.auth.load().get("state") == "paused":  # e.g. its cookie was deleted just now
            raise ToolError(self.auth.paused_text() + maybe)
        if status == 429 and isinstance(body, str):
            await self._slow_down(cls, session)
        if isinstance(body, str):
            raise ToolError(f"{name}: {body}{maybe}")
        text = body.decode("utf-8", "replace")
        if ep.kind == "html":
            if status == 429:
                await self._slow_down(cls, session)
            if status != 200:
                raise ToolError(f"{name} answered {status}")
            return text
        data: Any = None
        stripped = text.removeprefix("for (;;);").strip()
        if stripped[:1] in ("{", "["):
            try:
                data = json.loads(stripped)
            except ValueError:
                data = None
        problem = _problem(status, data, text, ep.kind)
        if problem == "stale_tokens":
            raise _StaleTokens(cls, tokens)
        if ep.kind == "graphql":
            self._stale.clear()  # any other answer: the tokens aren't what's wrong
        if problem in ("challenge", "user_agent"):
            await self._stop(problem, session, cls)
        if problem == "slow_down":
            await self._slow_down(cls, session)
        self._empty = self._empty + 1 if status == 200 and not stripped else 0
        # A page where data belongs: a wall, a changed site. (Marked login pages were stopped above; an ordinary "not
        # found" or a single empty reply is just an error.)
        if data is None and status < 500 and status not in (400, 404) and self._empty != 1:
            await self._stop("not_json", session, cls)
        if data is None:
            raise ToolError(f"{name} answered {status}{maybe}")
        if not isinstance(data, dict):
            raise ToolError(f"{name} sent an unexpected reply (HTTP {status}){maybe}")
        if ep.kind == "graphql":
            return await self._graphql_data(d, data, status, cls, session)
        if status >= 400 or data.get("status") == "fail" or "errorSummary" in data or data.get("error") \
                or data.get("errors"):
            msg = redact(str(data.get("message", "")))[:200]
            raise ToolError(f"{name} error {status}" + (f": {msg}" if msg else "") + (maybe if status >= 500 else ""))
        if cls != "read":
            await self._check_result(data, session)
        if cls != "read" and (not data or _failed(data)):
            raise ToolError(f"{name} didn't confirm the change{maybe}")
        return data

    async def _check_result(self, result: Any, session: tuple) -> None:
        """The stop signals, in a change's own result objects (they hold no one's posts)."""
        for r in _nested(result):
            problem = _problem(200, {k: v for k, v in r.items() if k not in ("summary", "description")}, "", "json")
            if problem in ("challenge", "user_agent"):
                await self._stop(problem, session, "write")
            if problem == "slow_down":
                await self._slow_down("write", session)

    async def _slow_down(self, cls: str, session: tuple) -> None:
        if cls == "read":
            if await asyncio.to_thread(self.auth.mark_cooling, session):
                self.auth.cooling_notice()
            raise ToolError(f"{self.s.name} asked to slow down; no requests for an hour. Don't try another way.")
        await self._stop("feedback_required", session, cls)  # a refused change: stop until the owner looks at the account

    async def _graphql_data(self, d: Doc | None, data: dict, status: int, cls: str, session: tuple) -> Any:
        assert d is not None
        inner = data.get("data")
        if cls != "read":
            # Only a clean, non-empty result counts as done.
            results = list(inner.values()) if isinstance(inner, dict) else []
            await self._check_result(inner, session)
            good = [r for r in results if isinstance(r, dict) and not _failed(r)
                    and any(r.get(k) in ("ok", "success", True) for k in d.success)]
            if status >= 400 or data.get("errors") or data.get("error") or not results or len(good) != len(results) \
                    or (d.root and not inner.get(d.root)):
                raise ToolError(f"{self.s.name} didn't confirm the change (`{d.name}`){MAYBE}")
            return inner
        if status >= 400 or data.get("error") or not isinstance(inner, dict) or not (
                inner.get(d.root) if d.root else inner):
            raise ToolError(f"{self.s.name} changed its web app; the `{d.name}` query needs updating")
        return inner

    def _token_key(self) -> tuple:
        """Page tokens belong to one session: new cookies or a --resume mean fetching them again."""
        d = self.auth.load()
        return (d.get("cookies") or {}).get("sessionid"), identity(d), d.get("resumed_at")

    async def _page_tokens(self) -> dict:
        """fb_dtsg, lsd and jazoest from the owner's logged-in page (fetched again every 30 minutes). One fetch at a
        time; each caller keeps the set it got, so a refresh by another caller can't pull it away."""
        async with self._token_lock:
            t = self._tokens
            key = self._token_key()
            if key != self._stale_key:  # another session: what was refused before says nothing about it
                self._stale.clear()
                self._stale_key = key
            if t and t["key"] == key and self._clock() - t["at"] <= TOKEN_AGE:
                return t
            self._tokens = {}
            session = identity(self.auth.load())
            page = await self._call(HOME[self.s.key], None, None, None, None, None, False)
            if _LOGIN_PAGE.search(page):  # before looking for tokens: login pages carry some too
                await self._stop("login_required", session)
            dtsg = re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]{8,200})"', page)
            lsd = re.search(r'"LSD",\[\],\{"token":"([^"]{4,200})"', page)
            if not dtsg or not lsd:
                # A 200 page without the tokens: stop (paused, so this is never fetched again in a loop), and name a
                # cause only when the page shows it.
                await self._stop("login_required" if any(m in page for m in _LOGIN_MARKERS) else
                                 "user_agent" if _UA_RE.search(page) else "no_page_tokens", session)
            fb_dtsg, lsd_token = dtsg.group(1), lsd.group(1)
            jazoest = "2" + str(sum(ord(c) for c in fb_dtsg))
            for v in (fb_dtsg, lsd_token):
                register_secret(v)
            self._tokens = {"fb_dtsg": fb_dtsg, "lsd": lsd_token, "jazoest": jazoest, "at": self._clock(),
                            "key": self._token_key()}
            self._stale_key = self._tokens["key"]
            return self._tokens


def _token_form(cookies: dict[str, str], tokens: dict) -> dict:
    return {"av": cookies["ds_user_id"], "__user": "0", "__a": "1", "__comet_req": "29",
            "fb_dtsg": tokens["fb_dtsg"], "jazoest": tokens["jazoest"], "lsd": tokens["lsd"]}


def _problem(status: int, data: Any, text: str, kind: str = "json") -> str | None:
    """challenge (pause until resumed), slow_down, user_agent, stale_tokens (GraphQL only, and only when nothing
    else in the reply says stop), or None."""
    entries = _entries(data)
    said = " | ".join(str(e.get(k) or "") for e in entries for k in _TEXT_KEYS).lower()
    codes = {int(c) for e in entries for k in ("code", "error", "error_code", "status_code")
             if isinstance(c := e.get(k), (int, str)) and re.fullmatch(r"\d{1,9}", str(c))}
    if any(c in said for c in _CHALLENGES) or _LOGIN_RE.search(said) or codes & {401, 1357001} \
            or (403 in codes and _AUTH_WORDS.search(said)) or any(
            e.get("challenge") or e.get("checkpoint_url") or e.get("require_login") is True or e.get("logout_reason")
            or e.get("user_has_logged_out") for e in entries):
        return "challenge"
    if _UA_RE.search(said):
        return "user_agent"
    if status in (401, 403):
        return "challenge"
    if status == 429 or 429 in codes or _SLOW_DOWN.search(said) or any(
            e.get("spam") is True for e in entries):
        return "slow_down"
    if kind == "graphql" and 1357004 in codes:
        return "stale_tokens"
    if data is None and _LOGIN_PAGE.search(text):
        return "challenge"
    return None


_TEXT_KEYS = ("message", "summary", "description", "errorSummary", "errorDescription", "error_type", "error", "code",
              "logout_reason")


_WRAPPERS = ("errors", "error", "extensions", "original_error", "originalError", "payload")


def _entries(data: Any) -> list[dict]:
    """The reply itself and what reports an error in it (GraphQL `errors`, `error` objects, their wrappers), not too deep."""
    out: list[dict] = []
    level = [data]
    for _ in range(6):
        following: list = []
        for e in level:
            if isinstance(e, str):
                e = {"message": e}
            if not isinstance(e, dict) or len(out) >= 100:
                continue
            out.append(e)
            for k in _WRAPPERS:
                v = e.get(k)
                following += v if isinstance(v, list) else [v] if isinstance(v, dict) or (k == "errors" and v) else []
        level = following
    return out


def _nested(x: Any, depth: int = 0) -> list[dict]:
    if depth > 5:
        return []
    if isinstance(x, list):
        return [d for v in x[:50] for d in _nested(v, depth + 1)]
    if isinstance(x, dict):
        return [x] + [d for v in x.values() for d in _nested(v, depth + 1)]
    return []


def _failed(result: Any) -> bool:
    """Anything in a change's result that says it didn't work."""
    return any(r.get("status") == "fail" or r.get("success") is False or r.get("error") or r.get("errors")
               for r in _nested(result))


# --- the owner's commands (on the server, as user ampitools) -------------------------------------------------


def session_path(home: Path, service: WebService) -> Path:
    return home / f"{service.key}-session.json"


def usage_path(home: Path, service: WebService) -> Path:
    return home / f"{service.key}-session-usage.json"


def make_client(home: Path, service: WebService, client: httpx.AsyncClient | None = None,
                notify: Callable[[str], None] | None = None) -> WebClient:
    """A dedicated httpx client per service (no shared cookie jar, no redirects)."""
    auth = SessionAuth(service, session_path(home, service), notify)
    return WebClient(service, auth, client or httpx.AsyncClient(follow_redirects=False, timeout=TIMEOUT),
                     usage=usage_path(home, service))


async def verify(c: WebClient) -> dict:
    """Whose session this is: the user behind ds_user_id (never accounts/current_user, which returns email/phone)."""
    uid = c.auth.cookies()["ds_user_id"]
    if c.s.key == "instagram":
        data = await c.call("ig.user_info", ids={"user": uid})
        user = data.get("user") if isinstance(data, dict) else None
    else:
        data = await c.call("th.graphql", doc="profile", variables={"userID": uid})
        user = data.get("user")
    if not isinstance(user, dict) or str(user.get("pk") or user.get("pk_id") or user.get("id") or "") != uid:
        raise ToolError(f"{c.s.name} didn't confirm that the session belongs to user {uid}")
    name = str(user.get("username", ""))
    if not USERNAME_RE.fullmatch(name):
        raise ToolError(f"{c.s.name} didn't say the session's username")
    return {"id": uid, "username": name}


def check_cookie(name: str, value: str) -> str:
    value = value.strip().strip('"')
    if not COOKIE_RE.fullmatch(value):
        raise ToolError(f"{name}: that doesn't look like a cookie value (copy just the value)")
    if name == "ds_user_id" and not ID_RE.fullmatch(value):
        raise ToolError("ds_user_id is a number")
    return value


async def store_session(home: Path, service: WebService, cookies: dict[str, str],
                        client: httpx.AsyncClient | None = None) -> dict:
    """Checks the cookies against the service, then saves them (atomically, 0600). Nothing is kept if it fails."""
    for k, v in cookies.items():
        register_secret(v)
    final = session_path(home, service)
    trial = final.with_name(final.name + ".new")
    auth = SessionAuth(service, trial)
    auth.save({"cookies": cookies, "state": "ok", "saved_at": time.time(), "sid": secrets.token_hex(8)})
    try:
        own = client is None
        http = client or httpx.AsyncClient(follow_redirects=False, timeout=TIMEOUT)
        try:
            c = WebClient(service, auth, http, usage=usage_path(home, service))
            who = await verify(c)
        finally:
            if own:
                await http.aclose()
        data = auth.load()
        if data.get("state") == "paused":
            raise ToolError(service.paused_text)
        data.update(user_id=who["id"], username=who["username"], saved_at=time.time(), state="ok")
        auth.save(data)
        with file_lock(final):
            os.replace(trial, final)
        return data
    finally:
        for p in (trial, trial.with_name(trial.name + ".lock")):
            with contextlib.suppress(OSError):
                p.unlink()


def reader_guard(home: Path) -> None:
    """Refuses until tg-reader (if it's installed) keeps Ampi's own chat out: that chat will hold social text."""
    try:
        import pwd
        pwd.getpwnam(os.environ.get("TG_READER_USER") or "tgreader")
    except (ImportError, KeyError):
        return  # no tg-reader here; one installed later reads nothing before it was told
    why = ("tg-reader doesn't confirm yet that it keeps Ampi's own Telegram chat out. Update it (deploy/install-tg-reader"
           ".sh), then start tg-reader, ampi-tools and claude-assistant ([tools] approvals = true), wait a minute and "
           "run this again.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(25)
            c.connect(os.environ.get("TG_WRITER_SOCKET") or "/run/tg-writer/tg-write.sock")
            c.sendall(b'{"cmd": "own_bots_status"}\n')
            reply = json.loads(c.makefile("rb").readline(65536))
    except (OSError, ValueError):
        raise ToolError(why) from None
    if not (isinstance(reply, dict) and reply.get("ok") and reply.get("active") is True):
        raise ToolError(why)


def session_command(home: Path, service: WebService, resume: bool = False, forget: bool = False,
                    guard: Callable[[Path], None] = reader_guard) -> None:
    auth = SessionAuth(service, session_path(home, service))
    if forget:
        print("Deleted the saved session." if auth.forget() else "No session was saved.")
        return
    if resume:
        try:
            auth.resume()
        except ToolError as e:
            sys.exit(str(e))
        print(f"{service.name} session resumed. If {service.name} asks again, Ampi pauses again.")
        return
    try:
        guard(home)
    except ToolError as e:
        sys.exit(f"Not saved: {e}")
    site = f"https://{service.host}"
    print(f"Connect your {service.name} account through your own logged-in browser session.\n"
          f"Against {service.name}'s terms: the account could be checkpointed or banned (docs/SETUP.md §8c-2).\n")
    print(f"1. In a desktop browser, log in at {site} (yourself; Ampi never sees your password).")
    print(f"2. Open the developer tools (F12) → Application/Storage → Cookies → {site}.")
    print("3. Copy each value below (values are not shown as you paste). Don't log out of that browser afterwards:")
    print("   logging out ends this session too.\n")
    cookies: dict[str, str] = {}
    try:
        for name in REQUIRED_COOKIES:
            cookies[name] = check_cookie(name, getpass.getpass(f"{name}: "))
        for name in OPTIONAL_COOKIES:
            v = getpass.getpass(f"{name} (optional, Enter to skip): ").strip()
            if v:
                cookies[name] = check_cookie(name, v)
    except ToolError as e:
        sys.exit(f"Not saved: {e}")
    try:
        data = asyncio.run(store_session(home, service, cookies))
    except ToolError as e:
        sys.exit(f"Not saved: {redact(e)}")
    print(f"Saved: @{data['username']}. The running service picks it up at once (no restart needed).")
