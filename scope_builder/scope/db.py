"""SQLite storage for projects, transcripts, scope versions, proposals and the approval audit trail."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    client TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- initial | follow_up
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scopes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    transcript_id INTEGER REFERENCES transcripts(id),
    parent_scope_id INTEGER REFERENCES scopes(id),
    status TEXT NOT NULL,               -- draft | approved | superseded
    data TEXT NOT NULL,                 -- ScopeData JSON
    change_report TEXT,                 -- ChangeReport JSON for follow-ups
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
    markdown TEXT NOT NULL,
    status TEXT NOT NULL,               -- draft | approved | rejected
    edited INTEGER NOT NULL DEFAULT 0,
    reviewer TEXT,
    review_note TEXT,
    llm TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    entity TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return int(self._conn.execute(sql, params).lastrowid or 0)

    def audit(
        self, project_id: int | None, entity: str, entity_id: int, action: str, actor: str | None, note: str | None = None
    ) -> None:
        self.execute(
            "INSERT INTO audit(project_id, entity, entity_id, action, actor, note, created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, entity, entity_id, action, actor, note, now_iso()),
        )
