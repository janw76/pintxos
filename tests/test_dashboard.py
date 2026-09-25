import pytest

from pintxos.dashboard import summary
from pintxos.db import connect, init_db
from pintxos.feedstats import bump


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh database under tmp_path so tests never touch ./data."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    init_db()
    conn = connect()
    yield conn
    conn.close()


def add_feed(conn, url="https://example.com/feed.xml", title="Example"):
    cur = conn.execute(
        "INSERT INTO feeds (url, title, created_at) VALUES (?, ?, ?)",
        (url, title, "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return cur.lastrowid


def add_item(
    conn,
    feed_id,
    guid,
    created_at,
    *,
    muted=0,
    word_count=None,
    summary=None,
    topic=None,
    model=None,
    auth=None,
    fetch_status=None,
    fallback=0,
):
    conn.execute(
        "INSERT INTO items (feed_id, guid, link, created_at, muted, word_count,"
        " summary, topic, model, auth, fetch_status, fallback)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            feed_id,
            guid,
            f"https://example.com/{guid}",
            created_at,
            muted,
            word_count,
            summary,
            topic,
            model,
            auth,
            fetch_status,
            fallback,
        ),
    )
    conn.commit()


def words(n):
    return " ".join(["word"] * n)


TODAY = "2026-09-20"
WINDOW_START = "2026-09-14"


def test_full_dashboard_exact_dict(db):
    feed_a = add_feed(db, "https://example.com/a.xml", "Feed A")
    feed_b = add_feed(db, "https://example.com/b.xml", "Feed B")

    # Feed A: today, today-3, today-6 (edge), today-7 (outside), and a muted item.
    add_item(
        db,
        feed_a,
        "a1",
        "2026-09-20T10:00:00+00:00",
        word_count=1000,
        summary=words(200),
        topic="science",
        model="gpt-a",
        auth="used",
        fetch_status="ok",
    )
    add_item(
        db,
        feed_a,
        "a2",
        "2026-09-17T00:00:00+00:00",
        word_count=500,
        topic="science",
        model="gpt-a",
        fetch_status="ok",
    )
    add_item(
        db,
        feed_a,
        "a3",
        "2026-09-14T00:00:00+00:00",
        topic="economy",
        model="gpt-b",
        fallback=1,
        fetch_status="error",
    )
    add_item(
        db,
        feed_a,
        "a4-outside",
        "2026-09-13T00:00:00+00:00",
        word_count=99999,
        topic="science",
        model="gpt-a",
        auth="used",
        fetch_status="ok",
    )
    add_item(
        db,
        feed_a,
        "a5-muted",
        "2026-09-20T05:00:00+00:00",
        muted=1,
        word_count=100,
        topic="science",
        model="gpt-a",
        auth="used",
        fetch_status="ok",
    )
    add_item(db, feed_a, "a6", "2026-09-18T00:00:00+00:00", fetch_status="ok")

    # Feed B: today, today-3, today-6.
    add_item(
        db,
        feed_b,
        "b1",
        "2026-09-20T08:00:00+00:00",
        word_count=300,
        summary=words(400),  # summary longer than the article: words_saved floors at 0
        fetch_status="ok",
    )
    add_item(
        db,
        feed_b,
        "b2",
        "2026-09-17T00:00:00+00:00",
        auth="failed",
        fetch_status="error",
    )
    add_item(
        db,
        feed_b,
        "b3",
        "2026-09-14T00:00:00+00:00",
        word_count=200,
        topic="economy",
        model="gpt-a",
        fetch_status="teaser",
    )

    # feed_stats filter counters: inside the window, plus one row before it.
    bump(db, feed_a, filtered_ads=2, filtered_keywords=1, day="2026-09-17")
    bump(db, feed_b, filtered_budget=3, filtered_topic=4, day="2026-09-14")
    bump(db, feed_a, filtered_ads=100, day="2026-09-13")  # outside window
    db.commit()

    result = summary(db, today=TODAY, days=7)

    assert result == {
        "window_start": "2026-09-14",
        "window_end": "2026-09-20",
        "days": 7,
        "feeds": 2,
        "summarized": 7,
        "per_day": [
            {"day": "2026-09-14", "n": 2},
            {"day": "2026-09-15", "n": 0},
            {"day": "2026-09-16", "n": 0},
            {"day": "2026-09-17", "n": 2},
            {"day": "2026-09-18", "n": 1},
            {"day": "2026-09-19", "n": 0},
            {"day": "2026-09-20", "n": 2},
        ],
        "filtered": {"ads": 2, "keywords": 1, "budget": 3, "topic": 4, "total": 10},
        "unreadable": 1,
        "avg_words": 500,
        "words_saved": 800,
        "minutes_saved": 3,
        "feed_extremes": {
            "most_per_day": {"feed_id": feed_a, "title": "Feed A", "value": pytest.approx(4 / 7)},
            "least_per_day": {"feed_id": feed_b, "title": "Feed B", "value": pytest.approx(3 / 7)},
            "longest": {"feed_id": feed_a, "title": "Feed A", "value": 750},
            "shortest": {"feed_id": feed_b, "title": "Feed B", "value": 250},
        },
        "topics": [
            {"slug": "economy", "name": "economy, business and finance", "n": 2, "percent": 50},
            {"slug": "science", "name": "science and technology", "n": 2, "percent": 50},
        ],
        "models": [
            {"model": "gpt-a", "n": 3, "percent": 75},
            {"model": "gpt-b", "n": 1, "percent": 25},
        ],
        "paywall": {"used": 1, "paywalled": 1, "login_failed": 1},
    }


def test_empty_db_is_all_zero_no_exception(db):
    result = summary(db, today=TODAY, days=7)

    assert result["window_start"] == "2026-09-14"
    assert result["window_end"] == "2026-09-20"
    assert result["days"] == 7
    assert result["feeds"] == 0
    assert result["summarized"] == 0
    assert result["per_day"] == [
        {"day": d, "n": 0}
        for d in (
            "2026-09-14",
            "2026-09-15",
            "2026-09-16",
            "2026-09-17",
            "2026-09-18",
            "2026-09-19",
            "2026-09-20",
        )
    ]
    assert result["filtered"] == {"ads": 0, "keywords": 0, "budget": 0, "topic": 0, "total": 0}
    assert result["unreadable"] == 0
    assert result["avg_words"] is None
    assert result["words_saved"] == 0
    assert result["minutes_saved"] == 0
    assert result["feed_extremes"] == {
        "most_per_day": None,
        "least_per_day": None,
        "longest": None,
        "shortest": None,
    }
    assert result["topics"] == []
    assert result["models"] == []
    assert result["paywall"] == {"used": 0, "paywalled": 0, "login_failed": 0}


def test_per_day_zero_filled_and_ordered(db):
    feed_id = add_feed(db)
    add_item(db, feed_id, "i1", "2026-09-14T00:00:00+00:00")
    add_item(db, feed_id, "i2", "2026-09-20T00:00:00+00:00")
    add_item(db, feed_id, "i3", "2026-09-20T12:00:00+00:00")

    result = summary(db, today=TODAY, days=7)

    assert [d["day"] for d in result["per_day"]] == [
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-19",
        "2026-09-20",
    ]
    assert len(result["per_day"]) == 7
    assert [d["n"] for d in result["per_day"]] == [1, 0, 0, 0, 0, 0, 2]


def test_muted_item_counts_for_nothing(db):
    feed_id = add_feed(db)
    add_item(
        db,
        feed_id,
        "muted",
        "2026-09-20T00:00:00+00:00",
        muted=1,
        word_count=500,
        summary=words(10),
        topic="science",
        model="gpt-a",
        auth="used",
        fetch_status="ok",
    )

    result = summary(db, today=TODAY, days=7)

    assert result["summarized"] == 0
    assert all(d["n"] == 0 for d in result["per_day"])
    assert result["avg_words"] is None
    assert result["words_saved"] == 0
    assert result["unreadable"] == 0
    assert result["topics"] == []
    assert result["models"] == []
    assert result["paywall"] == {"used": 0, "paywalled": 0, "login_failed": 0}
    assert result["feed_extremes"]["most_per_day"] == {
        "feed_id": feed_id,
        "title": "Example",
        "value": 0.0,
    }


def test_words_saved_floors_at_zero(db):
    feed_id = add_feed(db)
    add_item(
        db,
        feed_id,
        "i1",
        "2026-09-20T00:00:00+00:00",
        word_count=50,
        summary=words(200),  # summary far longer than the article
    )

    result = summary(db, today=TODAY, days=7)

    assert result["words_saved"] == 0
    assert result["minutes_saved"] == 0


def test_feed_with_zero_items_is_least_per_day_zero(db):
    feed_with_items = add_feed(db, "https://example.com/a.xml", "Has Items")
    feed_empty = add_feed(db, "https://example.com/b.xml", "Empty Feed")
    add_item(db, feed_with_items, "i1", "2026-09-20T00:00:00+00:00")

    result = summary(db, today=TODAY, days=7)

    assert result["feed_extremes"]["least_per_day"] == {
        "feed_id": feed_empty,
        "title": "Empty Feed",
        "value": 0.0,
    }


def test_feed_with_no_word_count_excluded_from_length_extremes(db):
    feed_with_words = add_feed(db, "https://example.com/a.xml", "Has Words")
    feed_no_words = add_feed(db, "https://example.com/b.xml", "No Words")
    add_item(db, feed_with_words, "i1", "2026-09-20T00:00:00+00:00", word_count=400)
    add_item(db, feed_no_words, "i2", "2026-09-20T00:00:00+00:00")

    result = summary(db, today=TODAY, days=7)

    assert result["feed_extremes"]["longest"] == {
        "feed_id": feed_with_words,
        "title": "Has Words",
        "value": 400,
    }
    assert result["feed_extremes"]["shortest"] == {
        "feed_id": feed_with_words,
        "title": "Has Words",
        "value": 400,
    }


def test_tie_lowest_feed_id_wins(db):
    feed_low = add_feed(db, "https://example.com/a.xml", "Low Id")
    feed_high = add_feed(db, "https://example.com/b.xml", "High Id")
    add_item(db, feed_low, "i1", "2026-09-20T00:00:00+00:00", word_count=300)
    add_item(db, feed_high, "i2", "2026-09-20T00:00:00+00:00", word_count=300)

    result = summary(db, today=TODAY, days=7)

    extremes = result["feed_extremes"]
    assert extremes["most_per_day"]["feed_id"] == feed_low
    assert extremes["least_per_day"]["feed_id"] == feed_low
    assert extremes["longest"]["feed_id"] == feed_low
    assert extremes["shortest"]["feed_id"] == feed_low


def test_filtered_sums_only_include_rows_inside_window(db):
    feed_id = add_feed(db)
    bump(db, feed_id, filtered_ads=5, day="2026-09-13")  # day before window_start
    bump(db, feed_id, filtered_keywords=7, day="2026-09-14")  # window_start
    bump(db, feed_id, filtered_budget=9, day="2026-09-20")  # today
    bump(db, feed_id, filtered_topic=11, day="2026-09-21")  # after today
    db.commit()

    result = summary(db, today=TODAY, days=7)

    assert result["filtered"] == {"ads": 0, "keywords": 7, "budget": 9, "topic": 0, "total": 16}


def test_topics_top_five_plus_other(db):
    feed_id = add_feed(db)
    slugs = ["science", "economy", "health", "sport", "crime", "arts", "weather"]
    for i, slug in enumerate(slugs):
        # Give each topic a distinct count, descending, so ordering is unambiguous.
        for j in range(len(slugs) - i):
            add_item(db, feed_id, f"{slug}-{j}", "2026-09-20T00:00:00+00:00", topic=slug)

    result = summary(db, today=TODAY, days=7)

    topics = result["topics"]
    assert len(topics) == 6
    assert [t["slug"] for t in topics[:5]] == ["science", "economy", "health", "sport", "crime"]
    assert topics[5]["slug"] == "other"
    total = sum(len(slugs) - i for i in range(len(slugs)))  # 7+6+5+4+3+2+1 = 28
    assert topics[5]["n"] == 3  # arts (2) + weather (1)
    assert sum(t["n"] for t in topics) == total


def test_buckets_match_app_bucket_sql(db):
    """dashboard._BUCKET_CASE is a hand-copy of app._BUCKET_SQL; compute the same
    buckets with app's own SQL and assert dashboard.summary() agrees, so the two
    copies can't silently drift apart."""
    from pintxos import app as app_module

    feed_id = add_feed(db)
    # (guid, auth, fetch_status, fallback, muted) covering every bucket and the
    # gaps between them.
    rows = [
        ("used-ok", "used", "ok", 0, 0),  # used only
        ("paywalled-missing-teaser", "missing", "teaser", 0, 0),  # paywalled (auth missing)
        ("paywalled-null-blocked", None, "blocked", 0, 0),  # paywalled (auth NULL)
        ("login-failed", "failed", "error", 1, 0),  # login_failed; not unreadable (auth != missing/NULL)
        ("unreadable-missing-error", "missing", "error", 1, 0),  # unreadable (fetch_status = 'error')
        ("unreadable-null-null", None, None, 1, 0),  # unreadable (fetch_status IS NULL)
        ("not-unreadable-fallback0", "missing", "error", 0, 0),  # nowhere: fallback = 0
        ("used-teaser-not-paywalled", "used", "teaser", 0, 0),  # used; not paywalled (auth = used)
        ("muted-used-ok", "used", "ok", 0, 1),  # muted: must count in neither
    ]
    for guid, auth, fetch_status, fallback, muted in rows:
        add_item(
            db,
            feed_id,
            guid,
            f"{TODAY}T00:00:00+00:00",
            auth=auth,
            fetch_status=fetch_status,
            fallback=fallback,
            muted=muted,
        )

    expected_row = db.execute(
        f"SELECT {app_module._bucket_sql('')} FROM items WHERE feed_id = ? AND muted = 0",
        (feed_id,),
    ).fetchone()

    result = summary(db, today=TODAY, days=7)

    assert result["paywall"] == {
        "used": int(expected_row["used"] or 0),
        "paywalled": int(expected_row["paywalled"] or 0),
        "login_failed": int(expected_row["login_failed"] or 0),
    }
    assert result["unreadable"] == int(expected_row["unreadable"] or 0)
