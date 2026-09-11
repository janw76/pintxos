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


def test_feeds_has_respect_language_column(db):
    assert "respect_language" in {r["name"] for r in db.execute("PRAGMA table_info(feeds)")}


def test_connect_migrates_existing_db_missing_respect_language(tmp_path, monkeypatch):
    """A DB created before the per-feed language override gains the column on connect()."""
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
            ads_filtered INTEGER NOT NULL DEFAULT 0,
            last_filtered TEXT,
            filter_ads INTEGER,
            ad_title_patterns TEXT,
            ad_patterns_mode INTEGER
        );
        INSERT INTO feeds (url) VALUES ('https://example.com/feed.xml');
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(feeds)")}
        assert "respect_language" in cols
        row = conn.execute("SELECT * FROM feeds").fetchone()
        assert row["respect_language"] is None  # existing feeds follow the global setting
    finally:
        conn.close()


def test_items_has_word_count_column(db):
    assert "word_count" in {r["name"] for r in db.execute("PRAGMA table_info(items)")}


def test_items_has_fetch_status_column(db):
    assert "fetch_status" in {r["name"] for r in db.execute("PRAGMA table_info(items)")}


def test_items_has_text_column(db):
    assert "text" in {r["name"] for r in db.execute("PRAGMA table_info(items)")}


def test_connect_migrates_existing_db_missing_items_word_count_and_auth_columns(
    tmp_path, monkeypatch
):
    """A DB from before word_count/auth/fetch_status gains all three on connect(), twice."""
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
        assert cols.count("fetch_status") == 1
        assert "fetch_status" in cols
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and columns stay singular.
    conn2 = connect()
    try:
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(items)")]
        assert cols2.count("word_count") == 1
        assert cols2.count("auth") == 1
        assert cols2.count("fetch_status") == 1

        feed_id = add_feed(conn2)
        item_id = add_item(conn2, feed_id)
        row = conn2.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["word_count"] is None  # not fetched/summarized -> no stats yet
        assert row["auth"] is None
        assert row["fetch_status"] is None  # pre-existing rows stay NULL
    finally:
        conn2.close()


def test_connect_migrates_existing_db_missing_items_text_column(tmp_path, monkeypatch):
    """A DB from before the text column gains it on connect(), and only once."""
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
            word_count INTEGER,
            auth TEXT,
            fetch_status TEXT,
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
        assert cols.count("text") == 1
        assert "text" in cols
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and the column stays singular.
    conn2 = connect()
    try:
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(items)")]
        assert cols2.count("text") == 1

        feed_id = add_feed(conn2)
        item_id = add_item(conn2, feed_id)
        row = conn2.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["text"] is None  # pre-existing rows stay NULL
    finally:
        conn2.close()


def test_connect_migrates_existing_db_missing_labels_column(tmp_path, monkeypatch):
    """A DB from before the labels column gains it on connect(), and only once."""
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
            word_count INTEGER,
            auth TEXT,
            fetch_status TEXT,
            text TEXT,
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
        assert cols.count("labels") == 1
        assert "labels" in cols
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and the column stays singular.
    conn2 = connect()
    try:
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(items)")]
        assert cols2.count("labels") == 1

        feed_id = add_feed(conn2)
        item_id = add_item(conn2, feed_id)
        row = conn2.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["labels"] is None  # pre-existing rows stay NULL
    finally:
        conn2.close()


def test_connect_migrates_existing_db_missing_topic_columns(tmp_path, monkeypatch):
    """A DB from before the topic columns gains them on connect(), and only once."""
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
        feed_cols = [r["name"] for r in conn.execute("PRAGMA table_info(feeds)")]
        for name in ("classify_topics", "mute_topics", "topic_counts"):
            assert feed_cols.count(name) == 1
        item_cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        for name in ("topic", "muted"):
            assert item_cols.count(name) == 1
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and columns stay singular.
    conn2 = connect()
    try:
        feed_cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(feeds)")]
        for name in ("classify_topics", "mute_topics", "topic_counts"):
            assert feed_cols2.count(name) == 1
        item_cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(items)")]
        for name in ("topic", "muted"):
            assert item_cols2.count(name) == 1

        feed_id = add_feed(conn2)
        feed = conn2.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        assert feed["classify_topics"] is None  # pre-existing feeds inherit the global setting
        assert feed["mute_topics"] is None
        assert feed["topic_counts"] is None

        item_id = add_item(conn2, feed_id)
        row = conn2.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["topic"] is None  # pre-existing rows stay unclassified
        assert row["muted"] == 0  # ... and unmuted
    finally:
        conn2.close()


def test_fresh_schema_has_topic_columns(db):
    """A DB created from the current SCHEMA already has the topic columns."""
    feed_cols = {r["name"] for r in db.execute("PRAGMA table_info(feeds)")}
    assert {"classify_topics", "mute_topics", "topic_counts"} <= feed_cols
    item_cols = {r["name"] for r in db.execute("PRAGMA table_info(items)")}
    assert {"topic", "muted"} <= item_cols


def test_connect_migrates_existing_db_missing_feed_stats(tmp_path, monkeypatch):
    """A DB from before feed_stats gains the table and the budget columns on connect()."""
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
            ads_filtered INTEGER NOT NULL DEFAULT 0,
            last_filtered TEXT,
            filter_ads INTEGER,
            ad_title_patterns TEXT,
            ad_patterns_mode INTEGER,
            respect_language INTEGER,
            classify_topics INTEGER,
            mute_topics TEXT,
            topic_counts TEXT
        );
        INSERT INTO feeds (url) VALUES ('https://example.com/feed.xml');
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect()
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert "feed_stats" in {r["name"] for r in tables}
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(feeds)")]
        for name in ("warn_volume", "daily_budget"):
            assert cols.count(name) == 1
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and columns stay singular.
    conn2 = connect()
    try:
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(feeds)")]
        for name in ("warn_volume", "daily_budget"):
            assert cols2.count(name) == 1
        row = conn2.execute("SELECT * FROM feeds").fetchone()
        assert row["warn_volume"] is None  # existing feeds keep the default warning
        assert row["daily_budget"] is None  # ... and stay unlimited
        stats_cols = {r["name"] for r in conn2.execute("PRAGMA table_info(feed_stats)")}
        assert {"feed_id", "day", "summaries", "classifications"} <= stats_cols
    finally:
        conn2.close()


def test_fresh_schema_has_feed_stats_and_budget_columns(db):
    """A DB created from the current SCHEMA already has feed_stats and the budget columns."""
    names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "feed_stats" in names
    feed_cols = {r["name"] for r in db.execute("PRAGMA table_info(feeds)")}
    assert {"warn_volume", "daily_budget"} <= feed_cols
