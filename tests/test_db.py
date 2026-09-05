import sqlite3

import pytest

from pintxos.config import db_path
from pintxos.db import connect, init_db, now


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh database under tmp_path so tests never touch ./data."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    init_db()
    conn = connect()
    yield conn
    conn.close()


def add_feed(conn, url="https://example.com/feed.xml"):
    cur = conn.execute(
        "INSERT INTO feeds (url, title, created_at) VALUES (?, ?, ?)",
        (url, "Example", now()),
    )
    conn.commit()
    return cur.lastrowid


def add_item(conn, feed_id, guid="guid-1"):
    cur = conn.execute(
        "INSERT INTO items (feed_id, guid, link, original_title, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (feed_id, guid, "https://example.com/1", "Title", now()),
    )
    conn.commit()
    return cur.lastrowid


def test_schema_creates_tables(db):
    names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"feeds", "items", "settings"} <= names


def test_insert_feed_and_item(db):
    feed_id = add_feed(db)
    add_item(db, feed_id)
    row = db.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchone()
    assert row["guid"] == "guid-1"
    assert row["fallback"] == 0


def test_duplicate_guid_per_feed_rejected(db):
    feed_id = add_feed(db)
    add_item(db, feed_id)
    with pytest.raises(sqlite3.IntegrityError):
        add_item(db, feed_id)


def test_same_guid_allowed_in_other_feed(db):
    a = add_feed(db, "https://example.com/a.xml")
    b = add_feed(db, "https://example.com/b.xml")
    add_item(db, a)
    add_item(db, b)
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 2


def test_duplicate_feed_url_rejected(db):
    add_feed(db)
    with pytest.raises(sqlite3.IntegrityError):
        add_feed(db)


def test_cascade_delete_removes_items(db):
    feed_id = add_feed(db)
    add_item(db, feed_id)
    db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


def test_get_setting_opens_own_connection_when_none_given(db):
    from pintxos.config import get_setting

    db.execute("INSERT INTO settings (key, value) VALUES ('PINTXOS_MODEL', 'from-db')")
    db.commit()
    assert get_setting("PINTXOS_MODEL") == "from-db"


def test_connect_migrates_existing_db_missing_ads_filtered_column(tmp_path, monkeypatch):
    """An older DB (created before ads_filtered existed) gains the column on connect()."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    old_conn = sqlite3.connect(db_path())
    old_conn.executescript(
        """
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            created_at TEXT,
            last_polled_at TEXT,
            last_error TEXT
        );
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
        assert "ads_filtered" in cols
    finally:
        conn.close()


def test_connect_migrates_existing_db_missing_per_feed_ad_columns(tmp_path, monkeypatch):
    """A DB created before the per-feed override columns gains them all on connect()."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    old_conn = sqlite3.connect(db_path())
    old_conn.executescript(
        """
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            created_at TEXT,
            last_polled_at TEXT,
            last_error TEXT,
            ads_filtered INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO feeds (url) VALUES ('https://example.com/feed.xml');
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
        assert {"filter_ads", "ad_title_patterns", "ad_patterns_mode"} <= cols
        row = conn.execute("SELECT * FROM feeds").fetchone()
        assert row["filter_ads"] is None  # existing feeds keep following the global setting
        assert row["ad_title_patterns"] is None
        assert row["ad_patterns_mode"] is None  # and inherit the global title patterns
    finally:
        conn.close()


def test_items_has_word_count_column(db):
    assert "word_count" in {r["name"] for r in db.execute("PRAGMA table_info(items)")}


def test_connect_migrates_existing_db_missing_items_word_count_and_auth_columns(
    tmp_path, monkeypatch
):
    """A DB created before word_count/auth existed gains both columns on connect(), twice."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    old_conn = sqlite3.connect(db_path())
    old_conn.executescript(
        """
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            created_at TEXT,
            last_polled_at TEXT,
            last_error TEXT
        );
        CREATE TABLE items (
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
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        assert cols.count("word_count") == 1
        assert "word_count" in cols
        assert cols.count("auth") == 1
        assert "auth" in cols
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and columns stay singular.
    conn2 = connect()
    try:
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(items)")]
        assert cols2.count("word_count") == 1
        assert cols2.count("auth") == 1

        feed_id = add_feed(conn2)
        item_id = add_item(conn2, feed_id)
        row = conn2.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["word_count"] is None  # not fetched/summarized -> no stats yet
        assert row["auth"] is None
    finally:
        conn2.close()
