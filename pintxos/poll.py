"""Poll feeds: fetch, extract the article, summarize once, store, prune."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from http.cookiejar import MozillaCookieJar

import curl_cffi.requests
import feedparser
import trafilatura
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from pintxos import adfilter
from pintxos.config import DEFAULTS, get_setting, is_truthy
from pintxos.cookies import get_jar, has_cookies_for
from pintxos.db import db, now
from pintxos.stats import word_count
from pintxos.summarize import MissingApiKey, SummarizeError, summarize

log = logging.getLogger("pintxos")

USER_AGENT = "pintxos/0.1 (+https://github.com/janw76/pintxos)"
MIN_ARTICLE_CHARS = 200
MIN_FALLBACK_CHARS = 50


def _make_client(profile: str | None) -> curl_cffi.requests.Session:
    """Build a curl_cffi session, optionally impersonating a browser's TLS/HTTP fingerprint."""
    # ponytail: impersonation supplies its own User-Agent, so we only set ours when off.
    headers = {} if profile else {"User-Agent": USER_AGENT}
    return curl_cffi.requests.Session(
        impersonate=profile or None, timeout=20, allow_redirects=True, headers=headers
    )


# ponytail: one shared client and one scheduler at module level. The scheduler runs a
# single worker thread, so every poll - scheduled or manual - is serialized by
# construction; ceiling is multi-process deployments (each process would poll).
# PINTXOS_IMPERSONATE is environment-only (see config.DEFAULTS), read directly from
# the environment here rather than via get_setting so building the client at import
# time never touches the DB.
_client = _make_client(os.environ.get("PINTXOS_IMPERSONATE", DEFAULTS["PINTXOS_IMPERSONATE"]))
scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})

# Which jar object (if any) is currently installed on `_client.cookies`. Compared by
# identity against get_jar()'s return value so we only reinstall when it changes.
_client_jar: MozillaCookieJar | None = None

# What each feed is doing right now, for the UI. In-memory: single process, dies with it.
_status: dict[int, str] = {}


def _get(url: str) -> curl_cffi.requests.Response:
    """Single seam for HTTP GETs so tests can monkeypatch one thing."""
    global _client_jar
    jar = get_jar()
    if jar is not _client_jar:
        # ponytail: jar installed on the shared session, swapped on identity; ceiling:
        # response cookies live only in memory.
        _client.cookies = (
            curl_cffi.requests.Cookies(jar) if jar is not None else curl_cffi.requests.Cookies()
        )
        _client_jar = jar
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


def _filter_ads_enabled(conn, feed) -> bool:
    """Effective ad-filter toggle for `feed`: its override if set, else the global setting."""
    override = feed["filter_ads"]
    if override is not None:
        return bool(int(override))
    return is_truthy(get_setting("PINTXOS_FILTER_ADS", conn))


def _extra_ad_patterns(conn, feed) -> list[re.Pattern]:
    """Extra title patterns for this feed, honouring its ad_patterns_mode.

    NULL (inherit) uses the global patterns only, 1 adds the feed's own on top,
    0 means no extra title patterns at all (the built-in rules still apply).
    Both sources are optional and independently fault-tolerant: a line that
    doesn't compile is logged and skipped, and the rest still apply.
    """
    mode = feed["ad_patterns_mode"]
    if mode == 0:
        return []
    global_patterns = adfilter.compile_patterns(
        get_setting("PINTXOS_AD_TITLE_PATTERNS", conn) or "",
        on_error=lambda lineno, line, e: log.warning(
            "invalid PINTXOS_AD_TITLE_PATTERNS line %d %r: %s", lineno, line, e
        ),
    )
    if mode != 1:
        return global_patterns
    feed_id = feed["id"]
    feed_patterns = adfilter.compile_patterns(
        feed["ad_title_patterns"] or "",
        on_error=lambda lineno, line, e: log.warning(
            "feed %s: invalid title pattern line %d %r: %s", feed_id, lineno, line, e
        ),
    )
    return global_patterns + feed_patterns


def _keep_patterns(conn) -> list[re.Pattern]:
    """Global keep patterns: titles matching any of these are never filtered."""
    return adfilter.compile_patterns(
        get_setting("PINTXOS_AD_KEEP_PATTERNS", conn) or "",
        on_error=lambda lineno, line, e: log.warning(
            "invalid PINTXOS_AD_KEEP_PATTERNS line %d %r: %s", lineno, line, e
        ),
    )


def _fetch_and_auth(
    link: str, jar: MozillaCookieJar | None
) -> tuple[str | None, str | None, int | None]:
    """Fetch `link`'s article text and work out (text, auth, word_count) for it."""
    had = bool(link) and jar is not None and has_cookies_for(jar, link)
    text = fetch_article(link) if link else None
    words = word_count(text) if text is not None else None
    auth = None
    if text is None:
        auth = "failed" if had else "missing"
    elif had:
        auth = "used"
    return text, auth, words


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


def poll_feed(feed_id: int) -> bool:
    """Poll one feed. Returns False if the whole run should stop (no API key)."""
    # Every DB connection below is short-lived: never hold a write transaction across a
    # network fetch or an Anthropic call, or the web UI blocks on "database is locked".
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:  # deleted between queueing and running: clear any queued status
            _status.pop(feed_id, None)
            return True
        url, feed_title = feed["url"], feed["title"]
        limit = int(get_setting("PINTXOS_ITEMS_PER_FEED", conn))
        filter_ads = _filter_ads_enabled(conn, feed)
        extra_ad_patterns = _extra_ad_patterns(conn, feed) if filter_ads else []
        keep_patterns = _keep_patterns(conn) if filter_ads else []

    try:
        try:
            _status[feed_id] = "Fetching feed…"
            resp = _get(url)
            if resp.status_code // 100 != 2:
                raise ValueError(f"HTTP {resp.status_code}")
            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(str(parsed.bozo_exception))
        except Exception as e:
            log.warning("feed fetch failed %s: %s", url, e)
            _set_error(feed_id, str(e))
            return True

        with db() as conn:
            title = (parsed.feed.get("title") or "").strip()
            if title and not feed_title:
                conn.execute("UPDATE feeds SET title = ? WHERE id = ?", (title, feed_id))
            seen = {
                row["guid"]
                for row in conn.execute("SELECT guid FROM items WHERE feed_id = ?", (feed_id,))
            }

        # Count the new entries up front so the status line can say "3 of 7".
        new_entries = []
        for entry in sorted(parsed.entries, key=_entry_sort_key)[:limit]:
            guid = entry.get("id") or entry.get("link")
            if not guid or guid in seen:
                continue
            new_entries.append((guid, entry.get("link") or guid, entry))

        # Ad/coupon entries are filtered before fetch/summarize, but never inserted or
        # otherwise recorded as seen -- they are simply re-evaluated on the next poll.
        filtered = []
        kept = new_entries
        if filter_ads:
            kept = []
            for guid, link, entry in new_entries:
                reason = adfilter.is_ad(entry, extra_ad_patterns, keep_patterns=keep_patterns)
                if reason is None:
                    kept.append((guid, link, entry))
                else:
                    filtered.append({"title": entry.get("title", ""), "reason": reason})
                    log.debug(
                        "feed %s: skipping ad (%s): %s", feed_id, reason, entry.get("title", "")
                    )
            if filtered:
                log.info("feed %s: filtered %d ad entries", feed_id, len(filtered))

        jar = get_jar()
        total = len(kept)
        for i, (guid, link, entry) in enumerate(kept, 1):
            original_title = entry.get("title", "")
            # Word count only when we actually read the article, on the full extracted
            # text (before summarize() truncates it); fallback items stay NULL.
            text, auth, words = _fetch_and_auth(link, jar)
            fallback = 0
            if text is None:
                fallback = 1
                text = _entry_text(entry)
                if len(text) < MIN_FALLBACK_CHARS:
                    text = original_title

            log.info("summarizing %s", link)
            _status[feed_id] = f"Summarizing {i}/{total}"
            try:
                headline, summary = summarize(text, original_title, link)
            except MissingApiKey:
                log.error("ANTHROPIC_API_KEY not set, stopping poll")
                _set_error(feed_id, "ANTHROPIC_API_KEY not set", polled=False)
                return False
            except SummarizeError as e:
                log.warning("summarize failed for %s: %s", link, e)
                continue  # not inserted: the next poll retries it

            with db() as conn:  # commit per item: a crash keeps what we already paid for
                conn.execute(
                    "INSERT OR IGNORE INTO items(feed_id, guid, link, original_title, "
                    "published_at, headline, summary, fallback, word_count, auth, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        feed_id, guid, link or "", original_title, _published_at(entry),
                        headline, summary, fallback, words, auth, now(),
                    ),
                )

        with db() as conn:
            conn.execute(
                "DELETE FROM items WHERE feed_id = ? AND id NOT IN "
                "(SELECT id FROM items WHERE feed_id = ? ORDER BY published_at DESC, id DESC "
                "LIMIT ?)",
                (feed_id, feed_id, limit),
            )
            conn.execute(
                "UPDATE feeds SET last_polled_at = ?, last_error = NULL, ads_filtered = ?, "
                "last_filtered = ? WHERE id = ?",
                (now(), len(filtered), json.dumps(filtered), feed_id),
            )
        return True
    finally:
        _status.pop(feed_id, None)


def retry_fallback(feed_id: int) -> None:
    """Re-fetch and re-summarize this feed's fallback items in place; never deletes."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, link, original_title FROM items WHERE feed_id = ? AND fallback = 1",
            (feed_id,),
        ).fetchall()

    jar = get_jar()
    total = len(rows)
    try:
        for i, row in enumerate(rows, 1):
            item_id, link, original_title = row["id"], row["link"], row["original_title"]
            _status[feed_id] = f"Retrying {i}/{total}"
            text, auth, words = _fetch_and_auth(link, jar)
            if text is None:
                with db() as conn:
                    conn.execute("UPDATE items SET auth = ? WHERE id = ?", (auth, item_id))
                continue

            try:
                headline, summary = summarize(text, original_title, link)
            except MissingApiKey:
                log.error("ANTHROPIC_API_KEY not set, stopping retry")
                _set_error(feed_id, "ANTHROPIC_API_KEY not set", polled=False)
                return
            except SummarizeError as e:
                log.warning("summarize failed for %s: %s", link, e)
                continue  # left as a fallback item; a later retry can try again

            with db() as conn:  # commit per item: a crash keeps what we already paid for
                # a row pruned meanwhile is a harmless no-op
                conn.execute(
                    "UPDATE items SET headline = ?, summary = ?, fallback = 0, auth = ?, "
                    "word_count = ? WHERE id = ?",
                    (headline, summary, auth, words, item_id),
                )
    finally:
        _status.pop(feed_id, None)


def retry_one(feed_id: int) -> None:
    """Queue a manual retry of one feed's fallback items."""
    _status.setdefault(feed_id, "Queued")
    scheduler.add_job(
        retry_fallback,
        args=[feed_id],
        id=f"retry-{feed_id}",
        replace_existing=True,
        misfire_grace_time=None,
    )


def poll_one(feed_id: int) -> None:
    """Queue a manual poll; a second click before it runs is a no-op."""
    _status.setdefault(feed_id, "Queued")
    scheduler.add_job(
        poll_feed,
        args=[feed_id],
        id=f"feed-{feed_id}",
        replace_existing=True,
        misfire_grace_time=None,
    )


def poll_all() -> None:
    """Poll every feed, sequentially."""
    # ponytail: sequential and global; switch to per-feed threads if >20 feeds.
    with db() as conn:
        feed_ids = [row["id"] for row in conn.execute("SELECT id FROM feeds ORDER BY id")]
    for feed_id in feed_ids:
        try:
            if not poll_feed(feed_id):
                break
        except Exception:
            log.exception("poll_feed %s blew up", feed_id)


def start_scheduler() -> None:
    """Start the background poller: every N minutes, plus one run 10s from now."""
    minutes = int(get_setting("PINTXOS_POLL_MINUTES"))
    scheduler.add_job(
        poll_all,
        "interval",
        minutes=minutes,
        id="poll_all",
        replace_existing=True,
        max_instances=1,
        next_run_time=datetime.now(UTC) + timedelta(seconds=10),
    )
    scheduler.start()
    log.info("scheduler started, polling every %s minutes", minutes)


def reschedule(minutes: int) -> None:
    """Change the poll interval at runtime (called when the setting changes)."""
    if scheduler.running:
        scheduler.reschedule_job("poll_all", trigger="interval", minutes=minutes)
