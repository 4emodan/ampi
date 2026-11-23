"""The engine's own state in the bridge database: trigger memory, the item queue, runs, parked runs, builder
sessions. None of it decides what a watcher may do (that lives in ampi-tools' keeper); losing or tampering
with it can only cause missed or repeated *checks*, never new permissions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS wt_triggers (
    profile TEXT NOT NULL, name TEXT NOT NULL, ident TEXT NOT NULL, state TEXT NOT NULL, updated TEXT NOT NULL,
    PRIMARY KEY (profile, name, ident)
);
CREATE TABLE IF NOT EXISTS wt_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL, name TEXT NOT NULL, ident TEXT NOT NULL, trigger TEXT NOT NULL,
    item TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, not_before TEXT, run_id TEXT
);
CREATE INDEX IF NOT EXISTS wt_items_queue ON wt_items (profile, name, status);
CREATE TABLE IF NOT EXISTS wt_runs (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL, item_id INTEGER,
    trigger TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, status TEXT NOT NULL, steps_done INTEGER DEFAULT 0,
    llm_calls INTEGER DEFAULT 0, actions INTEGER DEFAULT 0, error TEXT, summary TEXT, test INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS wt_runs_by_watcher ON wt_runs (profile, name, started);
CREATE TABLE IF NOT EXISTS wt_parked (
    run_id TEXT PRIMARY KEY, profile TEXT NOT NULL, name TEXT NOT NULL, step INTEGER NOT NULL,
    approval INTEGER NOT NULL, ctx TEXT NOT NULL, until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wt_builders (
    profile TEXT NOT NULL, name TEXT NOT NULL, session_id TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0,
    request TEXT NOT NULL, requester TEXT NOT NULL, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
    created TEXT NOT NULL, PRIMARY KEY (profile, name)
);
"""

# Run statuses: running, ok, stopped (a step ended it quietly), failed, parked (waiting for the owner),
# maybe (the bridge died during an action: may have happened), skipped (cap reached), cancelled.
FINAL = ("ok", "stopped", "failed", "maybe", "skipped", "cancelled", "retrying")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class WatchDB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- trigger memory -----------------------------------------------------------------------

    def trigger_state(self, profile: str, name: str, ident: str) -> dict | None:
        row = self.db.execute("SELECT state FROM wt_triggers WHERE profile=? AND name=? AND ident=?",
                              (profile, name, ident)).fetchone()
        return json.loads(row["state"]) if row else None

    def set_trigger_state(self, profile: str, name: str, ident: str, state: dict) -> None:
        self.db.execute(
            "INSERT INTO wt_triggers (profile, name, ident, state, updated) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(profile, name, ident) DO UPDATE SET state=excluded.state, updated=excluded.updated",
            (profile, name, ident, json.dumps(state), iso(now())))
        self.db.commit()

    # --- the queue ------------------------------------------------------------------------------

    def enqueue(self, profile: str, name: str, ident: str, trigger: str, items: list[dict], ttl_minutes: float,
                latest_only: bool) -> int:
        if not items:
            return 0
        if latest_only:  # keep only the newest pending item of this trigger
            self.db.execute("UPDATE wt_items SET status='expired' WHERE profile=? AND name=? AND ident=? "
                            "AND status='queued'", (profile, name, ident))
            items = items[-1:]
        t = now()
        for item in items:
            self.db.execute(
                "INSERT INTO wt_items (profile, name, ident, trigger, item, status, created, expires)"
                " VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                (profile, name, ident, trigger, json.dumps(item, ensure_ascii=False), iso(t),
                 iso(t + timedelta(minutes=ttl_minutes))))
        self.db.commit()
        return len(items)

    def next_item(self, profile: str, name: str) -> dict | None:
        t = iso(now())
        self.db.execute("UPDATE wt_items SET status='expired' WHERE profile=? AND name=? AND status='queued' "
                        "AND expires < ?", (profile, name, t))
        row = self.db.execute(
            "SELECT * FROM wt_items WHERE profile=? AND name=? AND status='queued' AND (not_before IS NULL OR "
            "not_before <= ?) ORDER BY id LIMIT 1", (profile, name, t)).fetchone()
        self.db.commit()
        if row is None:
            return None
        d = dict(row)
        d["item"] = json.loads(d["item"])
        return d

    def busy_count(self, profile: str, name: str) -> int:
        """Items queued, running or waiting for the owner."""
        return self.db.execute("SELECT COUNT(*) FROM wt_items WHERE profile=? AND name=? AND status IN "
                               "('queued', 'running', 'parked')", (profile, name)).fetchone()[0]

    def queued_count(self, profile: str, name: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM wt_items WHERE profile=? AND name=? AND status='queued'",
                               (profile, name)).fetchone()[0]

    def set_item(self, item_id: int, status: str, run_id: str | None = None, retry_at: datetime | None = None) -> None:
        if retry_at is not None:
            self.db.execute("UPDATE wt_items SET status='queued', attempts=attempts+1, not_before=? WHERE id=?",
                            (iso(retry_at), item_id))
        else:
            self.db.execute("UPDATE wt_items SET status=?, run_id=COALESCE(?, run_id) WHERE id=?",
                            (status, run_id, item_id))
        self.db.commit()

    def item(self, item_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM wt_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["item"] = json.loads(d["item"])
        return d

    def disown(self, item_id: int) -> None:
        row = self.item(item_id)
        if row and isinstance(row["item"], dict) and row["item"].get("owner"):
            self.db.execute("UPDATE wt_items SET item=? WHERE id=?",
                            (json.dumps({**row["item"], "owner": False}, ensure_ascii=False), item_id))
            self.db.commit()

    def requeue(self, item_id: int) -> None:
        self.db.execute("UPDATE wt_items SET status='queued', not_before=NULL, expires=? WHERE id=?",
                        (iso(now() + timedelta(hours=1)), item_id))
        self.db.commit()

    def recover(self) -> list[dict]:
        """After a restart: items that were running. Their runs are marked 'maybe' if they had started an action."""
        rows = self.db.execute("SELECT * FROM wt_items WHERE status='running'").fetchall()
        return [dict(r) for r in rows]

    def prune(self, days: int = 365) -> None:
        cutoff = iso(now() - timedelta(days=days))
        self.db.execute("DELETE FROM wt_runs WHERE started < ?", (cutoff,))
        self.db.execute("DELETE FROM wt_items WHERE created < ? AND status != 'queued'",
                        (iso(now() - timedelta(days=30)),))
        self.db.commit()

    # --- runs -----------------------------------------------------------------------------------

    def start_run(self, profile: str, name: str, hash_: str, item_id: int | None, trigger: str,
                  test: bool = False) -> str:
        run_id = now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.db.execute(
            "INSERT INTO wt_runs (id, profile, name, hash, item_id, trigger, started, status, test)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (run_id, profile, name, hash_, item_id, trigger, iso(now()), int(test)))
        self.db.commit()
        return run_id

    def update_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        if fields.get("status") in FINAL:
            fields.setdefault("ended", iso(now()))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE wt_runs SET {cols} WHERE id=?", (*fields.values(), run_id))
        self.db.commit()

    def run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM wt_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs(self, profile: str, name: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM wt_runs WHERE profile=? AND name=? ORDER BY started DESC LIMIT ?",
            (profile, name, limit)).fetchall()]

    def today(self, profile: str, name: str) -> dict:
        start = iso(now().replace(hour=0, minute=0, second=0, microsecond=0))
        row = self.db.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(llm_calls), 0) AS llm, COALESCE(SUM(actions), 0) AS actions,"
            " COALESCE(SUM(status='failed'), 0) AS failed FROM wt_runs WHERE profile=? AND name=? AND started >= ?"
            " AND test=0", (profile, name, start)).fetchone()
        return dict(row)

    def failed_streak(self, profile: str, name: str, since: str | None = None,
                      waiting: bool = False) -> int:
        """Failed runs in a row (retry attempts don't count unless `waiting`), only since `since` (the last resume or new version)."""
        n = 0
        for r in self.db.execute("SELECT status FROM wt_runs WHERE profile=? AND name=? AND test=0 AND status IN "
                                 "('ok', 'stopped', 'failed', ?) AND started >= ? ORDER BY started DESC, rowid DESC LIMIT 100",
                                 (profile, name, "retrying" if waiting else "failed", since or "")):
            if r["status"] not in ("failed", "retrying"):
                break
            n += 1
        return n

    # --- parked runs (waiting for an approval) ---------------------------------------------------

    def park(self, run_id: str, profile: str, name: str, step: int, approval: int, ctx: dict, until: datetime) -> None:
        self.db.execute("INSERT OR REPLACE INTO wt_parked (run_id, profile, name, step, approval, ctx, until)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (run_id, profile, name, step, approval, json.dumps(ctx, ensure_ascii=False), iso(until)))
        self.db.commit()

    def parked(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM wt_parked").fetchall():
            d = dict(r)
            d["ctx"] = json.loads(d["ctx"])
            out.append(d)
        return out

    def unpark(self, run_id: str) -> None:
        self.db.execute("DELETE FROM wt_parked WHERE run_id=?", (run_id,))
        self.db.commit()

    # --- builder sessions ------------------------------------------------------------------------

    def builder(self, profile: str, name: str) -> dict | None:
        row = self.db.execute("SELECT * FROM wt_builders WHERE profile=? AND name=?", (profile, name)).fetchone()
        return dict(row) if row else None

    def set_builder(self, profile: str, name: str, session_id: str, request: str, requester: str,
                    chat_id: int, thread_id: int) -> None:
        self.db.execute(
            "INSERT INTO wt_builders (profile, name, session_id, started, request, requester, chat_id, thread_id,"
            " created) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?) ON CONFLICT(profile, name) DO UPDATE SET"
            " chat_id=excluded.chat_id, thread_id=excluded.thread_id",
            (profile, name, session_id, request, requester, chat_id, thread_id, iso(now())))
        self.db.commit()

    def builder_started(self, profile: str, name: str) -> None:
        self.db.execute("UPDATE wt_builders SET started=1 WHERE profile=? AND name=?", (profile, name))
        self.db.commit()
