"""Per-feed, per-day counters: how many summaries and classifications a feed cost us.

Every function takes an already-open connection and never opens a transaction of its
own, so callers stay in charge of commit/rollback (see `db.db()`).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def today() -> str:
    """The current UTC date as YYYY-MM-DD, the key used by the `day` column."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


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
