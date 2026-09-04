"""FastAPI application."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from apscheduler.jobstores.base import JobLookupError
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from pintxos import adfilter
from pintxos.config import get_setting, is_truthy
from pintxos.db import db, init_db, now
from pintxos.feed_out import render_rss
from pintxos.poll import _status as poll_status
from pintxos.poll import poll_one, reschedule, scheduler, start_scheduler

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
    # ponytail: env flag so TestClient/CI don't spawn a real poller thread.
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


@app.get("/")
def index(request: Request) -> Response:
    with db() as conn:
        rows = conn.execute(
            "SELECT f.*, "
            "(SELECT COUNT(*) FROM items WHERE items.feed_id = f.id) AS item_count "
            "FROM feeds f ORDER BY f.id"
        ).fetchall()
        base_url = get_setting("PINTXOS_BASE_URL", conn) or str(request.base_url).rstrip("/")
    feeds = []
    for row in rows:
        feed = dict(row)
        feed["output_url"] = f"{base_url}/feeds/{feed['id']}.xml"
        feeds.append(feed)
    return templates.TemplateResponse(
        request, "index.html", {"feeds": feeds, "status": dict(poll_status)}
    )


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
def poll_feed_now(feed_id: int) -> Response:
    poll_one(feed_id)
    return _redirect("/")


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
        row = conn.execute("SELECT value FROM settings WHERE key = ?", ("ANTHROPIC_API_KEY",)).fetchone()
    env_key_set = env_pinned("ANTHROPIC_API_KEY")
    key_last4 = row["value"][-4:] if row and row["value"] else None
    filter_ads_on = is_truthy(filter_ads)
    filter_ads_env = env_pinned("PINTXOS_FILTER_ADS")
    patterns_env = env_pinned("PINTXOS_AD_TITLE_PATTERNS")
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

    patterns = "\n".join(line.rstrip() for line in ad_title_patterns.splitlines()).strip("\n")
    try:
        adfilter.compile_patterns(patterns)
    except ValueError as e:
        return _redirect("/settings", err=f"Invalid pattern: {e}")

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
        pairs.append(("PINTXOS_AD_TITLE_PATTERNS", patterns))

    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", pairs
        )

    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        reschedule(poll_minutes_i)

    return _redirect("/settings", msg="Saved")
