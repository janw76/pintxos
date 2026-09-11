"""Tests for the web UI: feed list/add/delete, poll now, settings."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pintxos.app as app_module
from pintxos import feedstats, llm, poll, topics
from pintxos.app import app
from pintxos.config import data_dir, get_setting
from pintxos.cookies import cookie_path, load_jar
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


def test_index_shows_feed_count_in_heading(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        page = c.get("/").text
        assert '<h1>Feeds (<span id="feed-count">0</span>)</h1>' in page

        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text
        assert '<h1>Feeds (<span id="feed-count">1</span>)</h1>' in page


def test_index_search_box_has_no_match_row_and_non_url_input(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        page = c.get("/").text
        assert "No feeds match." in page
        assert 'type="url"' not in page
        assert 'id="feed-search"' in page
        assert 'name="url"' in page
        assert "required" not in page.split('id="feed-search"')[1].split(">")[0]


def test_index_add_button_starts_disabled(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        page = c.get("/").text
        form = page.split('<form class="add-form"')[1].split("</form>")[0]
        button = [line for line in form.split("<button") if "submit" in line][0]
        assert "disabled" in button


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


def _items_cell(page: str, feed_id: int) -> str:
    """The Items <td> contents for one feed row (count and ads-skipped line only)."""
    row_start = page.index(f'<tr id="feed-{feed_id}"')
    row_end = page.index("</tr>", row_start)
    row = page[row_start:row_end]
    start = row.index('<td class="output">')
    start = row.index("</td>", start) + len("</td>")
    end = row.index('<td class="muted nowrap', start)
    return row[start:end]


def _status_cell(page: str, feed_id: int) -> str:
    """The Status <td> contents (raw HTML) for one feed row in the index table."""
    row_start = page.index(f'<tr id="feed-{feed_id}"')
    row_end = page.index("</tr>", row_start)
    row = page[row_start:row_end]
    start = row.index('<td class="status">')
    end = row.index("</td>", start)
    return row[start:end]


def _feed_page_status(page: str) -> str:
    """The Status block (raw HTML) on the feed-edit page: after the heading, up to the
    Summaries line (a feed-edit-only addition) or, failing that, the next <form> (the
    retry-fallback form when present, else the end of the page)."""
    start = page.index("<h2>Status</h2>") + len("<h2>Status</h2>")
    rest = page[start:]
    candidates = [
        pos
        for pos in (rest.find('<div class="muted">Summaries:'), rest.find("<form"))
        if pos != -1
    ]
    end = start + min(candidates) if candidates else len(page)
    return page[start:end]


def _strip_tags(html: str) -> str:
    """Visible text only: tags dropped, whitespace collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _titles(html: str) -> list[str]:
    """The title="..." tooltip values found in html, in document order."""
    return re.findall(r'title="([^"]*)"', html)


def _label(text: str) -> str:
    """The <span class="label">text</span> closing fragment for a table button's visible text."""
    return ">" + text + "</span>"


def test_poll_button_carries_poll_state_and_refresh(monkeypatch):
    """Poll progress does not live in the Status column: the poll button itself reads
    Polling… (disabled, progress in the tooltip, page polls /status in place), Failed
    (danger tint, error in the tooltip, still clickable) or Poll now (idle)."""
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
        assert _label("Polling…") in btn
        assert "disabled" in btn
        assert 'title="Summarizing 2/5"' in btn
        assert "btn-polling" in btn
        assert "btn-danger" not in btn
        assert "Summarizing 2/5" not in _items_cell(page, 1)
        assert "Summarizing 2/5" not in _status_cell(page, 1)

        # idle
        monkeypatch.setattr(app_module, "poll_status", {})
        page = c.get("/").text
        assert 'http-equiv="refresh"' not in page
        assert "location.reload" not in page
        assert 'fetch("/status")' in page
        btn = _poll_button(page)
        assert _label("Poll now") in btn
        assert "disabled" not in btn
        assert 'title="Poll now"' in btn
        assert "btn-tint-neutral" in btn
        assert "btn-polling" not in btn

        # failed: retry stays enabled, error text is the tooltip (escaped for the attribute)
        with db() as conn:
            conn.execute('UPDATE feeds SET last_error = ? WHERE id = 1', ('HTTP 500: "boom"',))
        page = c.get("/").text
        btn = _poll_button(page)
        assert _label("Failed") in btn
        assert "disabled" not in btn
        assert "btn-danger" in btn
        assert 'title="HTTP 500: &#34;boom&#34;"' in btn
        assert page.count("/feeds/1/poll") == 1

        # active wins over a stale last_error
        monkeypatch.setattr(app_module, "poll_status", {1: "Fetching"})
        btn = _poll_button(c.get("/").text)
        assert _label("Polling…") in btn and 'title="Fetching"' in btn and "btn-danger" not in btn

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
    assert _label("Poll now") in body


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
    assert _label("Polling…") in btn
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


def test_status_column_shows_last_error_message_unchanged(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text
        assert "<th>Status</th>" in page
        assert '<td class="status">' in page
        assert '<div class="error">' not in page  # no last_error yet

        with db() as conn:
            conn.execute("UPDATE feeds SET last_error = ? WHERE id = 1", ("timed out",))
        page = c.get("/").text
        assert '<div class="error">timed out</div>' in page


def test_settings_post_persists():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "claude-haiku-4-5-20251001",
                "poll_minutes": "15",
                "items_per_feed": "10",
                "api_key": "sk-test-1234",
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


def test_settings_page_shows_model_presets_and_key_fields():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert "claude-haiku-4-5-20251001" in page
    assert "anthropic/claude-haiku-4.5" in page
    assert "openai/gpt-5-mini" in page
    assert "google/gemini-2.5-flash-lite" in page
    assert "Names with a slash (vendor/model) go to OpenRouter, names without go to Anthropic." in page
    assert 'name="api_key"' in page
    assert 'name="openrouter_api_key"' in page


def test_settings_post_openrouter_model_without_key_rejected_and_model_unchanged():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "google/gemini-2.5-flash-lite",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "openrouter_api_key": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/settings?err=")
        assert "OPENROUTER_API_KEY" in location

    assert get_setting("PINTXOS_MODEL") != "google/gemini-2.5-flash-lite"


def test_settings_post_openrouter_model_with_key_stores_both():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "google/gemini-2.5-flash-lite",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "openrouter_api_key": "sk-or-test",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "err=" not in resp.headers["location"]

    assert get_setting("PINTXOS_MODEL") == "google/gemini-2.5-flash-lite"
    assert get_setting("OPENROUTER_API_KEY") == "sk-or-test"


def test_settings_post_openrouter_env_pinned_form_value_not_stored(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-envkey")
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "google/gemini-2.5-flash-lite",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
                "openrouter_api_key": "sk-or-submitted-should-not-be-stored",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "err=" not in resp.headers["location"]

    monkeypatch.delenv("OPENROUTER_API_KEY")
    from pintxos.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("OPENROUTER_API_KEY",)
        ).fetchone()
    assert row is None


def test_settings_post_anthropic_model_without_key_rejected():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "claude-haiku-4-5-20251001",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/settings?err=")
        assert "ANTHROPIC_API_KEY" in location


def test_settings_post_empty_model_rejected():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "   ",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/settings?err=")
        assert "Model+is+required" in location or "Model%20is%20required" in location


def test_settings_test_route_success(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "OK")
    with TestClient(app) as c:
        resp = c.post("/settings/test", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "err=" not in location
        page = c.get(location).text
    assert "answered: OK" in page


def test_settings_test_route_llm_error(monkeypatch):
    def _raise(*a, **k):
        raise llm.LLMError("boom")

    monkeypatch.setattr(llm, "complete", _raise)
    with TestClient(app) as c:
        resp = c.post("/settings/test", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "err=" in location
        page = c.get(location).text
    assert "boom" in page


def test_settings_test_route_missing_api_key(monkeypatch):
    def _raise(*a, **k):
        raise llm.MissingApiKey("OPENROUTER_API_KEY not set")

    monkeypatch.setattr(llm, "complete", _raise)
    with TestClient(app) as c:
        resp = c.post("/settings/test", follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "err=" in location
        page = c.get(location).text
    assert "OPENROUTER_API_KEY not set" in page


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
                "api_key": "sk-test-1234",
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
                "api_key": "sk-test-1234",
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
                "api_key": "sk-test-1234",
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
                "api_key": "sk-test-1234",
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
                "api_key": "sk-test-1234",
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
                "api_key": "sk-test-1234",
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


def test_settings_page_shows_full_text_default_on():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert 'name="full_text" value="1" checked' in page


def test_settings_post_without_full_text_stores_off():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert get_setting("PINTXOS_FULL_TEXT") == "0"
    assert 'name="full_text" value="1" checked' not in page


def test_settings_post_with_full_text_stores_on():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
                "full_text": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert get_setting("PINTXOS_FULL_TEXT") == "1"
    assert 'name="full_text" value="1" checked' in page


def test_settings_full_text_env_pinned_disables_control_and_ignores_submission(monkeypatch):
    monkeypatch.setenv("PINTXOS_FULL_TEXT", "0")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert 'name="full_text" value="1"  disabled' in page
        assert 'name="full_text" value="1" checked' not in page
        assert "Set by PINTXOS_FULL_TEXT in the environment." in page

        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
                "full_text": "1",
            },
            follow_redirects=False,
        )

    monkeypatch.delenv("PINTXOS_FULL_TEXT")
    from pintxos.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("PINTXOS_FULL_TEXT",)
        ).fetchone()
    assert row is None


def test_settings_page_shows_respect_language_default_on():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert 'name="respect_language" value="1" checked' in page


def test_settings_post_without_respect_language_stores_off():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert get_setting("PINTXOS_RESPECT_LANGUAGE") == "0"
    assert 'name="respect_language" value="1" checked' not in page


def test_settings_post_with_respect_language_stores_on():
    with TestClient(app) as c:
        resp = c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
                "respect_language": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        page = c.get("/settings").text

    assert get_setting("PINTXOS_RESPECT_LANGUAGE") == "1"
    assert 'name="respect_language" value="1" checked' in page


def test_settings_respect_language_env_pinned_disables_control_and_ignores_submission(
    monkeypatch,
):
    monkeypatch.setenv("PINTXOS_RESPECT_LANGUAGE", "0")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert 'name="respect_language" value="1"  disabled' in page
        assert 'name="respect_language" value="1" checked' not in page
        assert "Set by PINTXOS_RESPECT_LANGUAGE in the environment." in page

        c.post(
            "/settings",
            data={
                "model": "m",
                "poll_minutes": "30",
                "items_per_feed": "50",
                "api_key": "sk-test-1234",
                "respect_language": "1",
            },
            follow_redirects=False,
        )

    monkeypatch.delenv("PINTXOS_RESPECT_LANGUAGE")
    from pintxos.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("PINTXOS_RESPECT_LANGUAGE",)
        ).fetchone()
    assert row is None


def test_index_ads_skipped_cell_is_empty_without_a_count(monkeypatch):
    """ads_filtered defaults to 0, so nothing renders in the ads-skipped cell."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    assert "<div>0</div>" in page  # the items cell rendered
    assert "filtered" not in page


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

    assert "1 filtered" in page


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
                  "api_key": "sk-test-1234", "filter_ads": "1", "ad_title_patterns": ""},
            follow_redirects=False,
        )
    assert get_setting("PINTXOS_FILTER_ADS") == "1"


def test_index_pluralizes_ads_skipped(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute("UPDATE feeds SET ads_filtered = 2 WHERE id = 1")
        assert "2 filtered" in c.get("/").text


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
                "api_key": "sk-test-1234",
                "ad_title_patterns": "black friday\n\\bgiveaway\\b",
            },
            follow_redirects=False,
        )
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    # thirteen radios: three each for respect_language, filter_ads, ad_patterns_mode,
    # two each for classify_topics and warn_volume
    assert page.count('type="radio"') == 13
    assert 'name="filter_ads"' in page
    assert 'name="ad_patterns_mode"' in page
    assert 'name="ad_title_patterns"' in page
    assert 'name="respect_language"' in page
    # unsaved feed defaults to "inherit" (value="") for both groups
    assert 'name="filter_ads" value="" checked' in page
    assert 'name="filter_ads" value="1" checked' not in page
    assert 'name="filter_ads" value="0" checked' not in page
    assert 'name="ad_patterns_mode" value="" checked' in page
    assert 'name="ad_patterns_mode" value="1" checked' not in page
    assert 'name="ad_patterns_mode" value="0" checked' not in page
    assert 'name="respect_language" value="" checked' in page
    assert 'name="respect_language" value="1" checked' not in page
    assert 'name="respect_language" value="0" checked' not in page
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
        assert page.count('type="radio"') == 13
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


def test_feed_edit_page_shows_topic_checkboxes_off_by_default(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    assert page.count('name="mute_topics"') == len(topics.TOPICS)
    assert 'name="classify_topics" value="0" checked' in page
    assert 'name="classify_topics" value="1" checked' not in page
    assert "%)" not in page
    assert "classified" not in page
    for slug, name, definition in topics.TOPICS:
        assert f'value="{slug}"' in page
        assert f'title="{definition}"' in page
        assert name in page
        assert f'name="mute_topics" value="{slug}" checked' not in page


def test_feed_edit_post_topic_mute_saves_known_slugs(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "",
                "ad_patterns_mode": "",
                "classify_topics": "1",
                "mute_topics": ["sport", "politics", "not-a-real-topic"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with db() as conn:
            row = conn.execute(
                "SELECT classify_topics, mute_topics FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["classify_topics"] == 1
        # stored in topics.TOPICS order, not submission order
        assert json.loads(row["mute_topics"]) == ["politics", "sport"]

        page = c.get("/feeds/1").text
        assert 'name="classify_topics" value="1" checked' in page
        assert 'name="classify_topics" value="0" checked' not in page
        assert 'name="mute_topics" value="sport" checked' in page
        assert 'name="mute_topics" value="politics" checked' in page
        for slug, _name, _definition in topics.TOPICS:
            if slug not in ("sport", "politics"):
                assert f'name="mute_topics" value="{slug}" checked' not in page


def test_feed_edit_page_shows_topic_percentages(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute(
                "UPDATE feeds SET topic_counts = ? WHERE id = 1",
                (json.dumps({"sport": 3, "politics": 1}),),
            )
        page = c.get("/feeds/1").text

    assert "of 4 classified" in page
    sport_label = page.split('value="sport"')[1].split("</label>")[0]
    assert "(75%)" in sport_label
    politics_label = page.split('value="politics"')[1].split("</label>")[0]
    assert "(25%)" in politics_label
    # A topic with no classified items yet shows no percentage at all.
    arts_label = page.split('value="arts"')[1].split("</label>")[0]
    assert "%" not in arts_label


def test_feed_edit_page_shows_volume_defaults_and_summary_totals(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    assert 'name="warn_volume" value="1" checked' in page
    assert 'name="warn_volume" value="0" checked' not in page
    assert 'name="daily_budget"' in page
    assert 'name="daily_budget" min="0" step="1" value="">' in page
    assert "Summaries: 0 today, 0 total" in page


def test_feed_edit_post_saves_warn_volume_and_daily_budget(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={"filter_ads": "", "ad_patterns_mode": "", "warn_volume": "0", "daily_budget": "25"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?msg=Saved"

        with db() as conn:
            row = conn.execute(
                "SELECT warn_volume, daily_budget FROM feeds WHERE id = 1"
            ).fetchone()
        assert row["warn_volume"] == 0
        assert row["daily_budget"] == 25

        page = c.get("/feeds/1").text
        assert 'name="warn_volume" value="0" checked' in page
        assert 'name="warn_volume" value="1" checked' not in page
        assert 'name="daily_budget" min="0" step="1" value="25">' in page


def test_feed_edit_post_daily_budget_invalid_values_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)

        for bad_value in ("abc", "-1"):
            resp = c.post(
                "/feeds/1",
                data={"filter_ads": "", "ad_patterns_mode": "", "daily_budget": bad_value},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/feeds/1?err=")
            with db() as conn:
                row = conn.execute("SELECT daily_budget FROM feeds WHERE id = 1").fetchone()
            assert row["daily_budget"] is None


def test_feed_edit_page_shows_model_field_and_presets(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    assert 'id="feed_model"' in page
    assert 'name="model"' in page
    assert "claude-haiku-4-5-20251001" in page
    assert "anthropic/claude-haiku-4.5" in page
    assert "openai/gpt-5-mini" in page
    assert "google/gemini-2.5-flash-lite" in page
    assert "document.getElementById('feed_model').value=this.dataset.model" in page


def test_feed_edit_post_model_with_openrouter_key_stores_value(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "",
                "ad_patterns_mode": "",
                "model": "google/gemini-2.5-flash-lite",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?msg=Saved"

        with db() as conn:
            row = conn.execute("SELECT model FROM feeds WHERE id = 1").fetchone()
        assert row["model"] == "google/gemini-2.5-flash-lite"


def test_feed_edit_post_model_without_openrouter_key_rejected_and_unchanged(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "",
                "ad_patterns_mode": "",
                "model": "google/gemini-2.5-flash-lite",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/feeds/1?err=")
        assert "OPENROUTER_API_KEY" in resp.headers["location"]

        with db() as conn:
            row = conn.execute("SELECT model FROM feeds WHERE id = 1").fetchone()
        assert row["model"] is None


def test_feed_edit_post_blank_model_stores_null(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post(
            "/feeds/1",
            data={
                "filter_ads": "",
                "ad_patterns_mode": "",
                "model": "google/gemini-2.5-flash-lite",
            },
            follow_redirects=False,
        )
        resp = c.post(
            "/feeds/1",
            data={"filter_ads": "", "ad_patterns_mode": "", "model": "  "},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?msg=Saved"

        with db() as conn:
            row = conn.execute("SELECT model FROM feeds WHERE id = 1").fetchone()
        assert row["model"] is None


def test_feed_edit_page_shows_summaries_today_and_total(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            feedstats.bump(conn, 1, summaries=3, day=feedstats.today())
            feedstats.bump(conn, 1, summaries=5, day="2020-01-01")
        page = c.get("/feeds/1").text

    assert "Summaries: 3 today, 8 total" in page


def _first_item_block(xml_text: str) -> str:
    start = xml_text.index("<item>")
    end = xml_text.index("</item>", start) + len("</item>")
    return xml_text[start:end]


def test_feed_xml_no_warning_below_threshold(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            feedstats.bump(conn, 1, summaries=49, day=feedstats.today())
        body = c.get("/feeds/1.xml").text

    assert "pintxos-warning" not in body


def test_feed_xml_warning_at_50_is_first_item(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            feedstats.bump(conn, 1, summaries=50, day=feedstats.today())
        body = c.get("/feeds/1.xml").text

    today = feedstats.today()
    guid = f"pintxos-warning-1-50-{today}"
    first_item = _first_item_block(body)
    assert guid in first_item
    link = first_item[first_item.index("<link>") + len("<link>") : first_item.index("</link>")]
    assert "/feeds/1" in link
    assert ".xml" not in link


def test_feed_xml_warning_at_120_uses_100_level(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            feedstats.bump(conn, 1, summaries=120, day=feedstats.today())
        body = c.get("/feeds/1.xml").text

    today = feedstats.today()
    guid = f"pintxos-warning-1-100-{today}"
    first_item = _first_item_block(body)
    assert guid in first_item


def test_feed_xml_warn_volume_off_suppresses_warning(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute("UPDATE feeds SET warn_volume = 0 WHERE id = 1")
            feedstats.bump(conn, 1, summaries=120, day=feedstats.today())
        body = c.get("/feeds/1.xml").text

    assert "pintxos-warning" not in body


def test_feed_xml_warning_link_uses_base_url_setting(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setenv("PINTXOS_BASE_URL", "https://example.test")
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            feedstats.bump(conn, 1, summaries=50, day=feedstats.today())
        body = c.get("/feeds/1.xml").text

    first_item = _first_item_block(body)
    link = first_item[first_item.index("<link>") + len("<link>") : first_item.index("</link>")]
    assert link.startswith("https://example.test")


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


def test_feed_edit_post_respect_language_off_saved(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
                "respect_language": "0",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?msg=Saved"

        with db() as conn:
            row = conn.execute("SELECT respect_language FROM feeds WHERE id = 1").fetchone()
        assert row["respect_language"] == 0

        page = c.get("/feeds/1").text
        assert 'name="respect_language" value="0" checked' in page


def test_feed_edit_post_respect_language_unknown_choice_rejected(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
                "respect_language": "7",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303 and resp.headers["location"].startswith("/feeds/1?err=")


def test_feed_edit_page_shows_pencil_and_title_input(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post(
            "/feeds/1",
            data={
                "title": "FT World",
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
            },
            follow_redirects=False,
        )
        page = c.get("/feeds/1").text

    assert 'aria-label="Edit title"' in page
    assert 'name="title"' in page
    assert 'name="title" maxlength="200" value="FT World"' in page
    assert "<h1" in page and "FT World" in page


def test_feed_edit_post_title_saved_and_shown_on_index(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "title": "FT World",
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?msg=Saved"

        with db() as conn:
            row = conn.execute("SELECT title FROM feeds WHERE id = 1").fetchone()
        assert row["title"] == "FT World"

        page = c.get("/").text
        assert "FT World" in page


def test_feed_edit_post_blank_title_stores_null(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        c.post(
            "/feeds/1",
            data={
                "title": "FT World",
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
            },
            follow_redirects=False,
        )
        resp = c.post(
            "/feeds/1",
            data={
                "title": "   ",
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    with db() as conn:
        row = conn.execute("SELECT title FROM feeds WHERE id = 1").fetchone()
    assert row["title"] is None


def test_feed_edit_post_title_too_long_rejected_and_unchanged(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post(
            "/feeds/1",
            data={
                "title": "x" * 201,
                "filter_ads": "",
                "ad_patterns_mode": "",
                "ad_title_patterns": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/feeds/1?err=")

        with db() as conn:
            row = conn.execute("SELECT title FROM feeds WHERE id = 1").fetchone()
        assert row["title"] is None


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
    assert _label("Edit") in page
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


def test_index_copy_sits_inside_output_url_cell_and_actions_stay_on_one_line(monkeypatch):
    """Copy shares the Output URL cell, right after the URL; the actions cell holds exactly
    Edit, Poll now, Delete in that order and is styled never to wrap."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    # One cell holds the URL span (with the full URL in its tooltip) and then the Copy button.
    cell_start = page.index('<td class="output">')
    cell = page[cell_start : page.index("</td>", cell_start)]
    assert 'class="output-url"' in cell
    assert 'title="http://testserver/feeds/1.xml"' in cell
    assert "pintxosCopy(this, " in cell
    assert _label("Copy") in cell
    assert cell.index('class="output-url"') < cell.index("pintxosCopy(this, ")
    # One cell, one line: no break and no second cell opens before this one closes.
    assert "<br" not in cell
    assert "<td" not in cell[len('<td class="output">') :]
    assert '<td class="copy">' not in page

    # String guards: the URL ellipsises instead of wrapping, and disappears when too narrow.
    rule_start = page.index("\n    .output-url {")
    rule = page[rule_start : page.index("}", rule_start)]
    assert "white-space: nowrap;" in rule
    assert "text-overflow: ellipsis;" in rule
    assert "@container (max-width: 10rem) { .output-url { display: none; } }" in page

    # Actions cell: exactly the three buttons, in order, no Copy, no wrapping container.
    start = page.index('<td class="actions">')
    cell = page[start : page.index("</td>", start)]
    assert cell.count("<button") == 3
    assert _label("Copy") not in cell and "pintxosCopy" not in cell
    assert "actions-row" not in page
    order = [cell.index(_label(label)) for label in ("Edit", "Poll now", "Delete")]
    assert order == sorted(order)
    assert page.count("/feeds/1/poll") == 1

    # String guard: the rule that keeps the three buttons on one line.
    assert "td.actions { white-space: nowrap;" in page


def test_table_buttons_carry_feather_icons_for_narrow_screens(monkeypatch):
    """At <=720px the four table buttons collapse to icon-only Feather svgs; above that
    width they stay text buttons (a .label span wraps the visible text either way)."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    row_start = page.index('<tr id="feed-1"')
    row_end = page.index("</tr>", row_start) + len("</tr>")
    row = page[row_start:row_end]

    assert row.count("<svg") == 5
    for svg_start in (m.start() for m in re.finditer("<svg", row)):
        svg_tag_end = row.index(">", svg_start)
        svg_tag = row[svg_start:svg_tag_end]
        assert 'class="ico' in svg_tag
        assert 'aria-hidden="true"' in svg_tag

    assert 'aria-label="Copy output URL"' in row
    assert 'aria-label="Edit"' in row
    assert 'aria-label="Poll now"' in row
    assert 'aria-label="Delete feed"' in row
    assert "ico-done" in row

    assert "@media (max-width: 720px)" in page
    assert "table button .label { display: none; }" in page
    assert 'querySelector(".label")' in page
    assert '"copied"' in page

    # Media rules add no specificity, so the override block must come after the base rules
    # it overrides, or the later base rules win the cascade at equal specificity.
    label_hidden = page.index("table button .label { display: none; }")
    assert page.index("table button .ico { display: none;") < label_hidden
    assert page.index("td.actions .btn-poll { min-width: 5.5rem; }") < label_hidden
    assert page.index("table button { font-size: 0.75rem; padding: 0.2rem 0.45rem; }") < label_hidden

    license_path = Path(__file__).resolve().parents[1] / "docs" / "licenses" / "feather-icons-LICENSE.txt"
    assert license_path.exists()
    license_text = license_path.read_text()
    assert "MIT License" in license_text
    assert "Cole Bemis" in license_text

    for bad in ("cdn", "unpkg", "jsdelivr", "fonts.googleapis"):
        assert bad not in page


def test_index_colgroup_widths_sum_to_100_percent(monkeypatch):
    """Six fixed columns budgeted to fit 968px (1000px viewport) without scroll."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    widths = [int(w) for w in re.findall(r'<col style="width: (\d+)%">', page)]
    assert len(widths) == 6
    assert sum(widths) == 100
    assert len(re.findall(r"<th[ >]", page)) == 6
    assert "<th>Status</th>" in page
    assert "<th>Last error</th>" not in page


def test_index_empty_state_colspan_matches_columns():
    with TestClient(app) as c:
        page = c.get("/").text
    assert '<td colspan="6" class="empty">' in page


def test_index_empty_row_present_but_hidden_when_feeds_exist(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        page = c.get("/").text
        assert 'id="feed-empty">' in page
        assert "No feeds yet" in page

        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text
        assert 'id="feed-empty" hidden>' in page
        assert "No feeds yet" in page


def test_phone_layout_hides_items_last_polled_and_source_url(monkeypatch):
    """At <=600px, Items, Last polled and the source URL collapse; the table still has
    6 <th> and the fixed-layout escape hatch drops its old 640px min-width floor."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/").text

    assert page.count('<th class="phone-hide">') == 2
    assert page.index('<th class="phone-hide">Items</th>') < page.index(
        '<th class="phone-hide">Last polled</th>'
    )
    assert len(re.findall(r"<th[ >]", page)) == 6

    row_start = page.index('<tr id="feed-1"')
    row_end = page.index("</tr>", row_start)
    row = page[row_start:row_end]
    assert row.count("phone-hide") == 2
    assert 'class="muted url"' in row

    assert (
        '@media (max-width: 600px) { .phone-hide, table .url { display: none; } }' in page
    )
    assert "table-layout: auto;" in page
    assert "td.output { min-width: 5rem; }" in page
    assert "min-width: 640px" not in page


def test_feed_edit_radios_keep_their_controls(monkeypatch):
    """The 100%-width field rule must not reach radios, or the controls collapse."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text

    assert page.count('type="radio"') == 13
    assert ".field input, .field textarea { width: 100%; }" not in page
    assert "accent-color: var(--accent)" in page


def test_feed_edit_page_says_global_linked_to_settings(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        page = c.get("/feeds/1").text
        assert page.count('<a href="/settings">Global</a>') == 3
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


def test_feed_edit_page_filter_log_summarize_button_for_topic_and_ad_entries(monkeypatch):
    """A row with a guid gets a Summarize form/button, for both an ad entry and a
    topic entry; a legacy row without a guid gets none."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        with db() as conn:
            conn.execute(
                "UPDATE feeds SET last_polled_at = ?, last_filtered = ? WHERE id = 1",
                (
                    "2026-09-04T00:00:00+00:00",
                    json.dumps(
                        [
                            {
                                "kind": "ad",
                                "title": "Groupon Promo Codes: 60% Off",
                                "reason": "ad: coupon",
                                "guid": "guid-ad",
                                "link": "https://example.com/ad",
                                "published_at": "2026-09-04T00:00:00+00:00",
                            },
                            {
                                "kind": "topic",
                                "title": "Local Team Wins Match",
                                "reason": "topic: sport",
                                "guid": "guid-topic",
                                "link": "https://example.com/sport",
                                "published_at": "2026-09-04T00:00:00+00:00",
                            },
                            {
                                "kind": "ad",
                                "title": "Legacy Entry No Guid",
                                "reason": "ad: coupon",
                            },
                        ]
                    ),
                ),
            )
        page = c.get("/feeds/1").text

    assert page.count('action="/feeds/1/summarize"') == 2
    assert page.count('name="guid" value="guid-ad"') == 1
    assert page.count('name="guid" value="guid-topic"') == 1
    assert "Legacy Entry No Guid" in page
    legacy_row = page.split("Legacy Entry No Guid")[1].split("</li>")[0]
    assert "guid" not in legacy_row
    assert 'title="Summarize this item and include it in the output feed"' in page
    assert page.count("Summarize</button>") == 2


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


def _cookies_textarea_content(page: str) -> str:
    start = page.index('id="cookies_text"')
    open_end = page.index(">", start) + 1
    close = page.index("</textarea>", open_end)
    return page[open_end:close]


def test_settings_page_no_cookies_file_shows_placeholder():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert "No login saved yet." in page
    assert _cookies_textarea_content(page) == ""


def test_settings_page_links_cookie_editor():
    with TestClient(app) as c:
        page = c.get("/settings").text

    assert 'href="https://cookie-editor.com"' in page


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


def test_settings_expired_cookies_have_no_remove_button_and_empty_save_clears_them():
    past_expiry = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    write_cookies(f".ft.com\tTRUE\t/\tFALSE\t{past_expiry}\tsid\tabc\n")
    with TestClient(app) as c:
        page = c.get("/settings").text
        assert "all expired" in page
        assert 'action="/settings/cookies/delete"' not in page

        resp = c.post(
            "/settings/cookies", data={"cookies_text": ""}, follow_redirects=False
        )
        assert resp.status_code in (302, 303, 307, 308)
        location = resp.headers["location"]
        assert "Cookies+removed" in location or "Cookies%20removed" in location

        assert not (data_dir() / "cookies.txt").exists()

        page = c.get("/settings").text

    assert "No login saved yet." in page


def test_settings_cookies_section_is_outside_the_settings_form():
    write_cookies(f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n")
    with TestClient(app) as c:
        page = c.get("/settings").text

    form_start = page.index('action="/settings"')
    form_close = page.index("</form>", form_start)
    cookies_heading = page.index("Accessing Pay-Walled Content")
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
    ],
    ids=["garbage-json", "non-utf8"],
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


def test_cookies_empty_post_with_no_existing_file_redirects_removed():
    with TestClient(app) as c:
        resp = c.post("/settings/cookies", data={}, follow_redirects=False)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "err=" not in location
        assert "Cookies+removed" in location or "Cookies%20removed" in location

    assert not cookie_path().exists()


def test_cookies_empty_save_removes_existing_file():
    text = _netscape_cookies_text()
    with TestClient(app) as c:
        c.post("/settings/cookies", data={"cookies_text": text}, follow_redirects=False)

        resp = c.post(
            "/settings/cookies", data={"cookies_text": ""}, follow_redirects=False
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "Cookies+removed" in location or "Cookies%20removed" in location

        page = c.get("/settings").text

    assert not cookie_path().exists()
    assert "No login saved yet." in page


def test_settings_page_textarea_shows_current_cookies():
    text = _netscape_cookies_text()
    with TestClient(app) as c:
        resp = c.post(
            "/settings/cookies", data={"cookies_text": text}, follow_redirects=False
        )
        assert "UPLOADSECRET42" not in resp.headers["location"]

        page = c.get("/settings").text

    assert "UPLOADSECRET42" in _cookies_textarea_content(page)


def test_settings_page_escapes_cookie_text():
    # Not valid Netscape-format lines, so load_jar() fails to parse it (returns
    # None); the raw text must still be shown, HTML-escaped, in the textarea.
    text = "# Netscape HTTP Cookie File\nnot a valid cookie line <b>&\n"
    (data_dir() / "cookies.txt").write_text(text)
    assert load_jar() is None

    with TestClient(app) as c:
        page = c.get("/settings").text

    assert "&lt;b&gt;&amp;" in page
    textarea_content = _cookies_textarea_content(page)
    assert "&lt;b&gt;&amp;" in textarea_content
    assert "<b>&" not in textarea_content


def _insert_item(
    feed_id,
    guid,
    *,
    auth=None,
    link="https://www.example.com/a",
    published_at=None,
    fallback=0,
    fetch_status=None,
    muted=0,
    headline="Headline",
    summary="Summary.",
):
    with db() as conn:
        conn.execute(
            "INSERT INTO items(feed_id, guid, link, original_title, published_at, "
            "headline, summary, fallback, auth, fetch_status, created_at, muted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                feed_id,
                guid,
                link,
                "Original",
                published_at or db_now(),
                headline,
                summary,
                fallback,
                auth,
                fetch_status,
                db_now(),
                muted,
            ),
        )


def test_index_items_cell_no_longer_shows_login_indicator_counts(monkeypatch):
    """The 'via login / need login / unreadable with login' line left the Items cell
    for the Status column; Items keeps only the count and the ads-skipped line."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)

        _insert_item(1, "g1", auth="used")
        _insert_item(1, "g2", auth="used")
        _insert_item(1, "g3", auth="missing")
        _insert_item(1, "g4", auth="failed")
        _insert_item(1, "g5", auth=None)

        page = c.get("/").text

    assert "<div>5</div>" in page  # item_count for feed 1

    items_cell = _items_cell(page, 1)
    assert "via login" not in items_cell
    assert "need login" not in items_cell
    assert "unreadable with login" not in items_cell

    # No new column: still 6 <th>s, 6 <col> widths.
    widths = re.findall(r'<col style="width: (\d+)%">', page)
    assert len(widths) == 6
    assert len(re.findall(r"<th[ >]", page)) == 6


def test_status_cell_shows_paywalled_with_tooltip_and_settings_link(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="missing", fetch_status="teaser")
        _insert_item(1, "g2", auth="missing", fetch_status="teaser")
        _insert_item(1, "g3", auth=None, fetch_status="teaser")

        page = c.get("/").text

    assert "3 paywalled" in page
    assert "ℹ️" in page
    assert 'title="' in page
    assert "no login cookies are saved for" in page
    assert 'href="/settings#paywall"' in page
    assert "add login" in page


def test_status_cell_shows_login_failed_when_cookies_loaded(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="failed", fetch_status="teaser")
        write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n")

        page = c.get("/").text

    assert "unreadable with login" in page
    assert "check login" in page
    assert "Cookies for www.example.com are saved" in page
    assert "(earliest expiry 2100-01-01)" in page


def test_status_cell_shows_unreadable_for_error_and_null_fetch_status(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth=None, fallback=1, fetch_status="error")
        _insert_item(1, "g2", auth=None, fallback=1, fetch_status=None)

        page = c.get("/").text

    assert "2 unreadable" in page


def test_status_cell_shows_ok_muted_for_clean_feed(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth=None, fetch_status="ok")
        _insert_item(1, "g2", auth=None, fetch_status="ok")

        page = c.get("/").text

    assert 'class="info muted"' in page
    assert ">OK " in page


def test_feed_row_endpoint_matches_status_cell_on_index(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="missing", fetch_status="teaser")
        _insert_item(1, "g2", auth="missing", fetch_status="teaser")

        page = c.get("/").text
        row = c.get("/feeds/1/row").text.strip()

    start = page.index('<tr id="feed-1"')
    end = page.index("</tr>", start) + len("</tr>")
    assert page[start:end] == row
    assert "2 paywalled" in row


def test_feed_edit_page_status_shows_paywalled_with_tooltip_and_settings_link(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="missing", fetch_status="teaser")
        _insert_item(1, "g2", auth="missing", fetch_status="teaser")
        _insert_item(1, "g3", auth=None, fetch_status="teaser")

        page = c.get("/feeds/1").text

    assert "<h2>Status</h2>" in page
    assert "3 paywalled" in page
    assert "ℹ️" in page
    assert 'title="' in page
    assert "no login cookies are saved for" in page
    assert 'href="/settings#paywall"' in page
    assert "add login" in page


def test_feed_edit_page_status_shows_login_failed_when_cookies_loaded(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="failed", fetch_status="teaser")
        write_cookies(f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n")

        page = c.get("/feeds/1").text

    assert "1 unreadable with login" in page
    assert "check login" in page
    assert "Cookies for www.example.com are saved" in page
    assert "(earliest expiry 2100-01-01)" in page


def test_feed_edit_page_status_shows_unreadable_for_error_and_null_fetch_status(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth=None, fallback=1, fetch_status="error")
        _insert_item(1, "g2", auth=None, fallback=1, fetch_status=None)

        page = c.get("/feeds/1").text

    assert "2 unreadable" in page


def test_feed_edit_page_status_shows_ok_muted_for_clean_feed(monkeypatch):
    """The Status heading and its (muted) OK line are always shown, even for a feed
    with nothing wrong -- unlike the old Login section, which was omitted entirely."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth=None, fetch_status="ok")
        _insert_item(1, "g2", auth=None, fetch_status="ok")

        page = c.get("/feeds/1").text

    assert "<h2>Status</h2>" in page
    assert 'class="info muted"' in page
    assert ">OK " in page


def test_feed_edit_page_status_excludes_muted_items(monkeypatch):
    """items.muted = 1 rows are topic-muted items stored without a headline/summary
    and never published; the per-feed counts feeding the Status block and the
    retry-fallback button must not count them."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth=None, fetch_status="ok")
        _insert_item(
            1, "g2", auth=None, fetch_status="ok", muted=1, headline=None, summary=None
        )

        page = c.get("/feeds/1").text

    assert "All 1 articles read in full." in page


def test_feed_edit_page_status_domain_ignores_muted_newest_item(monkeypatch):
    """_feed_login_context derives the cookie domain from the most recently
    published item; a newer muted row (never published) must not hijack that
    domain lookup."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(
            1,
            "g1",
            auth="missing",
            fetch_status="teaser",
            link="https://www.example.com/a",
            published_at="2024-01-01T00:00:00+00:00",
        )
        _insert_item(
            1,
            "g2",
            auth=None,
            fetch_status="ok",
            muted=1,
            headline=None,
            summary=None,
            link="https://muted.example.org/x",
            published_at="2024-06-01T00:00:00+00:00",
        )

        page = c.get("/feeds/1").text

    assert "no login cookies are saved for www.example.com" in page
    assert "muted.example.org" not in page


def test_feed_page_status_matches_feeds_table(monkeypatch):
    """The feed-edit page's Status block and the feeds-table Status cell are built
    from the same counts and the same summarize() call; for the same feed they must
    read identically, both the visible phrases and the tooltip text."""
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        _insert_item(1, "g1", auth="missing", fetch_status="teaser")
        _insert_item(1, "g2", auth="failed", fetch_status="teaser")
        _insert_item(1, "g3", auth=None, fallback=1, fetch_status="error")
        _insert_item(1, "g4", auth="used", fetch_status="ok")

        index_page = c.get("/").text
        feed_page = c.get("/feeds/1").text

    index_status = _status_cell(index_page, 1)
    feed_status = _feed_page_status(feed_page)

    index_text = _strip_tags(index_status)
    feed_text = _strip_tags(feed_status)

    assert index_text == feed_text
    assert "paywalled" in index_text
    assert "unreadable with login" in index_text
    assert "unreadable" in index_text
    assert _titles(index_status) == _titles(feed_status)


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
        monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))
        monkeypatch.setattr(
            poll,
            "summarize",
            lambda text, original_title, url, respect_language=None, model=None: (
                "New Headline",
                "New summary",
            ),
        )
    else:
        monkeypatch.setattr(poll, "fetch_article", lambda link: (None, "error", []))

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
    monkeypatch.setattr(poll, "fetch_article", lambda link: ("FULL ARTICLE TEXT " * 20, "ok", []))

    from pintxos.summarize import MissingApiKey, SummarizeError

    calls = []

    def fake_summarize(text, original_title, url, respect_language=None, model=None):
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


# --- POST /feeds/{id}/summarize ---------------------------------------------


def test_summarize_route_unknown_feed_404(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    with TestClient(app) as c:
        resp = c.post("/feeds/999/summarize", data={"guid": "guid-1"}, follow_redirects=False)
    assert resp.status_code == 404


def test_summarize_route_item_not_found_redirects_with_err(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setattr(app_module, "filtered_entry", lambda feed_id, guid: None)
    calls = []
    monkeypatch.setattr(app_module, "summarize_one", lambda feed_id, guid: calls.append((feed_id, guid)))
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/summarize", data={"guid": "guid-1"}, follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/feeds/1?")
    assert "err=" in location
    assert "Item+not+found" in location or "Item%20not%20found" in location
    assert calls == []


def test_summarize_route_queues_summarize_one_and_redirects_with_msg(monkeypatch):
    monkeypatch.setattr(app_module, "poll_one", lambda feed_id: None)
    monkeypatch.setattr(
        app_module,
        "filtered_entry",
        lambda feed_id, guid: {"kind": "ad", "title": "t", "guid": guid, "link": "l"},
    )
    calls = []
    monkeypatch.setattr(app_module, "summarize_one", lambda feed_id, guid: calls.append((feed_id, guid)))
    with TestClient(app) as c:
        c.post("/feeds", data={"url": "https://example.com/feed.xml"}, follow_redirects=False)
        resp = c.post("/feeds/1/summarize", data={"guid": "guid-1"}, follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/feeds/1?")
    assert "msg=" in location
    assert calls == [(1, "guid-1")]
