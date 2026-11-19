"""Every action the assistant can take in Todoist and Gmail, and which ones need the owner's approval.

An operation is {"op": name, "args": {...}}. Arguments are checked against a strict schema (ids can't
smuggle in API paths). Critical operations (sending, trashing/deleting, changing existing Todoist tasks,
commenting) are *prepared* first: names are resolved to ids and email recipients/threads are fixed, so
the summary the owner approves describes exactly what will run. They are then queued in Approvals and
only run after the owner taps Approve in Telegram. The policy can be adjusted in
/home/ampitools/config.toml (never by the assistant).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from .gmail import IGNORABLE, Gmail, format_full, headers
from .todoist import Todoist, ToolError, api_priority

log = logging.getLogger(__name__)
Result = str | dict
APPROVAL_TTL = 3 * 24 * 3600
MAX_SUMMARY = 3300  # what fits in one Telegram message with the header; longer requests are refused
MAX_MAIL_BODY = 3000  # longer emails can't be shown in full for approval: draft them instead
MAX_PENDING = 10


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
    label: str = ""  # short name for the summary header, e.g. "Send email"


# --- argument schema --------------------------------------------------------------------

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _id(v: Any) -> str:
    if not isinstance(v, str) or not ID_RE.fullmatch(v):
        raise ToolError(f"bad id {v!r}")
    return v


def _ids(v: Any) -> list[str]:
    if not isinstance(v, list) or not 1 <= len(v) <= 500:
        raise ToolError("ids must be a list of 1-500 ids")
    return [_id(x) for x in v]


def _text(limit: int, oneline: bool) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if not isinstance(v, str) or len(v) > limit:
            raise ToolError(f"text too long or not text (max {limit} characters)")
        if oneline and ("\n" in v or "\r" in v):
            raise ToolError("this field can't contain line breaks")
        return v

    return check


def _strs(item_limit: int, count: int) -> Callable[[Any], list[str]]:
    def check(v: Any) -> list[str]:
        if not isinstance(v, list) or len(v) > count:
            raise ToolError(f"expected a list of at most {count} names")
        return [_text(item_limit, True)(x) for x in v]

    return check


def _int(lo: int, hi: int) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ToolError("expected a number")
        return max(lo, min(hi, v))

    return check


def _priority(v: Any) -> str:
    api_priority(v)  # raises if invalid
    return str(v)


def _date(v: Any) -> str:
    if not isinstance(v, str) or not DATE_RE.fullmatch(v):
        raise ToolError("dates must be YYYY-MM-DD")
    return v


SHORT, LONG = _text(300, True), _text(20_000, False)
FIELDS: dict[str, Callable[[Any], Any]] = {
    "id": _id, "ids": _ids, "reply_to": _id, "parent": _id,
    "content": _text(500, True), "description": _text(16_000, False), "text": _text(15_000, False),
    "due": _text(100, True), "deadline": _date, "priority": _priority, "labels": _strs(60, 30),
    "project": SHORT, "section": SHORT, "name": _text(120, True),
    "query": SHORT, "filter": SHORT, "since": SHORT, "until": SHORT, "limit": _int(1, 1000),
    "to": _text(2000, True), "cc": _text(2000, True), "subject": _text(500, True), "body": _text(100_000, False),
    "add": _strs(100, 20), "remove": _strs(100, 20),
}
# Filled in by prepare(); only the daemon sets these, never the caller.
INTERNAL = {"project_id", "section_id", "where", "resolved", "message_id"}


def validate(ops: Any) -> list[dict]:
    if not isinstance(ops, list) or not ops:
        raise ToolError("no operations")
    if len(ops) > 100:
        raise ToolError("at most 100 operations per request")
    out = []
    for o in ops:
        if not isinstance(o, dict) or o.get("op") not in OPS:
            raise ToolError(f"unknown operation {o.get('op') if isinstance(o, dict) else o!r}")
        raw = o.get("args") or {}
        if not isinstance(raw, dict):
            raise ToolError(f"{o['op']}: args must be an object")
        args = {}
        for key, value in raw.items():
            if value is None:
                continue
            if key not in FIELDS:
                raise ToolError(f"{o['op']}: unknown argument {key!r}")
            try:
                args[key] = FIELDS[key](value)
            except ToolError as e:
                raise ToolError(f"{o['op']} {key}: {e}") from None
        out.append({"op": o["op"], "args": args})
    return out


def need(args: dict, *keys: str) -> list[Any]:
    missing = [k for k in keys if args.get(k) in (None, "", [])]
    if missing:
        raise ToolError(f"missing {', '.join(missing)}")
    return [args[k] for k in keys]


def id_list(args: dict) -> list[str]:
    ids = args.get("ids") or ([args["id"]] if args.get("id") else [])
    if not ids:
        raise ToolError("which message ids?")
    return ids


_INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}  # control, format (bidi, zero-width), line/paragraph separators


def clean(s: Any, limit: int | None = None) -> str:
    """For text shown in an approval: one line, no control/bidi/zero-width characters."""
    s = IGNORABLE.sub("", str(s))
    s = "".join(" " if unicodedata.category(ch) in _INVISIBLE else ch for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if limit is None or len(s) <= limit else s[:limit] + "…"


def clean_block(s: str) -> str:
    """Multi-line text for an approval, every line marked so it can't pose as part of the summary."""
    lines = []
    for line in IGNORABLE.sub("", s).splitlines() or [""]:
        line = "".join(" " if unicodedata.category(ch) in _INVISIBLE else ch for ch in line).rstrip()
        lines.append(f"│ {line}")
    return "\n".join(lines)


# --- Todoist ----------------------------------------------------------------------------


async def td_tasks(s: Services, a: dict) -> Result:
    tasks = await s.todoist.tasks(a.get("filter"), a.get("project"), a.get("limit", 200))
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
        flags = (" (Inbox)" if p.get("inbox_project") else "") + (" (shared)" if p.get("is_shared") else "")
        lines.append(f"{indent}- {p['name']} [{p['id']}]{flags}")
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
    tasks = await s.todoist.tasks(a.get("filter"), a.get("project"), a.get("limit", 500))
    return {"ids": [t["id"] for t in tasks], "text": ""}


async def td_show(s: Services, a: dict) -> Result:
    ids = set(a.get("ids") or [])
    tasks = [t for t in await s.todoist.tasks(a.get("filter"), a.get("project"), 1000) if t["id"] in ids]
    return "\n".join(await s.todoist.describe(tasks))


def _task_fields(a: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("content", "description"):
        if a.get(key) is not None:
            out[key] = a[key]
    if a.get("due"):
        out["due_string"] = "no date" if a["due"] in ("none", "no date", "remove") else a["due"]
    if a.get("deadline"):
        out["deadline_date"] = a["deadline"]
    if a.get("priority") is not None:
        out["priority"] = api_priority(a["priority"])
    if a.get("labels") is not None:
        out["labels"] = list(a["labels"])
    return out


async def td_add(s: Services, a: dict) -> Result:
    need(a, "content")
    fields = _task_fields(a)
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
    p = await s.todoist.add_project(name, a.get("project"))  # optional parent project (name or id)
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
    fields = _task_fields(a)
    if not fields:
        raise ToolError("nothing to change")
    await s.todoist.update_task(tid, **fields)
    return f"Updated {tid}."


async def td_move(s: Services, a: dict) -> Result:
    (tid,) = need(a, "id")
    target = await _move_target(s, a)
    await s.todoist.move_task(tid, **{k: v for k, v in target.items() if k in ("project_id", "section_id")})
    return f"Moved {tid} to {target['where']}."


async def _move_target(s: Services, a: dict) -> dict:
    """Resolved once (prepare) and stored, so a project created later with the same name can't change it."""
    if a.get("project_id"):
        return {k: a[k] for k in ("project_id", "section_id", "where") if a.get(k)}
    (project,) = need(a, "project")
    pid = await s.todoist.project_id(project)
    names = {p["id"]: p["name"] for p in await s.todoist.projects()}
    out = {"project_id": pid, "where": names.get(pid, pid)}
    if a.get("section"):
        out = {"section_id": await s.todoist.section_id(pid, a["section"]),
               "where": f"{names.get(pid, pid)} / {a['section']}"}
    return out


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
    return f"“{clean(t.get('content', ''), 150)}” ({clean(projects.get(t.get('project_id'), '?'))})"


def _describe_task(verb: str, suffix: str = "") -> Callable[[Services, dict], Awaitable[str]]:
    async def describe(s: Services, a: dict) -> str:
        return f"{verb} {await _task_title(s, a.get('id', '?'))}{suffix}"

    return describe


async def describe_update(s: Services, a: dict) -> str:
    lines = [f"Change {await _task_title(s, a.get('id', '?'))}:"]
    for k in ("content", "due", "deadline", "priority", "labels"):
        if a.get(k) is not None:
            value = ", ".join(a[k]) if isinstance(a[k], list) else a[k]
            lines.append(f"  {k} → “{clean(value)}”")
    if a.get("description") is not None:
        lines.append("  description →")
        lines.append(clean_block(a["description"]))
    return "\n".join(lines)


async def describe_move(s: Services, a: dict) -> str:
    return f"Move {await _task_title(s, a.get('id', '?'))} to {clean(a.get('where') or a.get('project', '?'))}"


async def describe_comment(s: Services, a: dict) -> str:
    return f"Comment on {await _task_title(s, a.get('id', '?'))} (collaborators may be notified):\n" + \
        clean_block(a.get("text", ""))


# --- Gmail ------------------------------------------------------------------------------

# System labels the safe `label` op may add or remove. TRASH/SPAM (=deleting), SENT and DRAFT are not here.
SAFE_SYSTEM_LABELS = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
                      "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
BLOCKED_LABELS = {"TRASH", "SPAM", "SENT", "DRAFT", "CHAT"}


async def gm_search(s: Services, a: dict) -> Result:
    (query,) = need(a, "query")
    ids = await s.gmail.search_ids(query, min(a.get("limit", 20), 200))
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
    drafts = await s.gmail.drafts(min(a.get("limit", 20), 100))
    if not drafts:
        return "No drafts."
    summaries = await s.gmail.summaries([d["message"]["id"] for d in drafts])
    return "\n".join(f"draft {d['id']}: {line.removeprefix('- ')}" for d, line in zip(drafts, summaries))


async def gm_ids(s: Services, a: dict) -> Result:
    (query,) = need(a, "query")
    return {"ids": await s.gmail.search_ids(query, min(a.get("limit", 100), 500)), "text": ""}


async def gm_show(s: Services, a: dict) -> Result:
    return "\n".join(await s.gmail.summaries(id_list(a)[:100]))


def _check_safe_labels(names: list[str]) -> None:
    for n in names:
        up = n.upper()
        if up in BLOCKED_LABELS or (up.startswith("CATEGORY_") and up not in SAFE_SYSTEM_LABELS):
            raise ToolError(f"label {n!r} can't be set this way (use `gmail trash`, which asks the owner)")


async def gm_label(s: Services, a: dict) -> Result:
    ids = id_list(a)
    add, remove = list(a.get("add") or []), list(a.get("remove") or [])
    if not add and not remove:
        raise ToolError("add or remove which labels?")
    _check_safe_labels(add + remove)
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


def _mail_fields(a: dict) -> dict:
    if not a.get("body"):
        raise ToolError("the email needs a body")
    if not a.get("to") and not a.get("reply_to"):
        raise ToolError("who to? (--to, or --reply-to a message)")
    return {k: a.get(k) for k in ("to", "cc", "subject", "body", "reply_to")}


async def gm_draft(s: Services, a: dict) -> Result:
    d = await s.gmail.draft(_mail_fields(a))
    return f"Draft saved (id {d['id']}). The owner can review it in Gmail; sending needs their approval."


async def gm_send(s: Services, a: dict) -> Result:
    resolved = a.get("resolved")
    if not isinstance(resolved, dict):  # only possible if the policy lets sends run without approval
        resolved = await s.gmail.resolve_mail(_mail_fields(a))
    sent = await s.gmail.send_resolved(resolved)
    return f"Sent (message {sent.get('id')})."


async def gm_send_draft(s: Services, a: dict) -> Result:
    (did,) = need(a, "id")
    if a.get("message_id"):
        current = await s.gmail.get_draft(did)
        if current.get("message", {}).get("id") != a["message_id"]:
            raise ToolError("the draft was changed after it was approved; not sent")
    sent = await s.gmail.send_draft(did)
    return f"Draft sent (message {sent.get('id')})."


async def gm_trash(s: Services, a: dict) -> Result:
    ids = id_list(a)
    await s.gmail.trash(ids)
    return f"Moved {len(ids)} email(s) to the trash (restorable for 30 days)."


async def gm_delete_label(s: Services, a: dict) -> Result:
    (name,) = need(a, "name")
    _check_safe_labels([name])
    await s.gmail.delete_label(name)
    return f"Deleted label {name} (the emails stay)."


def _addresses(value: str) -> str:
    """Shows each recipient as display name plus the real address, so "Boss" <x@evil> is visible."""
    pairs = getaddresses([value or ""])
    return ", ".join(f"{clean(n)} <{clean(a)}>" if n else f"<{clean(a)}>" for n, a in pairs if a) or "(nobody)"


async def describe_send(s: Services, a: dict) -> str:
    r = a.get("resolved") or {}
    lines = [f"Send an email to {_addresses(r.get('to', ''))}"]
    if r.get("cc"):
        lines.append(f"Cc: {_addresses(r['cc'])}")
    lines.append(f"Subject: “{clean(r.get('subject', ''), 300)}”")
    if r.get("thread_id"):
        lines.append("(a reply in an existing conversation)")
    body = r.get("body", "")
    lines.append(f"Body ({len(body)} characters, shown in full):")
    lines.append(clean_block(body))
    return "\n".join(lines)


async def describe_send_draft(s: Services, a: dict) -> str:
    d = await s.gmail.get_draft(a.get("id", ""))
    msg = d.get("message", {})
    h = headers(msg)
    full = format_full(msg, await s.gmail.label_names())
    body = full.split("\n\n", 1)[1] if "\n\n" in full else ""
    lines = [f"Send draft {a.get('id')} to {_addresses(h.get('to', ''))}"]
    for key in ("cc", "bcc"):
        if h.get(key):
            lines.append(f"{key.capitalize()}: {_addresses(h[key])}")
    lines.append(f"Subject: “{clean(h.get('subject', ''), 300)}”")
    lines.append(f"Body ({len(body)} characters):")
    lines.append(clean_block(body))
    return "\n".join(lines)


async def describe_trash(s: Services, a: dict) -> str:
    ids = id_list(a)
    try:
        lines = [clean(line.split("\n")[0], 160) for line in await s.gmail.summaries(ids)]
    except ToolError:
        lines = [f"- {i}" for i in ids]
    return f"Move {len(ids)} email(s) to the trash:\n" + "\n".join(f"  {line}" for line in lines)


async def describe_delete_label(s: Services, a: dict) -> str:
    return f"Delete the Gmail label “{clean(a.get('name'))}” (the emails keep existing)"


async def shares_with_others(services: Services, o: dict) -> bool:
    """Adding a task or section to a shared Todoist project shows it to (and may notify) other people,
    so it needs approval like any other message."""
    if o["op"] not in ("todoist.add", "todoist.add_section"):
        return False
    a = o["args"]
    projects = {p["id"]: p for p in await services.todoist.projects()}
    pid = None
    if a.get("parent"):
        pid = (await services.todoist.task(a["parent"])).get("project_id")
    elif a.get("project"):
        pid = await services.todoist.project_id(a["project"])
    p = projects.get(pid, {}) if pid else {}
    return bool(p.get("is_shared") or p.get("workspace_id"))  # team workspace projects are visible to members


async def describe_add(s: Services, a: dict) -> str:
    where = a.get("project") or (f"the project of task {a.get('parent')}" if a.get("parent") else "Inbox")
    what = clean(a.get("content") or a.get("name", ""))
    if not await shares_with_others(s, {"op": "todoist.add", "args": a}):
        return f"Add “{what}” to “{clean(where)}”"
    lines = [f"Add “{what}” to the SHARED project “{clean(where)}” (its members will see it)"]
    if a.get("description"):
        lines.append(clean_block(a["description"]))
    return "\n".join(lines)


OPS: dict[str, Op] = {op.name: op for op in [
    # Todoist: reading and adding are safe; changing existing tasks and commenting need approval.
    Op("todoist.tasks", False, td_tasks),
    Op("todoist.task", False, td_task),
    Op("todoist.projects", False, td_projects),
    Op("todoist.labels", False, td_labels),
    Op("todoist.completed", False, td_completed),
    Op("todoist.ids", False, td_ids),
    Op("todoist.show", False, td_show),
    Op("todoist.add", False, td_add, describe_add, "Add task"),
    Op("todoist.add_project", False, td_add_project),
    Op("todoist.add_section", False, td_add_section, describe_add, "Add section"),
    Op("todoist.add_label", False, td_add_label),
    Op("todoist.comment", True, td_comment, describe_comment, "Comment"),
    Op("todoist.update", True, td_update, describe_update, "Change task"),
    Op("todoist.move", True, td_move, describe_move, "Move task"),
    Op("todoist.complete", True, td_complete, _describe_task("Complete"), "Complete task"),
    Op("todoist.reopen", True, td_reopen, _describe_task("Reopen"), "Reopen task"),
    Op("todoist.delete", True, td_delete, _describe_task("Delete", " and its subtasks"), "DELETE task"),
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
    Op("gmail.send", True, gm_send, describe_send, "SEND email"),
    Op("gmail.send_draft", True, gm_send_draft, describe_send_draft, "SEND draft"),
    Op("gmail.trash", True, gm_trash, describe_trash, "Trash emails"),
    Op("gmail.delete_label", True, gm_delete_label, describe_delete_label, "Delete label"),
]}

# Never runnable without approval, whatever the config says.
ALWAYS_CRITICAL = frozenset({"gmail.send", "gmail.send_draft", "gmail.trash", "todoist.delete", "todoist.comment"})


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
    decided REAL,
    nonce TEXT,
    message_id INTEGER
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
        have = {r[1] for r in self.db.execute("PRAGMA table_info(approvals)")}
        for column, kind in (("nonce", "TEXT"), ("message_id", "INTEGER")):
            if column not in have:  # databases created by older versions
                self.db.execute(f"ALTER TABLE approvals ADD COLUMN {column} {kind}")
        # Requests from before nonces existed can never be answered safely.
        self.db.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND nonce IS NULL")
        self.db.commit()

    def add(self, ops: list[dict], summary: str, reason: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO approvals (created, ops, summary, reason, nonce) VALUES (?, ?, ?, ?, ?)",
            (time.time(), json.dumps(ops), summary, reason, secrets.token_hex(8)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def expire(self) -> None:
        self.db.execute(
            "UPDATE approvals SET status='expired' WHERE status='pending' AND created < ?",
            (time.time() - APPROVAL_TTL,),
        )
        self.db.commit()

    def pending_count(self) -> int:
        self.expire()
        return self.db.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]

    def unnotified(self) -> list[dict]:
        self.expire()
        rows = self.db.execute(
            "SELECT id, summary, reason, nonce, created FROM approvals WHERE status='pending' AND notified=0 ORDER BY id"
        )
        return [dict(r) for r in rows]

    def mark_notified(self, approval_id: int, message_id: int | None) -> None:
        self.db.execute("UPDATE approvals SET notified=1, message_id=? WHERE id=?", (message_id, approval_id))
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


async def prepare(services: Services, ops: list[dict]) -> list[dict]:
    """Fixes everything a critical request depends on before the owner sees it."""
    out = []
    for o in ops:
        a = dict(o["args"])
        if o["op"] == "todoist.move":
            a.update(await _move_target(services, a))
        elif o["op"] == "gmail.send":
            resolved = await services.gmail.resolve_mail(_mail_fields(a))
            if len(resolved["body"]) > MAX_MAIL_BODY:
                raise ToolError(
                    f"that email is too long to show for approval ({len(resolved['body'])} characters, max "
                    f"{MAX_MAIL_BODY}); save it as a draft (`gmail draft`) and tell the owner to send it from Gmail"
                )
            a = {"resolved": resolved}
        elif o["op"] == "gmail.send_draft":
            d = await services.gmail.get_draft(a.get("id", ""))
            a["message_id"] = d.get("message", {}).get("id")
        out.append({"op": o["op"], "args": a})
    return out


async def execute(services: Services, ops: list[dict]) -> tuple[bool, str, dict]:
    """Runs ops in order and stops at the first failure. Returns (all ok, text, extra fields)."""
    texts: list[str] = []
    extra: dict = {}
    for i, o in enumerate(ops):
        try:
            result = await OPS[o["op"]].run(services, o["args"])
        except Exception as e:
            msg = str(e) if isinstance(e, ToolError) else f"{type(e).__name__}: {e}"
            if not isinstance(e, ToolError):
                log.exception("%s failed", o["op"])
            texts.append(f"✗ {o['op']}: {msg}")
            if len(ops) > 1:
                texts.append(f"(step {i + 1} of {len(ops)} failed; {i} done before it, {len(ops) - i - 1} not run)")
            return False, "\n".join(texts), extra
        if isinstance(result, dict):
            extra.update({k: v for k, v in result.items() if k != "text"})
            result = result.get("text", "")
        if result:
            texts.append(result)
    return True, "\n".join(texts), extra


async def summarize(services: Services, ops: list[dict], policy: Policy) -> str:
    """The text the owner approves: a header counting every action, then each action in full.
    Raises if it wouldn't fit, rather than hiding anything."""
    counts = Counter(OPS[o["op"]].label or o["op"] for o in ops)
    header = f"{len(ops)} action(s): " + ", ".join(f"{n}× {label}" for label, n in counts.items())
    lines = [header]
    for o in ops:
        op = OPS[o["op"]]
        if op.describe:
            line = await op.describe(services, o["args"])
        else:  # shown in full; if that is too long, the size check below refuses the request
            line = f"{o['op']} " + clean(json.dumps(o["args"], ensure_ascii=False))
        lines.append("• " + line)
    summary = "\n".join(lines)
    size = len(summary.encode("utf-16-le")) // 2  # how Telegram counts message length
    if size > MAX_SUMMARY:
        raise ToolError(
            f"this request is too big to show the owner in one message ({size} characters, max "
            f"{MAX_SUMMARY}); split it into smaller requests"
        )
    return summary
