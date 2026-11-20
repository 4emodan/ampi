"""`todoist`, `gmail` and `youtube`: the assistant's commands (standard library only).

They send operations to the ampi-tools daemon, which holds the tokens. Critical operations come back
as "waiting for the owner's approval" instead of running.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
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


def _main(prog: str, parser: argparse.ArgumentParser, to_ops, argv: list[str] | None) -> int:
    args = parser.parse_args(argv)
    if args.cmd == "approvals":
        response = request({"cmd": "approvals.list"})
    else:
        response = request({"cmd": "run", "ops": to_ops(args), "reason": args.reason})
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
