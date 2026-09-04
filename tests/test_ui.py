"""Tests for the web UI: feed list/add/delete, poll now, settings."""

from __future__ import annotations

from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos.app import app
from pintxos.config import get_setting


def test_add_feed_appears_in_list(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        resp = c.post(
            "/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        page = c.get("/").text
        assert "/feeds/1.xml" in page


def test_add_feed_triggers_exactly_one_poll(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: calls.append(feed_id))
    with TestClient(app) as c:
        resp = c.post(
            "/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False
        )
        assert resp.status_code == 303
    assert calls == [1]


def test_add_feed_invalid_url_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        resp = c.post("/feeds", data={"url": "ftp://nope"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]


def test_add_feed_duplicate_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]

        page = c.get("/").text
        assert page.count("/feeds/1/poll") == 1  # only one feed row, not two


def test_delete_feed_removes_it(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/delete", follow_redirects=False)
        assert resp.status_code == 303

        page = c.get("/").text
        assert "/feeds/1.xml" not in page


def test_poll_now_returns_303(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/poll", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


def test_status_column_and_refresh(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)

        monkeypatch.setattr(app_module, "poll_status", {1: "Summarizing 2/5"})
        page = c.get("/").text
        assert "Summarizing 2/5" in page
        assert 'http-equiv="refresh"' in page
        poll_form_start = page.index('/feeds/1/poll')
        poll_form_end = page.index("</form>", poll_form_start)
        assert "disabled" in page[poll_form_start:poll_form_end]

        monkeypatch.setattr(app_module, "poll_status", {})
        page = c.get("/").text
        assert 'http-equiv="refresh"' not in page
        poll_form_start = page.index('/feeds/1/poll')
        poll_form_end = page.index("</form>", poll_form_start)
        assert "disabled" not in page[poll_form_start:poll_form_end]

        page = c.get("/?err=Oops").text
        assert 'class="flash"' in page
        assert "Oops" in page


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


def test_feed_table_uses_fixed_layout_and_wraps_long_urls(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
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


def test_health_returns_ok_and_feed_count(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "feeds": 1}


def test_base_shell_has_wordmark_favicon_and_github_link():
    with TestClient(app) as c:
        page = c.get("/").text

    assert "Pintxøs" in page
    assert 'rel="icon"' in page
    assert "github.com/janw76/pintxos" in page
    assert "ui-sans-serif" in page
