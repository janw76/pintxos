"""Unit tests for the `ago` Jinja filter (pintxos.app.ago)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pintxos.app import ago

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


def _iso(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def test_none_is_never() -> None:
    assert ago(None, now=NOW) == "never"


def test_empty_is_never() -> None:
    assert ago("", now=NOW) == "never"


def test_seconds() -> None:
    assert ago(_iso(5), now=NOW) == "5s"


def test_minutes_and_seconds() -> None:
    assert ago(_iso(223), now=NOW) == "3m43s"


def test_minutes_no_seconds() -> None:
    assert ago(_iso(180), now=NOW) == "3m"


def test_hours_and_minutes() -> None:
    assert ago(_iso(8100), now=NOW) == "2h15m"


def test_hours_no_minutes() -> None:
    assert ago(_iso(7200), now=NOW) == "2h"


def test_days() -> None:
    assert ago(_iso(3 * 86400), now=NOW) == "3d"


def test_garbage_returned_unchanged() -> None:
    assert ago("garbage", now=NOW) == "garbage"


def test_ago_naive_timestamp_assumed_utc():
    naive = (NOW - timedelta(seconds=90)).replace(tzinfo=None).isoformat()
    assert ago(naive, now=NOW) == "1m30s"


def test_ago_boundaries_and_future_clamp():
    assert ago((NOW - timedelta(seconds=60)).isoformat(), now=NOW) == "1m"
    assert ago((NOW - timedelta(seconds=3600)).isoformat(), now=NOW) == "1h"
    assert ago((NOW - timedelta(seconds=86400)).isoformat(), now=NOW) == "1d"
    assert ago((NOW + timedelta(seconds=30)).isoformat(), now=NOW) == "0s"
