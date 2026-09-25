"""Every number the Stats page shows: a single 7-day dashboard query.

Like `pintxos.feedstats`, `summary()` takes an already-open connection and never
commits; the caller decides. All figures are computed live (no caching) over a
fixed window of `days` UTC calendar days ending at `today` inclusive.

The fetch-status bucket predicates (paywalled / login_failed / unreadable / used)
are owned by `pintxos.app._BUCKET_SQL`; importing that module here would risk a
circular import (app imports a lot), so the four predicates this module needs are
copied below, verbatim, as `_BUCKET_CASE`. If `app._BUCKET_SQL` changes, update the
copy here too.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from pintxos import feedstats, stats
from pintxos.topics import TOPIC_NAMES

# Copied from pintxos.app._BUCKET_SQL (source of truth), rewritten as CASE/SUM
# expressions over a single un-prefixed `items` table instead of a format string.
_BUCKET_CASE = {
    "paywalled": (
        "SUM(CASE WHEN fetch_status IN ('teaser', 'blocked')"
        " AND (auth = 'missing' OR auth IS NULL) THEN 1 ELSE 0 END)"
    ),
    "login_failed": "SUM(CASE WHEN auth = 'failed' THEN 1 ELSE 0 END)",
    "unreadable": (
        "SUM(CASE WHEN fallback = 1 AND (auth = 'missing' OR auth IS NULL)"
        " AND (fetch_status = 'error' OR fetch_status IS NULL) THEN 1 ELSE 0 END)"
    ),
    "used": "SUM(CASE WHEN auth = 'used' THEN 1 ELSE 0 END)",
}

# SQL for a kept item's summary word count: LENGTH(TRIM(summary)) -
# LENGTH(REPLACE(TRIM(summary), ' ', '')) + 1, only when summary is non-blank.
_SUMMARY_WORDS_SQL = (
    "(LENGTH(TRIM(summary)) - LENGTH(REPLACE(TRIM(summary), ' ', '')) + 1)"
)
_HAS_SUMMARY_SQL = "summary IS NOT NULL AND TRIM(summary) <> ''"


def _window_start(today: str, days: int) -> str:
    return (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()


def _day_range(window_start: str, today: str) -> list[str]:
    start = date.fromisoformat(window_start)
    end = date.fromisoformat(today)
    n = (end - start).days + 1
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def _per_day(conn: sqlite3.Connection, window_start: str, today: str) -> list[dict]:
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM items"
        " WHERE muted = 0 AND substr(created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY day",
        (window_start, today),
    ).fetchall()
    counts = {row["day"]: int(row["n"]) for row in rows}
    return [{"day": day, "n": counts.get(day, 0)} for day in _day_range(window_start, today)]


def _filtered(conn: sqlite3.Connection, window_start: str, today: str) -> dict:
    # topic is counted from stored items, not from feed_stats: topic-muted
    # articles are kept (items.muted = 1, set only on the topic-mute path in
    # poll.py), so the count is exact and covers the whole window immediately.
    # ads, keywords and budget entries are never inserted into items, so they
    # can only be counted at poll time via feed_stats, and only from the day
    # those counters shipped. feed_stats.filtered_topic is still bumped by
    # poll for consistency with the filter log, but it is not read here.
    row = conn.execute(
        "SELECT COALESCE(SUM(filtered_ads), 0) AS ads,"
        " COALESCE(SUM(filtered_keywords), 0) AS keywords,"
        " COALESCE(SUM(filtered_budget), 0) AS budget"
        " FROM feed_stats WHERE day BETWEEN ? AND ?",
        (window_start, today),
    ).fetchone()
    ads, keywords, budget = int(row["ads"]), int(row["keywords"]), int(row["budget"])
    topic_row = conn.execute(
        "SELECT COUNT(*) AS n FROM items"
        " WHERE muted = 1 AND substr(created_at, 1, 10) BETWEEN ? AND ?",
        (window_start, today),
    ).fetchone()
    topic = int(topic_row["n"])
    return {
        "ads": ads,
        "keywords": keywords,
        "budget": budget,
        "topic": topic,
        "total": ads + keywords + budget + topic,
    }


def _buckets(conn: sqlite3.Connection, window_start: str, today: str) -> dict:
    select = ", ".join(f"{expr} AS {name}" for name, expr in _BUCKET_CASE.items())
    row = conn.execute(
        f"SELECT {select} FROM items"
        " WHERE muted = 0 AND substr(created_at, 1, 10) BETWEEN ? AND ?",
        (window_start, today),
    ).fetchone()
    return {name: int(row[name] or 0) for name in _BUCKET_CASE}


def _avg_words(conn: sqlite3.Connection, window_start: str, today: str) -> int | None:
    row = conn.execute(
        "SELECT AVG(word_count) AS avg_words FROM items"
        " WHERE muted = 0 AND word_count IS NOT NULL"
        " AND substr(created_at, 1, 10) BETWEEN ? AND ?",
        (window_start, today),
    ).fetchone()
    return round(row["avg_words"]) if row["avg_words"] is not None else None


def _words_saved(conn: sqlite3.Connection, window_start: str, today: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(MAX(word_count - " + _SUMMARY_WORDS_SQL + ", 0)), 0) AS n"
        " FROM items"
        " WHERE muted = 0 AND word_count IS NOT NULL AND " + _HAS_SUMMARY_SQL +
        " AND substr(created_at, 1, 10) BETWEEN ? AND ?",
        (window_start, today),
    ).fetchone()
    return int(row["n"])


def _feed_extremes(conn: sqlite3.Connection, window_start: str, today: str, days: int) -> dict:
    feeds = conn.execute("SELECT id FROM feeds").fetchall()
    empty = {"most_per_day": None, "least_per_day": None, "longest": None, "shortest": None}
    if not feeds:
        return empty

    per_day_rows = conn.execute(
        "SELECT f.id AS feed_id, COALESCE(NULLIF(f.title, ''), f.url) AS title,"
        " COUNT(i.id) AS n"
        " FROM feeds f"
        " LEFT JOIN items i ON i.feed_id = f.id AND i.muted = 0"
        " AND substr(i.created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY f.id",
        (window_start, today),
    ).fetchall()
    per_day = [
        {"feed_id": row["feed_id"], "title": row["title"], "value": row["n"] / days}
        for row in per_day_rows
    ]
    most = min(per_day, key=lambda r: (-r["value"], r["feed_id"]))
    least = min(per_day, key=lambda r: (r["value"], r["feed_id"]))

    length_rows = conn.execute(
        "SELECT f.id AS feed_id, COALESCE(NULLIF(f.title, ''), f.url) AS title,"
        " AVG(i.word_count) AS avg_words"
        " FROM feeds f"
        " JOIN items i ON i.feed_id = f.id AND i.muted = 0 AND i.word_count IS NOT NULL"
        " AND substr(i.created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY f.id"
        " HAVING COUNT(i.id) > 0",
        (window_start, today),
    ).fetchall()
    lengths = [
        {"feed_id": row["feed_id"], "title": row["title"], "value": round(row["avg_words"])}
        for row in length_rows
    ]
    longest = min(lengths, key=lambda r: (-r["value"], r["feed_id"])) if lengths else None
    shortest = min(lengths, key=lambda r: (r["value"], r["feed_id"])) if lengths else None

    return {
        "most_per_day": most,
        "least_per_day": least,
        "longest": longest,
        "shortest": shortest,
    }


def _topics(conn: sqlite3.Connection, window_start: str, today: str) -> list[dict]:
    rows = conn.execute(
        "SELECT topic, COUNT(*) AS n FROM items"
        " WHERE muted = 0 AND topic IS NOT NULL"
        " AND substr(created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY topic"
        " ORDER BY n DESC, topic ASC",
        (window_start, today),
    ).fetchall()
    if not rows:
        return []
    total = sum(int(row["n"]) for row in rows)
    top = rows[:5]
    result = [
        {
            "slug": row["topic"],
            "name": TOPIC_NAMES.get(row["topic"], row["topic"]),
            "n": int(row["n"]),
            "percent": round(100 * int(row["n"]) / total),
        }
        for row in top
    ]
    if len(rows) > 5:
        other_n = total - sum(int(row["n"]) for row in top)
        result.append(
            {"slug": "other", "name": "Other", "n": other_n, "percent": round(100 * other_n / total)}
        )
    return result


def _models(conn: sqlite3.Connection, window_start: str, today: str) -> list[dict]:
    rows = conn.execute(
        "SELECT model, COUNT(*) AS n FROM items"
        " WHERE muted = 0 AND model IS NOT NULL"
        " AND substr(created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY model"
        " ORDER BY n DESC, model ASC",
        (window_start, today),
    ).fetchall()
    if not rows:
        return []
    total = sum(int(row["n"]) for row in rows)
    return [
        {
            "model": row["model"],
            "n": int(row["n"]),
            "percent": round(100 * int(row["n"]) / total),
        }
        for row in rows
    ]


def summary(conn: sqlite3.Connection, *, today: str | None = None, days: int = 7) -> dict:
    """Every number the Stats page needs, for the `days` UTC days ending at `today`.

    `today` defaults to `feedstats.today()`. "Kept" items mean `muted = 0`,
    everywhere including topics and models.
    """
    today = today or feedstats.today()
    window_start = _window_start(today, days)

    feeds = int(conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"])
    per_day = _per_day(conn, window_start, today)
    summarized = sum(row["n"] for row in per_day)
    filtered = _filtered(conn, window_start, today)
    buckets = _buckets(conn, window_start, today)
    avg_words = _avg_words(conn, window_start, today)
    words_saved = _words_saved(conn, window_start, today)
    minutes_saved = words_saved // stats.WORDS_PER_MINUTE

    return {
        "window_start": window_start,
        "window_end": today,
        "days": days,
        "feeds": feeds,
        "summarized": summarized,
        "per_day": per_day,
        "filtered": filtered,
        "unreadable": buckets["unreadable"],
        "avg_words": avg_words,
        "words_saved": words_saved,
        "minutes_saved": minutes_saved,
        "feed_extremes": _feed_extremes(conn, window_start, today, days),
        "topics": _topics(conn, window_start, today),
        "models": _models(conn, window_start, today),
        "paywall": {
            "used": buckets["used"],
            "paywalled": buckets["paywalled"],
            "login_failed": buckets["login_failed"],
        },
    }
