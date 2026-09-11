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


def _seed_fetch_status_cases():
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        rows = [
            # (guid suffix, headline suffix, auth, fetch_status)
            ("teaser", "Teaser", None, "teaser"),
            ("blocked", "Blocked", None, "blocked"),
            ("used-teaser", "UsedTeaser", "used", "teaser"),
            ("failed-blocked", "FailedBlocked", "failed", "blocked"),
            ("error", "Error", None, "error"),
        ]
        for guid, headline, auth, fetch_status in rows:
            conn.execute(
                """INSERT INTO items
                (feed_id, guid, link, original_title, published_at, headline, summary, fallback,
                 auth, fetch_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    feed_id, f"guid-{guid}", f"https://example.com/{guid}", f"Original {headline}",
                    "2026-09-03T12:00:00+00:00", f"Headline {headline}", f"Summary {guid}.",
                    1, auth, fetch_status, now(),
                ),
            )
    return feed_id


NOTES = {
    "used": "Read with your subscription.",
    "missing": "Login may be required; summarized from the feed excerpt.",
    "failed": "Your saved login did not work (cookies expired?); summarized from the feed excerpt.",
    "null_fallback": "Note: article fetch failed; summarized from feed excerpt.",
    "teaser": "Only a teaser was available (paywall); summarized from the feed excerpt.",
    "blocked": "The site blocked the fetch; summarized from the feed excerpt.",
}


@pytest.mark.parametrize(
    "seed_fn, feed_url, headline, expected_note_key",
    [
        (_seed_auth_cases, None, "Headline Used", "used"),
        (_seed_auth_cases, None, "Headline Missing", "missing"),
        (_seed_auth_cases, None, "Headline Failed", "failed"),
        (_seed, "/feeds/1.xml", "Headline Two", "null_fallback"),
        (_seed, "/feeds/1.xml", "Headline One", None),
        (_seed_fetch_status_cases, None, "Headline Teaser", "teaser"),
        (_seed_fetch_status_cases, None, "Headline Blocked", "blocked"),
        (_seed_fetch_status_cases, None, "Headline UsedTeaser", "used"),
        (_seed_fetch_status_cases, None, "Headline FailedBlocked", "failed"),
        (_seed_fetch_status_cases, None, "Headline Error", "null_fallback"),
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


def test_feed_xml_original_line_precedes_stats_line():
    _seed()
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline One")
    assert entry.description.index("Original:") < entry.description.index("min read")


FULL_TEXT_SAMPLE = "AT&T said 1 < 2\n\nSecond para"


def _seed_with_text(text, title="Original Text"):
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary, fallback, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-text",
                "https://example.com/text",
                title,
                "2026-09-04T12:00:00+00:00",
                "Headline Text",
                "Summary text.",
                0,
                text,
                now(),
            ),
        )
    return feed_id


def test_feed_xml_full_text_appends_marker_and_paragraphs():
    feed_id = _seed_with_text(FULL_TEXT_SAMPLE)
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    # feedparser normalises HTML entities in entry.description, so assert on the
    # XML-unescaped raw response body instead (this project's RSS <description>
    # embeds HTML as escaped text, not CDATA).
    import xml.sax.saxutils

    raw = xml.sax.saxutils.unescape(resp.text)
    assert "Original: Original Text" in raw
    assert "=== FULL TEXT BELOW ===" in raw
    assert raw.index("Original:") < raw.index("=== FULL TEXT BELOW ===")
    assert "AT&amp;T said 1 &lt; 2" in raw
    assert (
        "<p>AT&amp;T said 1 &lt; 2</p><p>Second para</p>" in raw
    )  # blank-line paragraph break becomes two contiguous <p> tags, no empty <p></p>


def test_feed_xml_full_text_off_has_no_marker(monkeypatch):
    monkeypatch.setenv("PINTXOS_FULL_TEXT", "0")
    feed_id = _seed_with_text(FULL_TEXT_SAMPLE)
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Text")
    assert "=== FULL TEXT BELOW ===" not in entry.description
    assert entry.description.rstrip().endswith("Original: Original Text</p>")


def test_feed_xml_full_text_null_has_no_marker():
    feed_id = _seed_with_text(None)
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Text")
    assert "=== FULL TEXT BELOW ===" not in entry.description


def test_feed_xml_full_text_skips_duplicate_first_line_title():
    import xml.sax.saxutils

    feed_id = _seed_with_text("Original Text\n\nBody para")
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    raw = xml.sax.saxutils.unescape(resp.text)
    assert "=== FULL TEXT BELOW ===</p><p>Body para</p>" in raw


def test_feed_xml_full_text_skips_duplicate_first_line_title_case_and_whitespace():
    import xml.sax.saxutils

    feed_id = _seed_with_text("original  text\n\nBody para")
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    raw = xml.sax.saxutils.unescape(resp.text)
    assert "=== FULL TEXT BELOW ===</p><p>Body para</p>" in raw


def test_feed_xml_full_text_skips_duplicate_first_line_title_curly_quotes():
    import xml.sax.saxutils

    feed_id = _seed_with_text("It’s Original Text\n\nBody para", title="It's Original Text")
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    raw = xml.sax.saxutils.unescape(resp.text)
    assert "=== FULL TEXT BELOW ===</p><p>Body para</p>" in raw


def test_feed_xml_full_text_keeps_first_line_when_not_a_duplicate():
    import xml.sax.saxutils

    feed_id = _seed_with_text("Different first line\n\nOriginal Text")
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    raw = xml.sax.saxutils.unescape(resp.text)
    assert (
        "=== FULL TEXT BELOW ===</p><p>Different first line</p><p>Original Text</p>" in raw
    )


def test_warning_level_boundaries():
    from pintxos.feed_out import warning_level

    assert warning_level(0) is None
    assert warning_level(49) is None
    assert warning_level(50) == 50
    assert warning_level(99) == 50
    assert warning_level(100) == 100
    assert warning_level(250) == 100


def test_warning_item_fields_and_escaping():
    from pintxos.feed_out import warning_item

    feed = {"id": 7, "title": "News & <Views>", "url": "https://example.com/feed.xml"}
    item = warning_item(
        feed,
        level=50,
        summaries_today=62,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/7",
        model="gpt-4o & friends",
    )

    assert item["guid"] == "pintxos-warning-7-50-2026-09-11"
    assert item["title"] == "Pintxøs: this feed produced 62 summaries today"
    assert item["link"] == "https://pintxos.example/feeds/7"
    assert item["pub_date"].tzinfo is not None

    description = item["description"]
    assert "News &amp; &lt;Views&gt;" in description
    assert "News & <Views>" not in description
    assert "62 summaries today" in description
    assert "call to gpt-4o &amp; friends." in description
    assert 'href="https://pintxos.example/feeds/7"' in description
    assert "Open the feed's settings" in description
    assert "Did you know? Pintxøs can skip ads" in description
    # Below the 100 threshold: no "far more than anyone reads" escalation sentence.
    assert "far more than anyone reads" not in description


def test_warning_item_level_100_adds_escalation_sentence():
    from pintxos.feed_out import warning_item

    feed = {"id": 3, "title": None, "url": "https://example.com/nofeed"}
    item = warning_item(
        feed,
        level=100,
        summaries_today=250,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/3",
        model="gpt-4o",
    )

    assert "far more than anyone reads in a day" in item["description"]
    assert "paid for and never opened" in item["description"]
    # Falls back to the feed URL when it has no title.
    assert "https://example.com/nofeed produced 250 summaries" in item["description"]


def test_render_rss_with_warning_prepends_warning_item():
    from pintxos.feed_out import warning_item

    feed_id = _seed()
    feed = {"id": feed_id, "title": "Example Feed", "url": FEED_URL}
    warning = warning_item(
        feed,
        level=50,
        summaries_today=62,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/1",
        model="gpt-4o",
    )

    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()

    from pintxos.feed_out import render_rss

    body = render_rss(db_feed, items, full_text=True, warning=warning)
    parsed = feedparser.parse(body)

    assert parsed.bozo == 0
    assert len(parsed.entries) == 3
    first = parsed.entries[0]
    assert first.title == warning["title"]
    assert first.link == warning["link"]
    assert first.guid == warning["guid"]
    titles = {e.title for e in parsed.entries[1:]}
    assert titles == {"Headline One", "Headline Two"}

    # Same shape with full_text disabled.
    body_no_full_text = render_rss(db_feed, items, full_text=False, warning=warning)
    parsed_no_full_text = feedparser.parse(body_no_full_text)
    assert len(parsed_no_full_text.entries) == 3
    assert parsed_no_full_text.entries[0].title == warning["title"]


def test_render_rss_with_warning_still_skips_muted_items():
    feed_id = _seed()
    with db() as conn:
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, word_count, created_at, topic, muted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-3",
                "https://example.com/3",
                "Original Three",
                "2026-09-03T12:00:00+00:00",
                None,
                None,
                0,
                None,
                now(),
                "sport",
                1,
            ),
        )
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()

    from pintxos.feed_out import render_rss, warning_item

    warning = warning_item(
        db_feed,
        level=50,
        summaries_today=62,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/1",
        model="gpt-4o",
    )
    body = render_rss(db_feed, items, full_text=True, warning=warning)
    parsed = feedparser.parse(body)

    assert "guid-3" not in body.decode("utf-8")
    assert "Original Three" not in body.decode("utf-8")
    titles = {e.title for e in parsed.entries}
    assert titles == {warning["title"], "Headline One", "Headline Two"}


def test_render_rss_without_warning_is_unchanged():
    from pintxos.feed_out import render_rss

    feed_id = _seed()
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()

    body = render_rss(db_feed, items, full_text=True)
    parsed = feedparser.parse(body)
    assert len(parsed.entries) == 2
    titles = {e.title for e in parsed.entries}
    assert titles == {"Headline One", "Headline Two"}


def _seed_with_model(model):
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, word_count, auth, text, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-model",
                "https://example.com/model",
                "Original Model",
                "2026-09-05T12:00:00+00:00",
                "Headline Model",
                "Summary model.",
                0,
                1200,
                "used",
                "Body text.",
                model,
                now(),
            ),
        )
    return feed_id


def _render_with_model(model, full_text=False):
    from pintxos.feed_out import render_rss

    feed_id = _seed_with_model(model)
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()
    body = render_rss(db_feed, items, full_text=full_text)
    import xml.sax.saxutils

    return xml.sax.saxutils.unescape(body.decode("utf-8"))


def test_render_rss_appends_model_after_summary_in_small_gray():
    raw = _render_with_model("google/gemini-2.5-flash-lite")
    assert (
        '<p>Summary model. <small style="color:#888">'
        "(google/gemini-2.5-flash-lite)</small></p>" in raw
    )


def test_render_rss_omits_small_tag_when_model_is_null():
    raw = _render_with_model(None)
    assert "<p>Summary model.</p>" in raw
    assert "<small" not in raw


def test_render_rss_escapes_model_name():
    raw = _render_with_model("a<b")
    assert '<small style="color:#888">(a&lt;b)</small>' in raw


def test_render_rss_description_order_is_summary_notes_original_stats_full_text():
    raw = _render_with_model("google/gemini-2.5-flash-lite", full_text=True)
    assert raw.index("Summary model.") < raw.index("Read with your subscription.")
    assert raw.index("Read with your subscription.") < raw.index("Original:")
    assert raw.index("Original:") < raw.index("About ")
    assert raw.index("About ") < raw.index("=== FULL TEXT BELOW ===")


def test_feed_xml_omits_muted_items():
    """A muted item is stored but never published: its topic is muted for this feed."""
    feed_id = _seed()
    with db() as conn:
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, word_count, created_at, topic, muted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-3",
                "https://example.com/3",
                "Original Three",
                "2026-09-03T12:00:00+00:00",
                None,
                None,
                0,
                None,
                now(),
                "sport",
                1,
            ),
        )

    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    assert resp.status_code == 200
    assert "guid-3" not in resp.text
    assert "Original Three" not in resp.text
    parsed = feedparser.parse(resp.content)
    assert {e.title for e in parsed.entries} == {"Headline One", "Headline Two"}
