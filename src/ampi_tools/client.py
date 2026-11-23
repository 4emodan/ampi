"""`todoist`, `gmail`, `youtube`, `threads`, `instagram` and `browse`: the assistant's commands (standard
library only). `browse` talks to the sandboxed ampi-browser service instead of the tools daemon.

They send operations to the ampi-tools daemon, which holds the tokens. Critical operations come back
as "waiting for the owner's approval" instead of running.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.parse
from pathlib import Path

from tg_reader.client import deliver

DEFAULT_SOCKET = "/run/ampi-tools/tools.sock"


def request(payload: dict, path: str | None = None, timeout: float = 300) -> dict:
    path = path or os.environ.get("AMPI_TOOLS_SOCKET") or DEFAULT_SOCKET
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            return {"ok": False, "error": "the tools service isn't running (ampi-tools on the server)"}
        except PermissionError:
            return {"ok": False, "error": f"no permission to use {path}"}
        try:
            s.sendall(json.dumps(payload).encode() + b"\n")
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        except OSError as e:
            return {"ok": False, "error": f"the tools service didn't answer ({type(e).__name__})"}
    try:
        reply = json.loads(b"".join(chunks))
    except ValueError:
        return {"ok": False, "error": "the tools service sent a broken reply"}
    return reply if isinstance(reply, dict) else {"ok": False, "error": "the tools service sent a broken reply"}


def workspace_file(name: str, max_bytes: int = 200_000) -> str:
    """Reads a file the assistant wrote in its workspace (the current directory) and nothing else:
    no absolute paths, no `..`, no symlinks, no hidden files, so these commands can't be used to read secrets."""
    root = Path.cwd().resolve()
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or any(part.startswith(".") for part in p.parts):
        raise SystemExit(f"{name!r}: use a file in your working directory (relative path, no '..' or hidden files)")
    full = root / p
    if full.is_symlink() or not full.resolve().is_relative_to(root) or not full.is_file():
        raise SystemExit(f"{name!r} is not a regular file in your working directory")
    if full.stat().st_size > max_bytes:
        raise SystemExit(f"{name!r} is too big")
    return full.read_text(encoding="utf-8")


def _body(args: argparse.Namespace) -> str | None:
    if getattr(args, "body_file", None):
        return workspace_file(args.body_file)
    return getattr(args, "body", None)


def _load_plan(path: str, prefix: str) -> list[dict]:
    try:
        plan = json.loads(workspace_file(path))
    except ValueError as e:
        raise SystemExit(f"can't read plan {path}: {e}")
    if isinstance(plan, dict):
        plan = plan.get("ops", [])
    if not isinstance(plan, list) or not all(isinstance(i, dict) for i in plan):
        raise SystemExit("a plan is a JSON list of {\"op\": ..., ...} objects")
    ops = []
    for item in plan:
        name = item.get("op", "")
        if "." not in name:
            name = f"{prefix}.{name.replace('-', '_')}"
        ops.append({"op": name, "args": item.get("args") or {k: v for k, v in item.items() if k != "op"}})
    return ops


# --- todoist ------------------------------------------------------------------------------


def todoist_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="todoist", description="The owner's Todoist.")
    p.add_argument("-m", "--reason", help="why (shown to the owner with approval requests)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("tasks", help="active tasks")
    s.add_argument("-f", "--filter", help='Todoist filter, e.g. "today | overdue", "#Inbox", "@waiting"')
    s.add_argument("-p", "--project")
    s.add_argument("-n", "--limit", type=int, default=200)
    sub.add_parser("task").add_argument("id")
    sub.add_parser("projects")
    sub.add_parser("labels")
    s = sub.add_parser("completed")
    s.add_argument("--since", help="YYYY-MM-DDTHH:MM:SSZ (default: 7 days ago)")
    s.add_argument("--until")

    def task_fields(sp: argparse.ArgumentParser, content_flag: bool) -> None:
        if content_flag:
            sp.add_argument("--content")
        sp.add_argument("-d", "--description")
        sp.add_argument("--due", help='natural language, e.g. "tomorrow 9am", "every monday"; "none" removes it')
        sp.add_argument("--deadline", help="YYYY-MM-DD")
        sp.add_argument("--priority", help="p1 (urgent) .. p4")
        sp.add_argument("-l", "--label", action="append", dest="labels", help="repeat for several")

    s = sub.add_parser("add", help="add a task")
    s.add_argument("content")
    s.add_argument("-p", "--project")
    s.add_argument("-s", "--section")
    s.add_argument("--parent", help="parent task id (makes a subtask)")
    task_fields(s, False)
    s = sub.add_parser("comment")
    s.add_argument("id")
    s.add_argument("text")
    s = sub.add_parser("new-project")
    s.add_argument("name")
    s.add_argument("--parent")
    s = sub.add_parser("new-section")
    s.add_argument("project")
    s.add_argument("name")
    sub.add_parser("new-label").add_argument("name")

    s = sub.add_parser("update", help="change a task (needs approval)")
    s.add_argument("id")
    task_fields(s, True)
    s.add_argument("--no-labels", action="store_true", help="remove all labels")
    s = sub.add_parser("move", help="move tasks (needs approval)")
    s.add_argument("ids", nargs="+")
    s.add_argument("-p", "--project", required=True)
    s.add_argument("-s", "--section")
    for name in ("complete", "reopen", "delete"):
        sub.add_parser(name, help="needs approval").add_argument("ids", nargs="+")
    sub.add_parser("apply", help="run a JSON plan of operations as one request").add_argument("file")
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def todoist_ops(a: argparse.Namespace) -> list[dict]:
    fields = {k: getattr(a, k, None) for k in ("content", "description", "due", "deadline", "priority", "labels")}
    if getattr(a, "no_labels", False):
        fields["labels"] = []
    fields = {k: v for k, v in fields.items() if v is not None}
    c = a.cmd
    if c == "tasks":
        return [{"op": "todoist.tasks", "args": {"filter": a.filter, "project": a.project, "limit": a.limit}}]
    if c in ("task", "projects", "labels"):
        return [{"op": f"todoist.{c}", "args": {"id": getattr(a, "id", None)}}]
    if c == "completed":
        return [{"op": "todoist.completed", "args": {"since": a.since, "until": a.until}}]
    if c == "add":
        return [{"op": "todoist.add", "args": {**fields, "content": a.content, "project": a.project,
                                                "section": a.section, "parent": a.parent}}]
    if c == "comment":
        return [{"op": "todoist.comment", "args": {"id": a.id, "text": a.text}}]
    if c == "new-project":
        return [{"op": "todoist.add_project", "args": {"name": a.name, "project": a.parent}}]
    if c == "new-section":
        return [{"op": "todoist.add_section", "args": {"project": a.project, "name": a.name}}]
    if c == "new-label":
        return [{"op": "todoist.add_label", "args": {"name": a.name}}]
    if c == "update":
        return [{"op": "todoist.update", "args": {"id": a.id, **fields}}]
    if c == "move":
        return [{"op": "todoist.move", "args": {"id": i, "project": a.project, "section": a.section}} for i in a.ids]
    if c in ("complete", "reopen", "delete"):
        return [{"op": f"todoist.{c}", "args": {"id": i}} for i in a.ids]
    if c == "apply":
        return _load_plan(a.file, "todoist")
    raise SystemExit(f"unknown command {c}")


# --- gmail --------------------------------------------------------------------------------


def gmail_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gmail", description="The owner's Gmail.")
    p.add_argument("-m", "--reason", help="why (shown to the owner with approval requests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help='Gmail search syntax, e.g. "in:inbox is:unread", "from:bank newer_than:7d"')
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=20)
    sub.add_parser("read").add_argument("id")
    sub.add_parser("thread").add_argument("id")
    sub.add_parser("labels")
    sub.add_parser("drafts")
    s = sub.add_parser("label", help="add/remove labels (missing labels are created)")
    s.add_argument("ids", nargs="+")
    s.add_argument("--add", action="append", default=[])
    s.add_argument("--remove", action="append", default=[])
    for name in ("archive", "mark-read", "mark-unread", "star", "unstar", "untrash"):
        sub.add_parser(name).add_argument("ids", nargs="+")
    for name, help_text in (("draft", "save a draft (safe)"), ("send", "send an email (needs approval)")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--to")
        s.add_argument("--cc")
        s.add_argument("--subject")
        s.add_argument("--body")
        s.add_argument("--body-file", help="read the body from this file (UTF-8)")
        s.add_argument("--reply-to", help="message id to reply to (keeps the thread)")
    sub.add_parser("send-draft", help="needs approval").add_argument("id")
    sub.add_parser("trash", help="needs approval").add_argument("ids", nargs="+")
    sub.add_parser("delete-label", help="needs approval").add_argument("name")
    sub.add_parser("apply", help="run a JSON plan of operations as one request").add_argument("file")
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def gmail_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd
    if c == "search":
        return [{"op": "gmail.search", "args": {"query": a.query, "limit": a.limit}}]
    if c in ("read", "thread", "send-draft"):
        return [{"op": f"gmail.{c.replace('-', '_')}", "args": {"id": a.id}}]
    if c in ("labels", "drafts"):
        return [{"op": f"gmail.{c}", "args": {}}]
    if c == "label":
        return [{"op": "gmail.label", "args": {"ids": a.ids, "add": a.add, "remove": a.remove}}]
    if c in ("archive", "mark-read", "mark-unread", "star", "unstar", "untrash", "trash"):
        return [{"op": f"gmail.{c.replace('-', '_')}", "args": {"ids": a.ids}}]
    if c in ("draft", "send"):
        args = {"to": a.to, "cc": a.cc, "subject": a.subject, "body": _body(a), "reply_to": a.reply_to}
        return [{"op": f"gmail.{c}", "args": {k: v for k, v in args.items() if v is not None}}]
    if c == "delete-label":
        return [{"op": "gmail.delete_label", "args": {"name": a.name}}]
    if c == "apply":
        return _load_plan(a.file, "gmail")
    raise SystemExit(f"unknown command {c}")


# --- youtube ------------------------------------------------------------------------------


def youtube_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="youtube", description="YouTube (public data and the owner's account).")
    p.add_argument("-m", "--reason", help="why (shown to the owner with approval requests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search YouTube (expensive: at most ~30 a day)")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--order", choices=["relevance", "date", "viewCount", "rating"])
    s.add_argument("--days", type=int, help="only from the last N days")
    s.add_argument("--channel", help="only this channel (@handle, link or UC… id)")
    s.add_argument("--type", choices=["video", "channel", "playlist"])
    sub.add_parser("video", help="details of videos (links or ids)").add_argument("videos", nargs="+")
    s = sub.add_parser("comments", help="top comments of a video")
    s.add_argument("video")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--order", choices=["relevance", "time"])
    sub.add_parser("channel", help="channel info and latest uploads").add_argument("channel")
    s = sub.add_parser("uploads", help="a channel's latest uploads")
    s.add_argument("channel")
    s.add_argument("-n", "--limit", type=int, default=15)
    s.add_argument("--shorts", action="store_true", help="include Shorts")
    s = sub.add_parser("playlist", help="videos in a playlist")
    s.add_argument("playlist", nargs="?", help="playlist link or id")
    s.add_argument("--name", help="one of the owner's playlists, by name")
    s.add_argument("-n", "--limit", type=int, default=50)
    sub.add_parser("playlists", help="the owner's playlists")
    sub.add_parser("subscriptions", help="the owner's subscriptions")
    sub.add_parser("liked", help="videos the owner liked").add_argument("-n", "--limit", type=int, default=20)
    s = sub.add_parser("feed", help="recent uploads from the owner's subscriptions or given channels")
    s.add_argument("--channel", action="append", help="repeat for several (default: all subscriptions)")
    s.add_argument("--days", type=int, default=2)
    s.add_argument("--shorts", action="store_true")
    s.add_argument("-n", "--limit", type=int, default=100)
    sub.add_parser("new-playlist", help="create a private playlist").add_argument("name")
    for name, help_text in (("add", "add videos to one of the owner's playlists (asks if it isn't private)"),
                            ("remove", "remove videos from a playlist (needs approval)")):
        s = sub.add_parser(name, help=help_text)
        target = s.add_mutually_exclusive_group(required=True)
        target.add_argument("--playlist", help="playlist link or id")
        target.add_argument("--name", help="playlist name")
        s.add_argument("videos", nargs="+")
    sub.add_parser("subscribe", help="needs approval").add_argument("channel")
    sub.add_parser("unsubscribe", help="needs approval").add_argument("channel")
    sub.add_parser("apply", help="run a JSON plan of operations as one request").add_argument("file")
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def youtube_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd

    def op(name: str, **args: object) -> list[dict]:
        return [{"op": f"youtube.{name}", "args": {k: v for k, v in args.items() if v not in (None, False, [])}}]

    if c == "search":
        return op("search", query=a.query, limit=a.limit, order=a.order, days=a.days, channel=a.channel, type=a.type)
    if c == "video":
        return op("video", videos=a.videos)
    if c == "comments":
        return op("comments", video=a.video, limit=a.limit, order=a.order)
    if c == "channel":
        return op("channel", channel=a.channel)
    if c == "uploads":
        return op("uploads", channel=a.channel, limit=a.limit, shorts=a.shorts)
    if c == "playlist":
        if not a.playlist and not a.name:
            raise SystemExit("youtube playlist: give a playlist link/id, or --name NAME")
        return op("playlist", playlist=a.playlist, playlist_name=a.name, limit=a.limit)
    if c in ("playlists", "subscriptions"):
        return op(c)
    if c == "liked":
        return op("liked", limit=a.limit)
    if c == "feed":
        return op("feed", source=",".join(a.channel) if a.channel else None, days=a.days, shorts=a.shorts,
                  limit=a.limit)
    if c == "new-playlist":
        return op("new_playlist", name=a.name)
    if c in ("add", "remove"):
        return op(c, playlist=a.playlist, playlist_name=a.name, videos=a.videos)
    if c in ("subscribe", "unsubscribe"):
        return op(c, channel=a.channel)
    if c == "apply":
        return _load_plan(a.file, "youtube")
    raise SystemExit(f"unknown command {c}")


# --- maps ------------------------------------------------------------------------------------


def maps_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maps", description="Google Maps (places, routes, weather).")
    p.add_argument("-m", "--reason", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="find places (a summary with Google Maps links)")
    s.add_argument("query")
    s.add_argument("--near", help="address, place or lat,lng to search around")
    s.add_argument("--radius", type=int, help="meters around --near lat,lng")
    s.add_argument("-n", "--limit", type=int, default=5, help="1-10 (more than 5 uses the small daily Pro budget)")
    s.add_argument("--details", action="store_true", help="reviews and more detail (Pro budget)")
    s = sub.add_parser("route", help="travel time and distance")
    s.add_argument("origin")
    s.add_argument("destination")
    s.add_argument("--mode", choices=["drive", "walk", "bicycle", "transit"], default="drive")
    s.add_argument("--via", action="append", help="a stop on the way (repeat; not for transit)")
    t = s.add_mutually_exclusive_group()
    t.add_argument("--depart", help="HH:MM or YYYY-MM-DD HH:MM (owner's time zone)")
    t.add_argument("--arrive", help="HH:MM or YYYY-MM-DD HH:MM")
    s.add_argument("--alternatives", action="store_true")
    s = sub.add_parser("weather", help="weather (and air quality / pollen) at a place")
    s.add_argument("place")
    s.add_argument("--date", help="YYYY-MM-DD (up to 9 days ahead)")
    s.add_argument("--hour", type=int, help="0-23, with --date")
    s.add_argument("--air", action="store_true", help="air quality too")
    s.add_argument("--pollen", action="store_true", help="pollen too (Pro budget)")
    sub.add_parser("place", help="what Google Maps links point at").add_argument("links", nargs="+")
    s = sub.add_parser("link", help="a Google Maps link to search or get directions (no API needed)")
    s.add_argument("query", nargs="?")
    s.add_argument("--from", dest="origin")
    s.add_argument("--to", dest="destination")
    s.add_argument("--mode", choices=["drive", "walk", "bicycle", "transit"])
    sub.add_parser("status", help="setup and today's budget")
    return p


def maps_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd
    if c == "search":
        return _op("maps.search", query=a.query, near=a.near, radius=a.radius, limit=a.limit, details=a.details)
    if c == "route":
        return _op("maps.route", origin=a.origin, destination=a.destination, mode=a.mode, via=a.via,
                   depart=a.depart, arrive=a.arrive, alternatives=a.alternatives)
    if c == "weather":
        return _op("maps.weather", place=a.place, date=a.date, hour=a.hour, air=a.air, pollen=a.pollen)
    if c == "place":
        return _op("maps.place", links=a.links)
    if c == "link":
        if not a.query and not (a.origin and a.destination):
            raise SystemExit("maps link: give a search text, or --from and --to")
        return _op("maps.link", query=a.query, origin=a.origin, destination=a.destination, mode=a.mode)
    if c == "status":
        return _op("maps.status")
    raise SystemExit(f"unknown command {c}")


def maps_main(argv: list[str] | None = None) -> int:
    return _main("maps", maps_parser(), maps_ops, argv)


# --- wolt ------------------------------------------------------------------------------------


def wolt_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wolt", description="Wolt: venues, menus, the owner's orders and baskets.")
    p.add_argument("-m", "--reason", help="why (shown to the owner if a request needs approval)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("venues", help="restaurants and shops delivering near the owner")
    s.add_argument("query", nargs="?", help="e.g. sushi, pizza, pharmacy")
    s.add_argument("--near", help="lat,lng (default: the owner's home area)")
    s.add_argument("--at", help="one of the owner's places by name, e.g. work (instead of --near)")
    s.add_argument("--open", action="store_true", help="only venues taking orders now")
    s.add_argument("-n", "--limit", type=int, default=15)
    s = sub.add_parser("dishes", help="search dishes and products across venues")
    s.add_argument("query")
    s.add_argument("--near")
    s.add_argument("--at", help="one of the owner's places by name, e.g. work")
    s.add_argument("-n", "--limit", type=int, default=20)
    s = sub.add_parser("venue", help="hours, open now, delivery estimate, minimum order, fees")
    s.add_argument("venue", help="slug or wolt.com link")
    s.add_argument("--near")
    s.add_argument("--at", help="one of the owner's places by name, e.g. work")
    s = sub.add_parser("menu", help="a venue's menu (shops: --search)")
    s.add_argument("venue")
    s.add_argument("--search")
    s = sub.add_parser("item", help="one item with all its choices")
    s.add_argument("venue")
    s.add_argument("item")
    sub.add_parser("orders", help="the owner's recent orders").add_argument("-n", "--limit", type=int, default=10)
    sub.add_parser("order", help="status of one order").add_argument("order_id")
    s = sub.add_parser("basket", help="put items into the owner's Wolt basket (they check out in the app)")
    s.add_argument("venue")
    s.add_argument("-f", "--file", required=True, help='JSON list: [{"id": ITEM_ID, "count": 1, "options": {...}}]')
    s.add_argument("--replace", action="store_true", help="replace an existing basket there (asks the owner)")
    s.add_argument("--near", help="lat,lng of the delivery area (default: the owner's home area)")
    s.add_argument("--at", help="deliver to one of the owner's places by name, e.g. work")
    sub.add_parser("status", help="setup")
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def wolt_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd
    near = getattr(a, "near", None)
    at = getattr(a, "at", None)
    if near and at:
        raise SystemExit("wolt: give --near or --at, not both")
    if c == "venues":
        return _op("wolt.venues", query=a.query, lat_lng=near, place=at, open=a.open, limit=a.limit)
    if c == "dishes":
        return _op("wolt.dishes", query=a.query, lat_lng=near, place=at, limit=a.limit)
    if c == "venue":
        return _op("wolt.venue", venue=a.venue, lat_lng=near, place=at)
    if c == "menu":
        return _op("wolt.menu", venue=a.venue, search=a.search)
    if c == "item":
        return _op("wolt.item", venue=a.venue, item=a.item)
    if c == "orders":
        return _op("wolt.orders", limit=a.limit)
    if c == "order":
        return _op("wolt.order", order_id=a.order_id)
    if c == "basket":
        try:
            items = json.loads(workspace_file(a.file, 20_000))
        except ValueError:
            raise SystemExit(f"wolt basket: {a.file} isn't valid JSON") from None
        return _op("wolt.basket_replace" if a.replace else "wolt.basket", venue=a.venue, items=items, lat_lng=near,
                   place=at)
    if c == "status":
        return _op("wolt.status")
    raise SystemExit(f"unknown command {c}")


def wolt_main(argv: list[str] | None = None) -> int:
    return _main("wolt", wolt_parser(), wolt_ops, argv)


# --- personal info, checkout, payment -----------------------------------------------------------

WAIT_DEFAULT = 540  # seconds; the skill runs these with the Bash tool's 600000 ms timeout


def _wait_for(approval: int, wait: float, poll: float = 3.0) -> dict:
    """Waits for the owner's answer to request #approval; returns the final status response."""
    deadline = time.monotonic() + wait
    while True:
        st = request({"cmd": "approvals.status", "id": approval})
        if not st.get("ok") or st.get("status") not in ("pending", "approved"):
            return st
        # Approved means it's running now (e.g. filling and paying): give it up to 50 s more, but stay inside
        # the Bash tool's 600 s limit.
        if time.monotonic() >= deadline + (50 if st.get("status") == "approved" else 0):
            return {"ok": True, "status": st.get("status"), "text": ""}
        time.sleep(poll)


def _run_waiting(prog: str, ops: list[dict], reason: str | None, wait: float, collect: bool = False) -> int:
    response = request({"cmd": "run", "ops": ops, "reason": reason, "origin": os.environ.get("AMPI_ORIGIN") or None})
    if not response.get("ok"):
        print(f"{prog}: {response.get('error', 'failed')}", file=sys.stderr)
        return 1
    approval = response.get("approval")
    if not approval:
        print(deliver(response.get("text", "") or "Done.", None, f"{prog}-run", save_dir=Path(prog)))
        return 0
    if wait <= 0:
        print(response.get("text", ""))
        return 0
    print(f"Waiting for the owner's answer to request #{approval} (up to {int(wait)} s)…", flush=True)
    st = _wait_for(approval, wait)
    status = st.get("status")
    if status == "approved":
        print(f"The owner approved request #{approval} and it's still running. Check with `{prog} status {approval}` "
              "in a minute; don't start it again.")
        return 0
    if status == "pending":
        print(f"No answer yet. The request (#{approval}) stays open for a while; check later with "
              f"`{prog} status {approval}`. Don't ask again.")
        return 0
    if status in ("rejected", "expired"):
        print(f"The owner {'rejected' if status == 'rejected' else 'did not answer'} request #{approval}; "
              "nothing was done.")
        return 1
    print(f"Request #{approval}: {status}. {st.get('text', '')}".strip())
    if collect and status == "done":
        got = request({"cmd": "run", "ops": [{"op": "info.collect", "args": {"approval": approval}}],
                       "origin": os.environ.get("AMPI_ORIGIN") or None})
        print(got.get("text") if got.get("ok") else f"{prog}: {got.get('error')}")
    return 0 if status == "done" else 1


def _status(prog: str, approval: int) -> int:
    st = request({"cmd": "approvals.status", "id": approval})
    print(f"#{approval}: {st.get('status')} {st.get('text', '')}" if st.get("ok") else f"{prog}: {st.get('error')}")
    return 0


def info_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="info", description="The owner's personal details (approval needed to use them).")
    p.add_argument("-m", "--reason", help="why (shown to the owner)")
    p.add_argument("--wait", type=int, default=WAIT_DEFAULT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fields", help="the entries (me, home, work, cards…), masked, and which sites may fill them")
    sub.add_parser("reveal", help="see values, e.g. me.email work.address.full (asks the owner every time; never "
                   "cards, locations or timezones)").add_argument("fields", nargs="+")
    sub.add_parser("revoke", help="remove a site permission (or all)").add_argument("site")
    s = sub.add_parser("save-place", help="save a named address for the owner (asks them), e.g. gym, mom, school")
    s.add_argument("entry", help="short name: lowercase letters, digits and -, e.g. gym")
    s.add_argument("--street", required=True)
    s.add_argument("--line2")
    s.add_argument("--postal")
    s.add_argument("--city", required=True)
    s.add_argument("--region")
    s.add_argument("--country", required=True, help="two letters, e.g. CY")
    s.add_argument("--company", help="for a workplace")
    sub.add_parser("status", help="answer to a request").add_argument("approval", type=int)
    return p


def info_main(argv: list[str] | None = None) -> int:
    a = info_parser().parse_args(argv)
    if a.cmd == "reveal":
        return _run_waiting("info", _op("info.reveal", fields=a.fields), a.reason, a.wait, collect=True)
    if a.cmd == "status":
        return _status("info", a.approval)
    if a.cmd == "save-place":
        return _run_waiting("info", _op("info.save_place", entry=a.entry, street=a.street, line2=a.line2,
                                        postal=a.postal, city=a.city, region=a.region, country=a.country,
                                        company=a.company), a.reason, a.wait)
    ops = _op("info.fields") if a.cmd == "fields" else _op("info.revoke", site=a.site)
    return _run_waiting("info", ops, a.reason, 0)


def _fill_pairs(pairs: list[str]) -> list[dict]:
    out = []
    for p in pairs:
        ref, sep, field = p.partition("=")
        if not sep:
            raise SystemExit(f"use REF=FIELD, e.g. f0:12=email (got {p!r})")
        out.append({"ref": ref.strip(), "field": field.strip()})
    return out


def checkout_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="checkout", description="The checkout browser (one session; read the skill).")
    p.add_argument("-m", "--reason", help="why (shown to the owner)")
    p.add_argument("--wait", type=int, default=WAIT_DEFAULT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("open", help="start a session at a URL (or go to another URL)").add_argument("url")
    sub.add_parser("snapshot", help="the page: text and numbered elements")
    sub.add_parser("click", help="click an element").add_argument("ref")
    s = sub.add_parser("type", help="type your own text into a field (never personal data or card details)")
    s.add_argument("ref")
    s.add_argument("text")
    s = sub.add_parser("choose", help="pick an option in a select")
    s.add_argument("ref")
    s.add_argument("option")
    sub.add_parser("back")
    sub.add_parser("close")
    sub.add_parser("fill", help="fill personal details by reference: REF=FIELD …").add_argument("pairs", nargs="+")
    s = sub.add_parser("pay", help="fill the card (and details) and press pay, after the owner approves")
    s.add_argument("pairs", nargs="+", help="REF=FIELD, e.g. f2:3=card.number f2:5=card.exp f2:7=card.cvc")
    s.add_argument("--button", required=True, help="the pay button's REF")
    s.add_argument("--amount", required=True)
    s.add_argument("--currency", required=True)
    s.add_argument("--what", required=True, help="what is bought, e.g. '2 × Pad thai, Thai Orchid'")
    s.add_argument("--card", help="which card entry (default: the owner's default card); `info fields` lists them")
    s = sub.add_parser("submit", help="press a button that books, sends or requests something (asks the owner)")
    s.add_argument("ref")
    s.add_argument("--what", required=True, help="what it does, e.g. 'book yoga class, Fri 10:00, 1 person'")
    sub.add_parser("status", help="answer to a request").add_argument("approval", type=int)
    return p


def checkout_main(argv: list[str] | None = None) -> int:
    a = checkout_parser().parse_args(argv)
    c = a.cmd
    if c == "status":
        return _status("checkout", a.approval)
    if c == "fill":
        return _run_waiting("checkout", _op("checkout.fill", fills=_fill_pairs(a.pairs)), a.reason, a.wait)
    if c == "pay":
        return _run_waiting("checkout", _op("checkout.pay", fills=_fill_pairs(a.pairs), button=a.button,
                                            amount=a.amount, currency=a.currency, what=a.what, card=a.card),
                            a.reason, a.wait)
    if c == "submit":
        return _run_waiting("checkout", _op("checkout.submit", ref=a.ref, what=a.what), a.reason, a.wait)
    args = {"open": {"url": getattr(a, "url", None)}, "click": {"ref": getattr(a, "ref", None)},
            "type": {"ref": getattr(a, "ref", None), "text": getattr(a, "text", None)},
            "choose": {"ref": getattr(a, "ref", None), "option": getattr(a, "option", None)}}.get(c, {})
    return _run_waiting("checkout", _op(f"checkout.{c}", **args), a.reason, 0)


def payment_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="payment", description="Purchases made with the owner's card.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("log", help="recent purchases").add_argument("-n", "--limit", type=int, default=20)
    a = p.parse_args(argv)
    return _run_waiting("payment", _op("payment.log", limit=a.limit), None, 0)


# --- threads and instagram ----------------------------------------------------------------


def _text(args: argparse.Namespace) -> str | None:
    if getattr(args, "text_file", None):
        return workspace_file(args.text_file, 20_000)
    return getattr(args, "text", None)


def _op(name: str, **args: object) -> list[dict]:
    return [{"op": name, "args": {k: v for k, v in args.items() if v not in (None, False, [])}}]


def threads_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="threads", description="The owner's Threads account (and, in social mode, "
                                "Threads through their own web session).")
    p.add_argument("-m", "--reason", help="why (shown to the owner with approval requests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile", help="the owner's profile, or anyone's with USER (social mode)").add_argument(
        "user", nargs="?")
    s = sub.add_parser("posts", help="the owner's posts, or USER's (social mode)")
    s.add_argument("user", nargs="?")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--days", type=int)
    sub.add_parser("post", help="one post with its replies (link); without a web session: one of the owner's "
                   "posts or replies, with stats").add_argument("target")
    sub.add_parser("feed", help="the Threads home feed (social mode)").add_argument("-n", "--limit", type=int,
                                                                                   default=20)
    for name in ("like", "unlike"):
        sub.add_parser(name, help="a post (link); needs approval").add_argument("target")
    for name in ("follow", "unfollow"):
        sub.add_parser(name, help="an account; needs approval").add_argument("user")
    s = sub.add_parser("replies", help="replies under one of the owner's posts")
    s.add_argument("target")
    s.add_argument("-n", "--limit", type=int, default=50)
    sub.add_parser("insights", help="account stats").add_argument("--days", type=int, default=7)
    s = sub.add_parser("publish", help="post publicly (needs approval)")
    s.add_argument("text", nargs="?")
    s.add_argument("--text-file", help="read the text from this file in the workspace")
    s.add_argument("--reply-to", help="a post or reply (id or link) under the owner's posts")
    for name in ("delete", "hide", "unhide"):
        sub.add_parser(name, help="needs approval").add_argument("target")
    sub.add_parser("apply", help="run a JSON plan of operations as one request").add_argument("file")
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def threads_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd
    if c == "profile":
        return _op("thweb.profile", user=a.user) if a.user else _op("threads.profile")
    if c == "posts":
        return _op("thweb.posts", user=a.user, limit=a.limit) if a.user else \
            _op("threads.posts", limit=a.limit, days=a.days)
    if c == "feed":
        return _op("thweb.feed", limit=a.limit)
    if c in ("like", "unlike"):
        return _op(f"thweb.{c}", target=a.target)
    if c in ("follow", "unfollow"):
        return _op(f"thweb.{c}", user=a.user)
    if c == "post":
        return _op("thweb.post" if _web_session("threads") else "threads.post", target=a.target)
    if c in ("delete", "hide", "unhide"):
        return _op(f"threads.{c}", target=a.target)
    if c == "replies":
        return _op("threads.replies", target=a.target, limit=a.limit)
    if c == "insights":
        return _op("threads.insights", days=a.days)
    if c == "publish":
        return _op("threads.publish", text=_text(a), target=a.reply_to)
    if c == "apply":
        return _load_plan(a.file, "threads")
    raise SystemExit(f"unknown command {c}")


def instagram_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="instagram", description="The owner's Instagram: the official API (professional "
                                "accounts) and, in social mode, their own web session.")
    p.add_argument("-m", "--reason", help="why (shown to the owner with approval requests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile", help="the owner's (official API), or USER's (social mode)").add_argument(
        "user", nargs="?")
    sub.add_parser("media", help="the owner's posts").add_argument("-n", "--limit", type=int, default=20)
    sub.add_parser("post", help="a post with its comments (instagram.com link)").add_argument("target")
    sub.add_parser("feed", help="the home feed (social mode)").add_argument("-n", "--limit", type=int, default=20)
    s = sub.add_parser("posts", help="an account's posts (social mode)")
    s.add_argument("user")
    s.add_argument("-n", "--limit", type=int, default=12)
    sub.add_parser("stories", help="who has stories now (social mode; nothing is marked seen)")
    sub.add_parser("story", help="one account's story (social mode; not marked seen)").add_argument("user")
    s = sub.add_parser("comment", help="comment on a post, or reply to a comment (needs approval)")
    s.add_argument("target")
    s.add_argument("text", nargs="?")
    s.add_argument("--text-file")
    s.add_argument("--reply-to", help="the comment id to reply to (a top-level comment)")
    for name in ("like", "unlike"):
        s = sub.add_parser(name, help="a post, or with --comment one of its comments (needs approval)")
        s.add_argument("target")
        s.add_argument("--comment")
    for name in ("follow", "unfollow"):
        sub.add_parser(name, help="an account (needs approval)").add_argument("user")
    sub.add_parser("dms", help="check new direct messages now (runs the owner's DM watcher; the summary goes to them)")
    s = sub.add_parser("comments", help="comments on one of the owner's posts")
    s.add_argument("target")
    s.add_argument("-n", "--limit", type=int, default=50)
    sub.add_parser("insights", help="account stats").add_argument("--days", type=int, default=7)
    s = sub.add_parser("reply", help="reply to a comment (needs approval)")
    s.add_argument("comment")
    s.add_argument("text", nargs="?")
    s.add_argument("--text-file")
    s = sub.add_parser("delete-comment", help="needs approval; with --post: through the web session (the owner's own "
                       "comments, or comments on the owner's posts)")
    s.add_argument("comment")
    s.add_argument("--post", help="the post the comment is on (link)")
    for name in ("hide", "unhide"):
        sub.add_parser(name, help="needs approval").add_argument("comment")
    sub.add_parser("apply", help="run a JSON plan of operations as one request").add_argument("file")
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def instagram_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd
    if c in ("profile",):
        return _op("igweb.profile", user=a.user) if a.user else _op("instagram.profile")
    if c == "media":
        return _op("instagram.media", limit=a.limit)
    if c == "post":
        return _op("igweb.post" if _web_session("instagram") else "instagram.post", target=a.target)
    if c == "comments":
        if _web_session("instagram"):
            return _op("igweb.post", target=a.target, limit=a.limit)
        return _op("instagram.comments", target=a.target, limit=a.limit)
    if c == "feed":
        return _op("igweb.feed", limit=a.limit)
    if c == "posts":
        return _op("igweb.posts", user=a.user, limit=a.limit)
    if c == "stories":
        return _op("igweb.stories")
    if c == "story":
        return _op("igweb.story", user=a.user)
    if c == "comment":
        return _op("igweb.comment", target=a.target, text=_text(a), comment=a.reply_to)
    if c in ("like", "unlike"):
        return _op(f"igweb.{c}", target=a.target, comment=a.comment)
    if c in ("follow", "unfollow"):
        return _op(f"igweb.{c}", user=a.user)
    if c == "delete-comment" and a.post:
        return _op("igweb.delete_comment", target=a.post, comment=a.comment)
    if c == "insights":
        return _op("instagram.insights", days=a.days)
    if c == "reply":
        return _op("instagram.reply", target=a.comment, text=_text(a))
    if c in ("delete-comment", "hide", "unhide"):
        return _op(f"instagram.{c.replace('-', '_')}", target=a.comment)
    if c == "apply":
        return _load_plan(a.file, "instagram")
    raise SystemExit(f"unknown command {c}")


def _web_session(service: str) -> bool:
    """Whether post links go through the owner's web session: only in a social-mode conversation (AMPI_ORIGIN; the
    daemon decides that from the process tree anyway) and only if a session is saved. Everywhere else the official
    API keeps working as before."""
    if os.environ.get("AMPI_ORIGIN") != "forwarded:social":
        return False
    try:
        return bool(request({"cmd": "social.sessions"}).get(service))
    except (OSError, AttributeError):
        return False


def _watcher_run(name: str) -> dict:
    """Runs one of the owner's watchers now, through the bridge (which allows it only from the owner's own chat)."""
    import socket
    path = os.environ.get("AMPI_WATCHERS_SOCKET") or "/run/claude-assistant/watchers.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(30)
            s.connect(path)
            s.sendall(json.dumps({"cmd": "run", "name": name}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf)
    except (OSError, ValueError, AttributeError) as e:
        return {"ok": False, "error": f"the bridge isn't reachable ({type(e).__name__})"}
    return {"ok": bool(reply.get("ok")), "text": reply.get("text", ""), "error": reply.get("text", "")}


DM_WATCHER = "instagram-dms"


def _main(prog: str, parser: argparse.ArgumentParser, to_ops, argv: list[str] | None) -> int:
    args = parser.parse_args(argv)
    if prog == "instagram" and args.cmd == "dms":
        # Direct messages never come into a chat: this only starts the owner's DM watcher, whose summary goes to them.
        response = _watcher_run(DM_WATCHER)
        if response.get("ok"):
            response["text"] = (f"{response['text']} The summary goes to the owner's chat as a watcher message (not in "
                                "quiet hours, at most once per 10 minutes from social mode); you won't see the messages here.")
        else:
            response["error"] = (f"{response.get('error') or 'failed'} (the DM watcher is “{DM_WATCHER}”: see "
                                 "docs/SETUP.md §8c-2)")
    elif args.cmd == "approvals":
        response = request({"cmd": "approvals.list"})
    else:
        response = request({"cmd": "run", "ops": to_ops(args), "reason": args.reason,
                            "origin": os.environ.get("AMPI_ORIGIN") or None})
    if not response.get("ok"):
        print(f"{prog}: {response.get('error', 'failed')}", file=sys.stderr)
        return 1
    print(deliver(response.get("text", "") or "Done.", None, f"{prog}-{args.cmd}", save_dir=Path(prog)))
    return 0


def todoist_main(argv: list[str] | None = None) -> int:
    return _main("todoist", todoist_parser(), todoist_ops, argv)


def gmail_main(argv: list[str] | None = None) -> int:
    return _main("gmail", gmail_parser(), gmail_ops, argv)


def youtube_main(argv: list[str] | None = None) -> int:
    return _main("youtube", youtube_parser(), youtube_ops, argv)


def threads_main(argv: list[str] | None = None) -> int:
    return _main("threads", threads_parser(), threads_ops, argv)


def instagram_main(argv: list[str] | None = None) -> int:
    return _main("instagram", instagram_parser(), instagram_ops, argv)


# --- telegram: writing as the owner (through ampi-tools) ----------------------------------------------

DRAFT_MAX_AGE = 600  # a draft file must have been written in the last 10 minutes
TELEGRAM_TIMEOUT = 110


def telegram_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telegram",
        description="Write in the owner's Telegram AS THE OWNER (their own account): send, edit, delete. Without "
                    "permission for a chat it files a one-time request the owner approves in Telegram. Reading is "
                    "`tg-read`.")
    p.add_argument("-m", "--reason", help="why (shown to the owner with requests)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="send a message as the owner")
    s.add_argument("chat", help='exact chat name, @username, id from `tg-read chats`, or "me" (Saved Messages)')
    s.add_argument("text", nargs="?", help="the message (better: -f FILE)")
    s.add_argument("-f", "--file", help="read the text from this file in the workspace (written just before)")
    s.add_argument("--reply-to", type=int, help="message id (#123 in tg-read history) to reply to")
    e = sub.add_parser("edit", help="change one of the owner's text messages")
    e.add_argument("chat")
    e.add_argument("message", type=int, help="message id (#123 in tg-read history)")
    e.add_argument("text", nargs="?")
    e.add_argument("-f", "--file")
    d = sub.add_parser("delete", help="delete the owner's messages for everyone (always asks the owner)")
    d.add_argument("chat")
    d.add_argument("messages", type=int, nargs="+", help="message ids")
    sub.add_parser("permissions", help="what may be written where without asking")
    sub.add_parser("log", help="recent writes").add_argument("-n", "--limit", type=int, default=20)
    r = sub.add_parser("revoke", help="remove a permission the owner gave with “always” (only in the owner's chat)")
    r.add_argument("chat")
    r.add_argument("--action", choices=["send", "edit"])
    sub.add_parser("approvals", help="recent approval requests and their outcome")
    return p


def draft_text(a: argparse.Namespace, now: float | None = None) -> str | None:
    if getattr(a, "file", None):
        text = workspace_file(a.file, 20_000)
        age = (time.time() if now is None else now) - Path(a.file).stat().st_mtime
        if age > DRAFT_MAX_AGE:
            raise SystemExit(f"{a.file!r} was written {int(age // 60)} minutes ago; write the draft again (to a new "
                             "file name) right before sending, so it is the text you mean")
        return text.rstrip("\n")
    return a.text


def telegram_ops(a: argparse.Namespace) -> list[dict]:
    c = a.cmd
    if c == "send":
        return _op("telegram.send", chat=a.chat, text=draft_text(a), reply_to_msg=a.reply_to)
    if c == "edit":
        return _op("telegram.edit", chat=a.chat, message=a.message, text=draft_text(a))
    if c == "delete":
        return _op("telegram.delete", chat=a.chat, messages=a.messages)
    if c == "permissions":
        return _op("telegram.permissions")
    if c == "log":
        return _op("telegram.log", limit=a.limit)
    if c == "revoke":
        return _op("telegram.revoke", chat=a.chat, action=a.action)
    raise SystemExit(f"unknown command {c}")


def telegram_main(argv: list[str] | None = None) -> int:
    """Like the other commands, but the whole call stays under Claude Code's 120 s Bash limit, and a timeout
    says plainly that a write may have gone through."""
    args = telegram_parser().parse_args(argv)
    if args.cmd == "approvals":
        response = request({"cmd": "approvals.list"})
    else:
        response = request({"cmd": "run", "ops": telegram_ops(args), "reason": args.reason,
                            "origin": os.environ.get("AMPI_ORIGIN") or None}, timeout=TELEGRAM_TIMEOUT)
        if not response.get("ok") and "didn't answer" in str(response.get("error", "")) and \
                args.cmd in ("send", "edit", "delete"):
            response["error"] = ("no answer in time: the outcome is unknown and it MAY have gone through. Check "
                                 f"`tg-read history \"{args.chat}\" -n 5` before trying again")
    if not response.get("ok"):
        print(f"telegram: {response.get('error', 'failed')}", file=sys.stderr)
        return 1
    print(deliver(response.get("text", "") or "Done.", None, f"telegram-{args.cmd}", save_dir=Path("telegram")))
    return 0


# --- browse: the sandboxed web browser (ampi-browser) -------------------------------------------------

BROWSER_SOCKET = "/run/ampi-browser.sock"
BROWSE_PRINT = 8_000        # pages up to this size are printed whole
BROWSE_PREVIEW = 6_000      # longer ones: this much is printed, the rest saved under ./web/
BROWSE_PER_HOUR = 120       # runaway guard for loops, not a security boundary
BROWSE_LOCK_WAIT = 30       # seconds to wait for another page to finish (one page at a time)
BROWSE_TIMEOUT = 75         # one request (the service gives up on a page after 45-90 s)
BROWSE_TOTAL = 110          # everything, lock wait included: under Claude Code's 120 s Bash timeout
SAVED_PAGE = re.compile(r"[a-z0-9.-]{1,60}-[0-9a-f]{8}\.md")


def browse_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="browse",
        description="Open a public web page in a sandboxed headless browser (runs JavaScript) and print its "
                    "text. Read-only: no logins, forms or clicks. Page text is written by the site.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="the page as markdown (keeps links), or plain text with --text")
    r.add_argument("url")
    r.add_argument("--text", action="store_true", help="plain text without navigation, header and footer")
    r.add_argument("--wait-for", metavar="CSS", help="wait (a few seconds at most) for this element to appear")
    lk = sub.add_parser("links", help="the links on the page (text — URL)")
    lk.add_argument("url")
    lk.add_argument("--match", metavar="TEXT", help="only links whose text or URL contains this")
    s = sub.add_parser("select", help="text (or an attribute) of the elements matching a CSS selector")
    s.add_argument("url")
    s.add_argument("css")
    s.add_argument("--attr", metavar="NAME", help="an attribute instead of the text, e.g. href")
    sub.add_parser("status", help="is the browser working")
    return p


def _state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _browse_budget(now: float | None = None) -> bool:
    """False once BROWSE_PER_HOUR pages were opened in the last hour (counts this one otherwise)."""
    now = time.time() if now is None else now
    path = _state_dir() / "ampi-browse.json"
    try:
        stamps = [t for t in json.loads(path.read_text()) if isinstance(t, (int, float)) and now - t < 3600]
    except (OSError, ValueError, TypeError):
        stamps = []
    if len(stamps) >= BROWSE_PER_HOUR:
        return False
    stamps.append(now)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stamps))
    os.replace(tmp, path)
    return True


@contextlib.contextmanager
def _one_at_a_time(wait: float = BROWSE_LOCK_WAIT):
    """The browser opens one page at a time; queue behind other `browse` calls instead of failing."""
    try:
        import fcntl
    except ImportError:  # not on the server (Windows dev machines)
        yield True
        return
    with open(_state_dir() / "ampi-browse.lock", "a") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    yield False
                    return
                time.sleep(0.25)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def browser_request(payload: dict, path: str | None = None, deadline: float | None = None) -> dict | None:
    """One JSON line out, one back, all before `deadline` (time.monotonic()). None when the service closed
    the connection without answering (busy, or it refused to start). Never half-closes: the service takes
    EOF as "the caller is gone" and stops the page."""
    path = path or os.environ.get("AMPI_BROWSER_SOCKET") or BROWSER_SOCKET
    deadline = time.monotonic() + BROWSE_TIMEOUT if deadline is None else deadline
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            return {"ok": False, "error": "the browser isn't installed or running (ampi-browser.socket on the server)"}
        except PermissionError:
            return {"ok": False, "error": f"no permission to use {path}"}
        try:
            s.sendall(json.dumps(payload).encode() + b"\n")
            chunks = []
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout
                s.settimeout(left)
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if chunk.endswith(b"\n"):
                    break
        except socket.timeout:
            return {"ok": False, "error": "the page took too long"}
        except OSError:
            chunks = []
    if not chunks:
        return None
    try:
        reply = json.loads(b"".join(chunks))
    except ValueError:
        return {"ok": False, "error": "the browser sent a broken reply"}
    return reply if isinstance(reply, dict) else {"ok": False, "error": "the browser sent a broken reply"}


def save_page(text: str, url: str, root: Path | None = None, now: float | None = None) -> Path:
    """Saves a long page to ./web/<host>-<hash>.md (pruning ones older than a day) and returns the path."""
    root = Path.cwd() if root is None else root
    web = root / "web"
    web.mkdir(exist_ok=True)
    if web.is_symlink() or not web.is_dir():
        raise SystemExit("browse: ./web must be a real folder")
    now = time.time() if now is None else now
    for old in web.glob("*.md"):
        with contextlib.suppress(OSError):  # only pages saved here, never other files in web/
            if SAVED_PAGE.fullmatch(old.name) and not old.is_symlink() and now - old.stat().st_mtime > 86400:
                old.unlink()
    host = re.sub(r"[^a-z0-9.-]", "", (urllib.parse.urlsplit(url).hostname or "page").lower())[:60].strip(".")
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    target = web / f"{host or 'page'}-{digest}.md"
    tmp = web / f".{target.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(tmp, flags, 0o644), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, target)  # rename replaces a planted symlink itself, never what it points to
    return target


def browse_main(argv: list[str] | None = None) -> int:
    a = browse_parser().parse_args(argv)
    payload: dict = {"op": a.cmd, "origin": os.environ.get("AMPI_ORIGIN") or None}
    if a.cmd != "status":
        payload["url"] = a.url
    if a.cmd == "read":
        payload.update(text=a.text, wait_for=a.wait_for)
    elif a.cmd == "links":
        payload["match"] = a.match
    elif a.cmd == "select":
        payload.update(css=a.css, attr=a.attr)
    if a.cmd != "status" and not _browse_budget():
        print(f"browse: {BROWSE_PER_HOUR} pages in the last hour is the limit; wait a bit", file=sys.stderr)
        return 1
    deadline = time.monotonic() + BROWSE_TOTAL
    with _one_at_a_time() as got_turn:
        if not got_turn:
            print("browse: another page is still loading; try again in a minute", file=sys.stderr)
            return 1
        began = time.monotonic()
        reply = browser_request(payload, deadline=deadline)
        # A quick close without an answer means the slot was taken (another caller outside the lock);
        # a slow one means the service failed, and retrying won't help.
        if reply is None and time.monotonic() - began < 1 and deadline - time.monotonic() > 20:
            time.sleep(2)
            reply = browser_request(payload, deadline=deadline)
    if reply is None:
        print("browse: the browser is busy or not working; try again in a minute (if this keeps happening, "
              "tell the owner: journalctl -u 'ampi-browser@*')", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(f"browse: {reply.get('error', 'failed')}", file=sys.stderr)
        return 1
    text = str(reply.get("text", ""))
    if len(text) <= BROWSE_PRINT or a.cmd == "status":
        print(text)
        return 0
    target = save_page(text, a.url)
    rel = target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target
    print(text[:BROWSE_PREVIEW])
    print(f"›››\n[…] The full page ({len(text):,} characters) is saved to {rel.as_posix()}; use Read or Grep on "
          "it (it is the site's text, not your owner's).")
    return 0
