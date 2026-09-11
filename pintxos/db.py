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
    last_filtered TEXT,
    filter_ads INTEGER,
    ad_title_patterns TEXT,
    ad_patterns_mode INTEGER,
    respect_language INTEGER,
    classify_topics INTEGER,
    mute_topics TEXT,
    topic_counts TEXT,
    warn_volume INTEGER,
    daily_budget INTEGER
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
    word_count INTEGER,
    auth TEXT,
    fetch_status TEXT,
    text TEXT,
    created_at TEXT,
    labels TEXT,
    topic TEXT,
    muted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(feed_id, guid)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- One row per feed per UTC day: how much work this feed cost us that day.
CREATE TABLE IF NOT EXISTS feed_stats (
    feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    summaries INTEGER NOT NULL DEFAULT 0,
    classifications INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(feed_id, day)
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
        ("last_filtered", "TEXT"),
        # NULL = "use the global setting"; 0/1 = explicit per-feed override.
        ("filter_ads", "INTEGER"),
        ("ad_title_patterns", "TEXT"),
        # NULL = inherit (global patterns only); 1 = global + this feed's; 0 = no extra patterns.
        ("ad_patterns_mode", "INTEGER"),
        # NULL = follow the global PINTXOS_RESPECT_LANGUAGE setting; 1/0 = explicit per-feed override.
        ("respect_language", "INTEGER"),
        # NULL = follow the global topic-classification setting; 1/0 = explicit per-feed override.
        ("classify_topics", "INTEGER"),
        # JSON array of IPTC topic slugs whose items are muted for this feed.
        ("mute_topics", "TEXT"),
        # JSON object {slug: count} of how often each topic was seen on this feed.
        ("topic_counts", "TEXT"),
        # Items/day above which the output feed carries a volume warning. NULL = on
        # (use the default threshold); 0 = warning off for this feed.
        ("warn_volume", "INTEGER"),
        # Max summaries per UTC day for this feed. NULL = unlimited.
        ("daily_budget", "INTEGER"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE feeds ADD COLUMN {name} {ddl}")

    item_cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for name, ddl in (
        # NULL = article was not fetched (fallback), so no stats.
        ("word_count", "INTEGER"),
        ("auth", "TEXT"),
        # Why the article ended up as it did: ok / short / teaser / blocked / error.
        # NULL = written before this column existed; callers treat it as "error".
        ("fetch_status", "TEXT"),
        # The text handed to summarize(). NULL = no real text (row written before this
        # column existed, or the fallback text was only the entry title).
        ("text", "TEXT"),
        # JSON array of publisher label strings (RSS categories + page section/tags/
        # keywords). NULL = row written before this column existed, or nothing found.
        ("labels", "TEXT"),
        # IPTC Media Topics top-level slug from classify_topic(). NULL = not classified
        # (feature off, classification failed, or row written before this column existed).
        ("topic", "TEXT"),
        # 1 = the item's topic is muted for its feed, so it is hidden from the output feed.
        ("muted", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in item_cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")
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
