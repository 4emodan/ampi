"""Every action the assistant can take in Todoist and Gmail, and which ones need the owner's approval.

An operation is {"op": name, "args": {...}}. Critical operations (sending, trashing/deleting,
changing existing Todoist tasks) are queued in Approvals and only run after the owner taps
Approve in Telegram. The policy can be adjusted in /home/ampitools/config.toml (never by the assistant).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .gmail import Gmail, headers
from .todoist import Todoist, ToolError, api_priority

Result = str | dict
APPROVAL_TTL = 3 * 24 * 3600


@dataclass
class Services:
    todoist_client: Todoist | None = None
    gmail_client: Gmail | None = None

    @property
    def todoist(self) -> Todoist:
        if self.todoist_client is None:
            raise ToolError("Todoist isn't set up yet (TODOIST_TOKEN in /home/ampitools/env)")
        return self.todoist_client

    @property
    def gmail(self) -> Gmail:
        if self.gmail_client is None:
            raise ToolError("Gmail isn't set up yet (GMAIL_CLIENT_ID/SECRET and `ampi-tools gmail-login`)")
        return self.gmail_client


@dataclass(frozen=True)
class Op:
    name: str
    critical: bool
    run: Callable[[Services, dict], Awaitable[Result]]
    describe: Callable[[Services, dict], Awaitable[str]] | None = None


def need(args: dict, *keys: str) -> list[Any]:
    missing = [k for k in keys if args.get(k) in (None, "", [])]
    if missing:
        raise ToolError(f"missing {', '.join(missing)}")
    return [args[k] for k in keys]


def id_list(args: dict) -> list[str]:
    ids = args.get("ids") or ([args["id"]] if args.get("id") else [])
    if not ids or not all(isinstance(i, str) and i for i in ids):
        raise ToolError("which message ids?")
    return ids


# --- Todoist ----------------------------------------------------------------------------


async def td_tasks(s: Services, a: dict) -> Result:
    tasks = await s.todoist.tasks(a.get("filter"), a.get("project"), int(a.get("limit") or 200))
    if not tasks:
        return "No tasks."
    lines = await s.todoist.describe(tasks)
    return f"{len(tasks)} tasks:\n" + "\n".join(lines)


async def td_task(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    t = await s.todoist.task(tid)
    out = (await s.todoist.describe([t]))[0]
    out += f"\n  added {t.get('added_at', '?')} · updated {t.get('updated_at', '?')}"
    comments = await s.todoist.comments(tid)
    if comments:
        out += "\nComments:\n" + "\n".join(f"- {c.get('posted_at', '')[:16]}: {c.get('content', '')}" for c in comments)
    return out


async def td_projects(s: Services, a: dict) -> Result:
    projects = await s.todoist.projects()
    sections = await s.todoist.sections()
    lines = []
    for p in projects:
        indent = "  " if p.get("parent_id") else ""
        lines.append(f"{indent}- {p['name']} [{p['id']}]" + (" (Inbox)" if p.get("inbox_project") else ""))
        for sec in (x for x in sections if x["project_id"] == p["id"]):
            lines.append(f"{indent}    § {sec['name']} [{sec['id']}]")
    return "\n".join(lines) or "No projects."


async def td_labels(s: Services, a: dict) -> Result:
    return ", ".join(f"@{label['name']}" for label in await s.todoist.labels()) or "No labels."


async def td_completed(s: Services, a: dict) -> Result:
    now = datetime.now(timezone.utc)
    since = a.get("since") or (now - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    until = a.get("until") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    items = await s.todoist.completed(since, until)
    if not items:
        return "Nothing completed in that period."
    projects = {p["id"]: p["name"] for p in await s.todoist.projects()}
    return "\n".join(
        f"- {i.get('completed_at', '')[:16]} · {i.get('content', '')} · {projects.get(i.get('project_id'), '?')}"
        for i in items
    )


async def td_ids(s: Services, a: dict) -> Result:
    tasks = await s.todoist.tasks(a.get("filter"), a.get("project"), int(a.get("limit") or 500))
    return {"ids": [t["id"] for t in tasks], "text": ""}


async def td_show(s: Services, a: dict) -> Result:
    ids = set(a.get("ids") or [])
    tasks = [t for t in await s.todoist.tasks(a.get("filter"), a.get("project"), 500) if t["id"] in ids]
    return "\n".join(await s.todoist.describe(tasks))


async def _task_fields(s: Services, a: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("content", "description"):
        if a.get(key) is not None:
            out[key] = a[key]
    if a.get("due"):
        if a["due"] in ("none", "no date", "remove"):
            out["due_string"] = "no date"
        else:
            out["due_string"] = a["due"]
    if a.get("deadline"):
        out["deadline_date"] = a["deadline"]
    if a.get("priority") is not None:
        out["priority"] = api_priority(a["priority"])
    if a.get("labels") is not None:
        out["labels"] = list(a["labels"])
    return out


async def td_add(s: Services, a: dict) -> Result:
    (content,) = need(a, "content")
    fields = await _task_fields(s, a)
    if a.get("project"):
        fields["project_id"] = await s.todoist.project_id(a["project"])
        if a.get("section"):
            fields["section_id"] = await s.todoist.section_id(fields["project_id"], a["section"])
    if a.get("parent"):
        fields["parent_id"] = a["parent"]
    t = await s.todoist.add_task(**fields)
    return "Added: " + (await s.todoist.describe([t]))[0].removeprefix("- ")


async def td_comment(s: Services, a: dict) -> Result:
    tid, text = need(a, "id", "text")
    await s.todoist.add_comment(tid, text)
    return f"Comment added to {tid}."


async def td_add_project(s: Services, a: dict) -> Result:
    (name,) = need(a, "name")
    p = await s.todoist.add_project(name, a.get("parent"))
    return f"Project created: {p['name']} [{p['id']}]"


async def td_add_section(s: Services, a: dict) -> Result:
    project, name = need(a, "project", "name")
    sec = await s.todoist.add_section(project, name)
    return f"Section created: {sec['name']} [{sec['id']}]"


async def td_add_label(s: Services, a: dict) -> Result:
    (name,) = need(a, "name")
    await s.todoist.add_label(name)
    return f"Label created: @{name}"


async def td_update(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    fields = await _task_fields(s, a)
    if not fields:
        raise ToolError("nothing to change")
    await s.todoist.update_task(tid, **fields)
    return f"Updated {tid}."


async def td_move(s: Services, a: dict) -> Result:
    tid, project = need(a, "id", "project")
    pid = await s.todoist.project_id(project)
    target = {"project_id": pid}
    if a.get("section"):
        target = {"section_id": await s.todoist.section_id(pid, a["section"])}
    await s.todoist.move_task(tid, **target)
    return f"Moved {tid}."


async def td_complete(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    await s.todoist.close_task(tid)
    return f"Completed {tid}."


async def td_reopen(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    await s.todoist.reopen_task(tid)
    return f"Reopened {tid}."


async def td_delete(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    await s.todoist.delete_task(tid)
    return f"Deleted {tid}."


async def _task_title(s: Services, tid: str) -> str:
    try:
        t = await s.todoist.task(tid)
    except ToolError:
        return f"task {tid} (not found)"
    projects = {p["id"]: p["name"] for p in await s.todoist.projects()}
    return f"“{t.get('content', '')}” ({projects.get(t.get('project_id'), '?')})"


def _describe_task(verb: str) -> Callable[[Services, dict], Awaitable[str]]:
    async def describe(s: Services, a: dict) -> str:
        return f"{verb} {await _task_title(s, a.get('id', '?'))}"

    return describe


async def describe_update(s: Services, a: dict) -> str:
    changes = [f"{k} → {a[k]!r}" for k in ("content", "description", "due", "deadline", "priority", "labels")
               if a.get(k) is not None]
    return f"Change {await _task_title(s, a.get('id', '?'))}: " + "; ".join(changes)


async def describe_move(s: Services, a: dict) -> str:
    where = a.get("project", "?") + (f" / {a['section']}" if a.get("section") else "")
    return f"Move {await _task_title(s, a.get('id', '?'))} to {where}"


# --- Gmail ------------------------------------------------------------------------------


async def gm_search(s: Services, a: dict) -> Result:
    (query,) = need(a, "query")
    ids = await s.gmail.search_ids(query, int(a.get("limit") or 20))
    if not ids:
        return f"No emails match {query!r}."
    return f"{len(ids)} emails (newest first):\n" + "\n".join(await s.gmail.summaries(ids))


async def gm_read(s: Services, a: dict) -> Result:
    (mid,) = need(a, "id")
    return await s.gmail.read(mid)


async def gm_thread(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    return await s.gmail.thread(tid)


async def gm_labels(s: Services, a: dict) -> Result:
    labels = await s.gmail.labels()
    user = sorted(label["name"] for label in labels if label.get("type") == "user")
    return "Your labels: " + (", ".join(user) or "none") + "\nSystem: " + ", ".join(
        sorted(label["name"] for label in labels if label.get("type") == "system")
    )


async def gm_drafts(s: Services, a: dict) -> Result:
    drafts = await s.gmail.drafts(int(a.get("limit") or 20))
    if not drafts:
        return "No drafts."
    summaries = await s.gmail.summaries([d["message"]["id"] for d in drafts])
    return "\n".join(f"draft {d['id']}: {line.removeprefix('- ')}" for d, line in zip(drafts, summaries))


async def gm_ids(s: Services, a: dict) -> Result:
    (query,) = need(a, "query")
    return {"ids": await s.gmail.search_ids(query, int(a.get("limit") or 100)), "text": ""}


async def gm_show(s: Services, a: dict) -> Result:
    return "\n".join(await s.gmail.summaries(id_list(a)))


async def gm_label(s: Services, a: dict) -> Result:
    ids = id_list(a)
    add, remove = list(a.get("add") or []), list(a.get("remove") or [])
    if not add and not remove:
        raise ToolError("add or remove which labels?")
    await s.gmail.modify(ids, add, remove)
    return f"{len(ids)} email(s): " + "; ".join(
        x for x in (f"added {', '.join(add)}" if add else "", f"removed {', '.join(remove)}" if remove else "") if x
    )


def _gm_labels_op(add: list[str], remove: list[str], done: str) -> Callable[[Services, dict], Awaitable[Result]]:
    async def run(s: Services, a: dict) -> Result:
        ids = id_list(a)
        await s.gmail.modify(ids, add, remove)
        return f"{done} {len(ids)} email(s)."

    return run


async def gm_untrash(s: Services, a: dict) -> Result:
    ids = id_list(a)
    await s.gmail.untrash(ids)
    return f"Restored {len(ids)} email(s) from the trash."


def _mail_args(a: dict) -> tuple[str, str, str, str | None, str | None]:
    body = a.get("body")
    if not body:
        raise ToolError("the email needs a body")
    if not a.get("to") and not a.get("reply_to"):
        raise ToolError("who to? (--to, or --reply-to a message)")
    return a.get("to") or "", a.get("subject") or "", body, a.get("cc"), a.get("reply_to")


async def gm_draft(s: Services, a: dict) -> Result:
    d = await s.gmail.draft(*_mail_args(a))
    return f"Draft saved (id {d['id']}). The owner can review it in Gmail; sending needs their approval."


async def gm_send(s: Services, a: dict) -> Result:
    sent = await s.gmail.send(*_mail_args(a))
    return f"Sent (message {sent.get('id')})."


async def gm_send_draft(s: Services, a: dict) -> Result:
    (did,) = need(a, "id")
    sent = await s.gmail.send_draft(did)
    return f"Draft sent (message {sent.get('id')})."


async def gm_trash(s: Services, a: dict) -> Result:
    ids = id_list(a)
    await s.gmail.trash(ids)
    return f"Moved {len(ids)} email(s) to the trash (restorable for 30 days)."


async def gm_delete_label(s: Services, a: dict) -> Result:
    (name,) = need(a, "name")
    await s.gmail.delete_label(name)
    return f"Deleted label {name} (the emails stay)."


async def describe_send(s: Services, a: dict) -> str:
    to = a.get("to") or "(reply to the sender)"
    head = f"Send an email to {to}" + (f", cc {a['cc']}" if a.get("cc") else "")
    if a.get("reply_to"):
        try:
            orig = await s.gmail.metadata([a["reply_to"]])
            h = headers(orig[0])
            head += f", replying to “{h.get('subject', '')}” from {h.get('from', '?')}"
        except ToolError:
            head += f", replying to message {a['reply_to']}"
    subject = a.get("subject") or "(Re: original subject)"
    return f"{head}\nSubject: {subject}\n\n{(a.get('body') or '')[:2500]}"


async def describe_send_draft(s: Services, a: dict) -> str:
    try:
        d = await s.gmail.get_draft(a.get("id", ""))
    except ToolError:
        return f"Send draft {a.get('id')}"
    from .gmail import format_full

    return "Send this draft:\n" + format_full(d["message"], await s.gmail.label_names())[:3000]


async def describe_trash(s: Services, a: dict) -> str:
    ids = id_list(a)
    try:
        lines = [line.split("\n")[0] for line in await s.gmail.summaries(ids[:30])]
    except ToolError:
        lines = [f"- {i}" for i in ids[:30]]
    more = f"\n…and {len(ids) - 30} more" if len(ids) > 30 else ""
    return f"Move {len(ids)} email(s) to the trash:\n" + "\n".join(lines) + more


async def describe_delete_label(s: Services, a: dict) -> str:
    return f"Delete the Gmail label “{a.get('name')}” (emails keep existing)"


OPS: dict[str, Op] = {op.name: op for op in [
    # Todoist: reading and adding are safe; changing existing tasks needs approval.
    Op("todoist.tasks", False, td_tasks),
    Op("todoist.task", False, td_task),
    Op("todoist.projects", False, td_projects),
    Op("todoist.labels", False, td_labels),
    Op("todoist.completed", False, td_completed),
    Op("todoist.ids", False, td_ids),
    Op("todoist.show", False, td_show),
    Op("todoist.add", False, td_add),
    Op("todoist.comment", False, td_comment),
    Op("todoist.add_project", False, td_add_project),
    Op("todoist.add_section", False, td_add_section),
    Op("todoist.add_label", False, td_add_label),
    Op("todoist.update", True, td_update, describe_update),
    Op("todoist.move", True, td_move, describe_move),
    Op("todoist.complete", True, td_complete, _describe_task("Complete")),
    Op("todoist.reopen", True, td_reopen, _describe_task("Reopen")),
    Op("todoist.delete", True, td_delete, _describe_task("Delete")),
    # Gmail: reading, labelling, archiving and drafting are safe; sending and trashing need approval.
    Op("gmail.search", False, gm_search),
    Op("gmail.read", False, gm_read),
    Op("gmail.thread", False, gm_thread),
    Op("gmail.labels", False, gm_labels),
    Op("gmail.drafts", False, gm_drafts),
    Op("gmail.ids", False, gm_ids),
    Op("gmail.show", False, gm_show),
    Op("gmail.label", False, gm_label),
    Op("gmail.archive", False, _gm_labels_op([], ["INBOX"], "Archived")),
    Op("gmail.mark_read", False, _gm_labels_op([], ["UNREAD"], "Marked as read:")),
    Op("gmail.mark_unread", False, _gm_labels_op(["UNREAD"], [], "Marked as unread:")),
    Op("gmail.star", False, _gm_labels_op(["STARRED"], [], "Starred")),
    Op("gmail.unstar", False, _gm_labels_op([], ["STARRED"], "Unstarred")),
    Op("gmail.untrash", False, gm_untrash),
    Op("gmail.draft", False, gm_draft),
    Op("gmail.send", True, gm_send, describe_send),
    Op("gmail.send_draft", True, gm_send_draft, describe_send_draft),
    Op("gmail.trash", True, gm_trash, describe_trash),
    Op("gmail.delete_label", True, gm_delete_label, describe_delete_label),
]}

# Never runnable without approval, whatever the config says.
ALWAYS_CRITICAL = frozenset({"gmail.send", "gmail.send_draft", "gmail.trash", "todoist.delete"})


@dataclass(frozen=True)
class Policy:
    auto_approve: frozenset[str] = frozenset()  # critical ops the owner chose to allow without asking
    require_approval: frozenset[str] = frozenset()  # safe ops the owner wants to approve anyway

    def critical(self, name: str) -> bool:
        if name in ALWAYS_CRITICAL or name in self.require_approval:
            return True
        return OPS[name].critical and name not in self.auto_approve


# --- approvals --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    ops TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    notified INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    decided REAL
);
"""


@dataclass
class Approvals:
    path: Path | str
    db: sqlite3.Connection = field(init=False)

    def __post_init__(self) -> None:
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def add(self, ops: list[dict], summary: str, reason: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO approvals (created, ops, summary, reason) VALUES (?, ?, ?, ?)",
            (time.time(), json.dumps(ops), summary, reason),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def expire(self) -> None:
        self.db.execute(
            "UPDATE approvals SET status='expired' WHERE status='pending' AND created < ?",
            (time.time() - APPROVAL_TTL,),
        )
        self.db.commit()

    def unnotified(self) -> list[dict]:
        self.expire()
        rows = self.db.execute("SELECT * FROM approvals WHERE status='pending' AND notified=0 ORDER BY id")
        return [dict(r) for r in rows]

    def mark_notified(self, approval_id: int) -> None:
        self.db.execute("UPDATE approvals SET notified=1 WHERE id=?", (approval_id,))
        self.db.commit()

    def get(self, approval_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, approval_id: int, status: str) -> dict | None:
        """Moves a pending request to `status`; returns it, or None if it wasn't pending (double tap)."""
        self.expire()
        cur = self.db.execute(
            "UPDATE approvals SET status=?, decided=? WHERE id=? AND status='pending'",
            (status, time.time(), approval_id),
        )
        self.db.commit()
        return self.get(approval_id) if cur.rowcount else None

    def finish(self, approval_id: int, status: str, result: str) -> None:
        self.db.execute("UPDATE approvals SET status=?, result=? WHERE id=?", (status, result, approval_id))
        self.db.commit()

    def recent(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


# --- running ----------------------------------------------------------------------------


def validate(ops: Any) -> list[dict]:
    if not isinstance(ops, list) or not ops:
        raise ToolError("no operations")
    if len(ops) > 200:
        raise ToolError("at most 200 operations per request")
    out = []
    for o in ops:
        if not isinstance(o, dict) or o.get("op") not in OPS:
            raise ToolError(f"unknown operation {o.get('op') if isinstance(o, dict) else o!r}")
        args = o.get("args") or {}
        if not isinstance(args, dict):
            raise ToolError(f"{o['op']}: args must be an object")
        out.append({"op": o["op"], "args": args})
    return out


async def execute(services: Services, ops: list[dict]) -> tuple[bool, str, dict]:
    """Runs ops in order and stops at the first failure. Returns (all ok, text, extra fields)."""
    texts: list[str] = []
    extra: dict = {}
    for i, o in enumerate(ops):
        try:
            result = await OPS[o["op"]].run(services, o["args"])
        except ToolError as e:
            texts.append(f"✗ {o['op']}: {e}")
            if len(ops) > 1:
                texts.append(f"(stopped; {len(ops) - i - 1} later step(s) not run)")
            return False, "\n".join(texts), extra
        if isinstance(result, dict):
            extra.update({k: v for k, v in result.items() if k != "text"})
            result = result.get("text", "")
        if result:
            texts.append(result)
    return True, "\n".join(texts), extra


async def summarize(services: Services, ops: list[dict]) -> str:
    lines = []
    for o in ops:
        op = OPS[o["op"]]
        try:
            line = await op.describe(services, o["args"]) if op.describe else None
        except ToolError:
            line = None
        lines.append("• " + (line or f"{o['op']} {json.dumps(o['args'], ensure_ascii=False)[:300]}"))
    return "\n".join(lines)
