"""FastAPI application."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from apscheduler.jobstores.base import JobLookupError
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from pintxos import adfilter, feed_out, feedstats, llm
from pintxos.config import data_dir, get_setting, is_truthy
from pintxos.cookies import cookie_path, expiry_for, get_jar, has_cookies_for, load_jar, summary
from pintxos.db import db, init_db, now
from pintxos.feed_out import render_rss
from pintxos.fetch_status import summarize
from pintxos.poll import _status as poll_status
from pintxos.poll import (
    filtered_entry,
    poll_one,
    reschedule,
    retry_one,
    scheduler,
    start_scheduler,
    summarize_one,
)
from pintxos.topics import TOPICS

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ponytail: the four fetch-status buckets, written once and shared by the feeds-table
# query (_load_feed_rows, column prefix "i.") and the single-feed query
# (feed_edit_page, no prefix) so the two pages can never disagree on the numbers.
# Semantics match pintxos.fetch_status.summarize()'s expectations exactly.
_BUCKET_SQL = {
    "paywalled": (
        "SUM({i}fetch_status IN ('teaser', 'blocked') "
        "AND ({i}auth = 'missing' OR {i}auth IS NULL))"
    ),
    "login_failed": "SUM({i}auth = 'failed')",
    "unreadable": (
        "SUM({i}fallback = 1 AND ({i}auth = 'missing' OR {i}auth IS NULL) "
        "AND ({i}fetch_status = 'error' OR {i}fetch_status IS NULL))"
    ),
    "used": "SUM({i}auth = 'used')",
}


def _bucket_sql(i: str) -> str:
    """The four bucket expressions as 'expr AS name' clauses, column-prefixed by i."""
    return ", ".join(f"{expr.format(i=i)} AS {name}" for name, expr in _BUCKET_SQL.items())


def ago(iso: str | None, now: datetime | None = None) -> str:
    """Render an ISO8601 UTC timestamp as a compact relative time.

    None/empty -> "never"; parse failure -> the raw string unchanged.
    """
    if not iso:
        return "never"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    delta = reference - parsed
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}m{seconds}s" if seconds else f"{minutes}m"
    if total_seconds < 86400:
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days = total_seconds // 86400
    return f"{days}d"


templates.env.filters["ago"] = ago


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # ponytail: env flag so TestClient/CI never spawn a poller thread. It also stalls manual
    # polls (poll_one queues on the same scheduler); tests rely on that, so do not "fix" it.
    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        start_scheduler()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Pintxøs", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    with db() as conn:
        feeds = conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"]
    return {"ok": True, "feeds": feeds}


@app.get("/feeds/{feed_id}.xml")
def feed_xml(request: Request, feed_id: int) -> Response:
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            raise HTTPException(status_code=404, detail="feed not found")
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()
        full_text = is_truthy(get_setting("PINTXOS_FULL_TEXT", conn))

        warn_on = feed["warn_volume"] is None or feed["warn_volume"] == 1
        summaries_today = feedstats.totals(conn, feed_id)[0]
        level = feed_out.warning_level(summaries_today)
        if warn_on and level is not None:
            base_url = get_setting("PINTXOS_BASE_URL", conn) or str(request.base_url).rstrip("/")
            feed_page_url = f"{base_url}/feeds/{feed_id}"
            model = get_setting("PINTXOS_MODEL", conn)
            warning = feed_out.warning_item(
                feed,
                level=level,
                summaries_today=summaries_today,
                day=feedstats.today(),
                feed_page_url=feed_page_url,
                model=model,
            )
        else:
            warning = None

        body = render_rss(feed, items, full_text=full_text, warning=warning)
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


def _redirect(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    # ponytail: flash messages via query params, no session/cookie machinery.
    if err:
        path = f"{path}?err={quote(err)}"
    elif msg:
        path = f"{path}?msg={quote(msg)}"
    return RedirectResponse(url=path, status_code=303)


def _feed_login_context(
    conn: sqlite3.Connection, feed_id: int, feed_url: str, jar: object
) -> tuple[str, bool, str | None]:
    """Domain and cookie state for a feed, derived from its most recently published item.

    Falls back to the feed URL's hostname (or "") when there is no item yet. Returns
    (domain, cookies_loaded, cookie_expiry).
    """
    latest_link = conn.execute(
        "SELECT link FROM items WHERE feed_id = ? AND muted = 0 "
        "ORDER BY published_at DESC, id DESC LIMIT 1",
        (feed_id,),
    ).fetchone()
    article_host = urlparse(latest_link["link"]).hostname if latest_link else None
    domain = article_host or urlparse(feed_url).hostname or ""
    cookies_loaded = bool(jar) and has_cookies_for(jar, f"https://{domain}/")
    expiry = expiry_for(jar, domain)
    return domain, cookies_loaded, expiry


@app.get("/feeds/{feed_id}")
def feed_edit_page(request: Request, feed_id: int) -> Response:
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            raise HTTPException(status_code=404, detail="feed not found")
        warn_volume = 1 if feed["warn_volume"] is None else feed["warn_volume"]
        daily_budget = feed["daily_budget"]
        summaries_today, summaries_total = feedstats.totals(conn, feed_id)
        global_filter_ads_on = is_truthy(get_setting("PINTXOS_FILTER_ADS", conn))
        global_patterns = get_setting("PINTXOS_AD_TITLE_PATTERNS", conn) or ""
        global_respect_language_on = is_truthy(get_setting("PINTXOS_RESPECT_LANGUAGE", conn))
        global_model = get_setting("PINTXOS_MODEL", conn)
        counts = conn.execute(
            f"SELECT COUNT(*) AS total, SUM(fallback = 1) AS fallback_count, "
            f"{_bucket_sql('')} FROM items WHERE feed_id = ? AND muted = 0",
            (feed_id,),
        ).fetchone()
        fallback_count = counts["fallback_count"] or 0
        jar = get_jar()
        domain, cookies_loaded_for_domain, domain_expiry = _feed_login_context(
            conn, feed_id, feed["url"], jar
        )
        fetch_status = summarize(
            {
                "paywalled": counts["paywalled"] or 0,
                "login_failed": counts["login_failed"] or 0,
                "unreadable": counts["unreadable"] or 0,
                "used": counts["used"] or 0,
            },
            total=counts["total"] or 0,
            domain=domain,
            cookies_loaded=cookies_loaded_for_domain,
            cookie_expiry=domain_expiry,
        )
    try:
        last_filtered = json.loads(feed["last_filtered"] or "[]")
        if not isinstance(last_filtered, list):
            raise ValueError("last_filtered is not a list")
    except ValueError:
        last_filtered = []

    try:
        topic_counts = json.loads(feed["topic_counts"] or "{}")
        if not isinstance(topic_counts, dict):
            raise ValueError("topic_counts is not an object")
    except ValueError:
        topic_counts = {}

    try:
        mute_topics = json.loads(feed["mute_topics"] or "[]")
        if not isinstance(mute_topics, list):
            raise ValueError("mute_topics is not a list")
    except ValueError:
        mute_topics = []

    def _count(slug: str) -> int:
        value = topic_counts.get(slug, 0)
        return value if isinstance(value, int) else 0

    classified_total = sum(_count(slug) for slug, _name, _definition in TOPICS)
    topics = [
        {
            "slug": slug,
            "name": name,
            "definition": definition,
            "percent": (
                round(100 * _count(slug) / classified_total) if _count(slug) > 0 else None
            ),
        }
        for slug, name, definition in TOPICS
    ]

    return templates.TemplateResponse(
        request,
        "feed_edit.html",
        {
            "feed": dict(feed),
            "ad_title_patterns": feed["ad_title_patterns"] or "",
            "global_filter_ads_on": global_filter_ads_on,
            "global_patterns": global_patterns,
            "global_respect_language_on": global_respect_language_on,
            "last_filtered": last_filtered,
            "fallback_count": fallback_count,
            "fetch_status": fetch_status,
            "topics": topics,
            "classified_total": classified_total,
            "classify_topics": feed["classify_topics"] or 0,
            "mute_topics": mute_topics,
            "warn_volume": warn_volume,
            "daily_budget": daily_budget,
            "summaries_today": summaries_today,
            "summaries_total": summaries_total,
            "global_model": global_model,
        },
    )


@app.post("/feeds/{feed_id}")
def feed_edit_save(
    feed_id: int,
    title: str = Form(""),
    filter_ads: str = Form(""),
    ad_patterns_mode: str = Form(""),
    ad_title_patterns: str = Form(""),
    respect_language: str = Form(""),
    classify_topics: str = Form(""),
    mute_topics: list[str] = Form([]),
    warn_volume: str = Form(""),
    daily_budget: str = Form(""),
    model: str = Form(""),
) -> Response:
    if filter_ads not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid filter choice")
    if ad_patterns_mode not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid patterns choice")
    if respect_language not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid language choice")
    if classify_topics not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid topic choice")
    if warn_volume not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid warning choice")

    daily_budget_stripped = daily_budget.strip()
    if not daily_budget_stripped:
        daily_budget_value = None
    else:
        try:
            daily_budget_value = int(daily_budget_stripped)
        except ValueError:
            return _redirect(f"/feeds/{feed_id}", err="Daily budget must be a whole number")
        if daily_budget_value < 0:
            return _redirect(f"/feeds/{feed_id}", err="Daily budget must be a whole number")

    title = title.strip()
    if len(title) > 200:
        return _redirect(f"/feeds/{feed_id}", err="Title too long (max 200 characters)")

    try:
        adfilter.compile_patterns(ad_title_patterns)
    except ValueError as e:
        return _redirect(f"/feeds/{feed_id}", err=f"Invalid pattern: {e}")

    filter_ads_value = int(filter_ads) if filter_ads else None
    patterns_mode_value = int(ad_patterns_mode) if ad_patterns_mode else None
    respect_language_value = int(respect_language) if respect_language else None
    classify_topics_value = int(classify_topics) if classify_topics else None
    warn_volume_value = int(warn_volume) if warn_volume else None

    submitted_topics = set(mute_topics)
    mute_topics_ordered = [slug for slug, _name, _definition in TOPICS if slug in submitted_topics]
    mute_topics_value = json.dumps(mute_topics_ordered) if mute_topics_ordered else None

    model_value = model.strip() or None

    with db() as conn:
        if model_value is not None:
            if llm.provider(model_value) == "openrouter":
                if not _key_available("OPENROUTER_API_KEY", "", conn):
                    return _redirect(
                        f"/feeds/{feed_id}",
                        err=f"Model {model_value} needs an OpenRouter API key (OPENROUTER_API_KEY)",
                    )
            elif not _key_available("ANTHROPIC_API_KEY", "", conn):
                return _redirect(
                    f"/feeds/{feed_id}",
                    err=f"Model {model_value} needs an Anthropic API key (ANTHROPIC_API_KEY)",
                )

        cur = conn.execute(
            "UPDATE feeds SET title = ?, filter_ads = ?, ad_patterns_mode = ?, "
            "ad_title_patterns = ?, respect_language = ?, classify_topics = ?, "
            "mute_topics = ?, warn_volume = ?, daily_budget = ?, model = ? WHERE id = ?",
            (
                title or None,
                filter_ads_value,
                patterns_mode_value,
                ad_title_patterns or None,
                respect_language_value,
                classify_topics_value,
                mute_topics_value,
                warn_volume_value,
                daily_budget_value,
                model_value,
                feed_id,
            ),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="feed not found")

    return _redirect("/", msg="Saved")


def _load_feed_rows(request: Request, feed_id: int | None = None) -> list[dict]:
    """Feed dicts for the index table: every feed, or just one when feed_id is given.

    Shared by index() and the single-row endpoint so both render identical rows.
    """
    sql = (
        "SELECT f.*, COUNT(i.id) AS item_count, "
        f"{_bucket_sql('i.')} "
        "FROM feeds f LEFT JOIN items i ON i.feed_id = f.id AND i.muted = 0"
    )
    params: tuple = ()
    if feed_id is not None:
        sql += " WHERE f.id = ?"
        params = (feed_id,)
    sql += " GROUP BY f.id ORDER BY f.id"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        base_url = get_setting("PINTXOS_BASE_URL", conn) or str(request.base_url).rstrip("/")
        jar = get_jar()
        feeds = []
        for row in rows:
            feed = dict(row)
            feed["output_url"] = f"{base_url}/feeds/{feed['id']}.xml"
            paywalled = feed.pop("paywalled") or 0
            login_failed = feed.pop("login_failed") or 0
            unreadable = feed.pop("unreadable") or 0
            used = feed.pop("used") or 0
            # ponytail: one extra query per feed for the latest item link/domain;
            # fine at feeds-table scale, would need batching if the feed count grows large.
            domain, cookies_loaded, expiry = _feed_login_context(
                conn, feed["id"], feed["url"], jar
            )
            feed["fetch_status"] = summarize(
                {
                    "paywalled": paywalled,
                    "login_failed": login_failed,
                    "unreadable": unreadable,
                    "used": used,
                },
                total=feed["item_count"],
                domain=domain,
                cookies_loaded=cookies_loaded,
                cookie_expiry=expiry,
            )
            feeds.append(feed)
    return feeds


@app.get("/")
def index(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"feeds": _load_feed_rows(request), "status": dict(poll_status)},
    )


@app.get("/feeds/{feed_id}/row")
def feed_row(request: Request, feed_id: int) -> Response:
    """The single <tr> for one feed, so the page can swap a row in place."""
    feeds = _load_feed_rows(request, feed_id)
    if not feeds:
        raise HTTPException(status_code=404, detail="feed not found")
    return templates.TemplateResponse(
        request,
        "_feed_row.html",
        {"feed": feeds[0], "status": dict(poll_status)},
        media_type="text/html",
    )


@app.get("/status")
def status() -> dict[str, str]:
    return {str(feed_id): text for feed_id, text in poll_status.items()}


@app.post("/feeds")
def add_feed(url: str = Form(...)) -> Response:
    if not (url.startswith("http://") or url.startswith("https://")):
        return _redirect("/", err="URL must start with http:// or https://")
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO feeds(url, created_at) VALUES (?, ?)", (url, now())
            )
        except sqlite3.IntegrityError:
            return _redirect("/", err="Already subscribed")
        new_id = cur.lastrowid
    poll_one(new_id)
    return _redirect("/")


@app.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int) -> Response:
    with db() as conn:
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    # A manual poll may still be queued for this feed; drop it and its "Queued" status.
    try:
        scheduler.remove_job(f"feed-{feed_id}")
    except JobLookupError:
        pass
    poll_status.pop(feed_id, None)
    return _redirect("/")


@app.post("/feeds/{feed_id}/poll")
def poll_feed_now(feed_id: int, request: Request) -> Response:
    poll_one(feed_id)
    if request.headers.get("x-requested-with") == "fetch":
        return Response(status_code=204)
    return _redirect("/")


@app.post("/feeds/{feed_id}/retry-fallback")
def retry_fallback_route(feed_id: int) -> Response:
    with db() as conn:
        feed = conn.execute("SELECT id FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            raise HTTPException(status_code=404, detail="feed not found")
        n = conn.execute(
            "SELECT COUNT(*) FROM items WHERE feed_id = ? AND fallback = 1", (feed_id,)
        ).fetchone()[0]
        if n == 0:
            return _redirect("/", msg="No fallback items")
    retry_one(feed_id)
    return _redirect("/", msg=f"Retrying {n} item{'s' if n != 1 else ''}")


@app.post("/feeds/{feed_id}/summarize")
def summarize_route(feed_id: int, guid: str = Form(...)) -> Response:
    with db() as conn:
        feed = conn.execute("SELECT id FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            raise HTTPException(status_code=404, detail="feed not found")
    if filtered_entry(feed_id, guid) is None:
        return _redirect(f"/feeds/{feed_id}", err="Item not found")
    summarize_one(feed_id, guid)
    return _redirect(f"/feeds/{feed_id}", msg="Summarizing…")


def env_pinned(key: str) -> bool:
    return bool(os.environ.get(key))


def _key_available(key_name: str, submitted: str, conn: sqlite3.Connection) -> bool:
    """True if `submitted` is non-empty, the key is env-pinned, or one is already stored."""
    if submitted:
        return True
    if env_pinned(key_name):
        return True
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key_name,)).fetchone()
    return bool(row and row["value"])


@app.get("/settings")
def settings_page(request: Request) -> Response:
    with db() as conn:
        model = get_setting("PINTXOS_MODEL", conn)
        poll_minutes = get_setting("PINTXOS_POLL_MINUTES", conn)
        items_per_feed = get_setting("PINTXOS_ITEMS_PER_FEED", conn)
        filter_ads = get_setting("PINTXOS_FILTER_ADS", conn)
        full_text = get_setting("PINTXOS_FULL_TEXT", conn)
        respect_language = get_setting("PINTXOS_RESPECT_LANGUAGE", conn)
        ad_title_patterns = get_setting("PINTXOS_AD_TITLE_PATTERNS", conn) or ""
        ad_keep_patterns = get_setting("PINTXOS_AD_KEEP_PATTERNS", conn) or ""
        row = conn.execute("SELECT value FROM settings WHERE key = ?", ("ANTHROPIC_API_KEY",)).fetchone()
        openrouter_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("OPENROUTER_API_KEY",)
        ).fetchone()
    env_key_set = env_pinned("ANTHROPIC_API_KEY")
    key_last4 = row["value"][-4:] if row and row["value"] else None
    openrouter_env_key_set = env_pinned("OPENROUTER_API_KEY")
    openrouter_key_last4 = (
        openrouter_row["value"][-4:] if openrouter_row and openrouter_row["value"] else None
    )
    filter_ads_on = is_truthy(filter_ads)
    filter_ads_env = env_pinned("PINTXOS_FILTER_ADS")
    full_text_on = is_truthy(full_text)
    full_text_env = env_pinned("PINTXOS_FULL_TEXT")
    respect_language_on = is_truthy(respect_language)
    respect_language_env = env_pinned("PINTXOS_RESPECT_LANGUAGE")
    patterns_env = env_pinned("PINTXOS_AD_TITLE_PATTERNS")
    keep_patterns_env = env_pinned("PINTXOS_AD_KEEP_PATTERNS")
    jar = get_jar()
    cookie_domains = summary(jar) if jar else []
    cookie_file = str(cookie_path())
    cookie_file_exists = cookie_path().exists()
    try:
        cookies_text = cookie_path().read_text(errors="replace")
    except OSError:  # removed between exists() and read: show an empty box
        cookies_text = ""
    cookie_soon = (datetime.now(UTC) + timedelta(days=7)).date().isoformat()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "model": model,
            "poll_minutes": poll_minutes,
            "items_per_feed": items_per_feed,
            "env_key_set": env_key_set,
            "key_last4": key_last4,
            "openrouter_env_key_set": openrouter_env_key_set,
            "openrouter_key_last4": openrouter_key_last4,
            "filter_ads_on": filter_ads_on,
            "filter_ads_env": filter_ads_env,
            "full_text_on": full_text_on,
            "full_text_env": full_text_env,
            "respect_language_on": respect_language_on,
            "respect_language_env": respect_language_env,
            "ad_title_patterns": ad_title_patterns,
            "patterns_env": patterns_env,
            "ad_keep_patterns": ad_keep_patterns,
            "keep_patterns_env": keep_patterns_env,
            "cookie_domains": cookie_domains,
            "cookie_file": cookie_file,
            "cookie_file_exists": cookie_file_exists,
            "cookies_text": cookies_text,
            "cookie_soon": cookie_soon,
        },
    )


@app.post("/settings")
def save_settings(
    model: str = Form(...),
    poll_minutes: str = Form(...),
    items_per_feed: str = Form(...),
    api_key: str = Form(""),
    openrouter_api_key: str = Form(""),
    filter_ads: str = Form(""),
    ad_title_patterns: str = Form(""),
    ad_keep_patterns: str = Form(""),
    full_text: str = Form(""),
    respect_language: str = Form(""),
) -> Response:
    try:
        poll_minutes_i = int(poll_minutes)
        items_per_feed_i = int(items_per_feed)
    except ValueError:
        return _redirect("/settings", err="Poll interval and items per feed must be numbers")
    if not (1 <= poll_minutes_i <= 1440):
        return _redirect("/settings", err="Poll interval must be between 1 and 1440 minutes")
    if not (1 <= items_per_feed_i <= 500):
        return _redirect("/settings", err="Items per feed must be between 1 and 500")

    try:
        adfilter.compile_patterns(ad_title_patterns)
    except ValueError as e:
        return _redirect("/settings", err=f"Invalid pattern: {e}")

    try:
        adfilter.compile_patterns(ad_keep_patterns)
    except ValueError as e:
        return _redirect("/settings", err=f"Invalid keep pattern: {e}")

    model = model.strip()
    if not model:
        return _redirect("/settings", err="Model is required")

    with db() as conn:
        if llm.provider(model) == "openrouter":
            if not _key_available("OPENROUTER_API_KEY", openrouter_api_key, conn):
                return _redirect(
                    "/settings",
                    err=f"Model {model} needs an OpenRouter API key (OPENROUTER_API_KEY)",
                )
        elif not _key_available("ANTHROPIC_API_KEY", api_key, conn):
            return _redirect(
                "/settings",
                err=f"Model {model} needs an Anthropic API key (ANTHROPIC_API_KEY)",
            )

    pairs = [
        ("PINTXOS_MODEL", model),
        ("PINTXOS_POLL_MINUTES", str(poll_minutes_i)),
        ("PINTXOS_ITEMS_PER_FEED", str(items_per_feed_i)),
    ]
    if api_key and not env_pinned("ANTHROPIC_API_KEY"):
        pairs.append(("ANTHROPIC_API_KEY", api_key))
    if openrouter_api_key and not env_pinned("OPENROUTER_API_KEY"):
        pairs.append(("OPENROUTER_API_KEY", openrouter_api_key))
    # Disabled checkboxes/textareas aren't submitted by browsers, so when the
    # corresponding env var is set, the field is env-pinned: ignore it entirely.
    if not env_pinned("PINTXOS_FILTER_ADS"):
        pairs.append(("PINTXOS_FILTER_ADS", "1" if filter_ads == "1" else "0"))
    if not env_pinned("PINTXOS_AD_TITLE_PATTERNS"):
        pairs.append(("PINTXOS_AD_TITLE_PATTERNS", ad_title_patterns))
    if not env_pinned("PINTXOS_AD_KEEP_PATTERNS"):
        pairs.append(("PINTXOS_AD_KEEP_PATTERNS", ad_keep_patterns))
    if not env_pinned("PINTXOS_FULL_TEXT"):
        pairs.append(("PINTXOS_FULL_TEXT", "1" if full_text == "1" else "0"))
    if not env_pinned("PINTXOS_RESPECT_LANGUAGE"):
        pairs.append(("PINTXOS_RESPECT_LANGUAGE", "1" if respect_language == "1" else "0"))

    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", pairs
        )

    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        reschedule(poll_minutes_i)

    return _redirect("/settings", msg="Saved")


@app.post("/settings/test")
def test_settings() -> Response:
    with db() as conn:
        model = get_setting("PINTXOS_MODEL", conn)
    try:
        text = llm.complete("You are a health check.", "Reply with the single word OK.", 50, model)
    except llm.LLMError as e:
        return _redirect("/settings", err=f"{model}: {e}")
    return _redirect("/settings", msg=f"{model} answered: {text.strip()}")


@app.post("/settings/cookies")
async def upload_cookies(
    cookies: UploadFile | None = File(None),
    cookies_text: str = Form(""),
) -> Response:
    data: bytes = b""
    if cookies is not None:
        data = await cookies.read()
    if not data and cookies_text.strip():
        data = cookies_text.encode()
    if not data:
        cookie_path().unlink(missing_ok=True)
        return _redirect("/settings", msg="Cookies removed")
    if len(data) > 1024 * 1024:  # 1 MiB
        return _redirect("/settings", err="File too large")

    tmp = tempfile.NamedTemporaryFile(dir=data_dir(), delete=False)  # 0600 by default; os.replace keeps the mode
    tmp_path = Path(tmp.name)
    tmp.write(data)
    tmp.close()

    replaced = False
    try:
        # Validate via load_jar() itself so the flash counts match what polling will see.
        jar = load_jar(tmp_path)
        if jar is None:
            return _redirect("/settings", err="Not a Netscape cookies.txt file")

        os.replace(tmp_path, cookie_path())
        replaced = True
    finally:
        if not replaced:
            tmp_path.unlink(missing_ok=True)

    domains = summary(jar)
    count = len(jar)
    return _redirect(
        "/settings", msg=f"Cookies saved: {count} cookies for {len(domains)} domains"
    )
