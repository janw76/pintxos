"""Poll feeds: fetch, extract the article, summarize once, store, prune."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookiejar import MozillaCookieJar
from urllib.parse import urlparse

import curl_cffi.requests
import feedparser
import trafilatura
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from pintxos import adfilter, feedstats, pagemarkers, topics
from pintxos.config import DEFAULTS, get_setting, is_truthy
from pintxos.cookies import get_jar, has_cookies_for, save_jar
from pintxos.db import db, now
from pintxos.stats import word_count
from pintxos.summarize import MissingApiKey, SummarizeError, summarize

log = logging.getLogger("pintxos")

USER_AGENT = "pintxos/0.1 (+https://github.com/janw76/pintxos)"
MIN_ARTICLE_CHARS = 200
MIN_FALLBACK_CHARS = 50
MAX_LABELS = 30


def _make_client(profile: str | None) -> curl_cffi.requests.Session:
    """Build a curl_cffi session, optionally impersonating a browser's TLS/HTTP fingerprint."""
    # ponytail: impersonation supplies its own User-Agent, so we only set ours when off.
    headers = {} if profile else {"User-Agent": USER_AGENT}
    return curl_cffi.requests.Session(
        impersonate=profile or None, timeout=20, allow_redirects=True, headers=headers
    )


def _parse_profiles(value: str) -> list[str]:
    """Split a comma-separated PINTXOS_IMPERSONATE value into profiles; "" means none.

    Entries that are empty after stripping (from stray commas or blank spaces) are
    dropped; if nothing remains, the result is [""] so impersonation stays off.
    """
    return [p for p in (s.strip() for s in value.split(",")) if p] or [""]


# ponytail: one shared client per profile and one scheduler at module level. The
# scheduler runs a single worker thread, so every poll - scheduled or manual - is
# serialized by construction; ceiling is multi-process deployments (each process would
# poll). PINTXOS_IMPERSONATE is environment-only (see config.DEFAULTS), read directly
# from the environment here rather than via get_setting so building the clients at
# import time never touches the DB.
_profiles = _parse_profiles(os.environ.get("PINTXOS_IMPERSONATE", DEFAULTS["PINTXOS_IMPERSONATE"]))
_clients = [_make_client(p or None) for p in _profiles]
scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})

# Which jar object (if any) is currently installed on the clients' cookies. Compared by
# identity against get_jar()'s return value so we only reinstall when it changes.
_client_jar: MozillaCookieJar | None = None

# What each feed is doing right now, for the UI. In-memory: single process, dies with it.
_status: dict[int, str] = {}

# ponytail: per-feed offset into the id-DESC candidate list for retry_fallback's
# only_blocked rotation, so each poll advances past the rows it already retried
# instead of always retrying the newest ones. In-memory: resets on restart, and the
# candidate list can shrink between polls as rows heal -- the modulo below keeps the
# cursor valid either way. Ceiling: persist in the feeds table if that matters.
_retry_cursor: dict[int, int] = {}

_CHALLENGE_ATTEMPTS = 3
_HOST_PAUSE = 2.0
_last_request: dict[str, float] = {}
_BLOCKED_RETRIES = 3


def _get(url: str) -> curl_cffi.requests.Response:
    """Single seam for HTTP GETs so tests can monkeypatch one thing."""
    global _client_jar
    jar = get_jar()
    if jar is not _client_jar:
        # ponytail: jar installed on every shared session, swapped on identity;
        # ceiling: response cookies live only in memory.
        cookies = (
            curl_cffi.requests.Cookies(jar) if jar is not None else curl_cffi.requests.Cookies()
        )
        for client in _clients:
            client.cookies = cookies
        _client_jar = jar

    host = urlparse(url).hostname or ""
    # A challenge is probabilistic per request; retry across the profile list. The
    # per-host pacing below already waits out the two seconds between attempts.
    for attempt in range(_CHALLENGE_ATTEMPTS):
        wait = _last_request.get(host, 0.0) + _HOST_PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resp = _clients[attempt % len(_clients)].get(url)
        _last_request[host] = time.monotonic()
        if resp.status_code != 403 or resp.headers.get("cf-mitigated") != "challenge":
            return resp
        if attempt + 1 < _CHALLENGE_ATTEMPTS:
            log.info("cloudflare challenge on %s, retrying", url)
    return resp


def _page_labels(html: str) -> list[str]:
    """Publisher labels from the page's own metadata: article:section/article:tag and
    keywords (trafilatura's .categories/.tags), each comma-split, stripped, and with
    empties dropped. Categories first, then tags, in their original order. Empty on
    any failure -- this is a nice-to-have signal, not required for a successful fetch.
    """
    try:
        meta = trafilatura.extract_metadata(html)
        if meta is None:
            return []
        labels: list[str] = []
        for value in (meta.categories or []) + (meta.tags or []):
            for part in (value or "").split(","):
                part = part.strip()
                if part:
                    labels.append(part)
        return labels
    except Exception:
        return []


def fetch_article(link: str) -> tuple[str | None, str, list[str]]:
    """Full article text and why: (text, "ok", labels), or (None, reason, []) when it
    can't be read.

    The reason is "blocked" for HTTP 401, 403 or 429 (a paywall or a bot block),
    "short" for a 2xx page under MIN_ARTICLE_CHARS that declares itself free
    (schema.org isAccessibleForFree) or is non-article media -- readable, not a
    fallback, and never counted as paywalled -- "teaser" when a 2xx HTML page yields
    less than MIN_ARTICLE_CHARS of extracted text with no such marker, and "error"
    for everything else: a failed request, any other non-2xx status, or a non-HTML
    content type. `labels` is the page's own metadata labels (see `_page_labels`);
    the caller combines it with the feed entry's RSS categories.
    """
    status = "error"
    try:
        resp = _get(link)
        if resp.status_code // 100 != 2:
            if resp.status_code in (401, 403, 429):
                status = "blocked"
            raise ValueError(f"HTTP {resp.status_code}")
        if "html" not in resp.headers.get("content-type", "").lower():
            raise ValueError(f"content-type {resp.headers.get('content-type')!r}")
        text = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
        if not text or len(text) < MIN_ARTICLE_CHARS:
            reason = pagemarkers.free_short_page(resp.text)
            if reason is not None:
                log.info(
                    "short page (%s), keeping %d chars: %s", reason, len(text or ""), link
                )
                return text or "", "short", _page_labels(resp.text)
            status = "teaser"
            m = pagemarkers.page_markers(resp.text)
            pw = pagemarkers.paywall_markers(resp.text)
            log.info(
                "teaser fingerprint url=%s status=%s bytes=%d chars=%d free=%s og_type=%s "
                "jsonld=%s paywall=%s",
                link,
                resp.status_code,
                len(resp.content),
                len(text or ""),
                m.is_free,
                m.og_type or "-",
                ",".join(sorted(m.jsonld_types)) or "-",
                ",".join(pw) or "-",
            )
            raise ValueError(f"extracted {len(text or '')} chars")
        page_labels = _page_labels(resp.text)
    except Exception as e:
        log.info("article fetch failed, using feed content: %s (%s)", link, e)
        return None, status, []
    log.info("fetched article %s", link)
    return text, "ok", page_labels


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


def _respect_language(conn, feed) -> bool:
    """Effective respect-language toggle for `feed`: its override if set, else global."""
    override = feed["respect_language"]
    if override is not None:
        return bool(int(override))
    return is_truthy(get_setting("PINTXOS_RESPECT_LANGUAGE", conn))


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
) -> tuple[str | None, str | None, int | None, str, list[str]]:
    """Fetch `link` and work out (text, auth, word_count, fetch_status, page_labels).

    An entry with no link is never fetched at all, which counts as "error".
    """
    had = bool(link) and jar is not None and has_cookies_for(jar, link)
    text, fetch_status, page_labels = fetch_article(link) if link else (None, "error", [])
    words = word_count(text) if text is not None else None
    auth = None
    if text is None:
        auth = "failed" if had else "missing"
    elif had:
        auth = "used"
        # Cookies were sent and the fetch succeeded: curl_cffi may have rotated some of
        # them in memory (Set-Cookie on the response). Persist the jar so a restart
        # doesn't replay the stale, possibly-invalidated tokens.
        save_jar(jar)
    return text, auth, words, fetch_status, page_labels


def _rss_labels(entry) -> list[str]:
    """This entry's RSS <category> terms, original case, in feed order."""
    labels = []
    for tag in entry.get("tags") or []:
        term = (tag.get("term") or "").strip()
        if term:
            labels.append(term)
    return labels


def _dedupe_labels(labels: list[str]) -> list[str]:
    """Case-insensitive de-dupe keeping first-seen casing and order, capped at MAX_LABELS."""
    seen: set[str] = set()
    result = []
    for label in labels:
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(label)
        if len(result) >= MAX_LABELS:
            break
    return result


@dataclass
class ArticleInput:
    """The text and metadata pintxos hands to `summarize()` for one feed entry, plus
    the fetch bookkeeping poll_feed stores alongside it."""

    title: str
    link: str
    labels: list[str]
    text: str
    fetch_status: str
    auth: str | None
    word_count: int | None
    fallback: bool
    title_only: bool


def article_input(entry, jar: MozillaCookieJar | None) -> ArticleInput:
    """Build the exact input pintxos summarizes for one feed entry: fetch the full
    article, falling back to the feed's own excerpt when the fetch fails and then to
    the title alone when even that excerpt is too short, plus the labels (this
    entry's RSS categories combined with the fetched page's own metadata) and the
    auth/fetch_status/word_count bookkeeping poll_feed stores alongside it. A
    "short" fetch with no extracted text at all (a bare video/audio page) falls
    back the same way, to the feed excerpt or the title, but is not a fallback
    item -- the fetch itself succeeded. This is the single input-construction path
    pintxos uses before calling `summarize()`; external tools (e.g. a
    training-corpus capture script) that need identical parsing should call this
    too, so their input matches pintxos's runtime input. word_count is computed on
    the full extracted text before summarize() truncates it, and stays None for
    fallback items.
    """
    title = entry.get("title", "")
    # Must stay equivalent to poll_feed's (guid, link) derivation so the fetched URL
    # and the stored one never diverge.
    link = entry.get("link") or entry.get("id") or ""
    text, auth, words, fetch_status, page_labels = _fetch_and_auth(link, jar)
    labels = _dedupe_labels(_rss_labels(entry) + page_labels)
    fallback = False
    title_only = False
    if text is None:
        fallback = True
        text = _entry_text(entry)
        if len(text) < MIN_FALLBACK_CHARS:
            text = title
            title_only = True
    elif fetch_status == "short" and text == "":
        # A short page that declares itself free but yields no extracted text at
        # all (e.g. a pure video embed): not a fallback -- the fetch succeeded --
        # but there is nothing to summarize but the feed's own excerpt or title.
        text = _entry_text(entry)
        if len(text) < MIN_FALLBACK_CHARS:
            text = title
            title_only = True
    return ArticleInput(
        title=title,
        link=link,
        labels=labels,
        text=text,
        fetch_status=fetch_status,
        auth=auth,
        word_count=words,
        fallback=fallback,
        title_only=title_only,
    )


def _merge_labels(existing_json: str | None, new_labels: list[str]) -> str | None:
    """Merge this item's stored labels (JSON array, or None) with freshly fetched page
    labels: existing entries win on a case-insensitive collision, same cap as a fresh
    poll. None (not "[]") when the result is empty."""
    existing = json.loads(existing_json) if existing_json else []
    merged = _dedupe_labels(existing + new_labels)
    return json.dumps(merged) if merged else None


def _bump_topic_count(feed_id: int, topic: str) -> None:
    """Add one to this feed's seen-count for `topic` (JSON map slug -> count).

    Its own short transaction: never held across the classify call that produced
    the slug. A malformed stored value is replaced by a fresh map.
    """
    with db() as conn:
        row = conn.execute("SELECT topic_counts FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if row is None:  # feed deleted mid-poll: nothing to count on
            return
        try:
            counts = json.loads(row["topic_counts"] or "{}")
            if not isinstance(counts, dict):
                raise ValueError("topic_counts is not an object")
        except ValueError:
            counts = {}
        counts[topic] = int(counts.get(topic, 0) or 0) + 1
        conn.execute(
            "UPDATE feeds SET topic_counts = ? WHERE id = ?", (json.dumps(counts), feed_id)
        )


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
        classify_topics = bool(feed["classify_topics"])
        mute_topics = json.loads(feed["mute_topics"] or "[]")
        respect_language = _respect_language(conn, feed)
        extra_ad_patterns = _extra_ad_patterns(conn, feed) if filter_ads else []
        keep_patterns = _keep_patterns(conn) if filter_ads else []
        daily_budget = feed["daily_budget"]
        feed_model = feed["model"]
        summaries_today = feedstats.totals(conn, feed_id)[0]

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
                    filtered.append(
                        {
                            "kind": "ad",
                            "title": entry.get("title", ""),
                            "reason": f"ad: {reason}",
                            "guid": guid,
                            "link": link,
                            "published_at": _published_at(entry),
                        }
                    )
                    log.debug(
                        "feed %s: skipping ad (%s): %s", feed_id, reason, entry.get("title", "")
                    )
            if filtered:
                log.info("feed %s: filtered %d ad entries", feed_id, len(filtered))

        jar = get_jar()
        total = len(kept)
        budget_skipped = 0
        for i, (guid, link, entry) in enumerate(kept, 1):
            if daily_budget is not None and summaries_today >= daily_budget:
                budget_skipped += 1
                filtered.append(
                    {
                        "kind": "budget",
                        "title": entry.get("title", ""),
                        "reason": f"budget: {daily_budget}/day reached",
                        "guid": guid,
                        "link": link,
                        "published_at": _published_at(entry),
                    }
                )
                continue

            article = article_input(entry, jar)
            original_title = article.title
            labels_json = json.dumps(article.labels) if article.labels else None

            topic = None
            if classify_topics:
                # A short lead is enough to place an article; title-only items have no
                # body to quote, so they are classified from title and labels alone.
                lead = "" if article.title_only else " ".join(article.text.split()[:80])
                try:
                    topic = topics.classify_topic(
                        article.title, article.labels, lead, model=feed_model
                    )
                except MissingApiKey as e:
                    log.error("%s, stopping poll", e)
                    _set_error(feed_id, str(e), polled=False)
                    return False
                with db() as conn:
                    feedstats.bump(conn, feed_id, classifications=1)

            if topic is not None and topic in mute_topics:
                # Muted: stored (so the next poll sees it) but never summarized, and
                # hidden from the output feed. No headline/summary: nothing was paid for.
                with db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO items(feed_id, guid, link, original_title, "
                        "published_at, headline, summary, fallback, word_count, auth, "
                        "fetch_status, text, created_at, labels, topic, muted) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            feed_id, guid, link or "", original_title, _published_at(entry),
                            None, None, int(article.fallback), article.word_count,
                            article.auth, article.fetch_status,
                            None if article.title_only else article.text, now(), labels_json,
                            topic, 1,
                        ),
                    )
                filtered.append(
                    {
                        "kind": "topic",
                        "title": article.title,
                        "reason": f"topic: {topic}",
                        "guid": guid,
                        "link": link,
                        "published_at": _published_at(entry),
                    }
                )
                if topic is not None:
                    _bump_topic_count(feed_id, topic)
                log.info("feed %s: muting %s (topic %s): %s", feed_id, link, topic, article.title)
                continue

            log.info("summarizing %s", link)
            _status[feed_id] = f"Summarizing {i}/{total}"
            try:
                headline, summary = summarize(
                    article.text,
                    original_title,
                    link,
                    respect_language=respect_language,
                    model=feed_model,
                )
            except MissingApiKey as e:
                log.error("%s, stopping poll", e)
                _set_error(feed_id, str(e), polled=False)
                return False
            except SummarizeError as e:
                log.warning("summarize failed for %s: %s", link, e)
                continue  # not inserted: the next poll retries it

            with db() as conn:  # commit per item: a crash keeps what we already paid for
                conn.execute(
                    "INSERT OR IGNORE INTO items(feed_id, guid, link, original_title, "
                    "published_at, headline, summary, fallback, word_count, auth, "
                    "fetch_status, text, created_at, labels, topic, muted) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        feed_id, guid, link or "", original_title, _published_at(entry),
                        headline, summary, int(article.fallback), article.word_count,
                        article.auth, article.fetch_status,
                        None if article.title_only else article.text, now(), labels_json,
                        topic, 0,
                    ),
                )

            if topic is not None:
                _bump_topic_count(feed_id, topic)
            with db() as conn:
                feedstats.bump(conn, feed_id, summaries=1)
            summaries_today += 1

        if budget_skipped:
            log.info(
                "feed %s: daily budget %d reached, skipping %d entries",
                feed_id, daily_budget, budget_skipped,
            )

        if jar is not None:
            # ponytail: three blocked items per poll; ceiling: a site that blocks
            # everything costs three fetches per poll, no summary calls.
            retry_fallback(feed_id, limit=_BLOCKED_RETRIES, only_blocked=True)

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


def _drop_filtered_entry(feed_id: int, guid: str) -> None:
    """Remove `guid`'s entry (any kind) from this feed's last_filtered log, if
    present, and decrement ads_filtered to match (never below 0).

    Its own short transaction, re-read fresh: never held across the fetch or the
    summarize call above it, and safe even if the log changed underneath us.
    A no-op (no decrement) when the guid isn't found in the log.
    """
    with db() as conn:
        feed = conn.execute(
            "SELECT last_filtered, ads_filtered FROM feeds WHERE id = ?", (feed_id,)
        ).fetchone()
        if feed is None:  # feed deleted mid-release: nothing to update
            return
        try:
            filtered = json.loads(feed["last_filtered"] or "[]")
            if not isinstance(filtered, list):
                raise ValueError("last_filtered is not a list")
        except ValueError:
            return
        remaining = [entry for entry in filtered if entry.get("guid") != guid]
        if len(remaining) == len(filtered):
            return  # not present: nothing removed, so nothing to decrement
        ads_filtered = max(0, int(feed["ads_filtered"] or 0) - 1)
        conn.execute(
            "UPDATE feeds SET last_filtered = ?, ads_filtered = ? WHERE id = ?",
            (json.dumps(remaining), ads_filtered, feed_id),
        )


def filtered_entry(feed_id: int, guid: str) -> dict | None:
    """The muted item row or last_filtered log entry matching (feed_id, guid), as a
    dict, or None if neither exists.

    A muted row comes back as {"kind": "topic", "title", "guid", "link"}; a log
    entry comes back as stored (kind "ad" or "topic", plus title/reason/guid/link/
    published_at). Synchronous and read-only: the route uses this for an up-front
    existence check before queueing summarize_one.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT guid, link, original_title FROM items "
            "WHERE feed_id = ? AND guid = ? AND muted = 1",
            (feed_id, guid),
        ).fetchone()
        if row is not None:
            return {
                "kind": "topic",
                "title": row["original_title"],
                "guid": row["guid"],
                "link": row["link"],
            }
        feed = conn.execute("SELECT last_filtered FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if feed is None:
        return None
    try:
        filtered = json.loads(feed["last_filtered"] or "[]")
    except ValueError:
        return None
    for entry in filtered:
        if isinstance(entry, dict) and entry.get("guid") == guid:
            return entry
    return None


def summarize_item(feed_id: int, guid: str) -> str | None:
    """Release one item a poll set aside: unmute a stored muted row in place, or
    fetch+summarize+insert an item for an ad-filtered log entry. Returns an error
    message, or None on success.

    Never holds a db() write transaction across the fetch or the summarize call.
    Synchronous; summarize_one runs this on the scheduler thread.
    """
    with db() as conn:
        feed = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if feed is None:
            return "Feed not found"
        respect_language = _respect_language(conn, feed)
        feed_model = feed["model"]
        row = conn.execute(
            "SELECT * FROM items WHERE feed_id = ? AND guid = ?", (feed_id, guid)
        ).fetchone()
        filtered = json.loads(feed["last_filtered"] or "[]")

    _status[feed_id] = "Summarizing 1/1"
    try:
        if row is not None and row["muted"]:
            text = row["text"] if row["text"] is not None else row["original_title"]
            try:
                headline, summary = summarize(
                    text,
                    row["original_title"],
                    row["link"],
                    respect_language=respect_language,
                    model=feed_model,
                )
            except MissingApiKey as e:
                return str(e)
            except SummarizeError as e:
                return str(e)
            with db() as conn:
                # a row pruned meanwhile is a harmless no-op
                conn.execute(
                    "UPDATE items SET headline = ?, summary = ?, muted = 0 WHERE id = ?",
                    (headline, summary, row["id"]),
                )
            with db() as conn:
                feedstats.bump(conn, feed_id, summaries=1)
        else:
            entry = next(
                (e for e in filtered if isinstance(e, dict) and e.get("guid") == guid), None
            )
            if entry is None:
                return "Item not found"
            # The log doesn't keep the original RSS entry, so there is no body,
            # categories, or date to recover -- just enough for article_input() (and
            # the helpers it calls: _entry_text, _rss_labels) to read title/link.
            # The log's own ISO published_at is used directly at insert below rather
            # than round-tripped through _published_at(), which expects a parsed
            # time struct this minimal entry doesn't have.
            minimal_entry = {
                "id": entry.get("guid", guid),
                "link": entry.get("link", ""),
                "title": entry.get("title", ""),
            }
            jar = get_jar()
            article = article_input(minimal_entry, jar)
            labels_json = json.dumps(article.labels) if article.labels else None
            try:
                headline, summary = summarize(
                    article.text, article.title, article.link,
                    respect_language=respect_language,
                    model=feed_model,
                )
            except MissingApiKey as e:
                return str(e)
            except SummarizeError as e:
                return str(e)
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO items(feed_id, guid, link, original_title, "
                    "published_at, headline, summary, fallback, word_count, auth, "
                    "fetch_status, text, created_at, labels, topic, muted) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        feed_id, guid, article.link or "", article.title,
                        entry.get("published_at") or now(), headline, summary,
                        int(article.fallback), article.word_count, article.auth,
                        article.fetch_status, None if article.title_only else article.text,
                        now(), labels_json, None, 0,
                    ),
                )
            with db() as conn:
                feedstats.bump(conn, feed_id, summaries=1)

        _drop_filtered_entry(feed_id, guid)
        return None
    finally:
        _status.pop(feed_id, None)


def _run_summarize_job(feed_id: int, guid: str) -> None:
    """Job body for summarize_one: run summarize_item and log its error, if any."""
    error = summarize_item(feed_id, guid)
    if error is not None:
        log.warning("summarize_item feed %s guid %s failed: %s", feed_id, guid, error)


def summarize_one(feed_id: int, guid: str) -> None:
    """Queue a manual release of one filtered/muted item."""
    _status.setdefault(feed_id, "Queued")
    scheduler.add_job(
        _run_summarize_job,
        args=[feed_id, guid],
        id=f"summarize-{feed_id}-{guid}",
        replace_existing=True,
        misfire_grace_time=None,
    )


def retry_fallback(feed_id: int, limit: int | None = None, only_blocked: bool = False) -> None:
    """Re-fetch and re-summarize this feed's fallback items in place; never deletes."""
    sql = (
        "SELECT id, link, original_title, labels FROM items "
        "WHERE feed_id = ? AND fallback = 1 AND muted = 0"
    )
    params: list[object] = [feed_id]
    if only_blocked:
        # NULL: rows from before the column existed; one attempt gives them a real status.
        # The SQL LIMIT is skipped so we can filter by cookie coverage in Python first,
        # then truncate -- otherwise a host with no cookies could crowd out the limit
        # with items that have no chance of succeeding.
        sql += " AND (fetch_status = 'blocked' OR fetch_status IS NULL) ORDER BY id DESC"
    elif limit is not None:
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

    jar = get_jar()
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        feed_row = conn.execute(
            "SELECT respect_language, model FROM feeds WHERE id = ?", (feed_id,)
        ).fetchone()
        override = feed_row["respect_language"] if feed_row is not None else None
        respect_language = (
            bool(int(override))
            if override is not None
            else is_truthy(get_setting("PINTXOS_RESPECT_LANGUAGE", conn))
        )
        feed_model = feed_row["model"] if feed_row is not None else None

    if only_blocked:
        candidates = [
            row for row in rows if jar is not None and has_cookies_for(jar, row["link"])
        ]
        if limit is not None and candidates:
            # Rotate through the candidates instead of always taking the newest `limit`
            # of them, so blocked rows past the limit still get a turn on a later poll.
            # The cursor is taken modulo the current candidate count, which also keeps
            # it valid when rows heal and drop out of the list between polls.
            n = len(candidates)
            start = _retry_cursor.get(feed_id, 0) % n
            take = min(limit, n)
            rows = [candidates[(start + k) % n] for k in range(take)]
            _retry_cursor[feed_id] = (start + take) % n
        elif limit is not None:
            rows = candidates[:limit]
        else:
            rows = candidates

    total = len(rows)
    prev = _status.get(feed_id)
    try:
        for i, row in enumerate(rows, 1):
            item_id, link, original_title, existing_labels = (
                row["id"], row["link"], row["original_title"], row["labels"],
            )
            _status[feed_id] = f"Retrying {i}/{total}"
            text, auth, words, fetch_status, page_labels = _fetch_and_auth(link, jar)
            if text is None:
                with db() as conn:
                    conn.execute(
                        "UPDATE items SET auth = ?, fetch_status = ? WHERE id = ?",
                        (auth, fetch_status, item_id),
                    )
                continue

            # The fetch succeeded (even if summarize doesn't, below): fold the freshly
            # fetched page labels into whatever this item already had.
            merged_labels = _merge_labels(existing_labels, page_labels)

            # A "short" page with no extracted text at all (a bare video/audio page)
            # has nothing to summarize but its own title; store NULL rather than "".
            summarize_text = original_title if fetch_status == "short" and text == "" else text
            stored_text = None if fetch_status == "short" and text == "" else text

            try:
                headline, summary = summarize(
                    summarize_text,
                    original_title,
                    link,
                    respect_language=respect_language,
                    model=feed_model,
                )
            except MissingApiKey as e:
                log.error("%s, stopping retry", e)
                _set_error(feed_id, str(e), polled=False)
                return
            except SummarizeError as e:
                log.warning("summarize failed for %s: %s", link, e)
                with db() as conn:
                    # fetch succeeded even though summarize didn't: record the fresh
                    # auth/fetch_status/labels so the UI doesn't report stale data, but
                    # leave fallback = 1, headline, and summary untouched so a later
                    # retry still picks this item up.
                    conn.execute(
                        "UPDATE items SET auth = ?, fetch_status = ?, labels = ? WHERE id = ?",
                        (auth, fetch_status, merged_labels, item_id),
                    )
                continue  # left as a fallback item; a later retry can try again

            with db() as conn:  # commit per item: a crash keeps what we already paid for
                # a row pruned meanwhile is a harmless no-op
                conn.execute(
                    "UPDATE items SET headline = ?, summary = ?, fallback = 0, auth = ?, "
                    "word_count = ?, fetch_status = ?, text = ?, labels = ? WHERE id = ?",
                    (
                        headline, summary, auth, words, fetch_status,
                        stored_text, merged_labels, item_id,
                    ),
                )
            with db() as conn:
                feedstats.bump(conn, feed_id, summaries=1)
    finally:
        if prev is None:
            _status.pop(feed_id, None)
        else:
            _status[feed_id] = prev


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
