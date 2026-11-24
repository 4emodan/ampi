"""The keeper: approved watchers (watchers v2), held by ampi-tools so nothing running as `assistant` can change them.

It stores, in /home/ampitools/watch.sqlite3:
- watchers and their state (proposed / active / paused / failing / deleted, mode all | ask, risk, manifest)
- snapshots: the exact bytes of every proposed version (frozen at propose time; approvals bind to their hash)
- proposals, grants (step + op + fixed/allowlisted route, bound to the snapshot hash), space files (with a log),
  and the audit of every watcher action (with idempotency keys).

The manifest and risk are computed here from the frozen snapshot (watch_spec), never taken from the builder.
Named routes (Telegram chats) are resolved to ids at propose time and those ids are what is frozen.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import watch_spec as ws
from .todoist import ToolError

SPACE_TOTAL = 20 * 1024 * 1024
SPACE_FILE = 5 * 1024 * 1024
WATCHER_PENDING_CAP = 3       # open approval requests per watcher
ALL_WATCHERS_PENDING_CAP = 10  # open approval requests of all watchers together (separate from chat's MAX_PENDING)
DEFAULT_ACTION_TTL = 1800
MAX_ACTION_TTL = 3 * 24 * 3600
ACTIVATE_TTL = 3 * 24 * 3600

ORIGIN_RE = re.compile(r"^watcher:([a-z0-9][a-z0-9_-]{0,31})/([a-z0-9][a-z0-9_-]{0,39})@([0-9a-f]{12})(?:#([a-z][a-z0-9_]{0,31}))?$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchers (
    profile TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, request TEXT, requester TEXT,
    created REAL NOT NULL, approved REAL, hash TEXT, mode TEXT, risk TEXT, manifest TEXT, description TEXT,
    PRIMARY KEY (profile, name));
CREATE TABLE IF NOT EXISTS snapshots (hash TEXT PRIMARY KEY, files TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL,
    request TEXT, requester TEXT, test_text TEXT, approval_id INTEGER, created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', manifest TEXT, risk TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL,
    step TEXT NOT NULL, op TEXT NOT NULL, route TEXT NOT NULL, limits TEXT, created REAL NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS space (
    profile TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, data BLOB NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY (profile, name, path));
CREATE TABLE IF NOT EXISTS space_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT, name TEXT, run TEXT, path TEXT, old_sha TEXT, new_sha TEXT,
    at REAL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, name TEXT NOT NULL, hash TEXT, run TEXT, step TEXT,
    op TEXT NOT NULL, args_summary TEXT, idem_key TEXT UNIQUE, status TEXT NOT NULL, approval_id INTEGER,
    result TEXT, at REAL NOT NULL, amount TEXT);
"""


def b64decode_files(files: Any, what: str = "files") -> dict[str, bytes]:
    if not isinstance(files, dict):
        raise ToolError(f"{what} must be an object of path: base64")
    out = {}
    for path, data in files.items():
        if not isinstance(path, str) or not isinstance(data, str):
            raise ToolError(f"{what}: bad entry")
        try:
            out[path] = base64.b64decode(data, validate=True)
        except ValueError:
            raise ToolError(f"{what}: {path} isn't valid base64") from None
    return out


def b64encode_files(files: dict[str, bytes]) -> dict[str, str]:
    return {p: base64.b64encode(d).decode() for p, d in files.items()}


def parse_origin(origin: str) -> tuple[str, str, str, str | None] | None:
    """(profile, name, hash12, step or None) for new-style watcher origins, else None."""
    m = ORIGIN_RE.fullmatch(origin or "")
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


_ADDR = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def norm_route_value(op: str, key: str, v: Any) -> Any:
    """Compare routes by meaning: email addresses as a sorted lowercase set, names case-insensitively."""
    if v is None:
        return None
    if isinstance(v, list):
        return [norm_route_value(op, key, x) for x in v]
    if op.startswith("gmail.") and key in ("to", "cc"):
        return ", ".join(sorted({a.lower() for a in _ADDR.findall(str(v))}))
    if isinstance(v, str):
        return v.strip().casefold()
    return v


def norm_route(op: str, route: dict) -> dict:
    return {k: (v if v in (None, "*") else norm_route_value(op, k, v)) for k, v in route.items()}


def route_fits(op: str, granted: dict, actual: dict) -> bool:
    """Like watch_spec.route_allowed, with normalised values; an address list fits an allowlist if every address
    is in it."""
    g, a = norm_route(op, granted), norm_route(op, actual)
    for k, gv in g.items():
        if gv is None or gv == "*":
            continue
        av = a.get(k)
        if op.startswith("gmail.") and k in ("to", "cc"):
            allowed = set()
            for x in (gv if isinstance(gv, list) else [gv]):
                allowed |= {y.strip() for y in str(x).split(",") if y.strip()}
            got = {y.strip() for y in str(av or "").split(",") if y.strip()}
            if not got <= allowed:
                return False
            continue
        if isinstance(gv, list):
            if av not in gv:
                return False
        elif av != gv:
            return False
    return True


def actual_route(op: str, args: dict, raw: dict | None = None) -> dict:
    """Where an action really goes, by the op's route arguments. `args` are the resolved/prepared args, `raw` the
    caller's (resolution replaces them for some ops). Where resolution decides the destination (Telegram chat →
    peer id, email recipients incl. Reply-To, the checkout page's site), the resolved value counts."""
    info = ws.WRITE_OPS.get(op)
    if info is None:
        return {}
    raw = raw if raw is not None else args
    r = args.get("resolved") if isinstance(args.get("resolved"), dict) else {}
    out: dict[str, Any] = {}
    for k in info.route:
        if op.startswith("telegram.") and k == "chat":
            chat = r.get("chat") if isinstance(r.get("chat"), dict) else {}
            out[k] = chat.get("peer_id", raw.get("chat"))
        elif op == "gmail.send" and k in ("to", "cc"):
            out[k] = r.get(k, raw.get(k)) or ""
        elif op.startswith("checkout.") and k == "site":
            out[k] = r.get("origin")
        else:
            # An argument left out counts as "" (not None = any), so a grant for "no project" (the Inbox) never
            # becomes a grant for every project.
            out[k] = raw.get(k) if raw.get(k) is not None else ""
    return out


class Keeper:
    def __init__(self, path: Path | str, clock=time.time):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(audit)")}
        # items: how many emails; detail: which (JSON); reported: 0 due, 1 told the owner, NULL not for a report
        for column, kind in (("items", "INTEGER"), ("detail", "TEXT"), ("reported", "INTEGER")):
            if column not in have:
                self.db.execute(f"ALTER TABLE audit ADD COLUMN {column} {kind}")
        # An action that was running when the service stopped.
        self.db.execute("UPDATE audit SET status='unknown' WHERE status='auto' AND reported=0")
        self.db.commit()
        self.clock = clock
        # checkout sessions a watcher opened: session id -> (profile, name, site)
        self.checkout_sessions: dict[str, tuple[str, str, str]] = {}

    # --- watchers ---------------------------------------------------------------------------------------

    def watcher(self, profile: str, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM watchers WHERE profile=? AND name=?", (profile, name)).fetchone()
        return dict(row) if row else None

    def active(self, profile: str, name: str, hash12: str | None = None) -> dict:
        w = self.watcher(profile, name)
        if not w or w["status"] != "active" or not w["hash"]:
            raise ToolError(f"watcher {name} isn't active")
        if hash12 is not None and not w["hash"].startswith(hash12):
            raise ToolError(f"watcher {name}: this isn't the approved version (it was changed or replaced)")
        return w

    def manifest(self, w: dict) -> list[dict]:
        return json.loads(w["manifest"] or "[]")

    def files(self, h: str) -> dict[str, bytes]:
        row = self.db.execute("SELECT files FROM snapshots WHERE hash=?", (h,)).fetchone()
        if not row:
            raise ToolError("snapshot missing")
        files = {p: base64.b64decode(d) for p, d in json.loads(row["files"]).items()}
        if ws.snapshot_hash(files) != h:  # re-hash before anyone relies on it
            raise ToolError("snapshot damaged (hash mismatch)")
        return files

    def pending_proposal(self, profile: str, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM proposals WHERE profile=? AND name=? AND status='pending' ORDER BY id DESC",
                              (profile, name)).fetchone()
        return dict(row) if row else None

    def store_proposal(self, profile: str, name: str, spec: ws.Spec, entries: list[dict], level: str, request: str,
                       requester: str, test_text: str) -> int:
        now = self.clock()
        self.db.execute("INSERT OR IGNORE INTO snapshots (hash, files, created) VALUES (?, ?, ?)",
                        (spec.hash, json.dumps(b64encode_files(spec.files)), now))
        cur = self.db.execute(
            "INSERT INTO proposals (profile, name, hash, request, requester, test_text, created, manifest, risk, "
            "description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (profile, name, spec.hash, request, requester, test_text, now, json.dumps(entries), level,
             spec.description))
        if not self.watcher(profile, name):
            self.db.execute("INSERT INTO watchers (profile, name, status, request, requester, created, description) "
                            "VALUES (?, ?, 'proposed', ?, ?, ?, ?)", (profile, name, request, requester, now,
                                                                       spec.description))
        self.db.commit()
        return int(cur.lastrowid)

    def link_approval(self, proposal_id: int, approval_id: int) -> None:
        self.db.execute("UPDATE proposals SET approval_id=? WHERE id=?", (approval_id, proposal_id))
        self.db.commit()

    def proposal(self, proposal_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return dict(row) if row else None

    def close_proposal(self, proposal_id: int, status: str) -> None:
        self.db.execute("UPDATE proposals SET status=? WHERE id=? AND status='pending'", (status, proposal_id))
        self.db.commit()

    def activate(self, proposal_id: int, mode: str) -> dict:
        p = self.proposal(proposal_id)
        if not p or p["status"] != "pending":
            raise ToolError("that proposal is no longer open")
        self.files(p["hash"])  # still intact
        now = self.clock()
        entries = json.loads(p["manifest"])
        prev = self.watcher(p["profile"], p["name"])
        request = p["request"] if not prev or not prev.get("request") or prev["status"] == "proposed" else prev["request"]
        self.db.execute(
            "INSERT INTO watchers (profile, name, status, request, requester, created, approved, hash, mode, risk, "
            "manifest, description) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(profile, name) DO UPDATE SET status='active', approved=excluded.approved, hash=excluded.hash, "
            "mode=excluded.mode, risk=excluded.risk, manifest=excluded.manifest, description=excluded.description, "
            "request=COALESCE(watchers.request, excluded.request)",
            (p["profile"], p["name"], request, p["requester"], now, now, p["hash"], mode, p["risk"], p["manifest"],
             p["description"]))
        # Grants of every earlier version stop working.
        self.db.execute("DELETE FROM grants WHERE profile=? AND name=? AND hash != ?", (p["profile"], p["name"], p["hash"]))
        if mode == "all":
            for g in ws.grant_list(entries):
                self.db.execute("INSERT INTO grants (profile, name, hash, step, op, route, limits, created, source) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creation')",
                                (p["profile"], p["name"], p["hash"], g["step"], g["op"], json.dumps(g["route"]),
                                 json.dumps(g.get("limits")), now))
        self.db.execute("UPDATE proposals SET status='approved' WHERE id=?", (proposal_id,))
        self.db.commit()
        return self.watcher(p["profile"], p["name"]) or {}

    def set_status(self, profile: str, name: str, status: str) -> None:
        w = self.watcher(profile, name)
        if not w or w["status"] in ("proposed", "deleted") or not w["hash"]:
            raise ToolError(f"watcher {name} isn't set up")
        if status == "active" and w["status"] not in ("paused", "failing", "active"):
            raise ToolError("only a paused or failing watcher can be resumed")
        self.db.execute("UPDATE watchers SET status=? WHERE profile=? AND name=?", (status, profile, name))
        self.db.commit()

    def delete(self, profile: str, name: str) -> None:
        if not self.watcher(profile, name):
            raise ToolError(f"no watcher {name}")
        self.db.execute("UPDATE watchers SET status='deleted' WHERE profile=? AND name=?", (profile, name))
        self.db.execute("DELETE FROM grants WHERE profile=? AND name=?", (profile, name))
        self.db.execute("UPDATE proposals SET status='withdrawn' WHERE profile=? AND name=? AND status='pending'",
                        (profile, name))
        self.db.commit()

    def list(self, profile: str) -> list[dict]:
        day = self.clock() - 24 * 3600
        out = []
        for w in self.db.execute("SELECT * FROM watchers WHERE profile=? AND status != 'deleted' ORDER BY name", (profile,)):
            w = dict(w)
            n_grants = self.db.execute("SELECT COUNT(*) FROM grants WHERE profile=? AND name=? AND hash=?",
                                       (profile, w["name"], w["hash"] or "")).fetchone()[0]
            today = self.db.execute("SELECT COUNT(*) FROM audit WHERE profile=? AND name=? AND at > ? AND status IN "
                                    "('auto', 'done', 'approved', 'failed', 'unknown')", (profile, w["name"], day)).fetchone()[0]
            pending = self.pending_proposal(profile, w["name"])
            out.append({"name": w["name"], "status": w["status"], "risk": w["risk"], "mode": w["mode"],
                        "hash": w["hash"], "description": w["description"], "request": w["request"],
                        "created": w["created"], "approved": w["approved"], "grants": n_grants,
                        "actions_today": today, "proposal_pending": bool(pending)})
        return out

    # --- grants and audit ----------------------------------------------------------------------------------

    def grants(self, profile: str, name: str, h: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM grants WHERE profile=? AND name=? AND hash=?", (profile, name, h))
        return [{**dict(r), "route": json.loads(r["route"]), "limits": json.loads(r["limits"] or "null")} for r in rows]

    def add_grant(self, profile: str, name: str, h: str, step: str, op: str, route: dict, source: str = "runtime") -> None:
        w = self.watcher(profile, name)
        if not w or w["hash"] != h:
            return  # the watcher changed meanwhile: the grant would belong to a version that no longer runs
        self.db.execute("INSERT INTO grants (profile, name, hash, step, op, route, limits, created, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                        (profile, name, h, step, op, json.dumps(route), self.clock(), source))
        self.db.commit()

    def granted(self, profile: str, name: str, h: str, step: str, op: str, route: dict) -> bool:
        return any(g["step"] == step and g["op"] == op and route_fits(op, g["route"], route)
                   for g in self.grants(profile, name, h))

    def audit_by_key(self, idem_key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM audit WHERE idem_key=?", (idem_key,)).fetchone()
        return dict(row) if row else None

    def audit_add(self, profile: str, name: str, h: str, run: str | None, step: str, op: str, summary: str,
                  idem_key: str | None, status: str, amount: str | None = None, items: int | None = None,
                  detail: list | None = None, report: bool = False) -> int:
        cur = self.db.execute(
            "INSERT INTO audit (profile, name, hash, run, step, op, args_summary, idem_key, status, at, amount, items, "
            "detail, reported) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (profile, name, h, run, step, op, summary[:2000], idem_key, status, self.clock(), amount, items,
             json.dumps(detail, ensure_ascii=False) if detail else None, 0 if report else None))
        self.db.commit()
        return int(cur.lastrowid)

    def audit_update(self, audit_id: int, status: str, result: str | None = None, approval_id: int | None = None) -> None:
        self.db.execute("UPDATE audit SET status=?, result=COALESCE(?, result), approval_id=COALESCE(?, approval_id) "
                        "WHERE id=?", (status, (result or "")[:4000] if result is not None else None, approval_id, audit_id))
        self.db.commit()

    def audit_for_approval(self, approval_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM audit WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def actions_today(self, profile: str, name: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM audit WHERE profile=? AND name=? AND at > ? AND status IN "
                               "('auto', 'done', 'approved', 'requested', 'failed', 'unknown')",
                               (profile, name, self.clock() - 24 * 3600)).fetchone()[0]

    def items_today(self, profile: str, name: str, op: str | None = None) -> int:
        """Emails it changed without asking (whatever came of it) in 24 hours."""
        q = "SELECT COALESCE(SUM(items), 0) FROM audit WHERE profile=? AND name=? AND at > ?"
        return self.db.execute(q + (" AND op=?" if op else ""), (profile, name, self.clock() - 24 * 3600,
                                                                 *((op,) if op else ()))).fetchone()[0]

    def due_reports(self, quiet: float = 120, longest: float = 900) -> list[tuple[str, str, list[dict]]]:
        """(profile, name, finished actions to report), once the watcher has been quiet for a while."""
        now, out = self.clock(), []
        for p, n, first, last in self.db.execute(
                "SELECT profile, name, MIN(at), MAX(at) FROM audit WHERE reported=0 AND status != 'auto' GROUP BY 1, 2").fetchall():
            if now - last >= quiet or now - first >= longest:
                rows = self.db.execute("SELECT * FROM audit WHERE profile=? AND name=? AND reported=0 AND status != 'auto' "
                                       "ORDER BY id LIMIT 300", (p, n)).fetchall()
                out.append((p, n, [dict(r) for r in rows]))
        return out

    def reported(self, ids: list[int]) -> None:
        self.db.executemany("UPDATE audit SET reported=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    def activity(self, limit: int = 150) -> list[dict]:
        rows = self.db.execute("SELECT id, profile, name, step, op, args_summary, status, approval_id, result, at, items "
                               "FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def list_all(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM watchers WHERE status NOT IN ('deleted', 'proposed') ORDER BY profile, name")
        return [{**dict(r), "actions_today": self.actions_today(r["profile"], r["name"])} for r in rows.fetchall()]

    def pays_this_month(self, profile: str, name: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM audit WHERE profile=? AND name=? AND op='checkout.pay' AND at > ? "
                               "AND status IN ('auto', 'done', 'approved', 'requested', 'unknown')",
                               (profile, name, self.clock() - 30 * 24 * 3600)).fetchone()[0]

    def pays_this_week(self, profile: str, name: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM audit WHERE profile=? AND name=? AND op='checkout.pay' AND at > ? "
                               "AND status IN ('auto', 'done', 'approved', 'requested', 'unknown')",
                               (profile, name, self.clock() - 7 * 24 * 3600)).fetchone()[0]

    def audit(self, profile: str, name: str, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT id, run, step, op, args_summary, status, approval_id, result, at FROM audit "
                               "WHERE profile=? AND name=? ORDER BY id DESC LIMIT ?", (profile, name, limit))
        return [dict(r) for r in rows]

    # --- space ------------------------------------------------------------------------------------------------

    def space_get(self, profile: str, name: str) -> dict[str, bytes]:
        rows = self.db.execute("SELECT path, data FROM space WHERE profile=? AND name=?", (profile, name))
        return {r["path"]: bytes(r["data"]) for r in rows}

    def space_put(self, profile: str, name: str, h: str, run: str | None, changes: dict[str, bytes | None]) -> int:
        w = self.watcher(profile, name)
        if not w or w["hash"] != h or w["status"] not in ("active", "paused", "failing"):
            raise ToolError("space can only be changed by the approved version of an active watcher")
        current = self.space_get(profile, name)
        new = dict(current)
        for path, data in changes.items():
            ws.check_path(path)
            if data is None:
                new.pop(path, None)
            else:
                if len(data) > SPACE_FILE:
                    raise ToolError(f"space file {path} is larger than {SPACE_FILE // 1024 // 1024} MB")
                new[path] = data
        if sum(len(d) for d in new.values()) > SPACE_TOTAL:
            raise ToolError(f"the watcher's space would be larger than {SPACE_TOTAL // 1024 // 1024} MB")
        now = self.clock()

        def sha(d: bytes | None) -> str | None:
            return hashlib.sha256(d).hexdigest()[:16] if d is not None else None

        n = 0
        for path, data in changes.items():
            old = current.get(path)
            if old == data:
                continue
            if data is None:
                self.db.execute("DELETE FROM space WHERE profile=? AND name=? AND path=?", (profile, name, path))
            else:
                self.db.execute("INSERT INTO space (profile, name, path, data, updated) VALUES (?, ?, ?, ?, ?) "
                                "ON CONFLICT(profile, name, path) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                                (profile, name, path, data, now))
            self.db.execute("INSERT INTO space_log (profile, name, run, path, old_sha, new_sha, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (profile, name, run, path, sha(old), sha(data), now))
            n += 1
        self.db.commit()
        return n


async def resolve_routes(services: Any, entries: list[dict]) -> list[dict]:
    """Freezes named Telegram chats as peer ids (with the title kept for display). Raises if a fixed chat can't be
    found, so the owner never approves a route that might point somewhere else later."""
    out = []
    for e in entries:
        e = json.loads(json.dumps(e))
        if e.get("kind") == "action" and e.get("op", "").startswith("telegram.") and "chat" in e.get("route", {}):
            v = e["route"]["chat"]
            if v not in (None, "*"):
                values = v if isinstance(v, list) else [v]
                ids, titles = [], []
                from .ops_telegram import _ask
                for name in values:
                    chat = await _ask(services, {"cmd": "resolve", "chat": str(name)})
                    if "peer_id" not in chat:
                        raise ToolError(f"can't find the Telegram chat {name!r}")
                    ids.append(chat["peer_id"])
                    titles.append(str(chat.get("title") or name))
                e["route"]["chat"] = ids if isinstance(v, list) else ids[0]
                e["chat_titles"] = titles
        out.append(e)
    return out
