"""SQLite storage. One file holds articles, chunks (with embeddings), images, escalations and logs.

SQLite is enough for a help center of a few thousand articles, needs no server, and the whole memory
can be copied or backed up as one file. Embeddings are stored as float32 blobs next to their chunk.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL DEFAULT 'helpcenter',      -- helpcenter | expert | document
    title TEXT,
    category TEXT,
    folder TEXT,
    lastmod TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending | active | removed | failed
    body_text TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    first_seen_at TEXT,
    fetched_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    alt TEXT,
    context TEXT,
    section TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending | described | failed | skipped
    model TEXT,
    error TEXT,
    UNIQUE(article_id, url)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    image_id INTEGER REFERENCES images(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    heading TEXT,
    text TEXT NOT NULL,
    embedding BLOB,
    embed_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_article ON chunks(article_id);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY,
    question TEXT NOT NULL,
    normalized TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'chat',            -- chat | inbox | scope | api
    context TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'open',            -- open | answered | dismissed
    expert_answer TEXT,
    expert_name TEXT,
    article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    times_asked INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                             -- sitemap_sync | images | reembed
    status TEXT NOT NULL,                           -- running | completed | failed
    params TEXT,
    stats TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY,
    question TEXT NOT NULL,
    source TEXT,
    answered INTEGER NOT NULL,
    confidence REAL,
    mode TEXT,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    latency_ms INTEGER,
    escalation_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized transaction. Commits on success, rolls back on error."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:  # type: ignore[type-arg]
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:  # type: ignore[type-arg]
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:  # type: ignore[type-arg]
        """Run a single write statement; returns lastrowid."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return int(cur.lastrowid or 0)

    # Knowledge version: bumped on every content change so the in-memory search index knows to rebuild.
    def bump_version(self, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self._conn
        with self._lock:
            c.execute(
                "INSERT INTO meta(key, value) VALUES('kb_version', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
            )

    def kb_version(self) -> int:
        row = self.one("SELECT value FROM meta WHERE key='kb_version'")
        return int(row["value"]) if row else 0

    def close(self) -> None:
        self._conn.close()
