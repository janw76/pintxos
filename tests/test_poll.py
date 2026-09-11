"""Poller tests: everything network- and LLM-facing is monkeypatched."""

from __future__ import annotations

import http.cookiejar
import json
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import curl_cffi.requests
import feedparser
import pytest
import trafilatura
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from pintxos import feedstats, poll, topics
from pintxos.config import DEFAULTS, db_path
from pintxos.cookies import cookie_path
from pintxos.db import connect, db, now
from pintxos.summarize import MissingApiKey, SummarizeError

from conftest import FUTURE_EXPIRY, write_cookies

FEED_URL = "https://example.com/feed.xml"
SAMPLE = (Path(__file__).parent / "fixtures" / "sample.xml").read_bytes()
SAMPLE_WITH_AD = (Path(__file__).parent / "fixtures" / "sample_with_ad.xml").read_bytes()
WIRED = (Path(__file__).parent / "fixtures" / "wired.xml").read_bytes()


class FakeResponse:
    def __init__(
        self, content: bytes, status_code: int = 200, content_type="application/xml", headers=None
    ):
        self.content = content
        self.status_code = status_code
        self.headers = headers if headers is not None else {"content-type": content_type}

    @property
    def text(self) -> str:
        return self.content.decode()


@pytest.fixture
def feed_id():
    with db() as conn:
        cur = conn.execute("INSERT INTO feeds(url, created_at) VALUES (?, ?)", (FEED_URL, now()))
        return cur.lastrowid


@pytest.fixture
def calls(monkeypatch):
    """Serve the fixture feed, never fetch articles, count summarize() calls."""
    seen: list[tuple[str, str, str]] = []

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(SAMPLE)
        raise AssertionError(f"unexpected GET {url}")

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
        seen.append((text, original_title, url))
        return f"HEADLINE {len(seen)}", f"summary of {original_title}"

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))
    monkeypatch.setattr(poll, "summarize", fake_summarize)
    return seen


@pytest.fixture
def calls_with_ad(monkeypatch):
    """Like `calls`, but serves a feed whose third entry is tagged as a coupon ad."""
    seen: list[tuple[str, str, str]] = []

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(SAMPLE_WITH_AD)
        raise AssertionError(f"unexpected GET {url}")

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
        seen.append((text, original_title, url))
        return f"HEADLINE {len(seen)}", f"summary of {original_title}"

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))
    monkeypatch.setattr(poll, "summarize", fake_summarize)
    return seen


@pytest.fixture
def _reset_retry_cursor(monkeypatch):
    """Isolate retry_fallback's rotation cursor so test order can't leak state."""
    monkeypatch.setattr(poll, "_retry_cursor", {})


def items():
    with db() as conn:
        return conn.execute("SELECT * FROM items ORDER BY published_at DESC").fetchall()


def feed_row(feed_id):
    with db() as conn:
        return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()


def feed_stats_today(feed_id):
    """(summaries, classifications) recorded for `feed_id` today; (0, 0) if no row."""
    with db() as conn:
        row = conn.execute(
            "SELECT summaries, classifications FROM feed_stats WHERE feed_id = ? AND day = ?",
            (feed_id, feedstats.today()),
        ).fetchone()
    return (row["summaries"], row["classifications"]) if row is not None else (0, 0)


def set_feed(feed_id, **columns):
    """Set per-feed columns (filter_ads, ad_title_patterns, ad_patterns_mode) via SQL."""
    with db() as conn:
        for name, value in columns.items():
            conn.execute(f"UPDATE feeds SET {name} = ? WHERE id = ?", (value, feed_id))


def test_first_poll_inserts_items(feed_id, calls):
    poll.poll_all()
    rows = items()
    assert len(rows) == 3
    assert len(calls) == 3
    assert rows[0]["headline"] == "HEADLINE 1"
    assert rows[0]["link"] == "https://example.com/one"
    assert rows[0]["published_at"].startswith("2025-09-01T10:00:00")
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    assert feed["title"] == "Sample Feed"
    assert feed["last_polled_at"]
    assert feed["last_error"] is None


def test_second_poll_is_a_noop(feed_id, calls):
    poll.poll_all()
    calls.clear()
    poll.poll_all()
    assert calls == []
    assert len(items()) == 3


def test_fallback_uses_feed_content(feed_id, calls):
    poll.poll_all()
    assert all(row["fallback"] == 1 for row in items())
    texts = {title: text for text, title, _url in calls}
    assert "ENCODED BODY" in texts["First article about a rocket launch"]
    assert "SUMMARY BODY" in texts["Second article about a merger"]
    # Third entry's body is too thin, so the original title is used as the text.
    assert texts["Third article with almost no body text at all"].startswith("Third article")


def test_article_text_wins_over_feed_content(feed_id, calls, monkeypatch):
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    poll.poll_all()
    assert all(row["fallback"] == 0 for row in items())
    assert all(text.startswith("FULL ARTICLE TEXT") for text, _t, _u in calls)


def test_fetched_article_stores_word_count(feed_id, calls, monkeypatch):
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("one two three " * 50, "ok", []))
    poll.poll_feed(feed_id)
    with db() as conn:
        rows = conn.execute("SELECT word_count FROM items").fetchall()
    assert rows
    assert all(row["word_count"] == 150 for row in rows)


def test_fallback_item_has_null_word_count(feed_id, calls):
    poll.poll_feed(feed_id)
    with db() as conn:
        rows = conn.execute("SELECT word_count, fallback FROM items").fetchall()
    assert rows
    assert all(row["word_count"] is None for row in rows)
    assert all(row["fallback"] == 1 for row in rows)


def test_fetched_article_text_stored_with_newlines(feed_id, calls, monkeypatch):
    article_text = "Paragraph one.\n\nParagraph two.\n\n" + "padding " * 30
    monkeypatch.setattr(poll, "fetch_article", lambda link: (article_text, "ok", []))
    poll.poll_feed(feed_id)
    rows = items()
    assert rows
    texts_by_title = {title: text for text, title, _url in calls}
    for row in rows:
        assert row["text"] == texts_by_title[row["original_title"]]
        assert "\n" in row["text"]


def test_fallback_excerpt_at_least_min_chars_is_stored(feed_id, calls):
    poll.poll_all()
    rows = {row["original_title"]: row for row in items()}
    texts_by_title = {title: text for text, title, _url in calls}
    row = rows["First article about a rocket launch"]
    assert row["text"] == texts_by_title["First article about a rocket launch"]
    assert "ENCODED BODY" in row["text"]


def test_fallback_excerpt_below_min_chars_stores_null_text(feed_id, calls):
    poll.poll_all()
    rows = {row["original_title"]: row for row in items()}
    row = rows["Third article with almost no body text at all"]
    assert row["text"] is None


def test_prune_keeps_newest_n(feed_id, calls, monkeypatch):
    with db() as conn:
        for n in range(5):
            conn.execute(
                "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
                "headline, summary, fallback, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (feed_id, f"old-{n}", "https://example.com/old", "old", "2020-01-0%d" % (n + 1),
                 "old headline", "old summary", 1, now()),
            )
    monkeypatch.setenv("PINTXOS_ITEMS_PER_FEED", "2")
    poll.poll_all()
    rows = items()
    assert len(rows) == 2
    assert [row["link"] for row in rows] == ["https://example.com/one", "https://example.com/two"]
    assert len(calls) == 2  # only the newest 2 entries were considered


def test_missing_api_key_aborts_without_inserting(feed_id, calls, monkeypatch):
    def boom(*_args, **_kwargs):
        raise MissingApiKey("ANTHROPIC_API_KEY not set")

    monkeypatch.setattr(poll, "summarize", boom)
    poll.poll_all()
    assert items() == []
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    assert feed["last_error"] == "ANTHROPIC_API_KEY not set"


def test_missing_openrouter_api_key_stores_that_message(feed_id, calls, monkeypatch):
    def boom(*_args, **_kwargs):
        raise MissingApiKey("OPENROUTER_API_KEY not set")

    monkeypatch.setattr(poll, "summarize", boom)
    poll.poll_all()
    assert items() == []
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    assert feed["last_error"] == "OPENROUTER_API_KEY not set"


def test_poll_feed_passes_feed_model_to_summarize_and_classify(feed_id, monkeypatch):
    set_feed(feed_id, model="x/y", classify_topics=1)
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(SAMPLE))
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))

    summarize_models = []
    classify_models = []

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
        summarize_models.append(model)
        return "HEADLINE", "summary"

    def fake_classify(title, labels, lead, model=None):
        classify_models.append(model)
        return "science"

    monkeypatch.setattr(poll, "summarize", fake_summarize)
    monkeypatch.setattr(topics, "classify_topic", fake_classify)

    poll.poll_feed(feed_id)

    assert summarize_models and all(m == "x/y" for m in summarize_models)
    assert classify_models and all(m == "x/y" for m in classify_models)
    rows = items()
    assert rows and all(row["model"] == "x/y" for row in rows)


def test_poll_feed_with_null_model_carries_global_model(feed_id, monkeypatch):
    """A feed with no per-feed model override reaches the LLM with the global
    PINTXOS_MODEL setting, exercised through the real summarize()/classify_topic()
    resolution (only llm.complete is mocked)."""
    from pintxos import llm

    monkeypatch.setenv("PINTXOS_MODEL", "global/model")
    set_feed(feed_id, classify_topics=1)
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(SAMPLE))
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))

    used_models = []

    def fake_complete(system, user, max_tokens, model, json=False):
        used_models.append(model)
        if json:
            return '{"headline": "H", "summary": "S"}'
        return "science"

    monkeypatch.setattr(llm, "complete", fake_complete)

    poll.poll_feed(feed_id)

    assert used_models and all(m == "global/model" for m in used_models)
    rows = items()
    assert rows and all(row["model"] == "global/model" for row in rows)


def test_connect_migrates_existing_db_missing_items_model_column(tmp_path, monkeypatch):
    """A DB from before the items.model column gains it on connect(), and only once."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    old_conn = sqlite3.connect(db_path())
    old_conn.executescript(
        """
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            created_at TEXT,
            last_polled_at TEXT,
            last_error TEXT
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            feed_id INTEGER REFERENCES feeds(id) ON DELETE CASCADE,
            guid TEXT NOT NULL,
            link TEXT NOT NULL,
            original_title TEXT,
            published_at TEXT,
            headline TEXT,
            summary TEXT,
            fallback INTEGER DEFAULT 0,
            created_at TEXT,
            UNIQUE(feed_id, guid)
        );
        """
    )
    old_conn.commit()
    old_conn.close()

    conn = connect()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        assert cols.count("model") == 1
        assert "model" in cols
    finally:
        conn.close()

    # Second connect() must be a no-op migration, not an error, and the column stays singular.
    conn2 = connect()
    try:
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(items)")]
        assert cols2.count("model") == 1

        cur = conn2.execute(
            "INSERT INTO feeds(url, created_at) VALUES (?, ?)", (FEED_URL, now())
        )
        feed_id = cur.lastrowid
        cur2 = conn2.execute(
            "INSERT INTO items(feed_id, guid, link, original_title, created_at) "
            "VALUES (?,?,?,?,?)",
            (feed_id, "guid-1", "https://example.com/one", "A title", now()),
        )
        row = conn2.execute(
            "SELECT * FROM items WHERE id = ?", (cur2.lastrowid,)
        ).fetchone()
        assert row["model"] is None  # pre-existing rows stay NULL
    finally:
        conn2.close()


def test_summarize_error_skips_only_that_item(feed_id, calls, monkeypatch):
    def flaky(text, original_title, url, respect_language=None, model=None):
        if url == "https://example.com/two":
            raise SummarizeError("API said no")
        return "HEADLINE", "summary"

    monkeypatch.setattr(poll, "summarize", flaky)
    poll.poll_all()
    links = [row["link"] for row in items()]
    assert links == ["https://example.com/one", "https://example.com/three"]


def test_feed_http_error_sets_last_error(feed_id, monkeypatch):
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(b"nope", status_code=503))
    poll.poll_all()
    assert items() == []
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    assert "503" in feed["last_error"]


def test_poll_one_double_click_runs_once(monkeypatch):
    """Two clicks before the job runs collapse into a single execution."""
    release = threading.Event()
    runs = []

    def blocking_poll_feed(fid):
        runs.append(fid)
        release.wait(5)
        return True

    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})
    monkeypatch.setattr(poll, "scheduler", scheduler)
    monkeypatch.setattr(poll, "poll_feed", blocking_poll_feed)
    # Paused so both clicks land before the worker can pick the job up - that is the
    # race the job id is meant to collapse, and pausing makes the test deterministic.
    scheduler.start(paused=True)
    try:
        poll.poll_one(1)
        poll.poll_one(1)  # same job id replaces the pending one
        assert [job.id for job in scheduler.get_jobs()] == ["feed-1"]
        assert poll._status[1] == "Queued"
        scheduler.resume()
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (scheduler.get_jobs() or not runs):
            time.sleep(0.02)
        scheduler.shutdown(wait=True)  # waits for the running job to finish
        assert runs == [1]
    finally:
        release.set()
        if scheduler.running:
            scheduler.shutdown(wait=True)
        poll._status.pop(1, None)  # poll_feed was faked, so nobody cleared it


def test_production_scheduler_is_single_worker():
    """The module-level scheduler is what actually serializes polls in prod.

    Fails loudly if someone bumps ThreadPoolExecutor(1) to (4) and quietly
    reintroduces concurrent polling.
    """
    assert poll.scheduler._executors["default"]._pool._max_workers == 1


def test_manual_poll_waits_for_running_poll_all(monkeypatch):
    """A manual poll_one queued while poll_all is running must not overlap it.

    poll.scheduler has a single-worker executor, so jobs are serialized by
    construction; this asserts the manual poll actually runs *after* the
    scheduled poll_all finishes, not concurrently with it.
    """
    release = threading.Event()
    order = []

    def blocking_poll_all():
        order.append("poll_all-start")
        release.wait(5)
        order.append("poll_all-end")

    def fake_poll_feed(fid):
        order.append(f"feed-{fid}-start")
        order.append(f"feed-{fid}-end")
        return True

    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})
    monkeypatch.setattr(poll, "scheduler", scheduler)
    monkeypatch.setattr(poll, "poll_feed", fake_poll_feed)
    scheduler.start(paused=True)
    try:
        scheduler.add_job(blocking_poll_all, id="poll_all", misfire_grace_time=None)
        scheduler.resume()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "poll_all-start" not in order:
            time.sleep(0.01)
        assert "poll_all-start" in order  # poll_all is now occupying the one worker thread

        poll.poll_one(1)  # queued behind poll_all on the single-thread executor
        assert poll._status[1] == "Queued"

        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "feed-1-end" not in order:
            time.sleep(0.01)
        scheduler.shutdown(wait=True)
        assert order == ["poll_all-start", "poll_all-end", "feed-1-start", "feed-1-end"]
    finally:
        release.set()
        if scheduler.running:
            scheduler.shutdown(wait=True)
        poll._status.pop(1, None)  # poll_feed was faked, so nobody cleared it


def test_poll_feed_deleted_row_clears_status_without_error(feed_id):
    """Deleting a feed while it's queued/polling must not crash poll_feed."""
    poll._status[feed_id] = "Fetching feed…"
    with db() as conn:
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    assert poll.poll_feed(feed_id) is True
    assert feed_id not in poll._status


def test_status_cleared_after_poll(feed_id, calls, monkeypatch):
    seen_status = []
    real_summarize = poll.summarize

    def spy(text, original_title, url, respect_language=None, model=None):
        seen_status.append(poll._status.get(feed_id))
        return real_summarize(text, original_title, url, respect_language=respect_language)

    monkeypatch.setattr(poll, "summarize", spy)
    assert poll.poll_feed(feed_id) is True
    assert feed_id not in poll._status
    assert seen_status and all(s.startswith("Summarizing") for s in seen_status)
    assert seen_status[0] == "Summarizing 1/3"


def test_fetch_article_extracts_html(monkeypatch):
    html = "<html><body><article><p>" + "Real body sentence. " * 30 + "</p></article></body></html>"
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(html.encode(), content_type="text/html; charset=utf-8"))
    text, status, _labels = poll.fetch_article("https://example.com/one")
    assert "Real body sentence." in text
    assert status == "ok"


def test_fetch_article_rejects_non_html(monkeypatch):
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(b"{}", content_type="application/json"))
    assert poll.fetch_article("https://example.com/one") == (None, "error", [])


# --- fetch_status classification --------------------------------------------------

ARTICLE_HTML = (
    "<html><body><article><p>" + "Real body sentence. " * 30 + "</p></article></body></html>"
)
# Extracts to well under MIN_ARTICLE_CHARS: a teaser, not an article.
TEASER_HTML = "<html><body><article><p>" + "Short teaser. " * 4 + "</p></article></body></html>"


@pytest.mark.parametrize(
    "status_code, content_type, body, expected_status",
    [
        (200, "text/html; charset=utf-8", ARTICLE_HTML, "ok"),
        (200, "text/html; charset=utf-8", TEASER_HTML, "teaser"),
        (401, "text/html", ARTICLE_HTML, "blocked"),
        (403, "text/html", ARTICLE_HTML, "blocked"),
        (429, "text/html", ARTICLE_HTML, "blocked"),
        (500, "text/html", ARTICLE_HTML, "error"),
        (200, "application/json", "{}", "error"),
    ],
)
def test_fetch_article_classifies_outcome(
    monkeypatch, status_code, content_type, body, expected_status
):
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(body.encode(), status_code=status_code, content_type=content_type),
    )
    text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == expected_status
    if expected_status == "ok":
        assert "Real body sentence." in text
    else:
        assert text is None


# A JSON-LD block declaring the page freely accessible, wrapped around the same
# too-short body as TEASER_HTML.
SHORT_FREE_HTML = (
    "<html><head><script type=\"application/ld+json\">"
    '{"@context":"https://schema.org","@type":"NewsArticle","isAccessibleForFree":true}'
    "</script></head><body><article><p>" + "Short teaser. " * 4 + "</p></article></body></html>"
)


def test_fetch_article_returns_short_for_free_marked_page(monkeypatch):
    """A 2xx page under MIN_ARTICLE_CHARS that declares itself free (schema.org
    isAccessibleForFree) is "short", not "teaser": the extracted text is kept
    rather than discarded."""
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(SHORT_FREE_HTML.encode(), content_type="text/html; charset=utf-8"),
    )
    text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == "short"
    assert "Short teaser." in text


def test_fetch_article_stays_teaser_without_free_or_media_markers(monkeypatch):
    """A short page with no free/media markup at all keeps today's "teaser"
    behaviour -- the markerless case must not be swept into "short"."""
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(TEASER_HTML.encode(), content_type="text/html; charset=utf-8"),
    )
    text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == "teaser"
    assert text is None


# GitHub issue #9: on the teaser path (and only there) fetch_article() logs a
# compact fingerprint line -- names and counts only, never page content -- so
# a future positive paywall detector has data to train on. The sentinel below
# stands in for real page content that must never end up in the logs.
TEASER_FINGERPRINT_HTML = (
    "<html><body><article><p>"
    + "Short teaser. " * 4
    + " SENTINEL-DO-NOT-LOG</p></article></body></html>"
)
PIANO_TEASER_FINGERPRINT_HTML = (
    '<html><body><div class="tp-modal">Subscribe</div><article><p>'
    + "Short teaser. " * 4
    + " SENTINEL-DO-NOT-LOG</p></article></body></html>"
)


def test_fetch_article_logs_teaser_fingerprint(monkeypatch, caplog):
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(
            TEASER_FINGERPRINT_HTML.encode(), content_type="text/html; charset=utf-8"
        ),
    )
    expected_chars = len(
        trafilatura.extract(
            TEASER_FINGERPRINT_HTML, include_comments=False, include_tables=False
        )
        or ""
    )
    with caplog.at_level("INFO"):
        _text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == "teaser"
    fingerprint_lines = [
        r.getMessage() for r in caplog.records if "teaser fingerprint" in r.getMessage()
    ]
    assert len(fingerprint_lines) == 1
    line = fingerprint_lines[0]
    assert "status=200" in line
    assert f"chars={expected_chars}" in line
    assert "free=None" in line
    assert "og_type=-" in line
    assert "jsonld=-" in line
    assert "paywall=-" in line
    assert "SENTINEL-DO-NOT-LOG" not in caplog.text


def test_fetch_article_teaser_fingerprint_reports_piano_marker(monkeypatch, caplog):
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(
            PIANO_TEASER_FINGERPRINT_HTML.encode(), content_type="text/html; charset=utf-8"
        ),
    )
    with caplog.at_level("INFO"):
        _text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == "teaser"
    assert "paywall=piano" in caplog.text
    assert "SENTINEL-DO-NOT-LOG" not in caplog.text


def test_fetch_article_short_page_does_not_log_teaser_fingerprint(monkeypatch, caplog):
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(
            SHORT_FREE_HTML.encode(), content_type="text/html; charset=utf-8"
        ),
    )
    with caplog.at_level("INFO"):
        _text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == "short"
    assert "teaser fingerprint" not in caplog.text


def test_fetch_article_ok_page_does_not_log_teaser_fingerprint(monkeypatch, caplog):
    monkeypatch.setattr(
        poll,
        "_get",
        lambda url: FakeResponse(ARTICLE_HTML.encode(), content_type="text/html; charset=utf-8"),
    )
    with caplog.at_level("INFO"):
        _text, status, _labels = poll.fetch_article("https://example.com/one")
    assert status == "ok"
    assert "teaser fingerprint" not in caplog.text


def test_fetch_article_request_failure_is_an_error(monkeypatch):
    def boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(poll, "_get", boom)
    assert poll.fetch_article("https://example.com/one") == (None, "error", [])


def test_poll_feed_stores_fetch_status_ok_for_fetched_items(feed_id, calls, monkeypatch):
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    poll.poll_feed(feed_id)
    rows = items()
    assert rows
    assert all(row["fetch_status"] == "ok" for row in rows)
    assert all(row["fallback"] == 0 for row in rows)


def test_poll_feed_stores_fetch_status_for_fallback_items(feed_id, calls, monkeypatch):
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "teaser", []))
    poll.poll_feed(feed_id)
    rows = items()
    assert rows
    assert all(row["fetch_status"] == "teaser" for row in rows)
    assert all(row["fallback"] == 1 for row in rows)


def _seed_fallback_item(feed_id, guid="guid-1", link="https://example.com/one") -> int:
    with db() as conn:
        return conn.execute(
            "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
            "headline, summary, fallback, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (feed_id, guid, link, "A title", now(), "old headline", "old summary", 1, now()),
        ).lastrowid


def test_retry_fallback_sets_fetch_status_ok_on_success(feed_id, monkeypatch):
    item_id = _seed_fallback_item(feed_id)
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("New", "New summary"))

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]  # updated in place, never deleted
    assert rows[0]["fetch_status"] == "ok"
    assert rows[0]["fallback"] == 0
    assert rows[0]["headline"] == "New"


def test_retry_fallback_records_fetch_status_on_repeat_failure(feed_id, monkeypatch):
    item_id = _seed_fallback_item(feed_id)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "blocked", []))

    def boom_summarize(*_args, **_kwargs):
        raise AssertionError("summarize should not be called when the fetch fails")

    monkeypatch.setattr(poll, "summarize", boom_summarize)

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]  # still there, still a fallback
    assert rows[0]["fetch_status"] == "blocked"
    assert rows[0]["fallback"] == 1
    assert rows[0]["headline"] == "old headline"


def test_retry_fallback_updates_fetch_status_when_summarize_fails(feed_id, monkeypatch):
    """A fetch that succeeds but fails to summarize must not keep a stale fetch_status."""
    item_id = _seed_fallback_item(feed_id)
    second_id = _seed_fallback_item(feed_id, guid="guid-2", link="https://example.com/two")
    with db() as conn:
        conn.execute(
            "UPDATE items SET fetch_status = ?, auth = ? WHERE id IN (?, ?)",
            ("blocked", "missing", item_id, second_id),
        )
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))

    def boom_summarize(*_args, **_kwargs):
        raise SummarizeError("boom")

    monkeypatch.setattr(poll, "summarize", boom_summarize)

    poll.retry_fallback(feed_id)

    rows = {row["id"]: row for row in items()}
    assert set(rows) == {item_id, second_id}  # both still there, never deleted
    for row in rows.values():
        assert row["fetch_status"] == "ok"  # refreshed, not left at the stale "blocked"
        assert row["fallback"] == 1  # left for a later retry
        assert row["headline"] == "old headline"  # untouched: summarize never returned
        assert row["summary"] == "old summary"
    # a SummarizeError on the first item must not abort the loop before the second runs


def test_retry_fallback_success_writes_text(feed_id, monkeypatch):
    item_id = _seed_fallback_item(feed_id)
    fetched_text = "FULL ARTICLE TEXT " * 20
    monkeypatch.setattr(poll, "fetch_article", lambda link: (fetched_text, "ok", []))
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("New", "New summary"))

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]
    assert rows[0]["text"] == fetched_text
    assert rows[0]["model"] == DEFAULTS["PINTXOS_MODEL"]  # feed has no per-feed override


def test_retry_fallback_failure_leaves_text_unchanged(feed_id, monkeypatch):
    item_id = _seed_fallback_item(feed_id)
    with db() as conn:
        conn.execute("UPDATE items SET text = ? WHERE id = ?", ("original text", item_id))

    def boom_summarize(*_args, **_kwargs):
        raise AssertionError("summarize should not be called when the fetch fails")

    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "blocked", []))
    monkeypatch.setattr(poll, "summarize", boom_summarize)

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]
    assert rows[0]["text"] == "original text"


def test_retry_fallback_merges_page_labels_with_existing(feed_id, monkeypatch):
    """A successful retry-fetch folds its fresh page labels into whatever the item
    already had: existing entries first, deduped, in order."""
    item_id = _seed_fallback_item(feed_id)
    with db() as conn:
        conn.execute(
            "UPDATE items SET labels = ? WHERE id = ?",
            (json.dumps(["World News", "Sport"]), item_id),
        )
    monkeypatch.setattr(
        poll, "fetch_article",
        lambda link: ("FULL ARTICLE TEXT " * 20, "ok", ["Sport", "Cricket"]),
    )
    monkeypatch.setattr(
        poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("New", "New summary")
    )

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]
    assert json.loads(rows[0]["labels"]) == ["World News", "Sport", "Cricket"]


def test_retry_fallback_keeps_existing_labels_when_refetch_yields_none(feed_id, monkeypatch):
    """When the re-fetched page has no publisher-label metadata, the item's existing
    labels are kept unchanged."""
    item_id = _seed_fallback_item(feed_id)
    with db() as conn:
        conn.execute(
            "UPDATE items SET labels = ? WHERE id = ?",
            (json.dumps(["World News", "Sport"]), item_id),
        )
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(
        poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("New", "New summary")
    )

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]
    assert json.loads(rows[0]["labels"]) == ["World News", "Sport"]


def test_retry_fallback_repairs_into_short_summarizing_title_when_text_empty(feed_id, monkeypatch):
    """A re-fetch that comes back "short" with no extracted text at all summarizes
    the original title (not an empty string) and stores NULL text, but otherwise
    repairs the item exactly like a normal success: fetch_status "short",
    fallback 0."""
    item_id = _seed_fallback_item(feed_id)
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("", "short", []))
    seen_text = []

    def fake_summarize(text, title, url, respect_language=None, model=None):
        seen_text.append(text)
        return "New", "New summary"

    monkeypatch.setattr(poll, "summarize", fake_summarize)

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]
    assert rows[0]["fetch_status"] == "short"
    assert rows[0]["fallback"] == 0
    assert rows[0]["text"] is None
    assert rows[0]["headline"] == "New"
    assert seen_text == ["A title"]  # original_title, not the empty fetched text


def test_retry_fallback_repairs_into_short_summarizing_fetched_text_when_nonempty(
    feed_id, monkeypatch
):
    """A re-fetch that comes back "short" with real extracted text summarizes and
    stores that text as is, same as an "ok" repair."""
    item_id = _seed_fallback_item(feed_id)
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("A cartoon caption.", "short", []))
    monkeypatch.setattr(
        poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("New", "New summary")
    )

    poll.retry_fallback(feed_id)

    rows = items()
    assert [row["id"] for row in rows] == [item_id]
    assert rows[0]["fetch_status"] == "short"
    assert rows[0]["fallback"] == 0
    assert rows[0]["text"] == "A cartoon caption."
    assert rows[0]["headline"] == "New"


def _seed_blocked_items(feed_id, n, start_guid=1, host="example.com"):
    """Insert `n` blocked fallback rows, oldest guid first; returns their ids in insert order."""
    ids = []
    for i in range(start_guid, start_guid + n):
        item_id = _seed_fallback_item(
            feed_id, guid=f"blocked-{host}-{i}", link=f"https://{host}/b{i}"
        )
        with db() as conn:
            conn.execute("UPDATE items SET fetch_status = 'blocked' WHERE id = ?", (item_id,))
        ids.append(item_id)
    return ids


def _mark_sample_guids_seen(feed_id):
    """Insert rows for every SAMPLE guid so a poll of that fixture finds no new entries."""
    with db() as conn:
        for guid in ["https://example.com/one", "https://example.com/two", "https://example.com/three"]:
            conn.execute(
                "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
                "headline, summary, fallback, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (feed_id, guid, guid, guid, now(), "h", "s", 0, now()),
            )


def test_poll_feed_retries_three_newest_blocked_items(
    feed_id, calls, monkeypatch, _reset_retry_cursor
):
    """On a normal poll, only the three newest blocked-or-NULL fallback rows are
    retried; the rest, and a teaser row, are left untouched. A NULL fetch_status
    (an item written before that column existed) counts as retriable, same as
    'blocked'."""
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    blocked_ids = _seed_blocked_items(feed_id, 5)
    # The newest blocked row instead has no fetch_status at all (pre-migration row).
    null_status_id = blocked_ids[-1]
    with db() as conn:
        conn.execute("UPDATE items SET fetch_status = NULL WHERE id = ?", (null_status_id,))
    teaser_id = _seed_fallback_item(feed_id, guid="teaser-1", link="https://example.com/t1")
    with db() as conn:
        conn.execute("UPDATE items SET fetch_status = 'teaser' WHERE id = ?", (teaser_id,))
    _mark_sample_guids_seen(feed_id)

    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, **kwargs: ("H", "S"))

    assert poll.poll_feed(feed_id) is True

    rows = {row["id"]: row for row in items()}
    retried_ids = set(blocked_ids[-3:])  # three newest (highest id) rows, incl. the NULL one
    assert null_status_id in retried_ids
    for item_id in blocked_ids:
        row = rows[item_id]
        if item_id in retried_ids:
            assert row["fallback"] == 0
            assert row["fetch_status"] == "ok"
        else:
            assert row["fallback"] == 1
            assert row["fetch_status"] == "blocked"
    assert rows[teaser_id]["fallback"] == 1
    assert rows[teaser_id]["fetch_status"] == "teaser"


def test_poll_feed_does_not_retry_blocked_items_without_cookies(feed_id, calls, monkeypatch):
    """No cookies.txt means get_jar() is None: poll_feed must skip the blocked retry."""
    blocked_ids = _seed_blocked_items(feed_id, 5)
    _mark_sample_guids_seen(feed_id)

    def boom_fetch_article(link):
        raise AssertionError("fetch_article should not be called: no cookies means no retry")

    monkeypatch.setattr(poll, "fetch_article", boom_fetch_article)

    assert poll.poll_feed(feed_id) is True

    rows = {row["id"]: row for row in items()}
    for item_id in blocked_ids:
        assert rows[item_id]["fallback"] == 1
        assert rows[item_id]["fetch_status"] == "blocked"


def test_poll_feed_only_retries_blocked_items_on_hosts_with_cookies(
    feed_id, calls, monkeypatch, _reset_retry_cursor
):
    """Only blocked items whose host has cookies in the jar are retried; blocked
    items on a host with no cookies are left untouched even though the jar is not
    empty (it just holds cookies for a different host)."""
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    example_ids = _seed_blocked_items(feed_id, 2, host="example.com")
    other_ids = _seed_blocked_items(feed_id, 2, host="other.com")
    _mark_sample_guids_seen(feed_id)

    def fetch_article(link):
        assert "other.com" not in link, "other.com has no cookies and must not be fetched"
        return "FULL ARTICLE TEXT " * 20, "ok", []

    monkeypatch.setattr(poll, "fetch_article", fetch_article)
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, **kwargs: ("H", "S"))

    assert poll.poll_feed(feed_id) is True

    rows = {row["id"]: row for row in items()}
    for item_id in example_ids:
        assert rows[item_id]["fallback"] == 0
        assert rows[item_id]["fetch_status"] == "ok"
    for item_id in other_ids:
        assert rows[item_id]["fallback"] == 1
        assert rows[item_id]["fetch_status"] == "blocked"


def test_poll_feed_rotates_blocked_item_retries(feed_id, calls, monkeypatch, _reset_retry_cursor):
    """Across successive polls, retry_fallback rotates through the blocked candidates
    instead of always retrying the newest `_BLOCKED_RETRIES` rows, so older blocked
    rows eventually get a turn too. Nothing here ever heals (fetch_article always
    reports "blocked"), so the candidate list stays the same five rows throughout."""
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    blocked_ids = _seed_blocked_items(feed_id, 5)
    _mark_sample_guids_seen(feed_id)
    # id-DESC candidate order (newest first) -- the same order retry_fallback builds.
    candidates = list(reversed(blocked_ids))
    with db() as conn:
        id_to_link = {
            row["id"]: row["link"]
            for row in conn.execute("SELECT id, link FROM items WHERE feed_id = ?", (feed_id,))
        }

    fetched: list[str] = []

    def fake_fetch_article(link):
        fetched.append(link)
        return None, "blocked", []

    monkeypatch.setattr(poll, "fetch_article", fake_fetch_article)

    # poll 1 retries positions 0,1,2; poll 2 wraps to 3,4,0; poll 3 continues at 1,2,3.
    expected_positions = [(0, 1, 2), (3, 4, 0), (1, 2, 3)]
    for positions in expected_positions:
        fetched.clear()
        assert poll.poll_feed(feed_id) is True
        expected_links = {id_to_link[candidates[p]] for p in positions}
        assert set(fetched) == expected_links
        assert len(fetched) == len(expected_links)  # no link repeated within one poll


def test_poll_feed_wraps_retries_when_fewer_blocked_items_than_limit(
    feed_id, calls, monkeypatch, _reset_retry_cursor
):
    """When there are fewer blocked candidates than the retry limit, the rotation must
    not skip or duplicate rows just because the limit doesn't divide the candidate
    count: each of the two rows is retried exactly once per poll."""
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    blocked_ids = _seed_blocked_items(feed_id, 2)
    _mark_sample_guids_seen(feed_id)
    with db() as conn:
        id_to_link = {
            row["id"]: row["link"]
            for row in conn.execute("SELECT id, link FROM items WHERE feed_id = ?", (feed_id,))
        }
    expected_links = {id_to_link[i] for i in blocked_ids}

    fetched: list[str] = []

    def fake_fetch_article(link):
        fetched.append(link)
        return None, "blocked", []

    monkeypatch.setattr(poll, "fetch_article", fake_fetch_article)

    for _ in range(3):
        fetched.clear()
        assert poll.poll_feed(feed_id) is True
        assert sorted(fetched) == sorted(expected_links)
        assert len(fetched) == len(expected_links)


def test_retry_fallback_button_path_retries_all_hosts_regardless_of_cookies(feed_id, monkeypatch):
    """The manual retry-fallback button (only_blocked=False) must retry blocked rows
    on every host, cookies or not."""
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    example_ids = _seed_blocked_items(feed_id, 2, host="example.com")
    other_ids = _seed_blocked_items(feed_id, 2, host="other.com")

    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, **kwargs: ("H", "S"))

    poll.retry_fallback(feed_id)

    rows = {row["id"]: row for row in items()}
    for item_id in example_ids + other_ids:
        assert rows[item_id]["fallback"] == 0
        assert rows[item_id]["fetch_status"] == "ok"


def test_retry_fallback_button_path_still_retries_all_blocked_items(feed_id, monkeypatch):
    """The manual retry-fallback button calls retry_fallback with no limit/only_blocked,
    so it must still retry every fallback row regardless of fetch_status."""
    blocked_ids = _seed_blocked_items(feed_id, 5)
    teaser_id = _seed_fallback_item(feed_id, guid="teaser-1", link="https://example.com/t1")
    with db() as conn:
        conn.execute("UPDATE items SET fetch_status = 'teaser' WHERE id = ?", (teaser_id,))
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, **kwargs: ("H", "S"))

    poll.retry_fallback(feed_id)

    rows = {row["id"]: row for row in items()}
    for item_id in blocked_ids + [teaser_id]:
        assert rows[item_id]["fallback"] == 0
        assert rows[item_id]["fetch_status"] == "ok"


def test_retry_fallback_restores_callers_status_instead_of_popping(feed_id, monkeypatch):
    """When retry_fallback is invoked with a status already set for this feed (as
    poll_feed does), it must restore that status afterwards rather than popping it,
    so the caller's own status survives the nested call."""
    _seed_blocked_items(feed_id, 1)
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, **kwargs: ("H", "S"))

    poll._status[feed_id] = "Summarizing 2/3"
    poll.retry_fallback(feed_id, limit=1, only_blocked=True)
    assert poll._status[feed_id] == "Summarizing 2/3"

    poll._status.pop(feed_id, None)
    poll.retry_fallback(feed_id, limit=1, only_blocked=True)
    assert feed_id not in poll._status


def test_ui_can_write_while_polling(feed_id, calls, monkeypatch):
    """A second writer (the web UI) must not hit 'database is locked' mid-poll."""
    import sqlite3

    from pintxos.db import connect

    def summarize_and_write(text, original_title, url, respect_language=None, model=None):
        other = connect()
        other.execute("PRAGMA busy_timeout = 200")
        try:
            with other:
                other.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    ("PINTXOS_POLL_MINUTES", url),
                )
        except sqlite3.OperationalError as e:  # pragma: no cover - the bug we fixed
            pytest.fail(f"UI write blocked during poll: {e}")
        finally:
            other.close()
        return "HEADLINE", "summary"

    monkeypatch.setattr(poll, "summarize", summarize_and_write)
    poll.poll_all()
    assert len(items()) == 3
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", ("PINTXOS_POLL_MINUTES",)).fetchone()
    assert row["value"] == "https://example.com/three"


def test_ad_entry_filtered_before_summarize(feed_id, calls_with_ad, monkeypatch):
    """The coupon entry never reaches fetch/summarize and is never stored."""
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    seen_status = []
    fake_summarize = poll.summarize

    def spy(text, original_title, url, respect_language=None, model=None):
        seen_status.append(poll._status.get(feed_id))
        return fake_summarize(text, original_title, url, respect_language=respect_language)

    monkeypatch.setattr(poll, "summarize", spy)
    assert poll.poll_feed(feed_id) is True
    # 2, not 3: the filtered entry is gone before the status count is computed.
    assert seen_status == ["Summarizing 1/2", "Summarizing 2/2"]
    assert len(calls_with_ad) == 2
    links = [row["link"] for row in items()]
    assert "https://example.com/coupons" not in links


def test_filter_ads_disabled_summarizes_everything(feed_id, calls_with_ad, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "0")
    poll.poll_feed(feed_id)
    assert len(calls_with_ad) == 3
    assert "https://example.com/coupons" in [row["link"] for row in items()]


def test_keep_pattern_rescues_entry_from_ads_filtered_and_last_filtered(
    feed_id, calls_with_ad, monkeypatch
):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    monkeypatch.setenv("PINTXOS_AD_KEEP_PATTERNS", "groupon")
    poll.poll_feed(feed_id)
    assert len(calls_with_ad) == 3
    assert "https://example.com/coupons" in [row["link"] for row in items()]
    feed = feed_row(feed_id)
    assert feed["ads_filtered"] == 0
    assert json.loads(feed["last_filtered"]) == []


def test_invalid_extra_ad_pattern_logs_warning_and_keeps_builtin_rules(
    feed_id, calls_with_ad, monkeypatch, caplog
):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    monkeypatch.setenv("PINTXOS_AD_TITLE_PATTERNS", "(")
    with caplog.at_level("WARNING"):
        poll.poll_feed(feed_id)
    assert "invalid PINTXOS_AD_TITLE_PATTERNS" in caplog.text
    assert len(calls_with_ad) == 2
    assert "https://example.com/coupons" not in [row["link"] for row in items()]


def test_extra_ad_patterns_keeps_valid_lines_around_an_invalid_one(feed_id, monkeypatch, caplog):
    monkeypatch.setenv("PINTXOS_AD_TITLE_PATTERNS", "giveaway\n(bad\nsponsored:")
    with db() as conn, caplog.at_level("WARNING"):
        patterns = poll._extra_ad_patterns(conn, feed_row(feed_id))
    assert [p.pattern for p in patterns] == ["giveaway", "sponsored:"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "line 2" in warnings[0].message


def test_second_poll_re_evaluates_ad_and_does_not_store_it(
    feed_id, calls_with_ad, caplog, monkeypatch
):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    poll.poll_feed(feed_id)
    calls_with_ad.clear()
    with caplog.at_level("INFO"):
        poll.poll_feed(feed_id)
    assert len(calls_with_ad) == 0
    assert "filtered 1 ad entries" in caplog.text
    assert len(items()) == 2


def test_extra_pattern_filters_entry_not_caught_by_builtin_rules(feed_id, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    xml = SAMPLE_WITH_AD.decode().replace(
        "Groupon Promo Codes: 60% Off in September 2026",
        "Best Labor Day Deals 2026",
    ).replace(
        "<category>coupons</category>", ""
    ).encode()
    seen: list[str] = []

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(xml)
        raise AssertionError(f"unexpected GET {url}")

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
        seen.append(original_title)
        return "HEADLINE", f"summary of {original_title}"

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))
    monkeypatch.setattr(poll, "summarize", fake_summarize)
    monkeypatch.setenv("PINTXOS_AD_TITLE_PATTERNS", "best .* deals")

    poll.poll_feed(feed_id)
    assert seen == [
        "First article about a rocket launch",
        "Second article about a merger",
    ]
    assert len(items()) == 2


def _seed_item(feed_id, guid, link, title):
    with db() as conn:
        conn.execute(
            "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
            "headline, summary, fallback, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (feed_id, guid, link, title, now(), "old headline", "old summary", 0, now()),
        )


def test_stored_ad_looking_item_survives_poll_with_filter_enabled(feed_id, calls, monkeypatch):
    """Turning the filter on never touches items already stored: it only applies to
    entries seen after it is enabled."""
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    _seed_item(
        feed_id, "old-ad", "https://example.com/deals/groupon-promo-code/",
        "Groupon Promo Codes: 60% Off",
    )

    poll.poll_feed(feed_id)

    titles = {row["original_title"] for row in items()}
    assert "Groupon Promo Codes: 60% Off" in titles


def test_ads_filtered_column_set_when_filter_enabled(feed_id, calls_with_ad, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    poll.poll_feed(feed_id)
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    assert feed["ads_filtered"] == 1


def test_ads_filtered_column_zero_when_filter_disabled(feed_id, calls_with_ad, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "0")
    poll.poll_feed(feed_id)
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    assert feed["ads_filtered"] == 0


def test_last_filtered_records_title_and_reason_for_wired_fixture(feed_id, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")

    def fake_get(url):
        assert url == FEED_URL
        return FakeResponse(WIRED)

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))
    monkeypatch.setattr(
        poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("H", f"summary of {title}")
    )

    poll.poll_feed(feed_id)

    feed = feed_row(feed_id)
    assert feed["ads_filtered"] == 22
    last_filtered = json.loads(feed["last_filtered"])
    assert len(last_filtered) == 22
    for entry in last_filtered:
        assert set(entry) == {"kind", "title", "reason", "guid", "link", "published_at"}
        assert entry["kind"] == "ad"
        assert entry["title"]
        assert entry["guid"]
        assert entry["link"]
        assert entry["published_at"]
        reason = entry["reason"]
        assert reason.startswith("ad: ")
        detail = reason.removeprefix("ad: ")
        assert detail == "link" or detail.startswith("tag:") or detail.startswith("title:")


def _serve(monkeypatch, xml: bytes) -> list[str]:
    """Serve `xml` as the feed, never fetch articles, record the titles summarized."""
    seen: list[str] = []

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(xml)
        raise AssertionError(f"unexpected GET {url}")

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
        seen.append(original_title)
        return "HEADLINE", f"summary of {original_title}"

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))
    monkeypatch.setattr(poll, "summarize", fake_summarize)
    return seen


@pytest.mark.parametrize(
    "feed_override, global_setting, expect_ad_filtered",
    [
        (1, "0", True),  # override on wins over a global setting that is off
        (0, "1", False),  # override off wins over a global setting that is on
        (None, "1", True),  # no override: follows global on
        (None, "0", False),  # no override: follows global off
    ],
)
def test_feed_override_and_global_interaction(
    feed_id, calls_with_ad, monkeypatch, feed_override, global_setting, expect_ad_filtered
):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", global_setting)
    if feed_override is not None:
        set_feed(feed_id, filter_ads=feed_override)
    else:
        assert feed_row(feed_id)["filter_ads"] is None  # a new feed has no override

    poll.poll_feed(feed_id)

    if expect_ad_filtered:
        assert len(calls_with_ad) == 2
        assert "https://example.com/coupons" not in [row["link"] for row in items()]
        assert feed_row(feed_id)["ads_filtered"] == 1
    else:
        assert len(calls_with_ad) == 3
        assert "https://example.com/coupons" in [row["link"] for row in items()]
        assert feed_row(feed_id)["ads_filtered"] == 0


def test_poll_feed_passes_feed_override_respect_language_false(feed_id, calls, monkeypatch):
    set_feed(feed_id, respect_language=0)
    seen_kwargs = []
    fake_summarize = poll.summarize

    def spy(text, original_title, url, respect_language=None, model=None):
        seen_kwargs.append(respect_language)
        return fake_summarize(text, original_title, url, respect_language=respect_language)

    monkeypatch.setattr(poll, "summarize", spy)
    poll.poll_feed(feed_id)
    assert seen_kwargs
    assert all(value is False for value in seen_kwargs)


def test_poll_feed_falls_back_to_global_respect_language_when_unset(
    feed_id, calls, monkeypatch
):
    assert feed_row(feed_id)["respect_language"] is None  # a new feed has no override
    monkeypatch.delenv("PINTXOS_RESPECT_LANGUAGE", raising=False)
    seen_kwargs = []
    fake_summarize = poll.summarize

    def spy(text, original_title, url, respect_language=None, model=None):
        seen_kwargs.append(respect_language)
        return fake_summarize(text, original_title, url, respect_language=respect_language)

    monkeypatch.setattr(poll, "summarize", spy)
    poll.poll_feed(feed_id)
    assert seen_kwargs
    assert all(value is True for value in seen_kwargs)


def test_retry_fallback_passes_feed_override_respect_language_false(feed_id, monkeypatch):
    set_feed(feed_id, respect_language=0)
    _seed_fallback_item(feed_id)
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    seen_kwargs = []

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
        seen_kwargs.append(respect_language)
        return "New", "New summary"

    monkeypatch.setattr(poll, "summarize", fake_summarize)
    poll.retry_fallback(feed_id)
    assert seen_kwargs == [False]


# The third entry is renamed to something only a per-feed pattern catches, the second to
# something only a global pattern catches; neither trips a built-in rule.
GIVEAWAY_XML = (
    SAMPLE.decode()
    .replace("Third article with almost no body text at all", "Big Giveaway")
    .replace("Second article about a merger", "Best Labor Day Deals 2026")
    .encode()
)


def test_feed_ad_title_patterns_filter_entry_not_caught_by_builtin_rules(feed_id, monkeypatch):
    seen = _serve(monkeypatch, GIVEAWAY_XML)
    set_feed(feed_id, filter_ads=1, ad_patterns_mode=1, ad_title_patterns="giveaway")

    poll.poll_feed(feed_id)

    assert seen == ["First article about a rocket launch", "Best Labor Day Deals 2026"]
    assert feed_row(feed_id)["ads_filtered"] == 1


def test_global_and_feed_ad_title_patterns_both_apply(feed_id, monkeypatch):
    seen = _serve(monkeypatch, GIVEAWAY_XML)
    monkeypatch.setenv("PINTXOS_AD_TITLE_PATTERNS", "best .* deals")
    set_feed(feed_id, filter_ads=1, ad_patterns_mode=1, ad_title_patterns="giveaway")

    poll.poll_feed(feed_id)

    assert seen == ["First article about a rocket launch"]
    assert feed_row(feed_id)["ads_filtered"] == 2


def test_extra_ad_patterns_per_mode(feed_id, monkeypatch):
    """The three modes, straight off the helper: inherit, on, off."""
    monkeypatch.setenv("PINTXOS_AD_TITLE_PATTERNS", "best .* deals")
    set_feed(feed_id, ad_title_patterns="giveaway")

    def patterns():
        with db() as conn:
            return [p.pattern for p in poll._extra_ad_patterns(conn, feed_row(feed_id))]

    assert patterns() == ["best .* deals"]  # mode NULL: global only
    set_feed(feed_id, ad_patterns_mode=1)
    assert patterns() == ["best .* deals", "giveaway"]  # mode 1: global + feed
    set_feed(feed_id, ad_patterns_mode=0)
    assert patterns() == []  # mode 0: nothing extra


def test_feed_ad_patterns_ignored_when_filter_disabled(feed_id, monkeypatch):
    seen = _serve(monkeypatch, GIVEAWAY_XML)
    set_feed(feed_id, filter_ads=0, ad_patterns_mode=1, ad_title_patterns="giveaway")

    poll.poll_feed(feed_id)

    assert "Big Giveaway" in seen


def test_invalid_feed_ad_pattern_warns_with_feed_id_and_still_filters_with_good_line(
    feed_id, monkeypatch, caplog
):
    seen = _serve(monkeypatch, GIVEAWAY_XML)
    set_feed(feed_id, filter_ads=1, ad_patterns_mode=1, ad_title_patterns="giveaway\n(bad")

    with db() as conn, caplog.at_level("WARNING"):
        patterns = poll._extra_ad_patterns(conn, feed_row(feed_id))

    assert [p.pattern for p in patterns] == ["giveaway"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert f"feed {feed_id}" in warnings[0].getMessage()
    assert "line 2" in warnings[0].getMessage()

    caplog.clear()
    with caplog.at_level("WARNING"):
        poll.poll_feed(feed_id)

    assert f"feed {feed_id}" in caplog.text
    assert "Big Giveaway" not in seen
    assert feed_row(feed_id)["ads_filtered"] == 1


# --- client construction / impersonation -----------------------------------------


@pytest.mark.parametrize(
    "profile, expect_impersonate, expect_pintxos_ua",
    [
        ("safari17_0", "safari17_0", False),
        ("", None, True),
    ],
)
def test_make_client_impersonation(profile, expect_impersonate, expect_pintxos_ua):
    client = poll._make_client(profile)
    assert client.impersonate == expect_impersonate
    if expect_pintxos_ua:
        assert client.headers.get("User-Agent") == poll.USER_AGENT


# --- _get() and cookie jar propagation -------------------------------------------


@pytest.fixture(autouse=False)
def _reset_client_jar(monkeypatch):
    """Cookie-jar tests must not leak the installed jar across test order."""
    monkeypatch.setattr(poll, "_client_jar", None)
    poll._clients[0].cookies = curl_cffi.requests.Cookies()
    yield
    poll._client_jar = None
    poll._clients[0].cookies = curl_cffi.requests.Cookies()


@pytest.fixture
def _reset_last_request(monkeypatch):
    """Per-host pacing tests must not leak the last-request clock across test order."""
    monkeypatch.setattr(poll, "_last_request", {})


def test_get_installs_jar_on_client_and_clears_it_when_file_removed(_reset_client_jar, monkeypatch):
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")

    def fake(url):
        return FakeResponse(b"<html>ok</html>", content_type="text/html")

    monkeypatch.setattr(poll._clients[0], "get", fake)

    poll._get("https://www.example.com/a")

    assert poll._clients[0].cookies.get("sid", domain=".example.com") == "abc"
    installed_jar = poll._clients[0].cookies.jar

    # Cookies file is unchanged, so the second call must not reinstall the jar.
    poll._get("https://www.example.com/a")
    assert poll._clients[0].cookies.jar is installed_jar

    cookie_path().unlink()

    poll._get("https://www.example.com/a")
    assert len(poll._clients[0].cookies) == 0


# --- Cloudflare challenge retry across profiles ------------------------------------


class FakeClient:
    """Stands in for a curl_cffi Session: returns responses from a scripted list."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


def _challenge_response():
    return FakeResponse(b"", status_code=403, headers={"cf-mitigated": "challenge"})


def _plain_403_response():
    return FakeResponse(b"", status_code=403)


def _ok_response():
    return FakeResponse(b"<html>ok</html>", content_type="text/html")


def test_get_retries_challenge_then_succeeds(_reset_client_jar, _reset_last_request, monkeypatch):
    fake = FakeClient([_challenge_response(), _ok_response()])
    monkeypatch.setattr(poll, "_clients", [fake])
    clock = [1000.0]
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(poll.time, "sleep", fake_sleep)
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])

    resp = poll._get("https://example.com/a")

    assert resp.status_code == 200
    assert sleeps == [poll._HOST_PAUSE]


def test_get_gives_up_after_all_challenge_attempts(_reset_client_jar, _reset_last_request, monkeypatch):
    responses = [_challenge_response(), _challenge_response(), _challenge_response()]
    fake = FakeClient(responses)
    monkeypatch.setattr(poll, "_clients", [fake])
    clock = [1000.0]
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(poll.time, "sleep", fake_sleep)
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])

    resp = poll._get("https://example.com/a")

    assert resp is responses[-1]
    assert sleeps == [poll._HOST_PAUSE, poll._HOST_PAUSE]


def test_get_gives_up_after_all_challenge_attempts_reports_blocked(_reset_client_jar, _reset_last_request, monkeypatch):
    fake = FakeClient([_challenge_response(), _challenge_response(), _challenge_response()])
    monkeypatch.setattr(poll, "_clients", [fake])
    monkeypatch.setattr(poll.time, "sleep", lambda s: None)

    text, status, _labels = poll.fetch_article("https://example.com/a")

    assert text is None
    assert status == "blocked"


def test_get_retries_second_attempt_on_next_profile(_reset_client_jar, _reset_last_request, monkeypatch):
    client1 = FakeClient([_challenge_response(), _ok_response()])
    client2 = FakeClient([_ok_response()])
    monkeypatch.setattr(poll, "_clients", [client1, client2])
    monkeypatch.setattr(poll.time, "sleep", lambda s: None)

    resp = poll._get("https://example.com/a")

    assert resp.status_code == 200
    assert client1.calls == 1
    assert client2.calls == 1


def test_get_plain_403_returns_immediately_without_retry(_reset_client_jar, _reset_last_request, monkeypatch):
    fake = FakeClient([_plain_403_response(), _ok_response()])
    monkeypatch.setattr(poll, "_clients", [fake])
    sleeps = []
    monkeypatch.setattr(poll.time, "sleep", lambda s: sleeps.append(s))

    resp = poll._get("https://example.com/a")

    assert resp.status_code == 403
    assert fake.calls == 1
    assert sleeps == []


# --- Per-host pacing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url1, url2, elapsed, expected_sleeps",
    [
        # Same host, 0.5s elapsed: second call waits out the remaining 1.5s.
        ("https://example.com/a", "https://example.com/b", 0.5, [1.5]),
        # Different hosts: no pacing wait.
        ("https://example.com/a", "https://other.com/b", 0.5, []),
        # Same host, but the pause has already elapsed: no wait.
        ("https://example.com/a", "https://example.com/b", 3.0, []),
    ],
)
def test_get_paces_requests_to_same_host(
    _reset_client_jar, _reset_last_request, monkeypatch, url1, url2, elapsed, expected_sleeps
):
    fake = FakeClient([_ok_response(), _ok_response()])
    monkeypatch.setattr(poll, "_clients", [fake])
    sleeps = []
    monkeypatch.setattr(poll.time, "sleep", lambda s: sleeps.append(s))

    clock = [1000.0]
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])

    poll._get(url1)
    clock[0] += elapsed
    poll._get(url2)

    assert sleeps == pytest.approx(expected_sleeps)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("a, b", ["a", "b"]),
        ("", [""]),
        ("safari17_0", ["safari17_0"]),
        ("a,,b", ["a", "b"]),
        ("a, ", ["a"]),
        (" , ", [""]),
    ],
)
def test_parse_profiles(value, expected):
    assert poll._parse_profiles(value) == expected


def test_cookies_only_sent_to_matching_domain_and_zero_expiry_is_a_session_cookie():
    # Proves domain scoping at the stdlib http.cookiejar level, which both the
    # loader (get_jar) and curl_cffi's Cookies wrapper delegate to: a cookie
    # jarred for .ft.com is offered on a request to www.ft.com, and withheld on
    # a request to an unrelated domain. A "0" expiry (used by some cookies.txt
    # exporters for session cookies) must still be sent on the wire.
    write_cookies(
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc123\n"
        ".ft.com\tTRUE\t/\tFALSE\t0\tsess\tdef456\n"
    )
    jar = poll.get_jar()
    assert jar is not None

    ft_req = urllib.request.Request("https://www.ft.com/x")
    jar.add_cookie_header(ft_req)
    ft_cookie_header = ft_req.get_header("Cookie")
    assert ft_cookie_header is not None
    assert "sid" in ft_cookie_header
    assert "sess" in ft_cookie_header

    other_req = urllib.request.Request("https://www.example.com/x")
    jar.add_cookie_header(other_req)
    assert other_req.get_header("Cookie") is None


@pytest.mark.parametrize(
    "cookies_present, fetch_ok, expected_auth, expected_fallback",
    [
        (False, True, None, 0),
        (True, True, "used", 0),
        (True, False, "failed", 1),
        (False, False, "missing", 1),
    ],
)
def test_auth_outcome_from_cookies_presence_and_fetch_result(
    feed_id, calls, monkeypatch, _reset_client_jar, cookies_present, fetch_ok, expected_auth, expected_fallback
):
    if cookies_present:
        write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    if fetch_ok:
        monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL TEXT " * 30, "ok", []))
    poll.poll_all()
    rows = items()
    assert len(rows) == 3
    assert all(row["auth"] == expected_auth for row in rows)
    assert all(row["fallback"] == expected_fallback for row in rows)


@pytest.mark.parametrize(
    "cookies_present, expected_auth",
    [(True, "used"), (False, None)],
)
def test_short_empty_text_marks_auth_without_failure(
    feed_id, calls, monkeypatch, _reset_client_jar, cookies_present, expected_auth
):
    """A "short" fetch with no extracted text at all (e.g. a bare video page) is
    not a failure: auth reflects only whether cookies were sent -- never "failed"
    or "missing" -- word_count is 0, and the item is not a fallback."""
    if cookies_present:
        write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("", "short", []))
    poll.poll_all()
    rows = items()
    assert len(rows) == 3
    assert all(row["auth"] == expected_auth for row in rows)
    assert all(row["fetch_status"] == "short" for row in rows)
    assert all(row["word_count"] == 0 for row in rows)
    assert all(row["fallback"] == 0 for row in rows)


# --- persisting rotated cookies back to cookies.txt -------------------------------


def test_authenticated_fetch_persists_rotated_cookie_to_disk(feed_id, calls, monkeypatch):
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")

    def fake_fetch_article(link):
        # Simulate curl_cffi rotating the session cookie in memory on a successful,
        # authenticated fetch.
        jar = poll.get_jar()
        rotated = http.cookiejar.Cookie(
            version=0,
            name="sid",
            value="rotated-value",
            port=None,
            port_specified=False,
            domain=".example.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=False,
            expires=FUTURE_EXPIRY,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        jar.set_cookie(rotated)
        return "FULL ARTICLE TEXT " * 30, "ok", []

    monkeypatch.setattr(poll, "fetch_article", fake_fetch_article)

    poll.poll_all()

    rows = items()
    assert len(rows) == 3
    assert all(row["auth"] == "used" for row in rows)
    assert "rotated-value" in cookie_path().read_text()


def test_failed_authenticated_fetch_does_not_rewrite_cookies_file(feed_id, calls):
    write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc")
    # `calls` fixture leaves poll.fetch_article returning (None, "error"), so every fetch
    # fails
    # even though cookies are present for the article domain (auth == "failed").

    before_mtime_ns = cookie_path().stat().st_mtime_ns
    before_content = cookie_path().read_text()

    poll.poll_all()

    rows = items()
    assert len(rows) == 3
    assert all(row["auth"] == "failed" for row in rows)
    assert cookie_path().stat().st_mtime_ns == before_mtime_ns
    assert cookie_path().read_text() == before_content


# --- publisher labels (RSS categories + page section/tags/keywords) --------------

LABEL_ARTICLE_BODY = "Real body sentence. " * 20  # comfortably over MIN_ARTICLE_CHARS


def _label_feed_xml(link: str, categories: list[str]) -> bytes:
    """A one-item RSS feed whose entry carries the given <category> terms."""
    cats = "".join(f"      <category>{c}</category>\n" for c in categories)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel><title>Label Feed</title>'
        "<link>https://example.com/</link><description>desc</description>"
        "<item>"
        "<title>Cricket news roundup</title>"
        f"<link>{link}</link>"
        f"<guid>{link}</guid>"
        "<pubDate>Mon, 01 Sep 2025 10:00:00 +0000</pubDate>"
        f"{cats}"
        "<description>teaser</description>"
        "</item></channel></rss>"
    ).encode()


def _label_article_html(
    section: str | None = None, tag: str | None = None, keywords: str | None = None
) -> bytes:
    """A minimal article page with the given publisher-label meta tags, long enough
    for trafilatura to extract at least MIN_ARTICLE_CHARS of body text."""
    meta = ""
    if section is not None:
        meta += f'<meta property="article:section" content="{section}">\n'
    if tag is not None:
        meta += f'<meta property="article:tag" content="{tag}">\n'
    if keywords is not None:
        meta += f'<meta name="keywords" content="{keywords}">\n'
    return (
        f"<html><head>{meta}</head><body><article><p>{LABEL_ARTICLE_BODY}"
        "</p></article></body></html>"
    ).encode()


def test_poll_stores_rss_and_page_labels_in_order(feed_id, monkeypatch):
    """Labels combine this entry's RSS <category> terms (original case, feed order)
    with the fetched page's own metadata labels (categories then tags, each
    comma-split), RSS first."""
    link = "https://example.com/cricket"
    feed_xml = _label_feed_xml(link, ["World News", "Breaking"])
    article_html = _label_article_html(section="Sport", tag="Cricket", keywords="ashes, england")

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(feed_xml)
        if url == link:
            return FakeResponse(article_html, content_type="text/html; charset=utf-8")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("H", "S"))

    assert poll.poll_feed(feed_id) is True

    rows = items()
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "ok"
    assert json.loads(rows[0]["labels"]) == [
        "World News", "Breaking", "Sport", "Cricket", "ashes", "england",
    ]


def test_poll_stores_null_labels_when_no_rss_tags_and_no_page_meta(feed_id, monkeypatch):
    """An entry with no RSS categories, whose fetched page has no publisher-label
    metadata, stores NULL -- not an empty JSON array -- even though the fetch itself
    succeeds."""
    link = "https://example.com/cricket"
    feed_xml = _label_feed_xml(link, [])
    article_html = _label_article_html()  # no section/tag/keywords meta

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(feed_xml)
        if url == link:
            return FakeResponse(article_html, content_type="text/html; charset=utf-8")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("H", "S"))

    assert poll.poll_feed(feed_id) is True

    rows = items()
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "ok"
    assert rows[0]["labels"] is None


def test_poll_dedupes_labels_case_insensitively_keeping_first_seen_casing(feed_id, monkeypatch):
    """The RSS category "Sport" and the page's lower-case "sport" section collapse
    into a single label, keeping the RSS entry's casing because it was seen first."""
    link = "https://example.com/cricket"
    feed_xml = _label_feed_xml(link, ["Sport"])
    article_html = _label_article_html(section="sport")

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(feed_xml)
        if url == link:
            return FakeResponse(article_html, content_type="text/html; charset=utf-8")
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("H", "S"))

    assert poll.poll_feed(feed_id) is True

    rows = items()
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "ok"
    assert json.loads(rows[0]["labels"]) == ["Sport"]


# --- article_input: the public capture point for pintxos's summarize() input -----


def test_article_input_uses_fetched_article_text(monkeypatch):
    """A successful fetch wins: fallback/title_only are both False, the text is
    exactly what was extracted, and the labels include the fetched page's labels."""
    entry = feedparser.parse(SAMPLE).entries[0]
    monkeypatch.setattr(
        poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", ["Cricket"])
    )

    article = poll.article_input(entry, None)

    assert article.fallback is False
    assert article.title_only is False
    assert article.text == "FULL ARTICLE TEXT " * 20
    assert article.labels == ["Cricket"]
    assert article.fetch_status == "ok"
    assert article.title == "First article about a rocket launch"
    assert article.link == "https://example.com/one"


def test_article_input_falls_back_to_feed_excerpt_when_fetch_fails(monkeypatch):
    """When the fetch fails but the feed's own excerpt is long enough, that excerpt
    is used as the text and fallback (but not title_only) is set."""
    entry = feedparser.parse(SAMPLE).entries[0]  # content:encoded is well over MIN_FALLBACK_CHARS
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))

    article = poll.article_input(entry, None)

    assert article.fallback is True
    assert article.title_only is False
    assert "ENCODED BODY" in article.text
    assert article.fetch_status == "error"
    assert article.word_count is None


def test_article_input_falls_back_to_title_when_excerpt_too_short(monkeypatch):
    """When the fetch fails and the feed's own excerpt is too short, the title alone
    is used as the text and title_only is set."""
    entry = feedparser.parse(SAMPLE).entries[2]  # "tiny" description
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))

    article = poll.article_input(entry, None)

    assert article.fallback is True
    assert article.title_only is True
    assert article.text == "Third article with almost no body text at all"


def test_article_input_short_empty_text_uses_feed_excerpt_not_fallback(monkeypatch):
    """A "short" fetch (e.g. a bare video page) that extracted no text at all is
    not a fallback -- the fetch itself succeeded -- but there is nothing to
    summarize, so the feed's own excerpt is used instead, same as a failed fetch."""
    entry = feedparser.parse(SAMPLE).entries[0]  # content:encoded is well over MIN_FALLBACK_CHARS
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("", "short", []))

    article = poll.article_input(entry, None)

    assert article.fallback is False
    assert article.title_only is False
    assert "ENCODED BODY" in article.text
    assert article.fetch_status == "short"


def test_article_input_short_empty_text_falls_back_to_title_when_excerpt_too_short(monkeypatch):
    """Same as above, but when the feed's own excerpt is also too short, the title
    alone is used and title_only is set -- fallback still stays False."""
    entry = feedparser.parse(SAMPLE).entries[2]  # "tiny" description
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("", "short", []))

    article = poll.article_input(entry, None)

    assert article.fallback is False
    assert article.title_only is True
    assert article.text == "Third article with almost no body text at all"
    assert article.fetch_status == "short"


def test_article_input_short_nonempty_text_used_as_is(monkeypatch):
    """A "short" fetch that did extract some text (e.g. a New Yorker cartoon
    caption) uses that text as is -- neither a fallback nor title_only."""
    entry = feedparser.parse(SAMPLE).entries[0]
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("A cartoon caption.", "short", ["Cartoons"]))

    article = poll.article_input(entry, None)

    assert article.fallback is False
    assert article.title_only is False
    assert article.text == "A cartoon caption."
    assert article.fetch_status == "short"
    assert article.labels == ["Cartoons"]


# --- topic classification ------------------------------------------------------


def mock_classify(monkeypatch, answer):
    """Record every topics.classify_topic call; `answer` is a slug (or None), or a
    callable mapping the article title to one. Returns the list of (title, labels, lead)."""
    seen: list[tuple[str, list[str], str]] = []

    def fake_classify(title, labels, lead, model=None):
        seen.append((title, labels, lead))
        return answer(title) if callable(answer) else answer

    monkeypatch.setattr(topics, "classify_topic", fake_classify)
    return seen


def _expected_summarize_calls() -> list[tuple[str, str, str]]:
    """The (text, title, link) triples poll_feed hands summarize() for the sample feed
    when the article fetch fails: the feed excerpt, or the title when it is too short."""
    expected = []
    for entry in sorted(feedparser.parse(SAMPLE).entries, key=poll._entry_sort_key):
        text = poll._entry_text(entry)
        if len(text) < poll.MIN_FALLBACK_CHARS:
            text = entry.title
        expected.append((text, entry.title, entry.link))
    return expected


def test_classify_switch_off_never_classifies_and_leaves_summarize_input_unchanged(
    feed_id, calls, monkeypatch
):
    """The switch is off by default: no classify call at all, and summarize() sees
    exactly the arguments it saw before topic classification existed."""
    classify_calls = mock_classify(monkeypatch, "sport")

    assert poll.poll_feed(feed_id) is True

    assert classify_calls == []
    assert calls == _expected_summarize_calls()
    rows = items()
    assert [row["topic"] for row in rows] == [None, None, None]
    assert [row["muted"] for row in rows] == [0, 0, 0]
    assert feed_row(feed_id)["topic_counts"] is None


def test_muted_topic_is_stored_muted_and_never_summarized(feed_id, calls, monkeypatch):
    set_feed(feed_id, classify_topics=1, mute_topics=json.dumps(["sport"]))
    classify_calls = mock_classify(
        monkeypatch, lambda title: "sport" if "rocket" in title else "economy"
    )

    assert poll.poll_feed(feed_id) is True

    assert len(classify_calls) == 3
    leads = {title: lead for title, _labels, lead in classify_calls}
    assert leads["First article about a rocket launch"].startswith("ENCODED BODY:")
    # A title-only item has no body to quote, so its lead is empty.
    assert leads["Third article with almost no body text at all"] == ""

    # The muted entry never reached summarize()...
    assert [title for _text, title, _url in calls] == [
        "Second article about a merger",
        "Third article with almost no body text at all",
    ]
    # ... but it is stored, so the next poll does not re-classify it.
    rows = {row["original_title"]: row for row in items()}
    muted = rows["First article about a rocket launch"]
    assert muted["muted"] == 1
    assert muted["topic"] == "sport"
    assert muted["headline"] is None
    assert muted["summary"] is None
    assert muted["model"] is None  # no summary was written, so no model to record
    assert muted["fallback"] == 1
    assert muted["word_count"] is None
    assert muted["fetch_status"] == "error"
    assert "ENCODED BODY" in muted["text"]
    assert rows["Second article about a merger"]["muted"] == 0

    feed = feed_row(feed_id)
    assert feed["ads_filtered"] == 1  # the one counter covers both kinds
    assert json.loads(feed["last_filtered"]) == [
        {
            "kind": "topic",
            "title": "First article about a rocket launch",
            "reason": "topic: sport",
            "guid": "https://example.com/one",
            "link": "https://example.com/one",
            "published_at": "2025-09-01T10:00:00+00:00",
        }
    ]
    assert json.loads(feed["topic_counts"]) == {"sport": 1, "economy": 2}


def test_muted_item_logs_at_info(feed_id, calls, monkeypatch, caplog):
    set_feed(feed_id, classify_topics=1, mute_topics=json.dumps(["sport"]))
    mock_classify(monkeypatch, "sport")

    with caplog.at_level("INFO"):
        poll.poll_feed(feed_id)

    assert "topic sport" in caplog.text


def test_unmuted_topic_is_summarized_stored_and_counted(feed_id, calls, monkeypatch):
    set_feed(feed_id, classify_topics=1, mute_topics=json.dumps(["sport"]))
    mock_classify(monkeypatch, "science")

    assert poll.poll_feed(feed_id) is True

    assert len(calls) == 3
    rows = items()
    assert [row["topic"] for row in rows] == ["science"] * 3
    assert [row["muted"] for row in rows] == [0, 0, 0]
    feed = feed_row(feed_id)
    assert json.loads(feed["topic_counts"]) == {"science": 3}
    assert feed["ads_filtered"] == 0
    assert json.loads(feed["last_filtered"]) == []


def test_topic_counts_accumulate_across_polls(feed_id, calls, monkeypatch):
    """Counts are read-modify-written, so a second poll adds to the stored map."""
    set_feed(feed_id, classify_topics=1, topic_counts=json.dumps({"science": 5}))
    mock_classify(monkeypatch, "science")

    poll.poll_feed(feed_id)

    assert json.loads(feed_row(feed_id)["topic_counts"]) == {"science": 8}


def test_summarize_error_does_not_double_count_topic_on_retry(feed_id, monkeypatch):
    """A permanently-retried item is reclassified every poll (it is never inserted,
    so it never becomes "seen"), but it must only be counted once summarize() finally
    succeeds and the item is actually stored -- not once per classify call."""
    link = "https://example.com/cricket"
    feed_xml = _label_feed_xml(link, [])

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(feed_xml)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))

    set_feed(feed_id, classify_topics=1)
    mock_classify(monkeypatch, "science")

    summarize_calls = {"n": 0}

    def flaky_summarize(text, original_title, url, respect_language=None, model=None):
        summarize_calls["n"] += 1
        if summarize_calls["n"] == 1:
            raise SummarizeError("boom")
        return "HEADLINE", "summary"

    monkeypatch.setattr(poll, "summarize", flaky_summarize)

    assert poll.poll_feed(feed_id) is True  # first poll: summarize fails, nothing stored
    assert items() == []
    assert feed_row(feed_id)["topic_counts"] is None

    assert poll.poll_feed(feed_id) is True  # second poll: summarize succeeds
    rows = items()
    assert len(rows) == 1
    assert rows[0]["headline"] == "HEADLINE"
    assert json.loads(feed_row(feed_id)["topic_counts"]) == {"science": 1}


def test_failed_classification_fails_open_and_is_never_counted(feed_id, calls, monkeypatch):
    """classify_topic returning None mutes nothing and counts nothing."""
    set_feed(feed_id, classify_topics=1, mute_topics=json.dumps(["sport"]))
    classify_calls = mock_classify(monkeypatch, None)

    assert poll.poll_feed(feed_id) is True

    assert len(classify_calls) == 3
    assert len(calls) == 3
    rows = items()
    assert [row["topic"] for row in rows] == [None, None, None]
    assert [row["muted"] for row in rows] == [0, 0, 0]
    assert feed_row(feed_id)["topic_counts"] is None


def test_seen_entries_are_never_reclassified(feed_id, calls, monkeypatch):
    set_feed(feed_id, classify_topics=1)
    classify_calls = mock_classify(monkeypatch, "science")

    poll.poll_feed(feed_id)
    assert len(classify_calls) == 3
    classify_calls.clear()
    poll.poll_feed(feed_id)

    assert classify_calls == []
    assert json.loads(feed_row(feed_id)["topic_counts"]) == {"science": 3}


def test_missing_api_key_from_classify_stops_the_poll(feed_id, calls, monkeypatch):
    set_feed(feed_id, classify_topics=1)

    def boom(title, labels, lead, model=None):
        raise MissingApiKey("ANTHROPIC_API_KEY not set")

    monkeypatch.setattr(topics, "classify_topic", boom)

    assert poll.poll_feed(feed_id) is False

    assert items() == []
    assert calls == []
    feed = feed_row(feed_id)
    assert feed["last_error"] == "ANTHROPIC_API_KEY not set"
    assert feed["last_polled_at"] is None


def test_ad_log_entry_carries_kind_guid_link_and_prefixed_reason(
    feed_id, calls_with_ad, monkeypatch
):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")

    poll.poll_feed(feed_id)

    entries = json.loads(feed_row(feed_id)["last_filtered"])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "ad"
    assert entry["title"] == "Groupon Promo Codes: 60% Off in September 2026"
    assert entry["guid"] == "https://example.com/coupons"
    assert entry["link"] == "https://example.com/coupons"
    assert entry["reason"].startswith("ad: ")
    assert entry["published_at"] == "2025-08-30T10:00:00+00:00"


def test_filtered_log_holds_both_kinds_in_poll_order(feed_id, calls_with_ad, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "1")
    set_feed(feed_id, classify_topics=1, mute_topics=json.dumps(["sport"]))
    mock_classify(monkeypatch, lambda title: "sport" if "rocket" in title else "economy")

    poll.poll_feed(feed_id)

    feed = feed_row(feed_id)
    entries = json.loads(feed["last_filtered"])
    # Ads are filtered before the fetch loop, so the ad comes first in poll order.
    assert [e["kind"] for e in entries] == ["ad", "topic"]
    assert [e["link"] for e in entries] == [
        "https://example.com/coupons",
        "https://example.com/one",
    ]
    assert feed["ads_filtered"] == 2
    assert len(calls_with_ad) == 1


def test_retry_fallback_skips_muted_rows(feed_id, monkeypatch):
    """A muted item is a fallback row with no summary: retrying it would pay for a
    summary of something the user asked never to see."""
    item_id = _seed_fallback_item(feed_id)
    with db() as conn:
        conn.execute(
            "UPDATE items SET muted = 1, topic = 'sport', headline = NULL, summary = NULL "
            "WHERE id = ?",
            (item_id,),
        )
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))

    def boom_summarize(*_args, **_kwargs):
        raise AssertionError("a muted item must never be summarized")

    monkeypatch.setattr(poll, "summarize", boom_summarize)

    poll.retry_fallback(feed_id)

    row = items()[0]
    assert row["muted"] == 1
    assert row["fallback"] == 1
    assert row["summary"] is None


# --- summarize_item / summarize_one / filtered_entry ----------------------------


def _seed_muted_item(
    feed_id,
    guid="muted-guid",
    link="https://example.com/muted",
    text="STORED ARTICLE TEXT",
    topic="sport",
    original_title="A muted title",
) -> int:
    with db() as conn:
        return conn.execute(
            "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
            "headline, summary, fallback, text, topic, muted, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (feed_id, guid, link, original_title, now(), None, None, 1, text, topic, 1, now()),
        ).lastrowid


def _set_filtered_log(feed_id, entries, ads_filtered=None):
    with db() as conn:
        conn.execute(
            "UPDATE feeds SET last_filtered = ?, ads_filtered = ? WHERE id = ?",
            (
                json.dumps(entries),
                len(entries) if ads_filtered is None else ads_filtered,
                feed_id,
            ),
        )


def test_summarize_item_releases_muted_row_with_stored_text(feed_id, monkeypatch):
    _seed_muted_item(feed_id, guid="g1", link="https://example.com/m1", text="STORED TEXT")
    _set_filtered_log(
        feed_id,
        [
            {
                "kind": "topic",
                "title": "A muted title",
                "reason": "topic: sport",
                "guid": "g1",
                "link": "https://example.com/m1",
                "published_at": "2025-09-01T10:00:00+00:00",
            }
        ],
    )
    seen = []

    def fake_summarize(text, title, link, respect_language=None, model=None):
        seen.append((text, title, link))
        return "New headline", "New summary"

    monkeypatch.setattr(poll, "summarize", fake_summarize)

    error = poll.summarize_item(feed_id, "g1")

    assert error is None
    assert seen == [("STORED TEXT", "A muted title", "https://example.com/m1")]
    row = items()[0]
    assert row["muted"] == 0
    assert row["headline"] == "New headline"
    assert row["summary"] == "New summary"
    assert row["topic"] == "sport"  # kept, not re-classified
    assert row["model"] == DEFAULTS["PINTXOS_MODEL"]  # feed has no per-feed override
    feed = feed_row(feed_id)
    assert json.loads(feed["last_filtered"]) == []
    assert feed["ads_filtered"] == 0


def test_summarize_item_releases_muted_row_title_only_when_text_is_null(feed_id, monkeypatch):
    _seed_muted_item(feed_id, guid="g1", text=None, original_title="Title only")
    _set_filtered_log(feed_id, [{"kind": "topic", "title": "Title only", "guid": "g1",
                                  "link": "https://example.com/muted",
                                  "reason": "topic: sport",
                                  "published_at": "2025-09-01T10:00:00+00:00"}])
    seen = []
    monkeypatch.setattr(
        poll, "summarize",
        lambda text, title, link, respect_language=None, model=None: (seen.append(text), ("H", "S"))[1],
    )

    error = poll.summarize_item(feed_id, "g1")

    assert error is None
    assert seen == ["Title only"]


def test_summarize_item_ads_filtered_never_goes_below_zero(feed_id, monkeypatch):
    _seed_muted_item(feed_id, guid="g1")
    _set_filtered_log(
        feed_id,
        [{"kind": "topic", "title": "A muted title", "guid": "g1",
          "link": "https://example.com/muted", "reason": "topic: sport",
          "published_at": "2025-09-01T10:00:00+00:00"}],
        ads_filtered=0,
    )
    monkeypatch.setattr(poll, "summarize", lambda *a, **k: ("H", "S"))

    error = poll.summarize_item(feed_id, "g1")

    assert error is None
    assert feed_row(feed_id)["ads_filtered"] == 0


def test_summarize_item_releases_ad_log_entry(feed_id, monkeypatch):
    _set_filtered_log(
        feed_id,
        [
            {
                "kind": "ad",
                "title": "Groupon Promo Codes",
                "reason": "ad: title",
                "guid": "ad-guid",
                "link": "https://example.com/ad",
                "published_at": "2025-08-30T10:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        poll, "fetch_article", lambda link: ("FETCHED ARTICLE TEXT " * 20, "ok", [])
    )
    seen = []

    def fake_summarize(text, title, link, respect_language=None, model=None):
        seen.append((text, title, link))
        return "Ad headline", "Ad summary"

    monkeypatch.setattr(poll, "summarize", fake_summarize)

    error = poll.summarize_item(feed_id, "ad-guid")

    assert error is None
    assert seen == [
        ("FETCHED ARTICLE TEXT " * 20, "Groupon Promo Codes", "https://example.com/ad")
    ]
    rows = items()
    assert len(rows) == 1
    row = rows[0]
    assert row["guid"] == "ad-guid"
    assert row["link"] == "https://example.com/ad"
    assert row["original_title"] == "Groupon Promo Codes"
    assert row["published_at"] == "2025-08-30T10:00:00+00:00"
    assert row["headline"] == "Ad headline"
    assert row["summary"] == "Ad summary"
    assert row["topic"] is None
    assert row["muted"] == 0
    assert row["model"] == DEFAULTS["PINTXOS_MODEL"]  # feed has no per-feed override
    feed = feed_row(feed_id)
    assert json.loads(feed["last_filtered"]) == []
    assert feed["ads_filtered"] == 0


def test_summarize_item_ad_fetch_failure_still_inserts_via_fallback(feed_id, monkeypatch):
    """An ad entry whose fetch fails still inserts via article_input's fallback path
    -- here title-only, since the log doesn't carry an RSS excerpt to fall back to."""
    _set_filtered_log(
        feed_id,
        [
            {
                "kind": "ad",
                "title": "Groupon Promo Codes",
                "reason": "ad: title",
                "guid": "ad-guid",
                "link": "https://example.com/ad",
                "published_at": "2025-08-30T10:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))
    monkeypatch.setattr(poll, "summarize", lambda *a, **k: ("Ad headline", "Ad summary"))

    error = poll.summarize_item(feed_id, "ad-guid")

    assert error is None
    row = items()[0]
    assert row["fallback"] == 1
    assert row["text"] is None  # title-only: text column stays NULL, like poll_feed
    assert row["headline"] == "Ad headline"


def test_summarize_item_feed_not_found(feed_id):
    assert poll.summarize_item(feed_id + 1000, "whatever") == "Feed not found"


def test_summarize_item_item_not_found(feed_id):
    assert poll.summarize_item(feed_id, "no-such-guid") == "Item not found"


def test_summarize_item_summarize_error_leaves_muted_row_and_log_entry(feed_id, monkeypatch):
    _seed_muted_item(feed_id, guid="g1")
    log_entry = {
        "kind": "topic", "title": "A muted title", "guid": "g1",
        "link": "https://example.com/muted", "reason": "topic: sport",
        "published_at": "2025-09-01T10:00:00+00:00",
    }
    _set_filtered_log(feed_id, [log_entry])

    def boom(*_a, **_k):
        raise SummarizeError("boom")

    monkeypatch.setattr(poll, "summarize", boom)

    error = poll.summarize_item(feed_id, "g1")

    assert error == "boom"
    row = items()[0]
    assert row["muted"] == 1
    assert row["headline"] is None
    feed = feed_row(feed_id)
    assert json.loads(feed["last_filtered"]) == [log_entry]
    assert feed["ads_filtered"] == 1


def test_summarize_item_summarize_error_leaves_ad_log_entry_and_no_row(feed_id, monkeypatch):
    log_entry = {
        "kind": "ad", "title": "Ad title", "guid": "ad-guid",
        "link": "https://example.com/ad", "reason": "ad: title",
        "published_at": "2025-08-30T10:00:00+00:00",
    }
    _set_filtered_log(feed_id, [log_entry])
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL TEXT " * 20, "ok", []))

    def boom(*_a, **_k):
        raise SummarizeError("boom")

    monkeypatch.setattr(poll, "summarize", boom)

    error = poll.summarize_item(feed_id, "ad-guid")

    assert error == "boom"
    assert items() == []
    feed = feed_row(feed_id)
    assert json.loads(feed["last_filtered"]) == [log_entry]
    assert feed["ads_filtered"] == 1


def test_summarize_item_missing_api_key(feed_id, monkeypatch):
    _seed_muted_item(feed_id, guid="g1")
    _set_filtered_log(feed_id, [{"kind": "topic", "title": "A muted title", "guid": "g1",
                                  "link": "https://example.com/muted", "reason": "topic: sport",
                                  "published_at": "2025-09-01T10:00:00+00:00"}])

    def boom(*_a, **_k):
        raise MissingApiKey("ANTHROPIC_API_KEY not set")

    monkeypatch.setattr(poll, "summarize", boom)

    error = poll.summarize_item(feed_id, "g1")

    assert error == "ANTHROPIC_API_KEY not set"
    assert poll._status.get(feed_id) is None  # popped in the finally, like poll_feed


def test_filtered_entry_returns_muted_row_as_topic_kind(feed_id):
    _seed_muted_item(feed_id, guid="g1", link="https://example.com/m1")
    entry = poll.filtered_entry(feed_id, "g1")
    assert entry == {
        "kind": "topic", "title": "A muted title", "guid": "g1", "link": "https://example.com/m1",
    }


def test_filtered_entry_returns_log_entry_when_no_row(feed_id):
    log_entry = {
        "kind": "ad", "title": "Ad title", "guid": "ad-guid",
        "link": "https://example.com/ad", "reason": "ad: title",
        "published_at": "2025-08-30T10:00:00+00:00",
    }
    _set_filtered_log(feed_id, [log_entry])
    assert poll.filtered_entry(feed_id, "ad-guid") == log_entry


def test_filtered_entry_none_when_neither_found(feed_id):
    assert poll.filtered_entry(feed_id, "nope") is None


def test_summarize_one_queues_job(monkeypatch):
    """Two clicks before the job runs collapse into a single execution, same shape
    as retry_one/poll_one."""
    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})
    monkeypatch.setattr(poll, "scheduler", scheduler)
    scheduler.start(paused=True)
    try:
        poll.summarize_one(1, "g1")
        poll.summarize_one(1, "g1")  # same job id replaces the pending one
        assert [job.id for job in scheduler.get_jobs()] == ["summarize-1-g1"]
        assert poll._status[1] == "Queued"
    finally:
        scheduler.shutdown(wait=False)
        poll._status.pop(1, None)


def test_summarize_one_job_logs_error_at_warning(monkeypatch, caplog):
    monkeypatch.setattr(poll, "summarize_item", lambda feed_id, guid: "Item not found")
    with caplog.at_level("WARNING"):
        poll._run_summarize_job(1, "g1")
    assert "Item not found" in caplog.text


# --- daily budget & feed_stats counters -----------------------------------------


def test_budget_already_reached_before_poll_logs_every_entry(feed_id, calls, monkeypatch):
    """A feed already at (or past) its daily budget skips every entry before any
    fetch, classify, or summarize call, logging each as a `kind: budget` entry."""
    set_feed(feed_id, daily_budget=2, classify_topics=1)
    with db() as conn:
        feedstats.bump(conn, feed_id, summaries=2)
    classify_calls = mock_classify(monkeypatch, "science")

    def boom_fetch(link):
        raise AssertionError("fetch_article should not be called when the budget is exhausted")

    monkeypatch.setattr(poll, "fetch_article", boom_fetch)

    assert poll.poll_feed(feed_id) is True

    assert calls == []
    assert classify_calls == []
    assert items() == []
    feed = feed_row(feed_id)
    assert feed["ads_filtered"] == 3
    entries = json.loads(feed["last_filtered"])
    expected_titles = [
        "First article about a rocket launch",
        "Second article about a merger",
        "Third article with almost no body text at all",
    ]
    assert [e["title"] for e in entries] == expected_titles
    for entry in entries:
        assert entry["kind"] == "budget"
        assert entry["reason"] == "budget: 2/day reached"
        assert entry["guid"] and entry["link"]
        assert set(entry) == {"kind", "title", "reason", "guid", "link", "published_at"}
    assert feed_stats_today(feed_id) == (2, 0)  # unchanged: nothing new was summarized


def test_budget_reached_mid_poll_summarizes_first_n_then_logs_rest(feed_id, calls, monkeypatch):
    set_feed(feed_id, daily_budget=2)

    assert poll.poll_feed(feed_id) is True

    assert len(calls) == 2
    rows = items()
    assert len(rows) == 2
    assert {row["original_title"] for row in rows} == {
        "First article about a rocket launch",
        "Second article about a merger",
    }
    feed = feed_row(feed_id)
    assert feed["ads_filtered"] == 1
    entries = json.loads(feed["last_filtered"])
    assert len(entries) == 1
    assert entries[0]["kind"] == "budget"
    assert entries[0]["title"] == "Third article with almost no body text at all"
    assert entries[0]["reason"] == "budget: 2/day reached"
    assert feed_stats_today(feed_id) == (2, 0)


def test_budget_skip_logged_once_per_poll(feed_id, calls, monkeypatch, caplog):
    set_feed(feed_id, daily_budget=1)
    with caplog.at_level("INFO"):
        assert poll.poll_feed(feed_id) is True
    assert caplog.text.count("daily budget 1 reached") == 1
    assert "skipping 2 entries" in caplog.text


def test_budget_zero_skips_every_entry(feed_id, calls, monkeypatch):
    set_feed(feed_id, daily_budget=0)

    assert poll.poll_feed(feed_id) is True

    assert calls == []
    assert items() == []
    feed = feed_row(feed_id)
    assert feed["ads_filtered"] == 3
    assert all(e["kind"] == "budget" for e in json.loads(feed["last_filtered"]))


def test_budget_skipped_entries_are_not_seen_and_are_reevaluated_next_poll(
    feed_id, calls, monkeypatch
):
    set_feed(feed_id, daily_budget=0)
    poll.poll_feed(feed_id)
    assert items() == []
    calls.clear()

    set_feed(feed_id, daily_budget=None)
    poll.poll_feed(feed_id)

    assert len(calls) == 3
    assert len(items()) == 3


def test_feed_stats_counts_summaries_and_classifications_when_classify_on(
    feed_id, calls, monkeypatch
):
    set_feed(feed_id, classify_topics=1)
    mock_classify(monkeypatch, "science")

    poll.poll_feed(feed_id)

    assert feed_stats_today(feed_id) == (3, 3)


def test_feed_stats_counts_only_summaries_when_classify_off(feed_id, calls):
    poll.poll_feed(feed_id)

    assert feed_stats_today(feed_id) == (3, 0)


def test_feed_stats_does_not_count_muted_rows_as_summaries(feed_id, calls, monkeypatch):
    set_feed(feed_id, classify_topics=1, mute_topics=json.dumps(["sport"]))
    mock_classify(monkeypatch, lambda title: "sport" if "rocket" in title else "economy")

    poll.poll_feed(feed_id)

    # All three entries are classified, but the muted one is never summarized.
    assert feed_stats_today(feed_id) == (2, 3)


def test_summarize_item_bumps_summaries_and_ignores_budget(feed_id, monkeypatch):
    set_feed(feed_id, daily_budget=0)
    _seed_muted_item(feed_id, guid="g1", link="https://example.com/m1", text="STORED TEXT")
    _set_filtered_log(
        feed_id,
        [
            {
                "kind": "topic",
                "title": "A muted title",
                "guid": "g1",
                "link": "https://example.com/m1",
                "reason": "topic: sport",
                "published_at": "2025-09-01T10:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(poll, "summarize", lambda *a, **k: ("New headline", "New summary"))

    error = poll.summarize_item(feed_id, "g1")

    assert error is None
    assert feed_stats_today(feed_id) == (1, 0)  # a budget of 0 never applies here


def test_retry_fallback_bumps_summaries_on_success(feed_id, monkeypatch):
    _seed_fallback_item(feed_id)
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
    monkeypatch.setattr(
        poll, "summarize", lambda text, title, url, respect_language=None, model=None: ("New", "New summary")
    )

    poll.retry_fallback(feed_id)

    assert feed_stats_today(feed_id) == (1, 0)


# --- feeds-page paywalled bucket -------------------------------------------------


def test_short_item_is_never_counted_as_paywalled(feed_id, calls, monkeypatch):
    """The feeds-page paywalled bucket only counts fetch_status IN ('teaser',
    'blocked'): a "short" item -- even with no login cookies at all, i.e.
    auth IS NULL -- must never land in it (GitHub issue #9, Option A)."""
    from pintxos.app import _bucket_sql

    monkeypatch.setattr(poll, "fetch_article", lambda link: ("A cartoon caption.", "short", []))

    assert poll.poll_feed(feed_id) is True

    with db() as conn:
        counts = conn.execute(
            f"SELECT {_bucket_sql('')} FROM items WHERE feed_id = ? AND muted = 0", (feed_id,)
        ).fetchone()
    assert counts["paywalled"] == 0
