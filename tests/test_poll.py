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
SAMPLE_WITH_AD = (Path(__file__).parent / "fixtures" / "sample_with_ad.xml").read_bytes()


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


@pytest.fixture
def calls_with_ad(monkeypatch):
    """Like `calls`, but serves a feed whose third entry is tagged as a coupon ad."""
    seen: list[tuple[str, str, str]] = []

    def fake_get(url):
        if url == FEED_URL:
            return FakeResponse(SAMPLE_WITH_AD)
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


def test_ad_entry_filtered_before_summarize(feed_id, calls_with_ad, monkeypatch):
    """The coupon entry never reaches fetch/summarize and is never stored."""
    seen_status = []
    fake_summarize = poll.summarize

    def spy(text, original_title, url):
        seen_status.append(poll._status.get(feed_id))
        return fake_summarize(text, original_title, url)

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


def test_invalid_extra_ad_pattern_logs_warning_and_keeps_builtin_rules(
    feed_id, calls_with_ad, monkeypatch, caplog
):
    monkeypatch.setenv("PINTXOS_AD_TITLE_PATTERNS", "(")
    with caplog.at_level("WARNING"):
        poll.poll_feed(feed_id)
    assert "invalid PINTXOS_AD_TITLE_PATTERNS" in caplog.text
    assert len(calls_with_ad) == 2
    assert "https://example.com/coupons" not in [row["link"] for row in items()]


def test_second_poll_re_evaluates_ad_and_does_not_store_it(feed_id, calls_with_ad, caplog):
    poll.poll_feed(feed_id)
    calls_with_ad.clear()
    with caplog.at_level("INFO"):
        poll.poll_feed(feed_id)
    assert len(calls_with_ad) == 0
    assert "filtered 1 ad entries" in caplog.text
    assert len(items()) == 2


def test_extra_pattern_filters_entry_not_caught_by_builtin_rules(feed_id, monkeypatch):
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

    def fake_summarize(text, original_title, url):
        seen.append(original_title)
        return "HEADLINE", f"summary of {original_title}"

    monkeypatch.setattr(poll, "_get", fake_get)
    monkeypatch.setattr(poll, "fetch_article", lambda link: None)
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


def test_purge_stored_ads_removes_pre_filter_coupon_rows_on_poll(feed_id, calls, caplog):
    _seed_item(
        feed_id, "old-ad-1", "https://example.com/deals/acme-promo-code/",
        "Acme Promo Codes: 40% Off",
    )
    _seed_item(
        feed_id, "old-ad-2", "https://example.com/deals/foo-coupon/", "Foo Coupons",
    )
    _seed_item(
        feed_id, "old-real", "https://example.com/story/real-news/", "Real News",
    )

    with caplog.at_level("INFO"):
        poll.poll_feed(feed_id)

    guids = {row["guid"] for row in items()}
    assert "old-ad-1" not in guids
    assert "old-ad-2" not in guids
    assert "old-real" in guids
    assert "purged 2 stored ad items" in caplog.text


def test_purge_stored_ads_is_idempotent(feed_id, calls):
    _seed_item(
        feed_id, "old-ad-1", "https://example.com/deals/acme-promo-code/",
        "Acme Promo Codes: 40% Off",
    )
    _seed_item(
        feed_id, "old-ad-2", "https://example.com/deals/foo-coupon/", "Foo Coupons",
    )
    poll.poll_feed(feed_id)
    assert poll.purge_stored_ads(feed_id) == 0


def test_purge_stored_ads_skipped_when_filter_disabled(feed_id, calls, monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "0")
    _seed_item(
        feed_id, "old-ad-1", "https://example.com/deals/acme-promo-code/",
        "Acme Promo Codes: 40% Off",
    )
    _seed_item(
        feed_id, "old-ad-2", "https://example.com/deals/foo-coupon/", "Foo Coupons",
    )
    poll.poll_feed(feed_id)
    guids = {row["guid"] for row in items()}
    assert "old-ad-1" in guids
    assert "old-ad-2" in guids


def test_purge_stored_ads_on_feed_with_no_items_returns_zero(feed_id):
    assert poll.purge_stored_ads(feed_id) == 0
