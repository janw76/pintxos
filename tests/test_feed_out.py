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


def _seed_topic_cases():
    """A fetched+classified item, a fallback+classified item, and an unclassified fetched item."""
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        rows = [
            # (guid, headline, fallback, word_count, topic)
            ("topic-fetched", "Headline Topic Fetched", 0, 1200, "lifestyle"),
            ("topic-fallback", "Headline Topic Fallback", 1, None, "health"),
            ("topic-unclassified", "Headline Topic Unclassified", 0, 900, None),
        ]
        for guid, headline, fallback, words, topic in rows:
            conn.execute(
                """INSERT INTO items
                (feed_id, guid, link, original_title, published_at, headline, summary,
                 fallback, word_count, created_at, topic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    feed_id,
                    f"guid-{guid}",
                    f"https://example.com/{guid}",
                    f"Original {headline}",
                    "2026-09-06T12:00:00+00:00",
                    headline,
                    f"Summary {guid}.",
                    fallback,
                    words,
                    now(),
                    topic,
                ),
            )
    return feed_id


def test_feed_xml_stats_line_includes_topic_for_fetched_classified_item():
    feed_id = _seed_topic_cases()
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Topic Fetched")
    assert "About 1,200 words · 6 min read · lifestyle and leisure" in entry.description


def test_feed_xml_stats_line_is_topic_only_for_fallback_classified_item():
    feed_id = _seed_topic_cases()
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Topic Fallback")
    assert "<em>health</em>" in entry.description
    assert "min read" not in entry.description


def test_feed_xml_stats_line_has_no_topic_for_unclassified_fetched_item():
    feed_id = _seed_topic_cases()
    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Topic Unclassified")
    assert "min read" in entry.description
    assert "min read ·" not in entry.description


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
    # The feed route always wires a base_url, so the copy-link paragraph (added by
    # render_rss's base_url support) is now the last paragraph, right after "Original:".
    assert (
        "Original: Original Text</p><p><a href="
        in entry.description.replace("&#34;", '"')
    )
    assert "Copy or share this article</a></p>" in entry.description.rstrip()
    assert entry.description.rstrip().endswith("</p>")


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
    assert warning_level(99) is None
    assert warning_level(100) == 100
    assert warning_level(179) == 100
    assert warning_level(180) == 180
    assert warning_level(250) == 180


def test_warning_level_custom_levels():
    from pintxos.feed_out import warning_level

    assert warning_level(30, (20, 40)) == 20
    assert warning_level(40, (20, 40)) == 40
    assert warning_level(19, (20, 40)) is None


def test_warn_levels_honours_settings_table():
    from pintxos.feed_out import warn_levels

    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('PINTXOS_WARN_AT', '10')"
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('PINTXOS_WARN_HARD_AT', '20')"
        )
        assert warn_levels(conn) == (10, 20)


def test_warn_levels_raises_hard_to_warn_when_lower():
    from pintxos.feed_out import warn_levels

    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('PINTXOS_WARN_AT', '10')"
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('PINTXOS_WARN_HARD_AT', '5')"
        )
        assert warn_levels(conn) == (10, 10)


def test_warn_levels_falls_back_to_defaults_on_non_numeric_value():
    from pintxos.feed_out import warn_levels

    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('PINTXOS_WARN_AT', 'not-a-number')"
        )
        assert warn_levels(conn) == (100, 180)


def test_warning_item_fields_and_escaping():
    from pintxos.feed_out import warning_item

    feed = {"id": 7, "title": "News & <Views>", "url": "https://example.com/feed.xml"}
    item = warning_item(
        feed,
        level=100,
        hard_level=180,
        summaries_today=62,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/7",
        model="gpt-4o & friends",
        kept_today=40,
    )

    assert item["guid"] == "pintxos-warning-7-warn-2026-09-11"
    assert item["title"] == "Pintxøs: this feed produced 62 summaries today"
    assert item["link"] == "https://pintxos.example/feeds/7"
    assert item["pub_date"].tzinfo is not None

    description = item["description"]
    assert "News &amp; &lt;Views&gt;" in description
    assert "News & <Views>" not in description
    assert "62 summaries today" in description
    assert "call to gpt-4o &amp; friends; 40 of them became new items." in description
    assert 'href="https://pintxos.example/feeds/7"' in description
    assert "Open the feed's settings" in description
    assert "Did you know? Pintxøs can skip ads" in description
    # Below the hard threshold: no "far more than anyone reads" escalation sentence.
    assert "far more than anyone reads" not in description
    # 40 * 2 = 80 >= 62: not a discard loop, no warning sentence.
    assert "re-summarize loop" not in description


def test_warning_item_hard_level_adds_escalation_sentence():
    from pintxos.feed_out import warning_item

    feed = {"id": 3, "title": None, "url": "https://example.com/nofeed"}
    item = warning_item(
        feed,
        level=180,
        hard_level=180,
        summaries_today=250,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/3",
        model="gpt-4o",
        kept_today=200,
    )

    assert "far more than anyone reads in a day" in item["description"]
    assert "paid for and never opened" in item["description"]
    # Falls back to the feed URL when it has no title.
    assert "https://example.com/nofeed produced 250 summaries" in item["description"]
    # Guid encodes the tier reached, not the raw threshold value.
    assert item["guid"] == "pintxos-warning-3-hard-2026-09-11"


def test_warning_item_loop_sentence_appears_when_mostly_discarded():
    from pintxos.feed_out import warning_item

    feed = {"id": 9, "title": "Discardy", "url": "https://example.com/discardy"}
    item = warning_item(
        feed,
        level=100,
        hard_level=180,
        summaries_today=60,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/9",
        model="gpt-4o",
        kept_today=1,
    )

    description = item["description"]
    assert "1 of them became new items." in description
    assert (
        "Paying for summaries that are then discarded usually means a "
        "re-summarize loop: check this feed's Filtered list and the container log."
        in description
    )


def test_warning_item_loop_sentence_absent_when_mostly_kept():
    from pintxos.feed_out import warning_item

    feed = {"id": 9, "title": "Keepy", "url": "https://example.com/keepy"}
    item = warning_item(
        feed,
        level=100,
        hard_level=180,
        summaries_today=60,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/9",
        model="gpt-4o",
        kept_today=40,
    )

    description = item["description"]
    assert "40 of them became new items." in description
    assert "re-summarize loop" not in description


def test_render_rss_with_warning_prepends_warning_item():
    from pintxos.feed_out import warning_item

    feed_id = _seed()
    feed = {"id": feed_id, "title": "Example Feed", "url": FEED_URL}
    warning = warning_item(
        feed,
        level=50,
        hard_level=180,
        summaries_today=62,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/1",
        model="gpt-4o",
        kept_today=40,
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
        hard_level=180,
        summaries_today=62,
        day="2026-09-11",
        feed_page_url="https://pintxos.example/feeds/1",
        model="gpt-4o",
        kept_today=40,
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


def test_text_lines_dedups_original_title():
    from pintxos.feed_out import text_lines

    feed_id = _seed_with_text("Original One\n\nfirst para\nsecond", title="original one")
    with db() as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? AND guid = 'guid-text'", (feed_id,)
        ).fetchone()

    assert text_lines(item) == ["first para", "second"]


def _seed_full_item():
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, word_count, text, model, topic, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-full",
                "https://example.com/exact",
                "Original Exact",
                "2026-09-07T12:00:00+00:00",
                "Headline Exact",
                "Summary exact.",
                0,
                1200,
                "First line\n\nSecond line",
                "gpt-4o",
                "science",
                now(),
            ),
        )
    with db() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE feed_id = ? AND guid = 'guid-full'", (feed_id,)
        ).fetchone()


def test_item_html_and_plain_exact():
    from pintxos.feed_out import item_html, item_plain

    item = _seed_full_item()

    html_no_full = (
        '<p><b style="font-size:1.15em">Headline Exact</b></p>\n'
        "<p>Summary exact. <small>(gpt-4o)</small></p>\n"
        "<p>Original: Original Exact</p>\n"
        "<p><em>About 1,200 words · 6 min read · science and technology</em></p>\n"
        '<p><a href="https://example.com/exact">https://example.com/exact</a></p>'
    )
    html_full = (
        html_no_full
        + "\n<p>First line</p>\n<p>Second line</p>"
    )
    plain_no_full = (
        "Headline Exact\n\n"
        "Summary exact. (gpt-4o)\n\n"
        "Original: Original Exact\n\n"
        "About 1,200 words · 6 min read · science and technology\n\n"
        "https://example.com/exact"
    )
    plain_full = plain_no_full + "\n\nFirst line\n\nSecond line"

    assert item_html(item, full=False) == html_no_full
    assert item_html(item, full=True) == html_full
    assert item_plain(item, full=False) == plain_no_full
    assert item_plain(item, full=True) == plain_full

    for rendered in (html_no_full, html_full, plain_no_full, plain_full):
        assert "/items/" not in rendered
        assert "/feeds/" not in rendered

    assert item_html(item, full=True).endswith("<p>First line</p>\n<p>Second line</p>")
    assert item_plain(item, full=True).endswith("First line\n\nSecond line")
    assert "First line" not in item_html(item, full=False)
    assert "Second line" not in item_html(item, full=False)
    assert "First line" not in item_plain(item, full=False)
    assert "Second line" not in item_plain(item, full=False)


def test_item_html_escapes():
    from pintxos.feed_out import item_html, item_plain

    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                "guid-escape",
                "https://example.com/escape",
                None,
                "2026-09-08T12:00:00+00:00",
                "<b>&",
                None,
                0,
                now(),
            ),
        )
        item = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? AND guid = 'guid-escape'", (feed_id,)
        ).fetchone()

    expected_html = (
        '<p><b style="font-size:1.15em">&lt;b&gt;&amp;</b></p>\n'
        '<p><a href="https://example.com/escape">https://example.com/escape</a></p>'
    )
    expected_plain = "<b>&\n\nhttps://example.com/escape"
    assert item_html(item, full=False) == expected_html
    assert item_plain(item, full=False) == expected_plain


def test_feed_description_has_item_link_before_full_text(monkeypatch):
    monkeypatch.setenv("PINTXOS_FULL_TEXT", "1")
    feed_id = _seed_with_text(FULL_TEXT_SAMPLE)
    with db() as conn:
        item_id = conn.execute(
            "SELECT id FROM items WHERE feed_id = ? AND guid = 'guid-text'", (feed_id,)
        ).fetchone()["id"]

    import xml.sax.saxutils

    with TestClient(app) as c:
        resp = c.get(f"/feeds/{feed_id}.xml")

    raw = xml.sax.saxutils.unescape(resp.text)
    marker = f'/items/{item_id}">Copy or share this article'
    assert marker in raw
    link_index = raw.index(marker)
    assert raw.index("Original:") < link_index < raw.index("=== FULL TEXT BELOW ===")

    parsed = feedparser.parse(resp.content)
    entry = next(e for e in parsed.entries if e.title == "Headline Text")
    assert entry.link == "https://example.com/text"


def test_render_rss_without_base_url_unchanged():
    from pintxos.feed_out import render_rss

    feed_id = _seed()
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()

    body = render_rss(db_feed, items, full_text=True)
    assert "/items/" not in body.decode("utf-8")


def _seed_item(guid, **overrides):
    """Insert one feed + one item row with sensible defaults, returning (feed_id, item)."""
    defaults = dict(
        original_title="Original Title",
        published_at="2026-09-10T12:00:00+00:00",
        headline="A Headline",
        summary="A summary.",
        fallback=0,
        word_count=None,
        text=None,
        model=None,
        summarize_attempts=0,
        summarize_error=None,
        excerpt=None,
        model_fallback=0,
    )
    defaults.update(overrides)
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (FEED_URL, "Example Feed", now()),
        ).lastrowid
        conn.execute(
            """INSERT INTO items
            (feed_id, guid, link, original_title, published_at, headline, summary,
             fallback, word_count, text, model, summarize_attempts, summarize_error,
             excerpt, model_fallback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                guid,
                f"https://example.com/{guid}",
                defaults["original_title"],
                defaults["published_at"],
                defaults["headline"],
                defaults["summary"],
                defaults["fallback"],
                defaults["word_count"],
                defaults["text"],
                defaults["model"],
                defaults["summarize_attempts"],
                defaults["summarize_error"],
                defaults["excerpt"],
                defaults["model_fallback"],
                now(),
            ),
        )
    with db() as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? AND guid = ?", (feed_id, guid)
        ).fetchone()
    return feed_id, item


def test_feed_xml_excludes_held_item():
    """A held item (no summary, attempts < 3) never appears in the output feed."""
    _seed_item("guid-held", summary=None, summarize_attempts=1)
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")
    parsed = feedparser.parse(resp.content)
    assert len(parsed.entries) == 0
    assert "guid-held" not in resp.text


def test_feed_xml_includes_exhausted_item():
    """An exhausted item (no summary, attempts >= 3) appears, unlike a held one."""
    _seed_item("guid-exhausted", summary=None, summarize_attempts=3)
    with TestClient(app) as c:
        resp = c.get("/feeds/1.xml")
    parsed = feedparser.parse(resp.content)
    assert len(parsed.entries) == 1


def test_render_rss_exhausted_uses_original_title_as_headline():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-exhausted-title",
        summary=None,
        summarize_attempts=3,
        original_title="The Real Title",
        headline="Stale Headline",
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    parsed = feedparser.parse(render_rss(db_feed, items, full_text=True))
    assert parsed.entries[0].title == "The Real Title"


def test_render_rss_exhausted_falls_back_to_headline_when_no_original_title():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-exhausted-nofallback",
        summary=None,
        summarize_attempts=3,
        original_title=None,
        headline="Only Headline",
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    parsed = feedparser.parse(render_rss(db_feed, items, full_text=True))
    assert parsed.entries[0].title == "Only Headline"


def test_render_rss_exhausted_json_error_note():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-json-error",
        summary=None,
        summarize_attempts=3,
        summarize_error="invalid JSON returned by model",
        excerpt="Short excerpt text.",
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    body = render_rss(db_feed, items, full_text=False).decode("utf-8")
    assert "returned an unusable answer three times" in body
    assert "kept failing" not in body


def test_render_rss_exhausted_generic_error_note():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-generic-error",
        summary=None,
        summarize_attempts=3,
        summarize_error="connection timed out",
        excerpt="Short excerpt text.",
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    body = render_rss(db_feed, items, full_text=False).decode("utf-8")
    assert "the AI service kept failing" in body
    assert "unusable answer" not in body


def test_render_rss_exhausted_full_text_off_shows_excerpt_no_model_byline():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-exhausted-excerpt",
        summary=None,
        summarize_attempts=3,
        summarize_error="boom",
        excerpt="This is the excerpt shown instead of a summary.",
        model="gpt-4o",
        text="Full article body.",
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    body = render_rss(db_feed, items, full_text=False).decode("utf-8")
    assert "This is the excerpt shown instead of a summary." in body
    assert "(gpt-4o)" not in body
    assert "=== FULL TEXT BELOW ===" not in body


def test_render_rss_exhausted_full_text_on_shows_full_text_no_excerpt():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-exhausted-fulltext",
        summary=None,
        summarize_attempts=3,
        summarize_error="boom",
        excerpt="Should not appear.",
        text="Full article body line.",
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    body = render_rss(db_feed, items, full_text=True).decode("utf-8")
    assert "=== FULL TEXT BELOW ===" in body
    assert "Full article body line." in body
    assert "Should not appear." not in body


def test_render_rss_fallback_model_shows_bold_note_not_small_byline():
    import xml.sax.saxutils

    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-fallback-model",
        summary="A fallback summary.",
        model="backup-model",
        model_fallback=1,
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    body = xml.sax.saxutils.unescape(render_rss(db_feed, items, full_text=True).decode("utf-8"))
    assert (
        "<p><strong>Note: Pintxøs used backup-model as a fallback for this item."
        "</strong></p>" in body
    )
    assert "<small" not in body


def test_render_rss_ordinary_fallback_zero_keeps_small_byline():
    from pintxos.feed_out import render_rss

    feed_id, _ = _seed_item(
        "guid-ordinary-model",
        summary="An ordinary summary.",
        model="primary-model",
        model_fallback=0,
    )
    with db() as conn:
        db_feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        items = conn.execute("SELECT * FROM items WHERE feed_id = ?", (feed_id,)).fetchall()
    body = render_rss(db_feed, items, full_text=True).decode("utf-8")
    assert "(primary-model)" in body
    assert "Note: Pintxøs used" not in body


def test_item_html_exhausted_head_and_full():
    from pintxos.feed_out import item_html

    _, item = _seed_item(
        "guid-item-html-exhausted",
        summary=None,
        summarize_attempts=3,
        summarize_error="bad JSON",
        excerpt="Excerpt paragraph.",
        model="gpt-4o",
        text="Line one\nLine two",
        original_title="Real Original",
        headline="Old Headline",
    )
    head = item_html(item, full=False)
    full = item_html(item, full=True)

    assert "Real Original" in head
    assert "Old Headline" not in head
    assert "returned an unusable answer three times" in head
    assert "Excerpt paragraph." in head
    assert "(gpt-4o)" not in head
    assert "<p>Line one</p>" not in head
    assert full.startswith(head)
    assert "<p>Line one</p>" in full
    assert "<p>Line two</p>" in full


def test_item_plain_exhausted_head_and_full():
    from pintxos.feed_out import item_plain

    _, item = _seed_item(
        "guid-item-plain-exhausted",
        summary=None,
        summarize_attempts=3,
        summarize_error="timeout",
        excerpt="Plain excerpt.",
        text="Plain line one\nPlain line two",
    )
    head = item_plain(item, full=False)
    full = item_plain(item, full=True)

    assert "the AI service kept failing" in head
    assert "Plain excerpt." in head
    assert full.startswith(head)
    assert "Plain line one" in full
    assert "Plain line two" in full


def test_item_html_fallback_model_bold_note():
    from pintxos.feed_out import item_html

    _, item = _seed_item(
        "guid-item-html-fallback",
        summary="Fallback summary.",
        model="backup-model",
        model_fallback=1,
    )
    rendered = item_html(item, full=False)
    assert (
        "<p><strong>Note: Pintxøs used backup-model as a fallback for this item."
        "</strong></p>" in rendered
    )
    assert "<small" not in rendered


def test_item_plain_fallback_model_note_line():
    from pintxos.feed_out import item_plain

    _, item = _seed_item(
        "guid-item-plain-fallback",
        summary="Fallback summary.",
        model="backup-model",
        model_fallback=1,
    )
    rendered = item_plain(item, full=False)
    assert "Note: Pintxøs used backup-model as a fallback for this item." in rendered
