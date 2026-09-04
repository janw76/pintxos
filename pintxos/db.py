"""SQLite storage. Timestamps are ISO8601 UTC strings."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from pintxos.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT,
    created_at TEXT,
    last_polled_at TEXT,
    last_error TEXT,
    ads_filtered INTEGER NOT NULL DEFAULT 0,
    filter_ads INTEGER,
    ad_title_patterns TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    link TEXT NOT NULL,
    original_title TEXT,
    published_at TEXT,
    headline TEXT,
    summary TEXT,
    fallback INTEGER DEFAULT 0,
    created_at TEXT,
    UNIQUE(feed_id, guid)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    """Current time as an ISO8601 UTC string."""
    return datetime.now(UTC).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)  # CREATE IF NOT EXISTS: cheap, and every caller gets a ready DB
    # ponytail: add-column-if-missing migration, not a migration framework. If this grows
    # much past a handful of columns, replace it with a real schema-version table.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
    for name, ddl in (
        ("ads_filtered", "INTEGER NOT NULL DEFAULT 0"),
        # NULL = "use the global setting"; 0/1 = explicit per-feed override.
        ("filter_ads", "INTEGER"),
        ("ad_title_patterns", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE feeds ADD COLUMN {name} {ddl}")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Connection that commits on success, rolls back on error, and always closes."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    connect().close()
