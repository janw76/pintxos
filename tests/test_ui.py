"""Tests for the web UI: feed list/add/delete, poll now, settings."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pintxos.app import app, status_label
from pintxos.config import get_setting


def test_add_feed_appears_in_list(quiet_engine):
    with TestClient(app) as c:
        resp = c.post(
            "/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        page = c.get("/").text
        assert "/feeds/1.xml" in page


def test_add_feed_invalid_url_rejected(quiet_engine):
    with TestClient(app) as c:
        resp = c.post("/feeds", data={"url": "ftp://nope"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]


def test_add_feed_duplicate_rejected(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]

        page = c.get("/").text
        assert page.count("/feeds/1/poll") == 1  # only one feed row, not two


def test_delete_feed_removes_it(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/delete", follow_redirects=False)
        assert resp.status_code == 303

        page = c.get("/").text
        assert "/feeds/1.xml" not in page


def test_poll_now_returns_303(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/poll", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


def test_settings_post_persists():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "claude-haiku-4-5-20251001",
                "poll_minutes": "15",
                "items_per_feed": "10",
                "api_key": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    assert get_setting("PINTXOS_POLL_MINUTES") == "15"
    assert get_setting("PINTXOS_ITEMS_PER_FEED") == "10"


def test_settings_post_invalid_interval_rejected():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "0",
                "items_per_feed": "10",
                "api_key": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]

    # unchanged from default
    assert get_setting("PINTXOS_POLL_MINUTES") == "30"


def test_settings_api_key_stored_and_masked():
    with TestClient(app) as c:
        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-supersecretvalue1234",
            },
            follow_redirects=False,
        )
        page = c.get("/settings").text

    assert "sk-supersecretvalue1234" not in page
    assert "1234" in page


def test_settings_env_key_set_shows_env_message_and_ignores_submission(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-envkey")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "environment variable" in page

        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-submitted-should-not-be-stored",
            },
            follow_redirects=False,
        )

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    from pintxos.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("ANTHROPIC_API_KEY",)
        ).fetchone()
    assert row is None


def test_feed_table_uses_fixed_layout_and_wraps_long_urls(quiet_engine):
    long_url = "https://example.com/" + "a" * 100 + "/feed.xml"
    with TestClient(app) as c:
        resp = c.post("/feeds", data={"url": long_url}, follow_redirects=False)
        assert resp.status_code == 303

        resp = c.get("/")
        assert resp.status_code == 200
        page = resp.text

    assert "table-layout: fixed" in page
    assert "<colgroup>" in page
    assert long_url in page


def test_base_shell_has_wordmark_favicon_and_github_link():
    with TestClient(app) as c:
        page = c.get("/").text

    assert "Pintxøs" in page
    assert 'rel="icon"' in page
    assert "github.com/janw76/pintxos" in page
    assert "ui-sans-serif" in page


def test_status_label_idle_is_blank():
    assert status_label("idle", None) == ""


def test_status_label_queued():
    assert status_label("queued", None) == "Queued"


def test_status_label_fetching():
    assert status_label("fetching", None) == "Fetching feed…"


def test_status_label_summarizing_with_progress():
    assert status_label("summarizing", {"done": 2, "total": 5}) == "Summarizing 2/5"


def test_status_label_summarizing_without_progress():
    assert status_label("summarizing", None) == "Summarizing"


def test_status_label_error():
    assert status_label("error", None) == "Error"


def test_status_label_unknown_state_returns_raw():
    assert status_label("something-weird", None) == "something-weird"


def test_index_has_status_column_and_seven_cols(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    assert "<th>Status</th>" in page
    assert page.count("<col ") == 7
    assert 'data-feed-id="1"' in page
    assert 'data-field="items"' in page
    assert 'data-field="status"' in page
    assert 'data-field="last_polled"' in page
    assert 'data-field="last_error"' in page
    assert 'data-field="poll-btn"' in page


def test_index_shows_live_summarizing_status_and_disables_poll_button(monkeypatch):
    import threading
    import time

    import pintxos.app as app_module

    release = threading.Event()

    def _wait_until(predicate, timeout=2.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def _blocking_poll(feed_id, reporter):
        reporter.fetching(feed_id)
        reporter.summarizing(feed_id, 2, 5)
        release.wait(timeout=5)
        reporter.finished(feed_id, 0, 0)
        return True

    monkeypatch.setattr(app_module.engine, "_poll_fn", _blocking_poll)

    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post("/feeds/1/poll", follow_redirects=False)

        assert _wait_until(
            lambda: app_module.engine.snapshot()["feeds"].get(1, {}).get("state")
            == "summarizing"
        )

        page = c.get("/").text
        assert "Summarizing 2/5" in page
        assert "disabled" in page

        release.set()

        assert _wait_until(
            lambda: app_module.engine.snapshot()["feeds"].get(1, {}).get("state") == "idle"
        )

        page = c.get("/").text

    # After finishing, the status cell for feed 1 is empty and the button enabled.
    import re

    row_match = re.search(r'<tr data-feed-id="1">.*?</tr>', page, re.DOTALL)
    assert row_match is not None
    row_html = row_match.group(0)
    status_match = re.search(r'data-field="status"[^>]*>(.*?)</td>', row_html, re.DOTALL)
    assert status_match is not None
    assert status_match.group(1).strip() == ""
    assert "disabled" not in row_html


def test_index_paused_banner_shown_when_engine_paused(monkeypatch):
    import threading
    import time

    import pintxos.app as app_module

    release = threading.Event()

    def _wait_until(predicate, timeout=2.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def _api_key_missing_poll(feed_id, reporter):
        reporter.fetching(feed_id)
        release.wait(timeout=5)
        reporter.api_key_missing(feed_id)
        return False

    monkeypatch.setattr(app_module.engine, "_poll_fn", _api_key_missing_poll)

    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post("/feeds/1/poll", follow_redirects=False)

        assert _wait_until(
            lambda: app_module.engine.snapshot()["feeds"].get(1, {}).get("state") == "fetching"
        )
        release.set()

        assert _wait_until(lambda: app_module.engine.snapshot()["paused_reason"] is not None)

        page = c.get("/").text

    assert "Polling is paused" in page
    assert 'href="/settings"' in page


def test_base_script_has_live_status_wiring():
    with TestClient(app) as c:
        page = c.get("/").text

    assert "replaceState" in page
    assert "/api/status" in page
    assert "visibilitychange" in page
