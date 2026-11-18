"""SQLite storage: conversations (Telegram topic -> Claude session), run log, update offsets."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    profile    TEXT    NOT NULL,
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL DEFAULT 0,
    session_id TEXT    NOT NULL,
    started    INTEGER NOT NULL DEFAULT 0,
    title      TEXT,
    model      TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (profile, chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile        TEXT    NOT NULL,
    chat_id        INTEGER NOT NULL,
    thread_id      INTEGER NOT NULL,
    session_id     TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT    NOT NULL,
    ok             INTEGER NOT NULL,
    cost_usd       REAL,
    duration_ms    INTEGER,
    error          TEXT,
    prompt_preview TEXT
);
CREATE TABLE IF NOT EXISTS offsets (
    profile TEXT PRIMARY KEY,
    next_update_id INTEGER NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Conversation:
    profile: str
    chat_id: int
    thread_id: int
    session_id: str
    started: bool
    title: str | None
    model: str | None


@dataclass(frozen=True)
class RunStats:
    runs: int
    failed: int
    cost_usd: float


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _conv(row: sqlite3.Row) -> Conversation:
        return Conversation(
            profile=row["profile"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            session_id=row["session_id"],
            started=bool(row["started"]),
            title=row["title"],
            model=row["model"],
        )

    def get_conversation(self, profile: str, chat_id: int, thread_id: int) -> Conversation | None:
        row = self._db.execute(
            "SELECT * FROM conversations WHERE profile=? AND chat_id=? AND thread_id=?",
            (profile, chat_id, thread_id),
        ).fetchone()
        return self._conv(row) if row else None

    def get_or_create(self, profile: str, chat_id: int, thread_id: int, title: str | None = None) -> Conversation:
        conv = self.get_conversation(profile, chat_id, thread_id)
        if conv:
            return conv
        ts = now_iso()
        self._db.execute(
            "INSERT INTO conversations (profile, chat_id, thread_id, session_id, started, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (profile, chat_id, thread_id, str(uuid.uuid4()), title, ts, ts),
        )
        self._db.commit()
        conv = self.get_conversation(profile, chat_id, thread_id)
        assert conv is not None
        return conv

    def reset_session(self, profile: str, chat_id: int, thread_id: int) -> Conversation:
        """Start a fresh Claude session for this conversation (keeps title and model)."""
        self.get_or_create(profile, chat_id, thread_id)
        self._db.execute(
            "UPDATE conversations SET session_id=?, started=0, updated_at=?"
            " WHERE profile=? AND chat_id=? AND thread_id=?",
            (str(uuid.uuid4()), now_iso(), profile, chat_id, thread_id),
        )
        self._db.commit()
        conv = self.get_conversation(profile, chat_id, thread_id)
        assert conv is not None
        return conv

    def mark_started(self, profile: str, chat_id: int, thread_id: int, session_id: str) -> None:
        self._db.execute(
            "UPDATE conversations SET started=1, updated_at=?"
            " WHERE profile=? AND chat_id=? AND thread_id=? AND session_id=?",
            (now_iso(), profile, chat_id, thread_id, session_id),
        )
        self._db.commit()

    def set_model(self, profile: str, chat_id: int, thread_id: int, model: str | None) -> None:
        self.get_or_create(profile, chat_id, thread_id)
        self._db.execute(
            "UPDATE conversations SET model=?, updated_at=? WHERE profile=? AND chat_id=? AND thread_id=?",
            (model, now_iso(), profile, chat_id, thread_id),
        )
        self._db.commit()

    def record_run(
        self,
        conv: Conversation,
        *,
        started_at: str,
        ok: bool,
        cost_usd: float | None,
        duration_ms: int | None,
        error: str | None,
        prompt: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO runs (profile, chat_id, thread_id, session_id, started_at, finished_at, ok,"
            " cost_usd, duration_ms, error, prompt_preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conv.profile,
                conv.chat_id,
                conv.thread_id,
                conv.session_id,
                started_at,
                now_iso(),
                int(ok),
                cost_usd,
                duration_ms,
                error,
                prompt[:200],
            ),
        )
        self._db.commit()

    def run_stats(self, profile: str, chat_id: int, thread_id: int) -> RunStats:
        row = self._db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(1 - ok), 0) AS failed, COALESCE(SUM(cost_usd), 0) AS cost"
            " FROM runs WHERE profile=? AND chat_id=? AND thread_id=?",
            (profile, chat_id, thread_id),
        ).fetchone()
        return RunStats(runs=row["n"], failed=row["failed"], cost_usd=row["cost"])

    def get_offset(self, profile: str) -> int | None:
        row = self._db.execute("SELECT next_update_id FROM offsets WHERE profile=?", (profile,)).fetchone()
        return row["next_update_id"] if row else None

    def set_offset(self, profile: str, next_update_id: int) -> None:
        self._db.execute(
            "INSERT INTO offsets (profile, next_update_id) VALUES (?, ?)"
            " ON CONFLICT(profile) DO UPDATE SET next_update_id=excluded.next_update_id",
            (profile, next_update_id),
        )
        self._db.commit()
