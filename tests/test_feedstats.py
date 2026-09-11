import pytest

from pintxos.db import connect, init_db, now
from pintxos.feedstats import bump, today, totals


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


def rows(conn, feed_id):
    return conn.execute(
        "SELECT day, summaries, classifications FROM feed_stats WHERE feed_id = ? ORDER BY day",
        (feed_id,),
    ).fetchall()


def test_today_is_an_iso_utc_date():
    from datetime import UTC, datetime

    assert today() == datetime.now(UTC).strftime("%Y-%m-%d")
    assert len(today()) == 10 and today().count("-") == 2


def test_bump_inserts_a_row_for_today(db):
    feed_id = add_feed(db)
    bump(db, feed_id, summaries=1, classifications=2)
    (row,) = rows(db, feed_id)
    assert (row["day"], row["summaries"], row["classifications"]) == (today(), 1, 2)


def test_bump_twice_on_the_same_day_accumulates(db):
    feed_id = add_feed(db)
    bump(db, feed_id, summaries=1, classifications=1)
    bump(db, feed_id, summaries=3, classifications=0)
    (row,) = rows(db, feed_id)  # upsert, not a second row
    assert (row["summaries"], row["classifications"]) == (4, 1)


def test_two_days_accumulate_separately(db):
    feed_id = add_feed(db)
    bump(db, feed_id, summaries=2, day="2026-09-10")
    bump(db, feed_id, summaries=5, day="2026-09-10")
    bump(db, feed_id, summaries=3, day=today())
    got = [(r["day"], r["summaries"]) for r in rows(db, feed_id)]
    assert sorted(got) == sorted([("2026-09-10", 7), (today(), 3)])


def test_totals_returns_today_and_all_days(db):
    feed_id = add_feed(db)
    bump(db, feed_id, summaries=4, day="2026-09-10")
    bump(db, feed_id, summaries=3)
    assert totals(db, feed_id) == (3, 7)


def test_totals_ignores_other_feeds(db):
    a = add_feed(db, "https://example.com/a.xml")
    b = add_feed(db, "https://example.com/b.xml")
    bump(db, a, summaries=2)
    bump(db, b, summaries=9, day="2026-09-10")
    assert totals(db, a) == (2, 2)
    assert totals(db, b) == (0, 9)


def test_totals_for_feed_without_rows_is_zero(db):
    feed_id = add_feed(db)
    assert totals(db, feed_id) == (0, 0)


def test_no_op_bump_writes_no_row(db):
    feed_id = add_feed(db)
    bump(db, feed_id)
    bump(db, feed_id, summaries=0, classifications=0, day="2026-09-10")
    assert rows(db, feed_id) == []
    assert totals(db, feed_id) == (0, 0)


def test_classifications_counted_independently_of_summaries(db):
    feed_id = add_feed(db)
    bump(db, feed_id, classifications=2)
    (row,) = rows(db, feed_id)
    assert (row["summaries"], row["classifications"]) == (0, 2)
    assert totals(db, feed_id) == (0, 0)  # totals() reports summaries only


def test_deleting_the_feed_cascades_its_stats(db):
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # connect() enables them
    feed_id = add_feed(db)
    bump(db, feed_id, summaries=1, day="2026-09-10")
    bump(db, feed_id, summaries=1)
    db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM feed_stats").fetchone()["n"] == 0


def test_bump_does_not_commit_on_its_own(db):
    """bump() leaves the transaction open so the caller decides when to commit."""
    feed_id = add_feed(db)
    bump(db, feed_id, summaries=1)
    assert db.in_transaction
    db.rollback()
    assert rows(db, feed_id) == []
