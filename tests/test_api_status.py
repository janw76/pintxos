"""Tests for GET /api/status and the engine wiring in app.py."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos.app import app


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_poll_now_dedup_and_status_shows_fetching(monkeypatch):
    release = threading.Event()

    def _blocking_poll(feed_id, reporter):
        reporter.fetching(feed_id)
        release.wait(timeout=5)
        reporter.finished(feed_id, 0, 0)
        return True

    monkeypatch.setattr(app_module.engine, "_poll_fn", _blocking_poll)

    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)

        resp1 = c.post("/feeds/1/poll", follow_redirects=False)
        assert resp1.status_code == 303

        # Wait for the worker to actually pick it up (state -> fetching).
        assert _wait_until(
            lambda: app_module.engine.snapshot()["feeds"].get(1, {}).get("state") == "fetching"
        )

        resp2 = c.post("/feeds/1/poll", follow_redirects=False)
        assert resp2.status_code == 303

        status = c.get("/api/status").json()
        feed = next(f for f in status["feeds"] if f["id"] == 1)
        assert feed["state"] == "fetching"
        assert 1 not in status["engine"]["queue"]

        release.set()

        assert _wait_until(
            lambda: c.get("/api/status").json()["feeds"][0]["state"] == "idle"
        )


def test_status_shape_and_idle_defaults(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.get("/api/status")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert set(body.keys()) == {"engine", "feeds"}
    assert set(body["engine"].keys()) == {
        "running",
        "current",
        "queue",
        "paused_reason",
        "last_run_started_at",
        "last_run_finished_at",
        "next_run_at",
    }
    assert len(body["feeds"]) == 1
    feed = body["feeds"][0]
    assert feed["state"] == "idle"
    assert feed["progress"] is None
    assert feed["item_count"] == 0
    assert feed["last_polled_ago"] == "never"


def test_poll_now_redirect_location_is_root(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/poll", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_delete_feed_clears_status_and_engine_state(quiet_engine):
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post("/feeds/1/delete", follow_redirects=False)
        status = c.get("/api/status").json()

    assert status["feeds"] == []
    assert 1 not in app_module.engine.snapshot()["feeds"]


def test_engine_worker_thread_joined_on_shutdown():
    before = threading.active_count()
    with TestClient(app):
        pass
    after = threading.active_count()
    assert after == before


def test_save_settings_self_heals_paused_engine(monkeypatch):
    release = threading.Event()

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

        # Now switch to a quiet poll_fn so the self-heal re-enqueue completes cleanly.
        def _quiet_poll(feed_id, reporter):
            reporter.finished(feed_id, 0, 0)
            return True

        monkeypatch.setattr(app_module.engine, "_poll_fn", _quiet_poll)

        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-newly-submitted-key",
            },
            follow_redirects=False,
        )

        assert _wait_until(
            lambda: app_module.engine.snapshot()["paused_reason"] is None
        )
