"""Tests for the item page route (Copy/Share buttons)."""

from __future__ import annotations

import html

from fastapi.testclient import TestClient

from pintxos.app import app
from pintxos.db import db, now
from pintxos.feed_out import item_html, item_plain

FEED_URL = "https://example.com/feed.xml"


def _seed():
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        item_id = conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, word_count, created_at, model, topic, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-1",
                "https://example.com/1",
                "Original One",
                "2026-09-01T12:00:00+00:00",
                "Test Headline",
                "Test summary sentence.",
                0,
                1200,
                now(),
                "test-model",
                "arts",
                "Original One\nfirst para\nsecond",
            ),
        ).lastrowid
    return item_id


def test_item_page_renders():
    item_id = _seed()
    with db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    expected_head = item_html(item, full=False)
    with TestClient(app) as c:
        resp = c.get(f"/items/{item_id}")
    assert resp.status_code == 200
    body = resp.text
    head_start = body.index('id="head"')
    head_div_open_end = body.index(">", head_start) + 1
    assert body[head_div_open_end : head_div_open_end + len(expected_head)] == expected_head
    assert "<p>first para</p>" in body
    assert "<p>second</p>" in body
    # The fetched text's first line duplicates original_title and is deduped by
    # text_lines(): it must not also show up as its own paragraph.
    assert "<p>Original One</p>" not in body
    assert body.count("<p>Original: Original One</p>") == 1


def test_item_page_plain_attrs():
    item_id = _seed()
    with db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    expected_head_plain = html.escape(item_plain(item, full=False), quote=True)
    expected_full_plain = html.escape(item_plain(item, full=True), quote=True)
    with TestClient(app) as c:
        resp = c.get(f"/items/{item_id}")
    body = resp.text
    assert f'id="head" data-plain="{expected_head_plain}"' in body
    assert f'id="item" data-plain="{expected_full_plain}"' in body


def test_item_page_buttons():
    item_id = _seed()
    with TestClient(app) as c:
        resp = c.get(f"/items/{item_id}")
    body = resp.text
    assert "Copy summary" in body
    assert "Copy with full text" in body
    assert "Share summary" in body
    assert "Share with full text" in body
    assert "ClipboardItem" in body
    assert 'execCommand("copy")' in body


def test_item_page_no_pintxos_links():
    item_id = _seed()
    with TestClient(app) as c:
        resp = c.get(f"/items/{item_id}")
    body = resp.text
    assert "/feeds/" not in body
    assert 'class="wordmark"' not in body


def test_item_page_404():
    with TestClient(app) as c:
        resp = c.get("/items/999999")
    assert resp.status_code == 404


def test_index_still_has_header():
    with TestClient(app) as c:
        resp = c.get("/")
    assert 'class="wordmark"' in resp.text
