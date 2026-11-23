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
import getpass
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
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

    def cookies(self) -> dict[str, str]:
        if not self.configured():
            raise ToolError(f"the owner's {self.s.name} session isn't set up (`ampi-tools {self.s.command}` on the "
                            "server)")
        c = self.load()["cookies"]
        out = {k: c[k] for k in (*REQUIRED_COOKIES, *OPTIONAL_COOKIES, "rur") if isinstance(c.get(k), str) and c[k]}
        for v in out.values():
            register_secret(v)
        return out

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
        text = {"user_agent": UA_TEXT, "no_page_tokens": NEUTRAL_TEXT, "stale_tokens": NEUTRAL_TEXT}.get(
            self.load().get("paused_reason"))
        return text.format(name=self.s.name, command=self.s.command) if text else self.s.paused_text

    def pause(self, reason: str) -> None:
        """Until the owner resumes. One Telegram notice, on the change only."""
        d = self.load()
        if not d or d.get("state") == "paused":
            return
        d.update(state="paused", paused_reason=reason, paused_at=self.clock())
        self.save(d)
        log.warning("%s session paused (%s)", self.s.name, reason)
        if self.notify is not None:
            notice = {"user_agent": UA_NOTICE, "no_page_tokens": NEUTRAL_NOTICE,
                      "stale_tokens": NEUTRAL_NOTICE}.get(reason, PAUSE_NOTICE)
            self.notify(notice.format(name=self.s.name, command=self.s.command))

    def cool_down(self) -> None:
        d = self.load()
        if not d or d.get("state") == "paused":
            return
        already = float(d.get("cooldown_until") or 0) > self.clock()
        d["cooldown_until"] = self.clock() + COOLDOWN
        self.save(d)
        log.warning("%s asked to slow down; pausing reads for an hour", self.s.name)
        if self.notify is not None and not already:
            self.notify(COOLDOWN_NOTICE.format(name=self.s.name))

    def rotate(self, name: str, value: str) -> None:
        if name not in ROTATED_COOKIES or not COOKIE_RE.fullmatch(value or ""):
            return
        register_secret(value)
        d = self.load()
        cookies = d.get("cookies") if isinstance(d.get("cookies"), dict) else None
        if cookies is None or cookies.get(name) == value:
            return
        cookies[name] = value
        self.save(d)

    def resume(self) -> None:
        d = self.load()
        if not d:
            raise ToolError(f"no {self.s.name} session saved")
        d.update(state="ok", cooldown_until=0, resumed_at=self.clock())  # page tokens are fetched again
        d.pop("paused_reason", None)
        self.save(d)

    def forget(self) -> bool:
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
        if path is not None:
            try:
                raw = json.loads(path.read_text())
                self.hits.update({k: [float(t) for t in v] for k, v in raw.items() if k in LIMITS})
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def _count(self, kind: str, window: float) -> int:
        now = self.clock()
        return sum(1 for t in self.hits[kind] if now - t < window)

    def take(self, cls: str, watch: bool, name: str) -> None:
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


_CHALLENGES = ("checkpoint_required", "challenge_required", "login_required")
_LOGIN_MARKERS = ("/accounts/login", '"is_logged_in":false', 'id="loginForm"')
# Whole phrases only: "This link has expired" or "Blog in maintenance" are not about the login.
_LOGIN_RE = re.compile(r"\bnot logged in\b|\blog ?in (?:is )?required\b|\bplease log ?in\b|\b(?:login|session) (?:has )?"
                       r"expired\b|\byou must log ?in\b|\blog in to continue\b", re.I)
HOME = {"instagram": "ig.home", "threads": "th.home"}


class _StaleTokens(Exception):
    """The page tokens (fb_dtsg/lsd) were refused (Meta error 1357004 on a GraphQL request)."""

    def __init__(self, cls: str, tokens: dict):
        super().__init__(cls)
        self.cls = cls
        self.tokens = tokens
_SLOW_DOWN = ("feedback_required", "please wait a few minutes", "rate_limit")


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
                    self.auth.pause("stale_tokens")
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
        cookies = self.auth.cookies()
        tokens: dict = {}
        if ep.kind == "graphql":
            tokens = await self._page_tokens()
            cookies = self.auth.cookies()  # fetching the page may have rotated some
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
            self.auth.check()
            self.budget.take(cls, watch, self.s.name)
            wait = self._last + self.gap - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last = self._clock()
            try:
                resp = await self._client.request(ep.method, f"https://{ep.host}{path}", params=params or None,
                                                  data=form or None, headers=headers, timeout=TIMEOUT,
                                                  follow_redirects=False)
                body = resp.content
            except Exception as e:  # never the exception text: it could carry the cookie header
                raise ToolError(f"{self.s.name} unreachable ({type(e).__name__})") from None
            finally:
                self._client.cookies.clear()  # no shared cookie jar: cookies only go in our own header
        self._set_cookies(resp)
        if len(body) > MAX_REPLY:
            raise ToolError(f"{self.s.name}: reply too big")
        return self._answer(name, ep, resp, body, cls, d, tokens)

    def _set_cookies(self, resp: httpx.Response) -> None:
        for raw in resp.headers.get_list("set-cookie"):
            first, _, attrs = raw.partition(";")
            cname, _, value = first.strip().partition("=")
            value = value.strip().strip('"')
            gone = not value or value == "deleted" or re.search(r"max-age=0\b", attrs, re.I) is not None \
                or re.search(r"expires=thu, 01[- ]jan[- ]1970", attrs, re.I) is not None
            if cname == "sessionid" and gone:
                self.auth.pause("logged_out")
            elif not gone:
                self.auth.rotate(cname, value)

    def _answer(self, name: str, ep: Endpoint, resp: httpx.Response, body: bytes, cls: str, d: Doc | None,
                tokens: dict) -> Any:
        status = resp.status_code
        if 300 <= status < 400:
            self.auth.pause("redirect")  # to /accounts/login, /challenge or anywhere: never followed
            raise ToolError(self.s.paused_text)
        if self.auth.load().get("state") == "paused":  # e.g. the session cookie was deleted just now
            raise ToolError(self.s.paused_text)
        text = body.decode("utf-8", "replace")
        if ep.kind == "html":
            if status in (401, 403):
                self.auth.pause(f"http_{status}")
                raise ToolError(self.s.paused_text)
            if status == 429:
                self._slow_down(cls)
            if status != 200:
                raise ToolError(f"{self.s.name} answered {status}")
            return text
        data: Any = None
        stripped = text.removeprefix("for (;;);").strip()
        if stripped[:1] in ("{", "["):
            try:
                data = json.loads(stripped)
            except ValueError:
                data = None
        problem = _problem(status, data, text, ep.kind)
        if problem == "challenge":
            self.auth.pause("challenge" if status not in (401, 403) else f"http_{status}")
            raise ToolError(self.s.paused_text)
        if problem == "stale_tokens":
            raise _StaleTokens(cls, tokens)
        if problem == "user_agent":
            self.auth.pause("user_agent")
            raise ToolError(UA_TEXT.format(name=self.s.name, command=self.s.command))
        if problem == "slow_down":
            self._slow_down(cls)
        if data is None:
            raise ToolError(f"{self.s.name} sent an unexpected reply (HTTP {status})")
        if ep.kind == "graphql":
            inner = self._graphql_data(d, data, status)
            self._stale.clear()
            return inner
        if status >= 400 or (isinstance(data, dict) and (data.get("status") == "fail" or "errorSummary" in data)):
            msg = redact(str(data.get("message", "")) if isinstance(data, dict) else "")[:200]
            raise ToolError(f"{self.s.name} error {status}" + (f": {msg}" if msg else ""))
        return data

    def _slow_down(self, cls: str) -> None:
        if cls == "read":
            self.auth.cool_down()
            raise ToolError(f"{self.s.name} asked to slow down; no requests for an hour. Don't try another way.")
        self.auth.pause("feedback_required")  # a refused change: stop until the owner looks at the account
        raise ToolError(self.s.paused_text)

    def _graphql_data(self, d: Doc | None, data: Any, status: int) -> Any:
        assert d is not None
        changed = f"{self.s.name} changed its web app; the `{d.name}` query needs updating"
        if not isinstance(data, dict):
            raise ToolError(changed)
        inner = data.get("data")
        if not isinstance(inner, dict) or (d.root and inner.get(d.root) is None):
            raise ToolError(changed)
        return inner

    def _token_key(self, cookies: dict[str, str]) -> tuple:
        """Page tokens belong to one session: new cookies or a --resume mean fetching them again."""
        d = self.auth.load()
        return cookies.get("sessionid"), d.get("saved_at"), d.get("resumed_at")

    async def _page_tokens(self) -> dict:
        """fb_dtsg, lsd and jazoest from the owner's logged-in page (fetched again every 30 minutes). One fetch at a
        time; each caller keeps the set it got, so a refresh by another caller can't pull it away."""
        async with self._token_lock:
            t = self._tokens
            if t and t["key"] == self._token_key(self.auth.cookies()) and self._clock() - t["at"] <= TOKEN_AGE:
                return t
            self._tokens = {}
            page = await self._call(HOME[self.s.key], None, None, None, None, None, False)
            dtsg = re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]{8,200})"', page)
            lsd = re.search(r'"LSD",\[\],\{"token":"([^"]{4,200})"', page)
            if not dtsg or not lsd:
                # A 200 page without the tokens: stop (paused, so this is never fetched again in a loop), and name a
                # cause only when the page shows it.
                self.auth.pause("login_required" if any(m in page for m in _LOGIN_MARKERS) else
                                "user_agent" if re.search(r"user ?agent mismatch", page, re.I) else "no_page_tokens")
                raise ToolError(self.auth.paused_text())
            fb_dtsg, lsd_token = dtsg.group(1), lsd.group(1)
            jazoest = "2" + str(sum(ord(c) for c in fb_dtsg))
            for v in (fb_dtsg, lsd_token):
                register_secret(v)
            self._tokens = {"fb_dtsg": fb_dtsg, "lsd": lsd_token, "jazoest": jazoest, "at": self._clock(),
                            "key": self._token_key(self.auth.cookies())}
            return self._tokens


def _token_form(cookies: dict[str, str], tokens: dict) -> dict:
    return {"av": cookies["ds_user_id"], "__user": "0", "__a": "1", "__comet_req": "29",
            "fb_dtsg": tokens["fb_dtsg"], "jazoest": tokens["jazoest"], "lsd": tokens["lsd"]}


def _problem(status: int, data: Any, text: str, kind: str = "json") -> str | None:
    """challenge (pause until resumed), slow_down, user_agent, stale_tokens (GraphQL only, and only when nothing
    else in the reply says stop), or None."""
    if isinstance(data, dict):
        msg = str(data.get("message") or "").lower()
        said = " | ".join(str(data.get(k) or "") for k in ("message", "errorSummary", "errorDescription"))
        if any(c in msg for c in _CHALLENGES) or data.get("challenge") or data.get("checkpoint_url") \
                or data.get("require_login") is True or data.get("error") == 1357001 or _LOGIN_RE.search(said):
            return "challenge"
        if "useragent" in msg or "user agent" in msg:
            return "user_agent"
    if status in (401, 403):
        return "challenge"
    if status == 429:
        return "slow_down"
    if isinstance(data, dict):
        if any(c in msg for c in _SLOW_DOWN) or data.get("spam") is True:
            return "slow_down"
        if kind == "graphql" and data.get("error") == 1357004:
            return "stale_tokens"
    if data is None and ("/accounts/login" in text or "/challenge/" in text):
        return "challenge"
    return None


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
    auth.save({"cookies": cookies, "state": "ok", "saved_at": time.time()})
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
        os.replace(trial, final)
        return data
    finally:
        try:
            trial.unlink()
        except FileNotFoundError:
            pass


def session_command(home: Path, service: WebService, resume: bool = False, forget: bool = False) -> None:
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
