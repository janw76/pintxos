"""Tests for the RSS output route."""

from __future__ import annotations

import feedparser
import pytest
from fastapi.testclient import TestClient

from pintxos.app import app
from pintxos.db import db, now

FEED_URL = "https://example.com/feed.xml"


def _seed():
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary, fallback, word_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-1",
                "https://example.com/1",
                "Original One",
                "2026-09-01T12:00:00+00:00",
                "Headline One",
                "Summary one.",
                0,
                1200,
                now(),
            ),
        )
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary, fallback, word_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-2",
                "https://example.com/2",
                "Original Two",
                "2026-09-02T12:00:00+00:00",
                "Headline Two",
                "Summary two.",
                1,
                None,
                now(),
            ),
        )
    return feed_id


def test_feed_xml_renders_items():
    _seed()
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")

    parsed = feedparser.parse(resp.content)
    assert parsed.bozo == 0
    assert len(parsed.entries) == 2

    titles = {e.title for e in parsed.entries}
    assert titles == {"Headline One", "Headline Two"}

    for entry in parsed.entries:
        assert entry.published_parsed is not None
        note = "article fetch failed; summarized from feed excerpt"
        if entry.title == "Headline Two":
            assert entry.link == "https://example.com/2"
            assert note in entry.description
            assert "Original: Original Two" in entry.description
        else:
            assert entry.link == "https://example.com/1"
            assert note not in entry.description
            assert "Original: Original One" in entry.description


def _seed_auth_cases():
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        for auth in ("used", "missing", "failed"):
            conn.execute(
                """INSERT INTO items
                (feed_id, guid, link, original_title, published_at, headline, summary, fallback, auth, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    feed_id, f"guid-{auth}", f"https://example.com/{auth}", f"Original {auth.title()}",
                    "2026-09-03T12:00:00+00:00", f"Headline {auth.title()}", f"Summary {auth}.",
                    1, auth, now(),
                ),
            )
    return feed_id


NOTES = {
    "used": "Read with your subscription.",
    "missing": "Login may be required; summarized from the feed excerpt.",
    "failed": "Your saved login did not work (cookies expired?); summarized from the feed excerpt.",
    "null_fallback": "Note: article fetch failed; summarized from feed excerpt.",
}


@pytest.mark.parametrize(
    "seed_fn, feed_url, headline, expected_note_key",
    [
        (_seed_auth_cases, None, "Headline Used", "used"),
        (_seed_auth_cases, None, "Headline Missing", "missing"),
        (_seed_auth_cases, None, "Headline Failed", "failed"),
        (_seed, "/feeds/1.xml", "Headline Two", "null_fallback"),
        (_seed, "/feeds/1.xml", "Headline One", None),
    ],
)
def test_feed_xml_note_reflects_auth_and_fallback(seed_fn, feed_url, headline, expected_note_key):
    feed_id = seed_fn()
    with TestClient(app) as c:
        resp = c.get(feed_url or f"/feeds/{feed_id}.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == headline)
    if expected_note_key is None:
        for note in NOTES.values():
            assert note not in entry.description
    else:
        assert NOTES[expected_note_key] in entry.description


def test_feed_xml_404_for_missing_feed():
    with TestClient(app) as c:
        resp = c.get("/feeds/999.xml")
    assert resp.status_code == 404


def test_feed_xml_contains_pintxos_wordmark_utf8():
    _seed()
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")

    assert resp.status_code == 200
    assert "charset=utf-8" in resp.headers["content-type"]

    xml = resp.content.decode("utf-8")
    assert "Pintxøs" in xml


def test_feed_xml_includes_reading_time_for_fetched_article():
    _seed()
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline One")
    assert "About 1,200 words" in entry.description
    assert "min read" in entry.description


def test_feed_xml_omits_reading_time_for_fallback_item():
    _seed()
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Two")
    assert "min read" not in entry.description


def test_feed_xml_stats_line_precedes_original_line():
    _seed()
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline One")
    assert entry.description.index("min read") < entry.description.index("Original:")
