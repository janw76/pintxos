"""Per-feed, per-day counters: how many summaries and classifications a feed cost us.

Every function takes an already-open connection and never opens a transaction of its
own, so callers stay in charge of commit/rollback (see `db.db()`).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime


def today() -> str:
    """The current UTC date as YYYY-MM-DD, the key used by the `day` column."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


_today = today  # kept for functions whose `today` parameter shadows the module function


def bump(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    summaries: int = 0,
    classifications: int = 0,
    day: str | None = None,
) -> None:
    """Add `summaries`/`classifications` to a feed's counters for `day` (default: today).

    Both counters zero is a no-op: no row is created for a feed that did no work.
    """
    if not summaries and not classifications:
        return
    conn.execute(
        "INSERT INTO feed_stats (feed_id, day, summaries, classifications)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(feed_id, day) DO UPDATE SET"
        " summaries = summaries + excluded.summaries,"
        " classifications = classifications + excluded.classifications",
        (feed_id, day or today(), summaries, classifications),
    )


def kept_today(conn: sqlite3.Connection, feed_id: int) -> int:
    """Rows inserted today for this feed, any muted/fallback state.

    Compare with `totals()[0]` (summaries today) to spot paid-and-discarded work: a
    feed that pays for many summaries but keeps few rows is likely stuck re-summarizing
    items it then drops.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE feed_id = ? AND substr(created_at, 1, 10) = ?",
        (feed_id, today()),
    ).fetchone()
    return int(row[0])


def item_stats(conn: sqlite3.Connection, feed_id: int, *, today: str | None = None) -> dict:
    """Basic live stats for a feed's items: volume, age spread, and average length.

    `days` counts UTC calendar days from the feed's earliest item through `today`
    (default: today()), inclusive, floored at 1. `avg_words` is rounded and only
    covers rows with a known `word_count`; it is None when none do. Zero items yields
    items=0, days=1, per_day=0.0, avg_words=None.
    """
    today = today or _today()
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS earliest,"
        " AVG(word_count) AS avg_words FROM items WHERE feed_id = ?",
        (feed_id,),
    ).fetchone()
    items = int(row["n"])
    if items == 0:
        return {"items": 0, "days": 1, "per_day": 0.0, "avg_words": None}
    earliest_day = date.fromisoformat(row["earliest"][:10])
    today_day = date.fromisoformat(today)
    days = max(1, (today_day - earliest_day).days + 1)
    avg_words = round(row["avg_words"]) if row["avg_words"] is not None else None
    return {"items": items, "days": days, "per_day": items / days, "avg_words": avg_words}


def totals(conn: sqlite3.Connection, feed_id: int) -> tuple[int, int]:
    """Return (summaries today, summaries over all days) for a feed. No rows = (0, 0)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(summaries), 0) AS n FROM feed_stats WHERE feed_id = ? AND day = ?",
        (feed_id, today()),
    ).fetchone()
    day_total = row[0]
    row = conn.execute(
        "SELECT COALESCE(SUM(summaries), 0) AS n FROM feed_stats WHERE feed_id = ?",
        (feed_id,),
    ).fetchone()
    return int(day_total), int(row[0])
