"""Tests for the RSS output route."""

from __future__ import annotations

import feedparser
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
            (feed_id, guid, link, original_title, published_at, headline, summary, fallback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-1",
                "https://example.com/1",
                "Original One",
                "2026-09-01T12:00:00+00:00",
                "Headline One",
                "Summary one.",
                0,
                now(),
            ),
        )
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary, fallback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-2",
                "https://example.com/2",
                "Original Two",
                "2026-09-02T12:00:00+00:00",
                "Headline Two",
                "Summary two.",
                1,
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
