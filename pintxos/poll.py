"""Poll feeds: fetch, extract the article, summarize once, store, prune."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import feedparser
import httpx
import trafilatura
from apscheduler.schedulers.background import BackgroundScheduler

from pintxos.config import get_setting
from pintxos.db import db, now
from pintxos.engine import NullReporter, Reporter
from pintxos.summarize import MissingApiKey, SummarizeError, summarize

log = logging.getLogger("pintxos")

USER_AGENT = "pintxos/0.1 (+https://github.com/janw76/pintxos)"
MIN_ARTICLE_CHARS = 200
MIN_FALLBACK_CHARS = 50

# ponytail: one shared client and one scheduler at module level. Fine for a
# single-process app; ceiling is multi-worker deployments (each worker would poll).
_client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
scheduler = BackgroundScheduler()


def _get(url: str) -> httpx.Response:
    """Single seam for HTTP GETs so tests can monkeypatch one thing."""
    return _client.get(url)


def fetch_article(link: str) -> str | None:
    """Full article text, or None if the page can't be fetched or is too thin."""
    try:
        resp = _get(link)
        if resp.status_code // 100 != 2:
            raise ValueError(f"HTTP {resp.status_code}")
        if "html" not in resp.headers.get("content-type", "").lower():
            raise ValueError(f"content-type {resp.headers.get('content-type')!r}")
        text = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
        if not text or len(text) < MIN_ARTICLE_CHARS:
            raise ValueError(f"extracted {len(text or '')} chars")
    except Exception as e:
        log.info("article fetch failed, using feed content: %s (%s)", link, e)
        return None
    log.info("fetched article %s", link)
    return text


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = trafilatura.extract(f"<html><body>{html}</body></html>")
    if text is None:
        text = re.sub(r"<[^>]+>", " ", html)
    return " ".join(text.split())


def _entry_text(entry) -> str:
    html = (entry.get("content") or [{}])[0].get("value") or entry.get("summary", "")
    return _strip_html(html)


def _published_at(entry) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=UTC).isoformat() if parsed else now()


def _entry_sort_key(entry):
    """Newest first; entries with no date sort last."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    # Negated so a plain ascending sort puts the newest first and the undated last.
    return (0, tuple(-value for value in parsed[:6])) if parsed else (1, ())


def _set_error(feed_id: int, message: str, polled: bool = True) -> None:
    with db() as conn:
        if polled:
            conn.execute(
                "UPDATE feeds SET last_error = ?, last_polled_at = ? WHERE id = ?",
                (message[:500], now(), feed_id),
            )
        else:
            conn.execute(
                "UPDATE feeds SET last_error = ? WHERE id = ?", (message[:500], feed_id)
            )


def poll_feed(feed_id: int, reporter: Reporter | None = None) -> bool:
    """Poll one feed. Returns False if the whole run should stop (no API key).

    Reports progress through `reporter` (see pintxos.engine.Reporter), respecting
    the engine's transition table: fetching is reported before the feed GET;
    summarizing (if there are any new entries) after each new entry is processed;
    finished or failed exactly once at the end. If the feed row is missing
    (deleted), nothing is reported at all -- the engine auto-finishes a feed that
    never left the "fetching" state.
    """
    reporter = reporter or NullReporter()
    # Every DB connection below is short-lived: never hold a write transaction across a
    # network fetch or an Anthropic call, or the web UI blocks on "database is locked".
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            return True
        url, feed_title = feed["url"], feed["title"]
        limit = int(get_setting("PINTXOS_ITEMS_PER_FEED", conn))

    reporter.fetching(feed_id)

    try:
        resp = _get(url)
        if resp.status_code // 100 != 2:
            raise ValueError(f"HTTP {resp.status_code}")
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(str(parsed.bozo_exception))
    except Exception as e:
        log.warning("feed fetch failed %s: %s", url, e)
        _set_error(feed_id, str(e))
        reporter.failed(feed_id, str(e))
        return True

    with db() as conn:
        title = (parsed.feed.get("title") or "").strip()
        if title and not feed_title:
            conn.execute("UPDATE feeds SET title = ? WHERE id = ?", (title, feed_id))
        seen = {
            row["guid"]
            for row in conn.execute("SELECT guid FROM items WHERE feed_id = ?", (feed_id,))
        }

    entries = sorted(parsed.entries, key=_entry_sort_key)[:limit]
    new_entries = []
    for entry in entries:
        guid = entry.get("id") or entry.get("link")
        if guid and guid not in seen:
            new_entries.append(entry)
    total = len(new_entries)
    done = 0
    inserted = 0
    skipped = 0
    if total > 0:
        reporter.summarizing(feed_id, done, total)

    for entry in new_entries:
        guid = entry.get("id") or entry.get("link")
        link = entry.get("link") or guid

        original_title = entry.get("title", "")
        text = fetch_article(link) if link else None
        fallback = 0
        if text is None:
            fallback = 1
            text = _entry_text(entry)
            if len(text) < MIN_FALLBACK_CHARS:
                text = original_title

        log.info("summarizing %s", link)
        try:
            headline, summary = summarize(text, original_title, link)
        except MissingApiKey:
            log.error("ANTHROPIC_API_KEY not set, stopping poll")
            _set_error(feed_id, "ANTHROPIC_API_KEY not set", polled=False)
            reporter.api_key_missing(feed_id)
            return False
        except SummarizeError as e:
            log.warning("summarize failed for %s: %s", link, e)
            skipped += 1
            done += 1
            reporter.summarizing(feed_id, done, total)
            continue  # not inserted: the next poll retries it

        with db() as conn:  # commit per item: a crash keeps what we already paid for
            conn.execute(
                "INSERT OR IGNORE INTO items(feed_id, guid, link, original_title, "
                "published_at, headline, summary, fallback, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    feed_id, guid, link or "", original_title, _published_at(entry),
                    headline, summary, fallback, now(),
                ),
            )
        inserted += 1
        done += 1
        reporter.summarizing(feed_id, done, total)

    with db() as conn:
        conn.execute(
            "DELETE FROM items WHERE feed_id = ? AND id NOT IN "
            "(SELECT id FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC LIMIT ?)",
            (feed_id, feed_id, limit),
        )
        conn.execute(
            "UPDATE feeds SET last_polled_at = ?, last_error = NULL WHERE id = ?",
            (now(), feed_id),
        )
    reporter.finished(feed_id, inserted=inserted, skipped=skipped)
    return True


def _poll_all_compat() -> None:
    """Poll every feed, sequentially, with no progress reporting.

    Temporary until app.py wires the engine in pintxos-akx.3: this is the
    default `job` for start_scheduler() when app.py calls it with no
    arguments, replicating the old poll_all() behaviour (sequential, no
    lock -- APScheduler's default executor already prevents overlapping
    runs of the same job).
    """
    with db() as conn:
        feed_ids = [row["id"] for row in conn.execute("SELECT id FROM feeds ORDER BY id")]
    for feed_id in feed_ids:
        try:
            if not poll_feed(feed_id):
                break
        except Exception:
            log.exception("poll_feed %s blew up", feed_id)


def start_scheduler(job: Callable[[], None] | None = None) -> None:
    """Start the background poller: every N minutes, plus one run 10s from now.

    `job` is the callable to schedule; defaults to `_poll_all_compat` (a
    temporary shim -- see its docstring) when app.py calls start_scheduler()
    with no arguments. The job id stays "poll_all" so reschedule() keeps
    working regardless of which callable is scheduled.
    """
    job = job or _poll_all_compat
    minutes = int(get_setting("PINTXOS_POLL_MINUTES"))
    scheduler.add_job(
        job,
        "interval",
        minutes=minutes,
        id="poll_all",
        replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=10),
    )
    scheduler.start()
    log.info("scheduler started, polling every %s minutes", minutes)


def reschedule(minutes: int) -> None:
    """Change the poll interval at runtime (called when the setting changes)."""
    if scheduler.running:
        scheduler.reschedule_job("poll_all", trigger="interval", minutes=minutes)
