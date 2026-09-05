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

from pintxos import adfilter
from pintxos.config import data_dir, get_setting, is_truthy
from pintxos.cookies import cookie_path, expiry_for, get_jar, has_cookies_for, load_jar, summary
from pintxos.db import db, init_db, now
from pintxos.feed_out import render_rss
from pintxos.poll import _status as poll_status
from pintxos.poll import poll_one, reschedule, retry_one, scheduler, start_scheduler

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
def feed_xml(feed_id: int) -> Response:
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            raise HTTPException(status_code=404, detail="feed not found")
        items = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC",
            (feed_id,),
        ).fetchall()
        body = render_rss(feed, items)
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


def _redirect(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    # ponytail: flash messages via query params, no session/cookie machinery.
    if err:
        path = f"{path}?err={quote(err)}"
    elif msg:
        path = f"{path}?msg={quote(msg)}"
    return RedirectResponse(url=path, status_code=303)


@app.get("/feeds/{feed_id}")
def feed_edit_page(request: Request, feed_id: int) -> Response:
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            raise HTTPException(status_code=404, detail="feed not found")
        global_filter_ads_on = is_truthy(get_setting("PINTXOS_FILTER_ADS", conn))
        global_patterns = get_setting("PINTXOS_AD_TITLE_PATTERNS", conn) or ""
        counts = conn.execute(
            "SELECT SUM(fallback = 1) AS fallback_count, SUM(auth = 'used') AS auth_used, "
            "SUM(auth = 'failed') AS auth_failed, SUM(auth = 'missing') AS auth_missing "
            "FROM items WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        fallback_count = counts["fallback_count"] or 0
        auth_used = counts["auth_used"] or 0
        auth_failed = counts["auth_failed"] or 0
        auth_missing = counts["auth_missing"] or 0
        latest_link = conn.execute(
            "SELECT link FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC "
            "LIMIT 1",
            (feed_id,),
        ).fetchone()
    try:
        last_filtered = json.loads(feed["last_filtered"] or "[]")
        if not isinstance(last_filtered, list):
            raise ValueError("last_filtered is not a list")
    except ValueError:
        last_filtered = []

    article_host = urlparse(latest_link["link"]).hostname if latest_link else None
    domain = article_host or urlparse(feed["url"]).hostname or ""

    jar = get_jar()
    cookies_loaded_for_domain = bool(jar) and has_cookies_for(jar, f"https://{domain}/")
    domain_expiry = expiry_for(jar, domain)

    return templates.TemplateResponse(
        request,
        "feed_edit.html",
        {
            "feed": dict(feed),
            "ad_title_patterns": feed["ad_title_patterns"] or "",
            "global_filter_ads_on": global_filter_ads_on,
            "global_patterns": global_patterns,
            "last_filtered": last_filtered,
            "fallback_count": fallback_count,
            "auth_used": auth_used,
            "auth_failed": auth_failed,
            "auth_missing": auth_missing,
            "domain": domain,
            "cookies_loaded_for_domain": cookies_loaded_for_domain,
            "domain_expiry": domain_expiry,
        },
    )


@app.post("/feeds/{feed_id}")
def feed_edit_save(
    feed_id: int,
    filter_ads: str = Form(""),
    ad_patterns_mode: str = Form(""),
    ad_title_patterns: str = Form(""),
) -> Response:
    if filter_ads not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid filter choice")
    if ad_patterns_mode not in ("", "0", "1"):
        return _redirect(f"/feeds/{feed_id}", err="Invalid patterns choice")

    try:
        adfilter.compile_patterns(ad_title_patterns)
    except ValueError as e:
        return _redirect(f"/feeds/{feed_id}", err=f"Invalid pattern: {e}")

    filter_ads_value = int(filter_ads) if filter_ads else None
    patterns_mode_value = int(ad_patterns_mode) if ad_patterns_mode else None

    with db() as conn:
        cur = conn.execute(
            "UPDATE feeds SET filter_ads = ?, ad_patterns_mode = ?, ad_title_patterns = ? "
            "WHERE id = ?",
            (filter_ads_value, patterns_mode_value, ad_title_patterns or None, feed_id),
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
        "SUM(i.auth = 'used') AS auth_used, "
        "SUM(i.auth = 'failed') AS auth_failed, "
        "SUM(i.auth = 'missing') AS auth_missing "
        "FROM feeds f LEFT JOIN items i ON i.feed_id = f.id"
    )
    params: tuple = ()
    if feed_id is not None:
        sql += " WHERE f.id = ?"
        params = (feed_id,)
    sql += " GROUP BY f.id ORDER BY f.id"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        base_url = get_setting("PINTXOS_BASE_URL", conn) or str(request.base_url).rstrip("/")
    feeds = []
    for row in rows:
        feed = dict(row)
        feed["output_url"] = f"{base_url}/feeds/{feed['id']}.xml"
        feed["auth_used"] = feed["auth_used"] or 0
        feed["auth_failed"] = feed["auth_failed"] or 0
        feed["auth_missing"] = feed["auth_missing"] or 0
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


def env_pinned(key: str) -> bool:
    return bool(os.environ.get(key))


@app.get("/settings")
def settings_page(request: Request) -> Response:
    with db() as conn:
        model = get_setting("PINTXOS_MODEL", conn)
        poll_minutes = get_setting("PINTXOS_POLL_MINUTES", conn)
        items_per_feed = get_setting("PINTXOS_ITEMS_PER_FEED", conn)
        filter_ads = get_setting("PINTXOS_FILTER_ADS", conn)
        ad_title_patterns = get_setting("PINTXOS_AD_TITLE_PATTERNS", conn) or ""
        ad_keep_patterns = get_setting("PINTXOS_AD_KEEP_PATTERNS", conn) or ""
        row = conn.execute("SELECT value FROM settings WHERE key = ?", ("ANTHROPIC_API_KEY",)).fetchone()
    env_key_set = env_pinned("ANTHROPIC_API_KEY")
    key_last4 = row["value"][-4:] if row and row["value"] else None
    filter_ads_on = is_truthy(filter_ads)
    filter_ads_env = env_pinned("PINTXOS_FILTER_ADS")
    patterns_env = env_pinned("PINTXOS_AD_TITLE_PATTERNS")
    keep_patterns_env = env_pinned("PINTXOS_AD_KEEP_PATTERNS")
    jar = get_jar()
    cookie_domains = summary(jar) if jar else []
    cookie_file = str(cookie_path())
    cookie_file_exists = cookie_path().exists()
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
            "filter_ads_on": filter_ads_on,
            "filter_ads_env": filter_ads_env,
            "ad_title_patterns": ad_title_patterns,
            "patterns_env": patterns_env,
            "ad_keep_patterns": ad_keep_patterns,
            "keep_patterns_env": keep_patterns_env,
            "cookie_domains": cookie_domains,
            "cookie_file": cookie_file,
            "cookie_file_exists": cookie_file_exists,
            "cookie_soon": cookie_soon,
        },
    )


@app.post("/settings")
def save_settings(
    model: str = Form(...),
    poll_minutes: str = Form(...),
    items_per_feed: str = Form(...),
    api_key: str = Form(""),
    filter_ads: str = Form(""),
    ad_title_patterns: str = Form(""),
    ad_keep_patterns: str = Form(""),
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

    pairs = [
        ("PINTXOS_MODEL", model),
        ("PINTXOS_POLL_MINUTES", str(poll_minutes_i)),
        ("PINTXOS_ITEMS_PER_FEED", str(items_per_feed_i)),
    ]
    if api_key and not env_pinned("ANTHROPIC_API_KEY"):
        pairs.append(("ANTHROPIC_API_KEY", api_key))
    # Disabled checkboxes/textareas aren't submitted by browsers, so when the
    # corresponding env var is set, the field is env-pinned: ignore it entirely.
    if not env_pinned("PINTXOS_FILTER_ADS"):
        pairs.append(("PINTXOS_FILTER_ADS", "1" if filter_ads == "1" else "0"))
    if not env_pinned("PINTXOS_AD_TITLE_PATTERNS"):
        pairs.append(("PINTXOS_AD_TITLE_PATTERNS", ad_title_patterns))
    if not env_pinned("PINTXOS_AD_KEEP_PATTERNS"):
        pairs.append(("PINTXOS_AD_KEEP_PATTERNS", ad_keep_patterns))

    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", pairs
        )

    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        reschedule(poll_minutes_i)

    return _redirect("/settings", msg="Saved")


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
        return _redirect("/settings", err="Nothing to upload")
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


@app.post("/settings/cookies/delete")
def delete_cookies() -> Response:
    cookie_path().unlink(missing_ok=True)
    return _redirect("/settings", msg="Cookies removed")
