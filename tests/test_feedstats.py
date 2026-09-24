import pytest

from pintxos.db import connect, init_db, now
from pintxos.feedstats import bump, item_stats, kept_today, today, totals


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


def add_item(conn, feed_id, guid, created_at):
    conn.execute(
        "INSERT INTO items (feed_id, guid, link, created_at) VALUES (?, ?, ?, ?)",
        (feed_id, guid, f"https://example.com/{guid}", created_at),
    )
    conn.commit()


def test_kept_today_counts_only_todays_rows_for_this_feed(db):
    a = add_feed(db, "https://example.com/a.xml")
    b = add_feed(db, "https://example.com/b.xml")
    add_item(db, a, "guid-today", now())
    add_item(db, a, "guid-yesterday", "2020-01-01T00:00:00.000000+00:00")
    add_item(db, b, "guid-other-feed-today", now())
    assert kept_today(db, a) == 1


def test_kept_today_for_feed_without_rows_is_zero(db):
    feed_id = add_feed(db)
    assert kept_today(db, feed_id) == 0


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


def add_item_with_words(conn, feed_id, guid, created_at, word_count=None):
    conn.execute(
        "INSERT INTO items (feed_id, guid, link, created_at, word_count)"
        " VALUES (?, ?, ?, ?, ?)",
        (feed_id, guid, f"https://example.com/{guid}", created_at, word_count),
    )
    conn.commit()


def test_item_stats_across_two_days_with_some_null_word_counts(db):
    feed_id = add_feed(db)
    add_item_with_words(db, feed_id, "g1", "2026-09-10T00:00:00.000000+00:00", 100)
    add_item_with_words(db, feed_id, "g2", "2026-09-10T12:00:00.000000+00:00", 200)
    add_item_with_words(db, feed_id, "g3", "2026-09-11T00:00:00.000000+00:00", None)
    stats = item_stats(db, feed_id, today="2026-09-11")
    assert stats["items"] == 3
    assert stats["days"] == 2
    assert stats["per_day"] == pytest.approx(1.5)
    assert stats["avg_words"] == 150


def test_item_stats_days_extend_through_today(db):
    feed_id = add_feed(db)
    add_item_with_words(db, feed_id, "g1", "2026-09-10T00:00:00.000000+00:00", 10)
    stats = item_stats(db, feed_id, today="2026-09-14")
    assert stats["days"] == 5
    assert stats["per_day"] == pytest.approx(1 / 5)


def test_item_stats_no_items_is_zero(db):
    feed_id = add_feed(db)
    stats = item_stats(db, feed_id, today="2026-09-11")
    assert stats["items"] == 0


def test_item_stats_all_null_word_counts_gives_none(db):
    feed_id = add_feed(db)
    add_item_with_words(db, feed_id, "g1", "2026-09-10T00:00:00.000000+00:00", None)
    add_item_with_words(db, feed_id, "g2", "2026-09-10T01:00:00.000000+00:00", None)
    stats = item_stats(db, feed_id, today="2026-09-10")
    assert stats["avg_words"] is None


def test_item_stats_ignores_other_feeds(db):
    a = add_feed(db, "https://example.com/a.xml")
    b = add_feed(db, "https://example.com/b.xml")
    add_item_with_words(db, a, "g1", "2026-09-10T00:00:00.000000+00:00", 50)
    add_item_with_words(db, b, "g2", "2026-09-10T00:00:00.000000+00:00", 999)
    stats = item_stats(db, a, today="2026-09-10")
    assert stats["items"] == 1
    assert stats["avg_words"] == 50
