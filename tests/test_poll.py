"""Poller tests: everything network- and LLM-facing is monkeypatched."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from pintxos import poll
from pintxos.db import db, now
from pintxos.summarize import MissingApiKey, SummarizeError

FEED_URL = "https://example.com/feed.xml"
SAMPLE = (Path(__file__).parent / "fixtures" / "sample.xml").read_bytes()


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200, content_type="application/xml"):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}

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

    def fake_summarize(text, original_title, url):
        seen.append((text, original_title, url))
        return f"HEADLINE {len(seen)}", f"summary of {original_title}"

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: None)
    monkeypatch.setattr(poll, "summarize", fake_summarize)
    return seen


def items():
    with db() as conn:
        return conn.execute("SELECT * FROM items ORDER BY published_at DESC").fetchall()


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
    monkeypatch.setattr(poll, "fetch_article", lambda link: "FULL ARTICLE TEXT " * 20)
    poll.poll_all()
    assert all(row["fallback"] == 0 for row in items())
    assert all(text.startswith("FULL ARTICLE TEXT") for text, _t, _u in calls)


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


def test_summarize_error_skips_only_that_item(feed_id, calls, monkeypatch):
    def flaky(text, original_title, url):
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


def test_status_cleared_after_poll(feed_id, calls, monkeypatch):
    seen_status = []
    real_summarize = poll.summarize

    def spy(text, original_title, url):
        seen_status.append(poll._status.get(feed_id))
        return real_summarize(text, original_title, url)

    monkeypatch.setattr(poll, "summarize", spy)
    assert poll.poll_feed(feed_id) is True
    assert feed_id not in poll._status
    assert seen_status and all(s.startswith("Summarizing") for s in seen_status)
    assert seen_status[0] == "Summarizing 1/3"


def test_fetch_article_extracts_html(monkeypatch):
    html = "<html><body><article><p>" + "Real body sentence. " * 30 + "</p></article></body></html>"
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(html.encode(), content_type="text/html; charset=utf-8"))
    assert "Real body sentence." in poll.fetch_article("https://example.com/one")


def test_fetch_article_rejects_non_html(monkeypatch):
    monkeypatch.setattr(poll, "_get", lambda url: FakeResponse(b"{}", content_type="application/json"))
    assert poll.fetch_article("https://example.com/one") is None


def test_ui_can_write_while_polling(feed_id, calls, monkeypatch):
    """A second writer (the web UI) must not hit 'database is locked' mid-poll."""
    import sqlite3

    from pintxos.db import connect

    def summarize_and_write(text, original_title, url):
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
