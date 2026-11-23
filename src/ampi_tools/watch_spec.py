"""Watcher specs (watchers v2): parsing, validation, templates, conditions, the manifest and the risk level.

Standard library only. Used by both sides:
- ampi-tools' keeper: freezes a proposed snapshot, computes the manifest and risk itself (never trusts the
  builder's description), checks every watcher action against it.
- the bridge's engine: runs the approved snapshot it gets from the keeper.

A snapshot is a dict {relative path: bytes}: "watcher.toml", "scripts/*.py", "prompts/*.md", "templates/*",
"lib/*.py" (the exact shared helpers the watcher uses).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,63}){0,3}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_FILE = 256 * 1024
MAX_SNAPSHOT = 2 * 1024 * 1024
MAX_FILES = 60
MAX_STEPS = 30
SNAPSHOT_DIRS = ("scripts", "prompts", "templates", "lib")
MODELS = ("haiku", "sonnet", "opus")
MODEL_ID_RE = re.compile(r"^claude-[a-z0-9.-]{3,60}$")


class SpecError(ValueError):
    """The spec or snapshot is invalid; the message says what to fix."""


# --- operations: what they change and where data goes ------------------------------------------------
#
# route:   arguments that decide where data goes (recipients, chats, projects, playlists, sites). A route given
#          by a template is "variable": an injected email could choose it.
# subject: arguments that pick which existing thing is changed (a message id, a task id).
# level:   risk of the operation itself: "medium" (changes something, reversible or contained) or "high"
#          (sends to people, publishes, deletes, pays, reveals personal data).
# Every ampi-tools operation not listed here is a read.

@dataclass(frozen=True)
class OpInfo:
    level: str
    route: tuple[str, ...] = ()
    subject: tuple[str, ...] = ()


WRITE_OPS: dict[str, OpInfo] = {
    "todoist.add": OpInfo("medium", route=("project", "section", "parent")),
    "todoist.add_project": OpInfo("medium"),
    "todoist.add_section": OpInfo("medium", route=("project",)),
    "todoist.add_label": OpInfo("medium"),
    "todoist.comment": OpInfo("medium", subject=("id",)),
    "todoist.update": OpInfo("medium", subject=("id",)),
    "todoist.move": OpInfo("medium", route=("project", "section", "parent"), subject=("id",)),
    "todoist.complete": OpInfo("medium", subject=("id",)),
    "todoist.reopen": OpInfo("medium", subject=("id",)),
    "todoist.delete": OpInfo("high", subject=("id",)),
    "gmail.label": OpInfo("medium", subject=("id", "ids")),
    "gmail.archive": OpInfo("medium", subject=("id", "ids")),
    "gmail.mark_read": OpInfo("medium", subject=("id", "ids")),
    "gmail.mark_unread": OpInfo("medium", subject=("id", "ids")),
    "gmail.star": OpInfo("medium", subject=("id", "ids")),
    "gmail.unstar": OpInfo("medium", subject=("id", "ids")),
    "gmail.untrash": OpInfo("medium", subject=("id", "ids")),
    "gmail.draft": OpInfo("medium", subject=("to", "cc", "reply_to")),  # a draft goes nowhere until sent
    "gmail.send": OpInfo("high", route=("to", "cc", "reply_to")),
    "gmail.send_draft": OpInfo("high", subject=("id",)),
    "gmail.trash": OpInfo("high", subject=("id", "ids")),
    "gmail.delete_label": OpInfo("high", subject=("name",)),
    "youtube.new_playlist": OpInfo("medium"),
    "youtube.add": OpInfo("medium", route=("playlist", "playlist_name")),
    "youtube.remove": OpInfo("high", route=("playlist", "playlist_name")),
    "youtube.subscribe": OpInfo("high", route=("channel",)),
    "youtube.unsubscribe": OpInfo("high", route=("channel",)),
    "threads.publish": OpInfo("high", route=("reply_to",)),
    "threads.delete": OpInfo("high", subject=("id",)),
    "threads.hide": OpInfo("medium", subject=("id",)),
    "threads.unhide": OpInfo("medium", subject=("id",)),
    "instagram.reply": OpInfo("high", route=("reply_to", "id")),
    "instagram.delete_comment": OpInfo("high", subject=("id",)),
    "instagram.hide": OpInfo("medium", subject=("id",)),
    "instagram.unhide": OpInfo("medium", subject=("id",)),
    "telegram.send": OpInfo("medium", route=("chat",)),
    "telegram.edit": OpInfo("medium", subject=("chat", "message")),
    "telegram.delete": OpInfo("high", subject=("chat", "message", "messages")),
    "wolt.basket": OpInfo("medium", route=("venue",)),
    "wolt.basket_replace": OpInfo("medium", route=("venue",)),
    "info.reveal": OpInfo("high"),
    "info.collect": OpInfo("high"),
    # Checkout: "site" is the https origin of the checkout session's page, checked by the keeper (it is not an
    # argument of the op). Watchers can use checkout only through actions declared with a fixed site.
    "checkout.open": OpInfo("high", route=("site",)),
    "checkout.snapshot": OpInfo("high", route=("site",)),
    "checkout.click": OpInfo("high", route=("site",)),
    "checkout.type": OpInfo("high", route=("site",)),
    "checkout.choose": OpInfo("high", route=("site",)),
    "checkout.back": OpInfo("high", route=("site",)),
    "checkout.close": OpInfo("high", route=("site",)),
    "checkout.fill": OpInfo("high", route=("site",)),
    "checkout.pay": OpInfo("high", route=("site",)),
    # Instagram/Threads through the owner's web session: public, in the owner's name (never for watchers, below).
    "igweb.comment": OpInfo("high", route=("target", "comment")),
    "igweb.like": OpInfo("high", subject=("target", "comment")),
    "igweb.unlike": OpInfo("high", subject=("target", "comment")),
    "igweb.follow": OpInfo("high", subject=("user",)),
    "igweb.unfollow": OpInfo("high", subject=("user",)),
    "igweb.delete_comment": OpInfo("high", subject=("target", "comment")),
    "thweb.like": OpInfo("high", subject=("target",)),
    "thweb.unlike": OpInfo("high", subject=("target",)),
    "thweb.follow": OpInfo("high", subject=("user",)),
    "thweb.unfollow": OpInfo("high", subject=("user",)),
}
CHECKOUT_LIMIT_KEYS = ("max_amount", "per_week", "per_month", "card", "item")  # extra keys of a declared checkout.pay
# Management operations a watcher never uses (they change Ampi's own permissions or are chat-only helpers).
# Personal info is never handed to a watcher (an AI step could pass it on); checkout.fill still fills it by reference.
# The owner's Instagram/Threads web session: watchers may only read direct messages (adapter steps, igweb.inbox /
# igweb.dm); its other reads belong to social-mode chats, and every change is approved by the owner one by one
# (a watcher's "allow all" must never cover them).
SOCIAL_SESSION_READS = frozenset({"igweb.feed", "igweb.profile", "igweb.posts", "igweb.post", "igweb.stories",
                                  "igweb.story", "thweb.feed", "thweb.profile", "thweb.posts", "thweb.post"})
SOCIAL_SESSION_WRITES = frozenset(op for op in WRITE_OPS if op.startswith(("igweb.", "thweb.")))
NOT_FOR_WATCHERS = frozenset({"telegram.revoke", "telegram.permissions", "info.revoke", "approvals.list",
                              "info.fields", "info.reveal", "info.collect", "info.save_place", "payment.log"}) \
    | SOCIAL_SESSION_READS | SOCIAL_SESSION_WRITES
# Reads a watcher test run never makes for real: it gets stub data (ampi_tools.social_dm) instead.
TEST_STUB_OPS = frozenset({"igweb.inbox", "igweb.dm"})
# A route that is variable on one of these makes the watcher high risk even when the op itself is medium.
SENDS_OUT = frozenset({"telegram.send", "youtube.add", "todoist.add", "todoist.move", "todoist.add_section",
                       "wolt.basket", "wolt.basket_replace"})


def is_write(op: str) -> bool:
    return op in WRITE_OPS


# --- triggers ------------------------------------------------------------------------------------------

TRIGGERS: dict[str, dict[str, Any]] = {
    # type: {"source": the field that identifies what is watched, "every": (default, minimum) minutes}
    "time.every": {"source": None, "every": None},
    "time.at": {"source": None, "every": None},
    "manual": {"source": None, "every": None},
    "gmail.new": {"source": "query", "every": (5, 1)},
    "todoist.new": {"source": "filter", "every": (5, 1)},
    "telegram.new": {"source": "chat", "every": (2, 1)},
    "youtube.new": {"source": "channels", "every": (60, 15)},
    "threads.reply": {"source": None, "every": (30, 10)},
    "instagram.comment": {"source": None, "every": (30, 10)},
    "web.change": {"source": "url", "every": (30, 10)},
}

STEP_TYPES = ("script", "llm", "adapter", "notify", "web")


# --- small validators -----------------------------------------------------------------------------------

HIDDEN_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def _str(v: Any, where: str, limit: int = 300, required: bool = True, multiline: bool = False) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
        raise SpecError(f"{where}: must be text of at most {limit} characters")
    if HIDDEN_RE.search(v) or (not multiline and ("\n" in v or "\r" in v)):
        raise SpecError(f"{where}: no line breaks or invisible characters")
    return v.strip()


def _num(v: Any, where: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise SpecError(f"{where}: must be a number between {lo:g} and {hi:g}")
    return float(v)


def _bool(v: Any, where: str) -> bool:
    if not isinstance(v, bool):
        raise SpecError(f"{where}: must be true or false")
    return v


def _str_list(v: Any, where: str, limit: int = 300, count: int = 50) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or len(v) > count or not all(isinstance(x, str) and 0 < len(x) <= limit for x in v):
        raise SpecError(f"{where}: must be a list of at most {count} texts")
    if any(HIDDEN_RE.search(x) or "\n" in x or "\r" in x for x in v):
        raise SpecError(f"{where}: no line breaks or invisible characters")
    return [x.strip() for x in v]


def _only(d: dict, allowed: set[str], where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise SpecError(f"{where}: unknown field(s) {', '.join(sorted(extra))}")


def check_path(p: str) -> str:
    """A snapshot or space file name: relative, plain characters, no '..'."""
    if not isinstance(p, str) or not PATH_RE.fullmatch(p) or ".." in p.split("/"):
        raise SpecError(f"bad file name {p!r} (letters, digits, . _ - and up to 3 folders; no ..)")
    return p


# --- snapshots ------------------------------------------------------------------------------------------

def check_snapshot(files: dict[str, bytes]) -> None:
    if not isinstance(files, dict) or "watcher.toml" not in files:
        raise SpecError("the snapshot needs watcher.toml")
    if len(files) > MAX_FILES:
        raise SpecError(f"at most {MAX_FILES} files per watcher")
    total = 0
    for path, data in files.items():
        check_path(path)
        if path != "watcher.toml" and path.split("/", 1)[0] not in SNAPSHOT_DIRS:
            raise SpecError(f"{path}: files go in {', '.join(SNAPSHOT_DIRS)}/")
        if not isinstance(data, bytes) or len(data) > MAX_FILE:
            raise SpecError(f"{path}: at most {MAX_FILE // 1024} KB")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise SpecError(f"{path}: must be UTF-8 text") from None
        total += len(data)
    if total > MAX_SNAPSHOT:
        raise SpecError(f"the watcher's files are larger than {MAX_SNAPSHOT // 1024 // 1024} MB")


def snapshot_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode() + b"\0" + hashlib.sha256(files[path]).hexdigest().encode() + b"\n")
    return h.hexdigest()


def text_of(files: dict[str, bytes], path: str) -> str:
    if path not in files:
        raise SpecError(f"missing file {path}")
    return files[path].decode("utf-8")


# --- LLM output schemas ------------------------------------------------------------------------------------

FIELD_RE = re.compile(r"^(text|int|number|bool)(?::(\d{1,5}))?(\?)?$")
LIST_RE = re.compile(r"^list\[(text|int|number)(?::(\d{1,4}))?\]:(\d{1,3})(\?)?$")
ENUM_RE = re.compile(r"^enum\[([A-Za-z0-9_ -]{1,40}(?:\|[A-Za-z0-9_ -]{1,40}){0,19})\](\?)?$")


def check_schema(schema: Any, where: str) -> dict:
    if not isinstance(schema, dict) or not schema or len(schema) > 20:
        raise SpecError(f"{where}: schema must be a table of 1-20 fields")
    out: dict[str, Any] = {}
    for name, kind in schema.items():
        if not STEP_ID_RE.fullmatch(name):
            raise SpecError(f"{where}: bad field name {name!r}")
        if isinstance(kind, list):  # a list of outcomes
            vals = _str_list(kind, f"{where}.{name}", 40, 20)
            if not vals:
                raise SpecError(f"{where}.{name}: needs at least one outcome")
            out[name] = {"type": "enum", "values": vals, "optional": False}
            continue
        if not isinstance(kind, str):
            raise SpecError(f"{where}.{name}: unknown type")
        if m := FIELD_RE.fullmatch(kind):
            limit = int(m.group(2) or (2000 if m.group(1) == "text" else 0))
            if m.group(1) == "text" and not 1 <= limit <= 8000:
                raise SpecError(f"{where}.{name}: text length 1-8000")
            out[name] = {"type": m.group(1), "limit": limit, "optional": bool(m.group(3))}
        elif m := LIST_RE.fullmatch(kind):
            out[name] = {"type": "list", "item": m.group(1), "limit": int(m.group(2) or 300),
                         "count": int(m.group(3)), "optional": bool(m.group(4))}
        elif m := ENUM_RE.fullmatch(kind):
            out[name] = {"type": "enum", "values": m.group(1).split("|"), "optional": bool(m.group(2))}
        else:
            raise SpecError(f"{where}.{name}: unknown type {kind!r} (text:N, int, number, bool, enum[a|b], "
                            "list[text:N]:COUNT, or a list of outcomes; add ? for optional)")
    return out


def _scalar(kind: str, v: Any, limit: int, where: str) -> Any:
    if kind == "text":
        if not isinstance(v, str):
            raise SpecError(f"{where}: expected text")
        return v[:limit]
    if kind == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise SpecError(f"{where}: expected a whole number")
        return v
    if kind == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SpecError(f"{where}: expected a number")
        return v
    if kind == "bool":
        if not isinstance(v, bool):
            raise SpecError(f"{where}: expected true or false")
        return v
    raise SpecError(f"{where}: unknown type")


def check_output(schema: dict, data: Any) -> dict:
    """Validates an LLM's answer against a checked schema; extra fields are dropped, text is cut to its limit."""
    if not isinstance(data, dict):
        raise SpecError("the answer must be a JSON object")
    out: dict[str, Any] = {}
    for name, f in schema.items():
        if name not in data or data[name] is None:
            if f["optional"]:
                out[name] = None
                continue
            raise SpecError(f"missing field {name}")
        v = data[name]
        if f["type"] == "enum":
            if v not in f["values"]:
                raise SpecError(f"{name} must be one of {', '.join(f['values'])}")
            out[name] = v
        elif f["type"] == "list":
            if not isinstance(v, list):
                raise SpecError(f"{name}: expected a list")
            out[name] = [_scalar(f["item"], x, f["limit"], name) for x in v[: f["count"]]]
        else:
            out[name] = _scalar(f["type"], v, f["limit"], name)
    return out


def schema_prompt(schema: dict) -> str:
    """How the schema is explained to the model."""
    lines = []
    for name, f in schema.items():
        opt = " (may be null)" if f["optional"] else ""
        if f["type"] == "enum":
            lines.append(f'- "{name}": exactly one of {json.dumps(f["values"], ensure_ascii=False)}{opt}')
        elif f["type"] == "list":
            lines.append(f'- "{name}": a list of at most {f["count"]} {f["item"]} values{opt}')
        elif f["type"] == "text":
            lines.append(f'- "{name}": text, at most {f["limit"]} characters{opt}')
        else:
            lines.append(f'- "{name}": {f["type"]}{opt}')
    return "\n".join(lines)


# --- templates and conditions -------------------------------------------------------------------------------

REF_RE = re.compile(r"\{\{\s*((?:item|steps|run|watcher)(?:\.[A-Za-z0-9_-]{1,40}){0,6})\s*\}\}")
WHOLE_REF_RE = re.compile(r"^\s*\{\{\s*((?:item|steps|run|watcher)(?:\.[A-Za-z0-9_-]{1,40}){0,6})\s*\}\}\s*$")


def lookup(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    return json.dumps(v, ensure_ascii=False)


def render(value: Any, ctx: dict) -> Any:
    """Fills {{item.x}} / {{steps.id.field}} references in the string leaves of an already-parsed value.

    One pass, never recursive: text that a reference brings in is never searched for references again. A string
    that is exactly one reference keeps the value's type (a list stays a list)."""
    if isinstance(value, str):
        whole = WHOLE_REF_RE.fullmatch(value)
        if whole:
            return copy.deepcopy(lookup(ctx, whole.group(1)))
        return REF_RE.sub(lambda m: _as_text(lookup(ctx, m.group(1))), value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}  # keys are never templated
    return value


def refs_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [m.group(1) for m in REF_RE.finditer(value)]
    if isinstance(value, list):
        return [r for v in value for r in refs_in(v)]
    if isinstance(value, dict):
        return [r for v in value.values() for r in refs_in(v)]
    return []


def has_template(value: Any) -> bool:
    return bool(refs_in(value))


COND_FORMS = ("equals", "in", "not_in", "exists", "true", "false")


def check_when(when: Any, where: str) -> dict:
    if not isinstance(when, dict) or "path" not in when:
        raise SpecError(f"{where}: when needs path and one of {', '.join(COND_FORMS)}")
    _only(when, {"path", *COND_FORMS}, where)
    path = when["path"]
    if not isinstance(path, str) or not re.fullmatch(r"(item|steps|run)(\.[A-Za-z0-9_-]{1,40}){1,6}", path):
        raise SpecError(f"{where}: path must look like steps.<id>.<field> or item.<field>")
    forms = [f for f in COND_FORMS if f in when]
    if len(forms) != 1:
        raise SpecError(f"{where}: exactly one of {', '.join(COND_FORMS)}")
    if forms[0] in ("in", "not_in") and not isinstance(when[forms[0]], list):
        raise SpecError(f"{where}: {forms[0]} must be a list")
    if forms[0] in ("exists", "true", "false") and when[forms[0]] is not True:
        raise SpecError(f"{where}: write {forms[0]} = true")
    return dict(when)


def condition(when: dict | None, ctx: dict) -> bool:
    if not when:
        return True
    v = lookup(ctx, when["path"])
    if "equals" in when:
        return v == when["equals"]
    if "in" in when:
        return v in when["in"]
    if "not_in" in when:
        return v not in when["not_in"]
    if "exists" in when:
        return v not in (None, "", [], {})
    if "true" in when:
        return v is True
    return v is False


# --- the spec ------------------------------------------------------------------------------------------------

def _trigger(t: Any, i: int) -> dict:
    where = f"triggers[{i}]"
    if not isinstance(t, dict) or t.get("type") not in TRIGGERS:
        raise SpecError(f"{where}: type must be one of {', '.join(TRIGGERS)}")
    kind = t["type"]
    info = TRIGGERS[kind]
    out: dict[str, Any] = {"type": kind}
    common = {"type", "batch", "ttl_minutes", "latest_only"}
    if kind == "time.every":
        _only(t, common | {"minutes"}, where)
        out["minutes"] = _num(t.get("minutes"), f"{where}.minutes", 1, 10080)
    elif kind == "time.at":
        _only(t, common | {"at", "days"}, where)
        at = _str_list(t.get("at"), f"{where}.at", 5, 24)
        if not at or not all(TIME_RE.fullmatch(x) for x in at):
            raise SpecError(f'{where}: at = ["HH:MM", ...] (24-hour)')
        days = [d.lower()[:3] for d in _str_list(t.get("days", list(DAYS)), f"{where}.days", 9, 7)]
        if not days or any(d not in DAYS for d in days):
            raise SpecError(f"{where}: days from {', '.join(DAYS)}")
        out.update(at=sorted(set(at)), days=days)
    elif kind == "manual":
        _only(t, common, where)
    else:
        src = info["source"]
        allowed = common | {"every_minutes"} | ({src} if src else set()) | ({"ignore"} if kind == "web.change" else set())
        _only(t, allowed, where)
        if src:
            out[src] = _str(t.get(src), f"{where}.{src}", 500)
        if kind == "web.change":
            if not re.fullmatch(r"https://[^\s{}]{4,490}", out["url"]):
                raise SpecError(f"{where}.url: a fixed https:// address")
            out["ignore"] = _str_list(t.get("ignore", []), f"{where}.ignore", 200, 20)
            for p in out["ignore"]:
                try:
                    re.compile(p)
                except re.error as e:
                    raise SpecError(f"{where}.ignore: bad regular expression {p!r}: {e}") from None
        default, minimum = info["every"]
        out["every_minutes"] = _num(t.get("every_minutes", default), f"{where}.every_minutes", minimum, 1440)
    out["batch"] = _bool(t.get("batch", False), f"{where}.batch")
    out["latest_only"] = _bool(t.get("latest_only", kind in ("web.change", "time.every", "time.at")),
                               f"{where}.latest_only")
    out["ttl_minutes"] = _num(t.get("ttl_minutes", 24 * 60), f"{where}.ttl_minutes", 1, 30 * 24 * 60)
    return out


def trigger_identity(t: dict) -> str:
    """What a trigger's memory belongs to: its type and source, not its interval (changing that loses nothing)."""
    src = TRIGGERS[t["type"]]["source"]
    ident = {"type": t["type"], **({src: t[src]} if src else {})}
    if t["type"] == "time.at":
        ident.update(at=t["at"], days=t["days"])
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]


def _declared_action(a: Any, where: str) -> dict:
    if not isinstance(a, dict) or not isinstance(a.get("op"), str):
        raise SpecError(f"{where}: each declared action needs op")
    op = a["op"]
    if op in NOT_FOR_WATCHERS:
        raise SpecError(f"{where}: {op} isn't available to watchers")
    info = WRITE_OPS.get(op)
    allowed = {"op", *(info.route if info else ()), *(info.subject if info else ())}
    if op == "checkout.pay":
        allowed |= set(CHECKOUT_LIMIT_KEYS)
    _only(a, allowed, where)
    if op.startswith("checkout."):
        site = a.get("site")
        if not isinstance(site, str) or not re.fullmatch(r"https://[a-z0-9.-]{3,253}(:\d{1,5})?", site):
            raise SpecError(f"{where}: {op} needs site = \"https://shop.example\" (the exact site, fixed)")
    if op == "checkout.pay":
        _num(a.get("max_amount"), f"{where}.max_amount", 0.01, 100000)
        _num(a.get("per_week", 1), f"{where}.per_week", 1, 50)
        if "per_month" in a:
            _num(a["per_month"], f"{where}.per_month", 1, 200)
        if "card" in a and (not isinstance(a["card"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", a["card"])):
            raise SpecError(f"{where}.card: the card entry's name, e.g. card-main")
        if "item" in a:
            _str(a["item"], f"{where}.item", 120)
    # A declared route is either a fixed value or a list of allowed values (an allowlist); "*" means any.
    return dict(a)


AGENT_TOOL_RE = re.compile(r"^(Read|Glob|Grep|WebSearch|WebFetch|Skill|Edit|Write|Bash\([a-z][a-z0-9-]{0,30}:\*\))$")
AGENT_FORBIDDEN_BASH = {"env", "sh", "bash", "dash", "zsh", "python", "python3", "uv", "uvx", "pip", "xargs",
                        "timeout", "nohup", "sudo", "su", "watchers", "node", "perl", "ruby", "curl", "wget"}


def _step(s: Any, i: int, earlier: list[str], files: dict[str, bytes]) -> dict:
    where = f"steps[{i}]"
    if not isinstance(s, dict):
        raise SpecError(f"{where}: must be a table")
    sid = s.get("id")
    if not isinstance(sid, str) or not STEP_ID_RE.fullmatch(sid):
        raise SpecError(f"{where}: id must be lowercase letters, digits or _ (starting with a letter)")
    where = f"step {sid}"
    if sid in earlier:
        raise SpecError(f"{where}: duplicate step id")
    kind = s.get("type")
    if kind not in STEP_TYPES:
        raise SpecError(f"{where}: type must be one of {', '.join(STEP_TYPES)}")
    common = {"id", "type", "when", "timeout", "on_error"}
    out: dict[str, Any] = {"id": sid, "type": kind}
    if "when" in s:
        out["when"] = check_when(s["when"], f"{where}.when")
    out["on_error"] = s.get("on_error", "fail")
    if out["on_error"] not in ("fail", "continue"):
        raise SpecError(f"{where}: on_error is fail or continue")

    if kind == "script":
        _only(s, common | {"run", "args", "actions"}, where)
        run = _str(s.get("run"), f"{where}.run", 80)
        if m := re.fullmatch(r"lib/([a-z][a-z0-9_]{0,40})(?:\.py)?", run):
            path = f"lib/{m.group(1)}.py"
        elif m := re.fullmatch(r"scripts/([A-Za-z0-9_][A-Za-z0-9_-]{0,40})(?:\.py)?", run):
            path = f"scripts/{m.group(1)}.py"
        else:
            raise SpecError(f'{where}.run: "lib/<name>" or "scripts/<name>"')
        if path not in files:
            raise SpecError(f"{where}: {path} is not in the watcher's files")
        args = s.get("args", {})
        if not isinstance(args, dict):
            raise SpecError(f"{where}.args: must be a table")
        actions = s.get("actions", [])
        if not isinstance(actions, list) or len(actions) > 10:
            raise SpecError(f"{where}.actions: a list of at most 10 declared actions")
        out.update(run=path, args=args, actions=[_declared_action(a, f"{where}.actions") for a in actions])
        out["timeout"] = _num(s.get("timeout", 60), f"{where}.timeout", 1, 120)
    elif kind == "llm":
        _only(s, common | {"mode", "model", "prompt", "input", "schema", "tools", "max_turns", "actions"}, where)
        mode = s.get("mode", "structured")
        if mode not in ("structured", "agent"):
            raise SpecError(f"{where}.mode: structured or agent")
        model = _str(s.get("model", "haiku" if mode == "structured" else "sonnet"), f"{where}.model", 64)
        if model not in MODELS and not MODEL_ID_RE.fullmatch(model):
            raise SpecError(f"{where}.model: haiku, sonnet, opus or a full claude-… model id")
        prompt = _str(s.get("prompt"), f"{where}.prompt", 80)
        if not prompt.startswith("prompts/") or prompt not in files:
            raise SpecError(f"{where}.prompt: a file in prompts/ that exists, e.g. prompts/summary.md")
        out.update(mode=mode, model=model, prompt=check_path(prompt), input=s.get("input", ""))
        if mode == "structured":
            if "tools" in s or "actions" in s:
                raise SpecError(f"{where}: structured LLM steps have no tools or actions (use mode = \"agent\")")
            out["schema"] = check_schema(s.get("schema"), f"{where}.schema")
            out["timeout"] = _num(s.get("timeout", 180), f"{where}.timeout", 10, 600)
        else:
            if "schema" in s:
                out["schema"] = check_schema(s["schema"], f"{where}.schema")
            tools = _str_list(s.get("tools", []), f"{where}.tools", 60, 30)
            for t in tools:
                if not AGENT_TOOL_RE.fullmatch(t):
                    raise SpecError(f"{where}.tools: {t!r} isn't allowed (Read, Glob, Grep, WebSearch, WebFetch, "
                                    "Skill, Edit, Write or Bash(command:*))")
                m = re.fullmatch(r"Bash\(([a-z0-9-]+):\*\)", t)
                if m and m.group(1) in AGENT_FORBIDDEN_BASH:
                    raise SpecError(f"{where}.tools: Bash({m.group(1)}:*) would let the step run anything")
            out["tools"] = tools
            # Actions the agent may take through ampi-tools that "allow all" grants (others ask each time).
            actions = s.get("actions", [])
            if not isinstance(actions, list) or len(actions) > 20:
                raise SpecError(f"{where}.actions: a list of at most 20 declared actions")
            out["actions"] = [_declared_action(a, f"{where}.actions") for a in actions]
            out["max_turns"] = int(_num(s.get("max_turns", 30), f"{where}.max_turns", 1, 100))
            out["timeout"] = _num(s.get("timeout", 900), f"{where}.timeout", 30, 1800)
    elif kind == "adapter":
        _only(s, common | {"op", "args"}, where)
        op = _str(s.get("op"), f"{where}.op", 60)
        if has_template(op) or not re.fullmatch(r"[a-z]+\.[a-z_]+", op):
            raise SpecError(f"{where}.op: an operation name like gmail.show (no templates)")
        if op in NOT_FOR_WATCHERS:
            raise SpecError(f"{where}: {op} isn't available to watchers")
        args = s.get("args", {})
        if not isinstance(args, dict):
            raise SpecError(f"{where}.args: must be a table")
        out.update(op=op, args=args)
        out["timeout"] = _num(s.get("timeout", 120), f"{where}.timeout", 1, 900)
    elif kind == "notify":
        _only(s, common | {"template", "text", "format"}, where)
        if ("template" in s) == ("text" in s):
            raise SpecError(f"{where}: give template (a file in templates/) or text")
        if "template" in s:
            t = _str(s["template"], f"{where}.template", 80)
            if not t.startswith("templates/") or t not in files:
                raise SpecError(f"{where}.template: a file in templates/ that exists")
            out["template"] = t
        else:
            out["text"] = _str(s["text"], f"{where}.text", 4000, multiline=True)
        out["format"] = s.get("format", "rich")
        if out["format"] not in ("rich", "plain", "markdown"):
            raise SpecError(f"{where}.format: rich, plain or markdown")
        out["timeout"] = 60.0
    else:  # web
        _only(s, common | {"url", "max_chars"}, where)
        url = _str(s.get("url"), f"{where}.url", 500)
        if has_template(url) or not re.fullmatch(r"https://[^\s{}]{4,490}", url):
            raise SpecError(f"{where}.url: a fixed https:// address (no templates)")
        out.update(url=url, max_chars=int(_num(s.get("max_chars", 20000), f"{where}.max_chars", 100, 200000)))
        out["timeout"] = _num(s.get("timeout", 90), f"{where}.timeout", 5, 120)

    # References may only point at the item, the run, or steps that come earlier.
    for ref in refs_in({k: v for k, v in out.items() if k not in ("when", "schema")}) + (
            [out["when"]["path"]] if "when" in out else []):
        parts = ref.split(".")
        if parts[0] == "steps" and (len(parts) < 2 or parts[1] not in earlier):
            raise SpecError(f"{where}: {ref} refers to a step that doesn't come before this one")
    return out


def _check_social_shape(steps: list[dict]) -> None:
    """A watcher that reads the owner's Instagram/Threads session (direct messages) may only summarize them for the
    owner: scripts, structured (tool-less) AI steps and markdown messages (defanged); no agent or web step and no
    change anywhere in it, so strangers' text can't reach a tool, a website or another person."""
    def social(op: str) -> bool:
        return op.startswith(("igweb.", "thweb."))

    reads = [s["id"] for s in steps if (s["type"] == "adapter" and social(s["op"]))
             or any(social(a["op"]) for a in s.get("actions", []))]
    if not reads:
        return
    where = f"step {reads[0]} reads the owner's Instagram/Threads session"
    for s in steps:
        if s["type"] == "web" or (s["type"] == "llm" and s["mode"] == "agent"):
            raise SpecError(f"{where}: no agent or web steps in this watcher (step {s['id']}); use a structured AI step")
        if s["type"] == "notify" and s["format"] != "markdown":
            raise SpecError(f"{where}: its messages must use format = \"markdown\" (step {s['id']})")
        used = [s["op"]] if s["type"] == "adapter" else [a["op"] for a in s.get("actions", [])]
        if any(is_write(op) or not social(op) for op in used):  # a search elsewhere would send the text out, too
            raise SpecError(f"{where}: it can't change anything or use other services (step {s['id']}); it only "
                            "messages the owner")


@dataclass
class Spec:
    name: str
    description: str
    triggers: list[dict]
    steps: list[dict]
    limits: dict
    files: dict[str, bytes] = field(repr=False, default_factory=dict)
    hash: str = ""
    uses: list[str] = field(default_factory=list)  # personal info entries/fields it may use (by reference only)

    @property
    def social(self) -> bool:
        """Reads the owner's Instagram/Threads web session: nothing of what its steps see is kept anywhere."""
        return any(op.startswith(("igweb.", "thweb.")) for s in self.steps
                   for op in ([s["op"]] if s["type"] == "adapter" else [a["op"] for a in s.get("actions", [])]))


LIMIT_DEFAULTS = {"runs_per_day": 200, "llm_calls_per_day": 50, "actions_per_day": 50, "run_timeout": 300}
LIMIT_RANGES = {"runs_per_day": (1, 5000), "llm_calls_per_day": (0, 500), "actions_per_day": (0, 1000),
                "run_timeout": (10, 3600)}


def parse(files: dict[str, bytes], expected_name: str | None = None) -> Spec:
    check_snapshot(files)
    try:
        raw = tomllib.loads(text_of(files, "watcher.toml"))
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"watcher.toml: {e}") from None
    _only(raw, {"name", "description", "triggers", "steps", "limits", "uses"}, "watcher.toml")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise SpecError("name: lowercase letters, digits, - or _ (up to 40)")
    if expected_name is not None and name != expected_name:
        raise SpecError(f"name in watcher.toml is {name!r}, expected {expected_name!r}")
    description = _str(raw.get("description"), "description", 500)
    uses = _str_list(raw.get("uses", []), "uses", 60, 20)
    for u in uses:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}(\.[a-z0-9_]{1,20}){0,2}", u):
            raise SpecError(f"uses: {u!r} isn't a personal info entry or field (like home.location, me.email, card-main)")
    triggers = raw.get("triggers")
    if not isinstance(triggers, list) or not 1 <= len(triggers) <= 5:
        raise SpecError("a watcher needs 1-5 [[triggers]]")
    trig = [_trigger(t, i) for i, t in enumerate(triggers)]
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise SpecError(f"a watcher needs 1-{MAX_STEPS} [[steps]]")
    out_steps: list[dict] = []
    for i, s in enumerate(steps):
        out_steps.append(_step(s, i, [x["id"] for x in out_steps], files))
    limits_raw = raw.get("limits", {})
    if not isinstance(limits_raw, dict):
        raise SpecError("[limits] must be a table")
    _only(limits_raw, set(LIMIT_DEFAULTS), "[limits]")
    limits = {k: int(_num(limits_raw.get(k, d), f"limits.{k}", *LIMIT_RANGES[k])) for k, d in LIMIT_DEFAULTS.items()}
    _check_social_shape(out_steps)
    # Every file must be used: an unused script would be approved without anyone reading it in context.
    used = {"watcher.toml"} | {s["run"] for s in out_steps if s["type"] == "script"} | \
           {s["prompt"] for s in out_steps if s["type"] == "llm"} | \
           {s["template"] for s in out_steps if s["type"] == "notify" and "template" in s}
    unused = sorted(set(files) - used)
    if unused:
        raise SpecError(f"files not used by any step: {', '.join(unused)} (remove them)")
    return Spec(name=name, description=description, triggers=trig, steps=out_steps, limits=limits,
                files=dict(files), hash=snapshot_hash(files), uses=uses)


# --- the manifest and risk --------------------------------------------------------------------------------

def _targets(op: str, args: dict) -> tuple[dict, dict]:
    info = WRITE_OPS.get(op)
    if info is None:
        return {}, {}
    route = {k: (None if has_template(args.get(k)) else args.get(k)) for k in info.route if k in args}
    subject = {k: (None if has_template(args.get(k)) else args.get(k)) for k in info.subject if k in args}
    return route, subject


def _declared_entries(s: dict, by: str) -> list[dict]:
    out = []
    for a in s.get("actions", []):
        info = WRITE_OPS.get(a["op"])
        routes = info.route if info else ()
        route = {k: (None if a.get(k, "*") == "*" else a[k]) for k in routes}  # undeclared = decided at run time
        entry = {"kind": "action", "step": s["id"], "op": a["op"], "write": is_write(a["op"]),
                 "route": route, "subject": {}, "declared_by": by}
        if a["op"] == "checkout.pay":
            entry["limits"] = {"max_amount": float(a["max_amount"]), "per_week": int(a.get("per_week", 1)),
                               **({"per_month": int(a["per_month"])} if "per_month" in a else {}),
                               **({"card": a["card"]} if a.get("card") else {}),
                               **({"item": a["item"]} if a.get("item") else {})}
        out.append(entry)
    return out


def manifest(spec: Spec) -> list[dict]:
    """Everything the watcher can do. Route values: a fixed value, a list (allowlist), "*" or None (variable)."""
    out: list[dict] = []
    if spec.uses:
        out.append({"kind": "uses", "uses": list(spec.uses)})
    for t in spec.triggers:
        out.append({"kind": "trigger", "type": t["type"], **{k: v for k, v in t.items() if k != "type"}})
    for s in spec.steps:
        if s["type"] == "adapter":
            route, subject = _targets(s["op"], s["args"])
            out.append({"kind": "action", "step": s["id"], "op": s["op"], "write": is_write(s["op"]),
                        "route": route, "subject": subject})
        elif s["type"] == "script":
            out.append({"kind": "script", "step": s["id"], "run": s["run"]})
            out += _declared_entries(s, "script")
        elif s["type"] == "llm":
            entry = {"kind": "llm", "step": s["id"], "mode": s["mode"], "model": s["model"]}
            if s["mode"] == "agent":
                entry["tools"] = s["tools"]
            out.append(entry)
            if s["mode"] == "agent":
                out += _declared_entries(s, "agent")
        elif s["type"] == "web":
            out.append({"kind": "web", "step": s["id"], "url": s["url"]})
        else:
            out.append({"kind": "notify", "step": s["id"]})
    return out


AGENT_CHANGING_TOOLS = re.compile(r"^(Edit|Write|Bash\(.*\))$")


def risk(entries: list[dict]) -> tuple[str, list[str]]:
    """("low" | "medium" | "high", reasons)."""
    level, reasons = "low", []

    def bump(to: str, why: str) -> None:
        nonlocal level
        order = ("low", "medium", "high")
        if order.index(to) > order.index(level):
            level = to
        if to != "low":
            reasons.append(why)

    for e in entries:
        if e["kind"] == "action" and e["write"]:
            info = WRITE_OPS[e["op"]]
            bump(info.level, f"{e['op']} ({'high-impact action' if info.level == 'high' else 'changes something'})")
            variable = [k for k, v in e["route"].items() if v is None or v == "*"]
            if variable:
                bump("high", f"{e['op']}: where it goes ({', '.join(variable)}) is decided at run time")
        elif e["kind"] == "llm" and e["mode"] == "agent":
            changing = [t for t in e["tools"] if AGENT_CHANGING_TOOLS.fullmatch(t)]
            if changing:
                bump("high", f"step {e['step']}: an AI agent with tools that act ({', '.join(changing)})")
            else:
                bump("medium", f"step {e['step']}: an AI agent (read-only tools)")
    return level, reasons


def grant_list(entries: list[dict]) -> list[dict]:
    """What "allow all" grants: one entry per write action, with its fixed (or allowlisted) routes."""
    out = []
    for e in entries:
        if e["kind"] == "action" and e["write"]:
            out.append({"op": e["op"], "step": e["step"], "route": e["route"],
                        **({"limits": e["limits"]} if "limits" in e else {})})
    return out


def route_allowed(granted: dict, actual: dict) -> bool:
    """Does an action's actual route fit a granted route? Fixed values must match exactly, an allowlist must
    contain the value, "*"/None (variable, granted explicitly) allows anything."""
    for k, g in granted.items():
        v = actual.get(k)
        if g is None or g == "*":
            continue
        if isinstance(g, list):
            if v not in g:
                return False
        elif v != g:
            return False
    return True


# --- describing a watcher to the owner ------------------------------------------------------------------------

RISK_ICON = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _c(v: Any, limit: int = 120) -> str:
    """A spec value as one clean line for the owner's approval message (no line breaks or invisible characters)."""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(HIDDEN_RE.sub(" ", s).split())
    return s if len(s) <= limit else s[:limit] + "…"


def _route_text(route: dict) -> str:
    parts = []
    for k, v in route.items():
        if v is None or v == "*":
            parts.append(f"{k}: decided at run time")
        elif isinstance(v, list):
            parts.append(f"{k}: one of {', '.join(_c(x, 80) for x in v)}")
        else:
            parts.append(f"{k}: {_c(v)}")
    return "; ".join(parts)


def trigger_text(t: dict) -> str:
    k = t["type"]
    if k == "time.every":
        return f"every {t['minutes']:g} min"
    if k == "time.at":
        days = "every day" if len(t["days"]) == 7 else ", ".join(t["days"])
        return f"at {', '.join(t['at'])} ({days})"
    if k == "manual":
        return "when you ask"
    src = TRIGGERS[k]["source"]
    what = {"gmail.new": "new email", "todoist.new": "new Todoist task", "telegram.new": "new Telegram message",
            "youtube.new": "new YouTube upload", "threads.reply": "new Threads reply",
            "instagram.comment": "new Instagram comment", "web.change": "page change"}[k]
    ignore = f", ignoring lines matching {len(t['ignore'])} pattern(s)" if t.get("ignore") else ""
    return f"{what}{f' ({_c(t[src])})' if src else ''}, checked every {t['every_minutes']:g} min{ignore}"


def describe(spec: Spec, entries: list[dict]) -> str:
    level, reasons = risk(entries)
    lines = [f"Risk: {RISK_ICON[level]} {level}"]
    lines += [f"  · {r}" for r in dict.fromkeys(reasons)]
    lines.append("When: " + "; ".join(trigger_text(t) for t in spec.triggers))
    lines.append("Steps:")
    for s in spec.steps:
        cond = f" (only if {s['when']['path']} …)" if "when" in s else ""
        if s["type"] == "script":
            acts = f", may ask for: {', '.join(a['op'] for a in s['actions'])}" if s["actions"] else ""
            lines.append(f"  {s['id']}: script {s['run']}{acts}{cond}")
        elif s["type"] == "llm" and s["mode"] == "structured":
            outcomes = next((f["values"] for f in s["schema"].values() if f["type"] == "enum"), None)
            lines.append(f"  {s['id']}: AI ({s['model']}, no tools) answers"
                         f"{' one of ' + '/'.join(_c(o, 40) for o in outcomes) if outcomes else ''}{cond}")
        elif s["type"] == "llm":
            lines.append(f"  {s['id']}: AI agent ({s['model']}) with tools: {', '.join(s['tools']) or 'none'}{cond}")
        elif s["type"] == "adapter":
            route, _ = _targets(s["op"], s["args"])
            extra = f" → {_route_text(route)}" if route else ""
            lines.append(f"  {s['id']}: {s['op']}{extra}{cond}")
        elif s["type"] == "notify":
            lines.append(f"  {s['id']}: message to you{cond}")
        else:
            lines.append(f"  {s['id']}: read {_c(s['url'], 200)}{cond}")
    grants = grant_list(entries)
    if grants:
        lines.append("Actions it can take (granted together with \"allow all\"):")
        for g in grants:
            lim = g.get("limits") or {}
            caps = (f" (at most {lim['max_amount']:g} per purchase, {lim['per_week']}/week"
                    + (f", {lim['per_month']}/month" if lim.get("per_month") else "")
                    + (f", card {_c(lim['card'], 30)}" if lim.get("card") else "")
                    + (f", for {_c(lim['item'], 60)}" if lim.get("item") else "") + ")") if lim else ""
            lines.append(f"  · {g['op']}" + (f" → {_route_text(g['route'])}" if g["route"] else "") + caps)
    elif any(e["kind"] == "llm" and e["mode"] == "agent" and any(AGENT_CHANGING_TOOLS.fullmatch(t) for t in e["tools"])
             for e in entries):
        lines.append("Its AI step can act: it asks you each time, except changing email labels with \"allow all\".")
    else:
        lines.append("It changes nothing: it only reads and messages you.")
    if spec.uses:
        lines.append("Personal info it may use (filled or searched by the server; it never sees the values): "
                     + ", ".join(_c(u, 60) for u in spec.uses))
    lim = spec.limits
    lines.append(f"Limits: {lim['runs_per_day']} runs, {lim['llm_calls_per_day']} AI calls, "
                 f"{lim['actions_per_day']} actions a day")
    return "\n".join(lines)
