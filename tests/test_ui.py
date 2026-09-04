"""Tests for the web UI: feed list/add/delete, poll now, settings."""

from __future__ import annotations

from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos.app import app
from pintxos.config import get_setting
from pintxos.db import db


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


def test_settings_page_shows_ad_filter_defaults():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert 'name="filter_ads"' in page
    assert 'name="filter_ads" value="1" checked' not in page
    assert '<textarea id="ad_title_patterns" name="ad_title_patterns" rows="4" class="mono" ></textarea>' in page
    assert "Set by PINTXOS_FILTER_ADS" not in page


def test_settings_post_without_filter_ads_stores_off():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert get_setting("PINTXOS_FILTER_ADS") == "0"
    assert 'name="filter_ads" value="1" checked' not in page


def test_settings_post_with_filter_ads_stores_on():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "filter_ads": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert get_setting("PINTXOS_FILTER_ADS") == "1"
    assert 'name="filter_ads" value="1" checked' in page


def test_settings_post_invalid_ad_pattern_rejected_and_nothing_saved():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "15",
                "items_per_feed": "10",
                "api_key": "",
                "ad_title_patterns": "ok\n(\n",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/settings?err=")

        page = c.get(location).text
        assert "Invalid pattern" in page
        assert "line 2" in page

    # nothing saved: poll_minutes still at its built-in default
    assert get_setting("PINTXOS_POLL_MINUTES") == "30"


def test_settings_post_ad_patterns_roundtrip():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "ad_title_patterns": "best .* deals\nfree shipping",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert "best .* deals" in page
    assert "free shipping" in page
    assert get_setting("PINTXOS_AD_TITLE_PATTERNS") == "best .* deals\nfree shipping"


def test_settings_filter_ads_env_pinned_disables_control_and_ignores_submission(monkeypatch):
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "0")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert 'name="filter_ads" value="1"  disabled' in page
        assert 'name="filter_ads" value="1" checked' not in page
        assert "Set by PINTXOS_FILTER_ADS in the environment." in page

        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "filter_ads": "1",
            },
            follow_redirects=False,
        )

    monkeypatch.delenv("PINTXOS_FILTER_ADS")
    from pintxos.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("PINTXOS_FILTER_ADS",)
        ).fetchone()
    assert row is None


def test_index_ads_skipped_cell_is_empty_without_a_count(monkeypatch):
    """ads_filtered defaults to 0, so nothing renders in the ads-skipped cell."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    assert "<td class=\"nowrap\">" in page  # the items cell rendered
    assert "skipped" not in page


def test_index_shows_ads_skipped_count(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute(
                "UPDATE feeds SET ads_filtered = 1 WHERE url = ?",
                ("https://example.com/feed.xml",),
            )
        page = c.get("/").text

    assert "1 ad skipped" in page


def test_empty_env_var_does_not_pin_filter_setting(monkeypatch):
    # An empty PINTXOS_FILTER_ADS (e.g. from an undefined compose variable) is
    # "unset" to get_setting, so the UI must treat it as unset too.
    monkeypatch.setenv("PINTXOS_FILTER_ADS", "")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "Set by PINTXOS_FILTER_ADS" not in page
        c.post(
            "/settings",
            data={"model": "m", "poll_minutes": "30", "items_per_feed": "50",
                  "api_key": "", "filter_ads": "1", "ad_title_patterns": ""},
            follow_redirects=False,
        )
    assert get_setting("PINTXOS_FILTER_ADS") == "1"


def test_index_pluralizes_ads_skipped(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute("UPDATE feeds SET ads_filtered = 2 WHERE id = 1")
        assert "2 ads skipped" in c.get("/").text


def test_flash_banner_renders_once_and_url_is_cleaned_client_side(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        page = c.get("/?msg=Saved").text
        assert 'class="msg"' in page and "Saved" in page
        assert "history.replaceState" in page
        clean = c.get("/").text
        assert 'class="msg"' not in clean


def test_delete_feed_drops_queued_manual_poll_and_status():
    import pintxos.poll as poll

    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        # scheduler is not started under PINTXOS_NO_SCHEDULER, so the job stays pending
        assert poll.scheduler.get_job("feed-1") is not None
        assert poll._status.get(1) == "Queued"
        resp = c.post("/feeds/1/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert poll.scheduler.get_job("feed-1") is None
        assert 1 not in poll._status
        assert "Queued" not in c.get("/").text
