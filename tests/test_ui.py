"""Tests for the web UI: feed list/add/delete, poll now, settings."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos import poll
from pintxos.app import app
from pintxos.config import data_dir, get_setting
from pintxos.cookies import cookie_path
from pintxos.db import db, now as db_now

from conftest import FUTURE_EXPIRY, write_cookies


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


def test_poll_now_via_fetch_returns_204(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: calls.append(feed_id))
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1/poll",
            headers={"X-Requested-With": "fetch"},
            follow_redirects=False,
        )
        assert resp.status_code == 204
        assert resp.text == ""
        assert calls == [1, 1]  # once from adding the feed, once from "Poll now"


def test_status_endpoint_returns_poll_status(monkeypatch):
    monkeypatch.setattr(app_module, "poll_status", {1: "Summarizing 2/5"})
    with TestClient(app) as c:
        resp = c.get("/status")
        assert resp.status_code == 200
        assert resp.json() == {"1": "Summarizing 2/5"}


def _poll_button(page: str) -> str:
    """The <button …>…</button> inside the /feeds/1/poll form."""
    start = page.index("/feeds/1/poll")
    end = page.index("</form>", start)
    return page[start:end]


def test_poll_button_carries_poll_state_and_refresh(monkeypatch):
    """No Status column: the poll button itself reads Polling… (disabled, progress in the
    tooltip, page polls /status in place), Failed (danger tint, error in the tooltip, still
    clickable) or Poll now (idle)."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)

        # active: the status text moves into the button's tooltip
        monkeypatch.setattr(app_module, "poll_status", {1: "Summarizing 2/5"})
        page = c.get("/").text
        assert 'http-equiv="refresh"' not in page
        assert "location.reload" not in page
        assert 'fetch("/status")' in page
        btn = _poll_button(page)
        assert ">Polling…<" in btn
        assert "disabled" in btn
        assert 'title="Summarizing 2/5"' in btn
        assert "btn-polling" in btn
        assert "btn-danger" not in btn
        assert "<th>Status</th>" not in page

        # idle
        monkeypatch.setattr(app_module, "poll_status", {})
        page = c.get("/").text
        assert 'http-equiv="refresh"' not in page
        assert "location.reload" not in page
        assert 'fetch("/status")' in page
        btn = _poll_button(page)
        assert ">Poll now<" in btn
        assert "disabled" not in btn
        assert "title=" not in btn
        assert "btn-tint-neutral" in btn
        assert "btn-polling" not in btn

        # failed: retry stays enabled, error text is the tooltip (escaped for the attribute)
        with db() as conn:
            conn.execute('UPDATE feeds SET last_error = ? WHERE id = 1', ('HTTP 500: "boom"',))
        page = c.get("/").text
        btn = _poll_button(page)
        assert ">Failed<" in btn
        assert "disabled" not in btn
        assert "btn-danger" in btn
        assert 'title="HTTP 500: &#34;boom&#34;"' in btn
        assert page.count("/feeds/1/poll") == 1

        # active wins over a stale last_error
        monkeypatch.setattr(app_module, "poll_status", {1: "Fetching"})
        btn = _poll_button(c.get("/").text)
        assert ">Polling…<" in btn and 'title="Fetching"' in btn and "btn-danger" not in btn

        page = c.get("/?err=Oops").text
        assert 'class="flash"' in page
        assert "Oops" in page


def test_poll_button_pulse_css_and_reduced_motion():
    with TestClient(app) as c:
        page = c.get("/").text
    assert "@keyframes pintxos-pulse" in page
    assert "animation: pintxos-pulse 1.6s ease-in-out infinite" in page
    assert "@media (prefers-reduced-motion: reduce) { .btn-polling, .btn-polling:hover { animation: none; } }" in page
    assert "td.actions .btn-poll { min-width: 5.5rem; }" in page
    assert "button:disabled { opacity: .7; cursor: default; }" in page


def test_feed_row_endpoint_returns_just_the_row(monkeypatch):
    """GET /feeds/{id}/row renders the shared _feed_row.html partial: one bare <tr>,
    no base layout, so the page can swap a single row in place."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setattr(app_module, "poll_status", {})
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.get("/feeds/1/row")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text.strip()
    assert body.startswith("<tr")
    assert body.endswith("</tr>")
    assert "<html" not in body and "<table" not in body
    assert 'id="feed-1"' in body
    assert "/feeds/1.xml" in body
    assert ">Poll now<" in body


def test_feed_row_endpoint_404_for_unknown_feed(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        assert c.get("/feeds/999/row").status_code == 404


def test_feed_row_endpoint_reflects_poll_status(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        monkeypatch.setattr(app_module, "poll_status", {1: "Summarizing 1/2"})
        body = c.get("/feeds/1/row").text

    btn = _poll_button(body)
    assert ">Polling…<" in btn
    assert "disabled" in btn
    assert 'title="Summarizing 1/2"' in btn
    assert 'data-feed-id="1"' in btn


def test_feed_row_endpoint_matches_the_row_on_the_index(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setattr(app_module, "poll_status", {})
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text
        row = c.get("/feeds/1/row").text.strip()

    start = page.index('<tr id="feed-1"')
    end = page.index("</tr>", start) + len("</tr>")
    assert page[start:end] == row


def test_last_error_column_shows_dash_or_message(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text
        assert "<th>Last error</th>" in page
        assert '<td class="error"><span class="muted">-</span></td>' in page

        with db() as conn:
            conn.execute("UPDATE feeds SET last_error = ? WHERE id = 1", ("timed out",))
        page = c.get("/").text
        assert '<td class="error">timed out</td>' in page
        assert '<span class="muted">-</span>' not in page


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


def test_settings_post_invalid_keep_pattern_rejected_and_nothing_saved():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "15",
                "items_per_feed": "10",
                "api_key": "",
                "ad_keep_patterns": "ok\n(\n",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/settings?err=")

        page = c.get(location).text
        assert "Invalid keep pattern" in page
        assert "line 2" in page

    # nothing saved: poll_minutes still at its built-in default
    assert get_setting("PINTXOS_POLL_MINUTES") == "30"


def test_settings_post_keep_patterns_roundtrip():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "ad_keep_patterns": "fraud\nnot a scam",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert "fraud" in page
    assert "not a scam" in page
    assert get_setting("PINTXOS_AD_KEEP_PATTERNS") == "fraud\nnot a scam"


def test_settings_keep_patterns_env_pinned_disables_control_and_ignores_submission(monkeypatch):
    monkeypatch.setenv("PINTXOS_AD_KEEP_PATTERNS", "fraud")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert 'id="ad_keep_patterns" name="ad_keep_patterns" rows="4" class="mono" disabled>' in page
        assert "Set by PINTXOS_AD_KEEP_PATTERNS in the environment." in page

        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "ad_keep_patterns": "should not be saved",
            },
            follow_redirects=False,
        )

    monkeypatch.delenv("PINTXOS_AD_KEEP_PATTERNS")
    from pintxos.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("PINTXOS_AD_KEEP_PATTERNS",)
        ).fetchone()
    assert row is None


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

    assert "<div>0</div>" in page  # the items cell rendered
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


def test_feed_edit_page_shows_radios_and_global_patterns_box(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "ad_title_patterns": "black friday\n\\bgiveaway\\b",
            },
            follow_redirects=False,
        )
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    # six radios: three for filter_ads, three for ad_patterns_mode
    assert page.count('type="radio"') == 6
    assert 'name="filter_ads"' in page
    assert 'name="ad_patterns_mode"' in page
    assert 'name="ad_title_patterns"' in page
    # unsaved feed defaults to "inherit" (value="") for both groups
    assert 'name="filter_ads" value="" checked' in page
    assert 'name="filter_ads" value="1" checked' not in page
    assert 'name="filter_ads" value="0" checked' not in page
    assert 'name="ad_patterns_mode" value="" checked' in page
    assert 'name="ad_patterns_mode" value="1" checked' not in page
    assert 'name="ad_patterns_mode" value="0" checked' not in page
    # read-only global patterns box shows the global text
    assert 'class="mono global-box"' in page
    assert "black friday" in page
    assert "\\bgiveaway\\b" in page


def test_feed_edit_post_off_and_patterns_saved(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "0",
                "ad_patterns_mode": "1",
                "ad_title_patterns": "giveaway",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?msg=Saved"

        with db() as conn:
            row = conn.execute(
                "SELECT filter_ads, ad_patterns_mode, ad_title_patterns FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["filter_ads"] == 0
        assert row["ad_patterns_mode"] == 1
        assert row["ad_title_patterns"] == "giveaway"

        # the edit page reflects what was just saved
        page = c.get("/feeds/1").text
        assert page.count('type="radio"') == 6
        assert 'name="filter_ads" value="0" checked' in page
        assert 'name="filter_ads" value="" checked' not in page
        assert 'name="ad_patterns_mode" value="1" checked' in page
        assert 'name="ad_patterns_mode" value="" checked' not in page
        assert "giveaway" in page

        # no pre-normalisation: the raw text is stored verbatim
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "1",
                "ad_patterns_mode": "1",
                "ad_title_patterns": "\n\n  foo  \n\n  bar\n\n",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with db() as conn:
            row = conn.execute(
                "SELECT ad_title_patterns FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["ad_title_patterns"] == "\n\n  foo  \n\n  bar\n\n"


def test_feed_edit_post_patterns_mode_off_stores_zero(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={"filter_ads": "", "ad_patterns_mode": "0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    with db() as conn:
        row = conn.execute("SELECT ad_patterns_mode FROM feeds WHERE id = 1").fetchone()
    assert row["ad_patterns_mode"] == 0


def test_feed_edit_post_invalid_regex_rejected_and_unchanged(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={"filter_ads": "1", "ad_patterns_mode": "1", "ad_title_patterns": "ok\n(\n"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/feeds/1?err=")

    with db() as conn:
        row = conn.execute(
            "SELECT filter_ads, ad_title_patterns FROM feeds WHERE id = 1"
        ).fetchone()
    assert row["filter_ads"] is None
    assert row["ad_title_patterns"] is None


def test_feed_edit_post_inherit_stores_null(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post(
            "/feeds/1",
            data={
                "filter_ads": "0",
                "ad_patterns_mode": "1",
                "ad_title_patterns": "giveaway",
            },
            follow_redirects=False,
        )
        # explicit empty ad_title_patterns (field submitted, but blank) clears it
        resp = c.post(
            "/feeds/1",
            data={"filter_ads": "", "ad_patterns_mode": "", "ad_title_patterns": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    with db() as conn:
        row = conn.execute(
            "SELECT filter_ads, ad_patterns_mode, ad_title_patterns FROM feeds WHERE id = 1"
        ).fetchone()
    assert row["filter_ads"] is None
    assert row["ad_patterns_mode"] is None
    assert row["ad_title_patterns"] is None


def test_feed_edit_page_404_for_unknown_feed():
    with TestClient(app) as c:
        resp = c.get("/feeds/999")
    assert resp.status_code == 404


def test_feed_edit_post_404_for_unknown_feed():
    with TestClient(app) as c:
        resp = c.post(
            "/feeds/999",
            data={"filter_ads": "1", "ad_patterns_mode": "1", "ad_title_patterns": ""},
            follow_redirects=False,
        )
    assert resp.status_code == 404
    assert "location" not in resp.headers


def test_index_has_edit_filters_link_to_feed(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text
    assert 'action="/feeds/1"' in page
    assert "Edit filters" in page
    assert page.count("/feeds/1/poll") == 1


def test_feed_edit_post_unknown_choice_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={"filter_ads": "maybe", "ad_patterns_mode": "", "ad_title_patterns": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and resp.headers["location"].startswith("/feeds/1?err=")
        with db() as conn:
            assert conn.execute("SELECT filter_ads FROM feeds WHERE id = 1").fetchone()[0] is None


def test_index_copy_has_own_column_and_actions_stay_on_one_line(monkeypatch):
    """Copy sits in its own cell right after Output URL; the actions cell holds exactly
    Edit filters, Poll now, Delete in that order and is styled never to wrap."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    # Copy cell immediately follows the output-url cell (only whitespace between them).
    url_start = page.index('<td><span class="output-url">')
    url_end = page.index("</td>", url_start) + len("</td>")
    copy_start = page.index('<td class="copy">')
    assert page[url_end:copy_start].strip() == ""
    copy_cell = page[copy_start : page.index("</td>", copy_start)]
    assert "pintxosCopy(this, " in copy_cell
    assert ">Copy<" in copy_cell

    # Actions cell: exactly the three buttons, in order, no Copy, no wrapping container.
    start = page.index('<td class="actions">')
    cell = page[start : page.index("</td>", start)]
    assert cell.count("<button") == 3
    assert ">Copy<" not in cell and "pintxosCopy" not in cell
    assert "actions-row" not in page
    order = [cell.index(label) for label in (">Edit filters<", ">Poll now<", ">Delete<")]
    assert order == sorted(order)
    assert page.count("/feeds/1/poll") == 1

    # String guard: the rule that keeps the three buttons on one line.
    assert "td.actions { white-space: nowrap;" in page


def test_index_colgroup_widths_sum_to_100_percent(monkeypatch):
    """Seven fixed columns budgeted to fit 968px (1000px viewport) without scroll."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    widths = [int(w) for w in re.findall(r'<col style="width: (\d+)%">', page)]
    assert len(widths) == 7
    assert sum(widths) == 100
    assert page.count("<th>") == 7
    assert "<th>Status</th>" not in page
    assert "<th>Last error</th>" in page


def test_index_empty_state_colspan_matches_columns():
    with TestClient(app) as c:
        page = c.get("/").text
    assert '<td colspan="7" class="empty">' in page


def test_feed_edit_radios_keep_their_controls(monkeypatch):
    """The 100%-width field rule must not reach radios, or the controls collapse."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    assert page.count('type="radio"') == 6
    assert ".field input, .field textarea { width: 100%; }" not in page
    assert "accent-color: var(--accent)" in page


def test_feed_edit_page_says_global_linked_to_settings(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text
        assert page.count('<a href="/settings">Global</a>') == 2
        assert "Inherit" not in page


def test_feed_edit_page_shows_filtered_title_and_reason(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute(
                "UPDATE feeds SET last_polled_at = ?, last_filtered = ? WHERE id = 1",
                (
                    "2026-09-04T00:00:00+00:00",
                    json.dumps([{"title": "Groupon Promo Codes: 60% Off", "reason": "tag:coupons"}]),
                ),
            )
        page = c.get("/feeds/1").text
    assert "Groupon Promo Codes: 60% Off" in page
    assert "tag:coupons" in page
    assert "Nothing filtered at last poll" not in page


def test_feed_edit_page_shows_empty_state_when_last_filtered_null(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute(
                "UPDATE feeds SET last_polled_at = ? WHERE id = 1",
                ("2026-09-04T00:00:00+00:00",),
            )
        page = c.get("/feeds/1").text
    assert "Nothing filtered at last poll" in page


def test_feed_edit_page_not_polled_yet_shows_placeholder(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text
    assert "Not polled yet" in page


def test_feed_edit_page_malformed_last_filtered_does_not_500(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute(
                "UPDATE feeds SET last_polled_at = ?, last_filtered = ? WHERE id = 1",
                ("2026-09-04T00:00:00+00:00", "not json"),
            )
        resp = c.get("/feeds/1")
    assert resp.status_code == 200
    assert "Nothing filtered at last poll" in resp.text


def test_settings_page_no_cookies_file_shows_placeholder():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert "No cookies.txt found." in page
    assert 'action="/settings/cookies/delete"' not in page


def test_settings_page_lists_cookie_domains_expiry_and_expiring_soon():
    soon_expiry = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    write_cookies(
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n"
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tuid\tdef\n"
        f".economist.com\tTRUE\t/\tFALSE\t{soon_expiry}\tsid\tghi\n"
    )
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert ".ft.com" in page
    assert ".economist.com" in page
    assert "2100-01-01" in page
    # Domain counts appear in the muted summary line.
    assert re.search(r"\.ft\.com — 2 cookies", page)
    assert re.search(r"\.economist\.com — 1 cookie[^s]", page)
    assert "expires soon" in page  # only .economist.com's near-term expiry trips this


def test_settings_expired_cookies_show_remove_button_and_delete_clears_them():
    past_expiry = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    write_cookies(f".ft.com\tTRUE\t/\tFALSE\t{past_expiry}\tsid\tabc\n")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "no usable cookies" in page
        assert 'action="/settings/cookies/delete"' in page

        resp = c.post("/settings/cookies/delete", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 308)

        assert not (data_dir() / "cookies.txt").exists()

        page = c.get("/settings").text

    assert "No cookies.txt found." in page


def test_settings_cookies_section_is_outside_the_settings_form():
    write_cookies(f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n")
    with TestClient(app) as c:
        page = c.get("/settings").text

    form_start = page.index('action="/settings"')
    form_close = page.index("</form>", form_start)
    cookies_heading = page.index("Subscription cookies")
    assert form_close < cookies_heading


def _netscape_cookies_text(value="UPLOADSECRET42"):
    return (
        "# Netscape HTTP Cookie File\n"
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\t{value}\n"
    )


def test_cookies_upload_file_stores_with_0600_and_lists_domain():
    data = _netscape_cookies_text().encode()
    with TestClient(app) as c:
        resp = c.post(
            "/settings/cookies",
            files={"cookies": ("cookies.txt", data, "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "Cookies+saved" in location or "Cookies%20saved" in location

        mode = stat.S_IMODE(os.stat(cookie_path()).st_mode)
        assert mode == 0o600

        page = c.get("/settings").text
    assert ".ft.com" in page


def test_cookies_upload_pasted_text_works_and_flash_count_reflects_load_jar_rules():
    # Future, session (0-expiry), and past-dated cookies: the flash count must
    # reflect load_jar()'s rules (past-dated dropped), not a raw parse of all three.
    past_expiry = 946684800  # 2000-01-01T00:00:00Z
    text = (
        "# Netscape HTTP Cookie File\n"
        f".a.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n"
        ".b.com\tTRUE\t/\tFALSE\t0\tsess\tdef\n"
        f".c.com\tTRUE\t/\tFALSE\t{past_expiry}\told\tghi\n"
    )
    with TestClient(app) as c:
        resp = c.post(
            "/settings/cookies", data={"cookies_text": text}, follow_redirects=False
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert (
            "2+cookies+for+2+domains" in location
            or "2%20cookies%20for%202%20domains" in location
        )

        page = c.get("/settings").text
    assert ".a.com" in page
    assert ".b.com" in page


@pytest.mark.parametrize(
    "payload",
    [
        b'{"not": "cookies"}',
        b"\xff\xfe\x00garbage",
        b"",
    ],
    ids=["garbage-json", "non-utf8", "empty"],
)
def test_cookies_upload_bad_payload_rejected_and_existing_kept(payload):
    valid = _netscape_cookies_text().encode()
    with TestClient(app) as c:
        resp = c.post(
            "/settings/cookies",
            files={"cookies": ("cookies.txt", valid, "text/plain")},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        resp2 = c.post(
            "/settings/cookies",
            files={"cookies": ("cookies.txt", payload, "text/plain")},
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        assert "err=" in resp2.headers["location"]

    assert cookie_path().read_bytes() == valid

    leftover = [
        p
        for p in data_dir().iterdir()
        if p.name != "cookies.txt" and not p.name.startswith("pintxos.db")
    ]
    assert leftover == []


def test_cookies_upload_nothing_rejected():
    with TestClient(app) as c:
        resp = c.post("/settings/cookies", data={}, follow_redirects=False)
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]
        assert "Nothing" in resp.headers["location"] or "err=Nothing" in resp.headers["location"]

    assert not cookie_path().exists()


def test_cookies_delete_removes_file():
    text = _netscape_cookies_text()
    with TestClient(app) as c:
        c.post("/settings/cookies", data={"cookies_text": text}, follow_redirects=False)

        resp = c.post("/settings/cookies/delete", follow_redirects=False)
        assert resp.status_code == 303

        page = c.get("/settings").text

    assert not cookie_path().exists()
    assert "No cookies.txt found." in page
    assert "Remove" not in page


def test_cookies_upload_never_echoes_value():
    text = _netscape_cookies_text()
    with TestClient(app) as c:
        resp = c.post(
            "/settings/cookies", data={"cookies_text": text}, follow_redirects=False
        )
        assert "UPLOADSECRET42" not in resp.headers["location"]

        page = c.get("/settings").text
        assert "UPLOADSECRET42" not in page

        resp2 = c.post(
            "/settings/cookies",
            files={"cookies": ("cookies.txt", text.encode(), "text/plain")},
            follow_redirects=False,
        )
        assert "UPLOADSECRET42" not in resp2.headers["location"]

        page2 = c.get("/settings").text
        assert "UPLOADSECRET42" not in page2


def _insert_item(
    feed_id, guid, *, auth=None, link="https://www.example.com/a", published_at=None, fallback=0
):
    with db() as conn:
        conn.execute(
            "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
            "headline, summary, fallback, auth, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                feed_id,
                guid,
                link,
                "Original",
                published_at or db_now(),
                "Headline",
                "Summary.",
                fallback,
                auth,
                db_now(),
            ),
        )


def test_index_shows_login_indicator_counts_without_adding_a_column(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post("/feeds", data={"url": "https://example.org/feed.xml"}, follow_redirects=False)

        _insert_item(1, "g1", auth="used")
        _insert_item(1, "g2", auth="used")
        _insert_item(1, "g3", auth="missing")
        _insert_item(1, "g4", auth="failed")
        _insert_item(1, "g5", auth=None)

        page = c.get("/").text

    assert "2 via login" in page
    assert "1 need login" in page
    assert "1 login failed" in page
    assert "<div>5</div>" in page  # item_count for feed 1

    # feed 2 has no items: none of the three labels appear for it. Since feed 1's row
    # already contains these labels, check they appear exactly once each (only feed 1's row).
    assert page.count("via login") == 1
    assert page.count("need login") == 1
    assert page.count("login failed") == 1

    # The Items cell gained extra muted lines, not a new column.
    widths = re.findall(r'<col style="width: (\d+)%">', page)
    assert len(widths) == 7
    assert page.count("<th>") == 7


def test_feed_edit_page_shows_login_section_with_sentences(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="used")
        _insert_item(1, "g2", auth="used")
        _insert_item(1, "g3", auth="missing")
        _insert_item(1, "g4", auth="failed")

        # No cookies.txt: "Add your subscription cookies" link is present.
        page = c.get("/feeds/1").text

    assert "<h2>Login</h2>" in page
    assert "2 items fetched with your subscription" in page
    assert "1 needs a login" in page
    assert "1 login failed" in page
    assert "Add your subscription cookies" in page
    assert '<a href="/settings">Settings page</a>' in page


def test_feed_edit_page_login_section_reflects_cookies_written_after_first_render(monkeypatch):
    """Writing cookies.txt mid-session changes the Login section on the next render."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="missing")
        _insert_item(1, "g2", auth="failed")

        page = c.get("/feeds/1").text
        assert "Add your subscription cookies" in page

        write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n")

        page = c.get("/feeds/1").text
        assert "Add your subscription cookies" not in page
        assert "1 login failed" in page
        assert "Cookies for www.example.com expire 2100-01-01." in page

        # Session cookie (0 expiry): reported as a session cookie, not a date.
        write_cookies(".example.com\tTRUE\t/\tFALSE\t0\tsid\tabc\n")

        page = c.get("/feeds/1").text
        assert "Cookies for www.example.com are session cookies." in page


def test_feed_edit_page_no_login_section_when_auth_all_null(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth=None)
        _insert_item(1, "g2", auth=None)

        page = c.get("/feeds/1").text

    assert "<h2>Login</h2>" not in page


# --- retry-fallback route and pintxos.poll.retry_fallback -------------------


# Each retry-fallback fixture feed is described as a list of
# (guid, fallback, auth) triples, inserted via the shared _insert_item helper.
_WITH_FALLBACK = [("guid-1", 1, "missing"), ("guid-2", 1, None), ("guid-3", 0, "used")]
_NO_FALLBACK = [("guid-1", 0, "used")]
_SINGLE_FALLBACK = [("guid-1", 1, None)]


def _seed_feed(items, url="https://example.com/feed.xml"):
    with db() as conn:
        feed_id = conn.execute(
            "INSERT INTO feeds(url, title, created_at) VALUES (?, ?, ?)",
            (url, "Example Feed", db_now()),
        ).lastrowid
    for guid, fallback, auth in items:
        _insert_item(feed_id, guid, auth=auth, fallback=fallback)
    return feed_id


def _item_rows(feed_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY id", (feed_id,)
        ).fetchall()


def test_retry_fallback_route_updates_in_place_and_queues_retry(monkeypatch):
    """The route never deletes: rows stay put, retry_one is queued instead, and
    only the fallback rows for this feed are reported as being retried."""
    feed_id = _seed_feed(_WITH_FALLBACK)
    other_feed_id = _seed_feed(_SINGLE_FALLBACK, "https://other.example.com/feed.xml")

    calls = []
    monkeypatch.setattr(app_module, "retry_one", lambda fid: calls.append(fid))
    with TestClient(app) as c:
        resp = c.post(f"/feeds/{feed_id}/retry-fallback", follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "Retrying" in location
    assert "2" in location

    rows = _item_rows(feed_id)
    assert {r["guid"] for r in rows} == {"guid-1", "guid-2", "guid-3"}  # nothing deleted

    other_row = _item_rows(other_feed_id)[0]
    assert other_row["fallback"] == 1  # untouched: different feed, never queued

    assert calls == [feed_id]


def test_retry_fallback_route_no_fallback_items_does_not_queue(monkeypatch):
    feed_id = _seed_feed(_NO_FALLBACK)
    calls = []
    monkeypatch.setattr(app_module, "retry_one", lambda fid: calls.append(fid))
    with TestClient(app) as c:
        resp = c.post(f"/feeds/{feed_id}/retry-fallback", follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "No fallback items" in location or "No%20fallback%20items" in location

    assert calls == []

    rows = _item_rows(feed_id)
    assert {r["guid"] for r in rows} == {"guid-1"}


def test_retry_fallback_route_unknown_feed_404(monkeypatch):
    monkeypatch.setattr(app_module, "retry_one", lambda fid: None)
    with TestClient(app) as c:
        resp = c.post("/feeds/999/retry-fallback", follow_redirects=False)
    assert resp.status_code == 404


def test_feed_edit_page_retry_form_pluralizes_fallback_count(monkeypatch):
    monkeypatch.setattr(app_module, "retry_one", lambda fid: None)

    plural_feed_id = _seed_feed(_WITH_FALLBACK, "https://example.com/plural.xml")
    with TestClient(app) as c:
        page = c.get(f"/feeds/{plural_feed_id}").text
    assert "Retry 2 fallback items" in page

    singular_feed_id = _seed_feed(_SINGLE_FALLBACK, "https://example.com/singular.xml")
    with TestClient(app) as c:
        page = c.get(f"/feeds/{singular_feed_id}").text
    assert "Retry 1 fallback item" in page
    assert "Retry 1 fallback items" not in page

    no_fallback_feed_id = _seed_feed(_NO_FALLBACK, "https://example.com/none.xml")
    with TestClient(app) as c:
        page = c.get(f"/feeds/{no_fallback_feed_id}").text
    assert "retry-fallback" not in page


@pytest.mark.parametrize(
    "fetch_ok, expect_auth, expect_fallback, expect_headline",
    [
        (True, None, 0, "New Headline"),
        (False, "missing", 1, "Headline"),
    ],
)
def test_retry_fallback_updates_row_in_place_on_success_or_records_auth_on_failure(
    monkeypatch, fetch_ok, expect_auth, expect_fallback, expect_headline
):
    """poll.retry_fallback updates the existing row (never inserts/deletes): on a
    successful fetch the headline/summary/word_count are refreshed and fallback
    clears; on a repeat failure only auth is recorded and the row stays a fallback."""
    feed_id = _seed_feed(_SINGLE_FALLBACK)

    if fetch_ok:
        monkeypatch.setattr(poll, "fetch_article", lambda link: "FULL ARTICLE TEXT " * 20)
        monkeypatch.setattr(
            poll, "summarize", lambda text, original_title, url: ("New Headline", "New summary")
        )
    else:
        monkeypatch.setattr(poll, "fetch_article", lambda link: None)

        def boom_summarize(*_args, **_kwargs):
            raise AssertionError("summarize should not be called when the fetch fails")

        monkeypatch.setattr(poll, "summarize", boom_summarize)

    poll.retry_fallback(feed_id)

    rows = _item_rows(feed_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["guid"] == "guid-1"  # same row, not a new insert
    assert row["fallback"] == expect_fallback
    assert row["headline"] == expect_headline
    assert row["auth"] == expect_auth
    assert feed_id not in poll._status


@pytest.mark.parametrize("error", ["missing_api_key", "summarize_error"])
def test_retry_fallback_error_paths(monkeypatch, error):
    """poll.retry_fallback's own try/except around summarize(): MissingApiKey stops
    the loop immediately (later rows untouched, last_error set); SummarizeError
    just skips that row (fallback=1 kept) and the loop continues to the next one."""
    feed_id = _seed_feed([("guid-1", 1, None), ("guid-2", 1, None)])
    monkeypatch.setattr(poll, "fetch_article", lambda link: "FULL ARTICLE TEXT " * 20)

    from pintxos.summarize import MissingApiKey, SummarizeError

    calls = []

    def fake_summarize(text, original_title, url):
        calls.append(url)
        if error == "missing_api_key":
            raise MissingApiKey("ANTHROPIC_API_KEY not set")
        if len(calls) == 1:
            raise SummarizeError("API said no")
        return "New Headline", "New summary"

    monkeypatch.setattr(poll, "summarize", fake_summarize)

    poll.retry_fallback(feed_id)

    rows = {r["guid"]: r for r in _item_rows(feed_id)}
    if error == "missing_api_key":
        assert len(calls) == 1  # stopped before the second item
        assert rows["guid-1"]["fallback"] == 1
        assert rows["guid-2"]["fallback"] == 1
        with db() as conn:
            feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        assert "ANTHROPIC_API_KEY" in feed["last_error"]
    else:
        assert len(calls) == 2  # loop continued past the failed item
        assert rows["guid-1"]["fallback"] == 1  # left as fallback: summarize failed
        assert rows["guid-2"]["fallback"] == 0  # second item's summarize succeeded
        assert rows["guid-2"]["headline"] == "New Headline"


def test_retry_one_queues_retry_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(poll, "retry_fallback", lambda fid: calls.append(fid))

    class FakeScheduler:
        def add_job(self, func, args, id, replace_existing, misfire_grace_time):
            calls.append(("queued", id))

    monkeypatch.setattr(poll, "scheduler", FakeScheduler())
    poll.retry_one(42)
    assert ("queued", "retry-42") in calls
