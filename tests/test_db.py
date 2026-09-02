import sqlite3

import pytest

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
