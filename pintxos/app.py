"""FastAPI application."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pintxos.config import get_setting
from pintxos.db import db, init_db, now
from pintxos.engine import PollEngine
from pintxos.feed_out import render_rss
from pintxos.poll import poll_feed, reschedule, scheduler, start_scheduler

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


# States in which a feed is actively being worked on by the engine.
ACTIVE_STATES = {"queued", "fetching", "summarizing"}


def status_label(state: str, progress: dict | None) -> str:
    """Render an engine feed state (+ optional progress) as a short UI label."""
    if state == "idle":
        return ""
    if state == "queued":
        return "Queued"
    if state == "fetching":
        return "Fetching feed…"
    if state == "summarizing":
        if progress is None:
            return "Summarizing"
        return f"Summarizing {progress['done']}/{progress['total']}"
    if state == "error":
        return "Error"
    return state


# Singleton in-process poll engine: one queue + worker thread for the whole
# app. poll_feed already has the (feed_id, reporter) signature the engine
# expects, so it's injected directly.
engine = PollEngine(poll_fn=poll_feed)


def enqueue_all_feeds() -> int:
    """Enqueue every feed on the engine; returns the count actually queued."""
    with db() as conn:
        feed_ids = [row["id"] for row in conn.execute("SELECT id FROM feeds ORDER BY id")]
    return engine.enqueue_all(feed_ids)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # The worker thread is cheap when idle, and tests need it running to
    # exercise the queue, so always start it (unlike the APScheduler below).
    engine.start()
    # ponytail: env flag so TestClient/CI don't spawn a real scheduler thread.
    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        start_scheduler(enqueue_all_feeds)
    yield
    engine.stop()
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Pintxøs", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    with db() as conn:
        feeds = conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"]
    snap = engine.snapshot()
    return {
        "ok": True,
        "feeds": feeds,
        "engine": {
            "running": snap["running"],
            "current": snap["current"],
            "queued": len(snap["queue"]),
            "paused_reason": snap["paused_reason"],
        },
    }


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
    snap = engine.snapshot()
    engine_feeds = snap["feeds"]
    feeds = []
    for row in rows:
        feed = dict(row)
        feed["output_url"] = f"{base_url}/feeds/{feed['id']}.xml"
        eng = engine_feeds.get(feed["id"])
        feed["state"] = eng["state"] if eng else "idle"
        feed["progress"] = eng["progress"] if eng else None
        feed["last_result"] = eng["last_result"] if eng else None
        feed["status_label"] = status_label(feed["state"], feed["progress"])
        feed["active"] = feed["state"] in ACTIVE_STATES
        feeds.append(feed)
    return templates.TemplateResponse(
        request, "index.html", {"feeds": feeds, "paused_reason": snap["paused_reason"]}
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
    engine.enqueue(new_id)
    return _redirect("/")


@app.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int) -> Response:
    with db() as conn:
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    engine.forget(feed_id)
    return _redirect("/")


@app.post("/feeds/{feed_id}/poll")
def poll_feed_now(feed_id: int) -> Response:
    engine.enqueue(feed_id)
    return _redirect("/")


@app.get("/settings")
def settings_page(request: Request) -> Response:
    with db() as conn:
        model = get_setting("PINTXOS_MODEL", conn)
        poll_minutes = get_setting("PINTXOS_POLL_MINUTES", conn)
        items_per_feed = get_setting("PINTXOS_ITEMS_PER_FEED", conn)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", ("ANTHROPIC_API_KEY",)).fetchone()
    env_key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
    key_last4 = row["value"][-4:] if row and row["value"] else None
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "model": model,
            "poll_minutes": poll_minutes,
            "items_per_feed": items_per_feed,
            "env_key_set": env_key_set,
            "key_last4": key_last4,
        },
    )


@app.post("/settings")
def save_settings(
    model: str = Form(...),
    poll_minutes: str = Form(...),
    items_per_feed: str = Form(...),
    api_key: str = Form(""),
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

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("PINTXOS_MODEL", model),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("PINTXOS_POLL_MINUTES", str(poll_minutes_i)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            ("PINTXOS_ITEMS_PER_FEED", str(items_per_feed_i)),
        )
        if api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                ("ANTHROPIC_API_KEY", api_key),
            )

    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        reschedule(poll_minutes_i)

    # Self-heal: if the engine paused itself for a missing API key and one is
    # now available (env var, or just submitted and stored above), re-enqueue
    # every feed so the pause clears on its own instead of waiting for the
    # next scheduled run.
    api_key_now_available = bool(os.environ.get("ANTHROPIC_API_KEY")) or bool(api_key)
    if engine.snapshot()["paused_reason"] and api_key_now_available:
        enqueue_all_feeds()

    return _redirect("/settings", msg="Saved")


@app.get("/api/status")
def api_status() -> Response:
    snap = engine.snapshot()
    engine_feeds = snap["feeds"]

    with db() as conn:
        rows = conn.execute(
            "SELECT f.*, "
            "(SELECT COUNT(*) FROM items WHERE items.feed_id = f.id) AS item_count "
            "FROM feeds f ORDER BY f.id"
        ).fetchall()

    feeds = []
    for row in rows:
        feed_id = row["id"]
        eng = engine_feeds.get(feed_id)
        state = eng["state"] if eng else "idle"
        progress = eng["progress"] if eng else None
        feeds.append(
            {
                "id": feed_id,
                "title": row["title"],
                "url": row["url"],
                "state": state,
                "progress": progress,
                "last_result": eng["last_result"] if eng else None,
                "item_count": row["item_count"],
                "last_polled_at": row["last_polled_at"],
                "last_polled_ago": ago(row["last_polled_at"]),
                "last_error": row["last_error"],
                "status_label": status_label(state, progress),
                "active": state in ACTIVE_STATES,
            }
        )

    next_run_at = None
    if scheduler.running:
        job = scheduler.get_job("poll_all")
        if job is not None and job.next_run_time is not None:
            next_run_at = job.next_run_time.isoformat()

    payload = {
        "engine": {
            "running": snap["running"],
            "current": snap["current"],
            "queue": snap["queue"],
            "paused_reason": snap["paused_reason"],
            "last_run_started_at": snap["last_run_started_at"],
            "last_run_finished_at": snap["last_run_finished_at"],
            "next_run_at": next_run_at,
        },
        "feeds": feeds,
    }
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})
