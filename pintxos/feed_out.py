"""Render feed items as RSS 2.0 XML."""

from __future__ import annotations

import html
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import format_datetime

from pintxos.config import DEFAULTS, get_setting
from pintxos.stats import format_stats
from pintxos.topics import TOPIC_NAMES

_AUTH_NOTES = {
    "used": "<p><em>Read with your subscription.</em></p>",
    "missing": "<p><em>Login may be required; summarized from the feed excerpt.</em></p>",
    "failed": (
        "<p><em>Your saved login did not work (cookies expired?); "
        "summarized from the feed excerpt.</em></p>"
    ),
}

_FETCH_NOTES = {
    "teaser": (
        "<p><em>Only a teaser was available (paywall); "
        "summarized from the feed excerpt.</em></p>"
    ),
    "blocked": (
        "<p><em>The site blocked the fetch; "
        "summarized from the feed excerpt.</em></p>"
    ),
}

_EXHAUSTED_JSON_NOTE = (
    "Not summarized: the AI service returned an unusable answer three times. "
    "Pintxøs will not retry on its own; use Retry on the feed page."
)
_EXHAUSTED_GENERIC_NOTE = (
    "Not summarized: the AI service kept failing. "
    "Pintxøs will not retry on its own; use Retry on the feed page."
)

_TITLE_NORM_TABLE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)


def _norm_title(s: str) -> str:
    """Normalize a title for loose comparison (whitespace, case, quotes, dashes)."""
    return " ".join(s.strip().translate(_TITLE_NORM_TABLE).split()).casefold()


def _is_muted(item: sqlite3.Row) -> bool:
    """True when this item's topic is muted for its feed, so it stays out of the feed.

    Tolerates rows selected without the column (older callers): absent means not muted.
    """
    try:
        return bool(item["muted"])
    except (IndexError, KeyError):
        return False


def _topic_name(item: sqlite3.Row) -> str | None:
    """The IPTC topic name for this item's topic slug, or None if absent/unknown.

    Tolerates rows selected without the column (older callers): absent means no topic.
    """
    try:
        slug = item["topic"]
    except (IndexError, KeyError):
        return None
    if not slug:
        return None
    return TOPIC_NAMES.get(slug)


def _is_exhausted(item: sqlite3.Row) -> bool:
    """True when this item was never summarized and has used up its retries.

    Tolerates rows selected without 'summarize_attempts' (older callers): absent
    means never attempted, so not exhausted.
    """
    try:
        attempts = item["summarize_attempts"]
    except (IndexError, KeyError):
        attempts = 0
    return item["summary"] is None and (attempts or 0) >= 3


def _not_summarized_note(item: sqlite3.Row) -> str:
    """The plain-language note explaining why an exhausted item has no summary."""
    try:
        error = item["summarize_error"]
    except (IndexError, KeyError):
        error = None
    if error and "JSON" in error:
        return _EXHAUSTED_JSON_NOTE
    return _EXHAUSTED_GENERIC_NOTE


def _exhausted_headline(item: sqlite3.Row) -> str | None:
    """The headline for an exhausted item: original_title, else item['headline']."""
    return item["original_title"] or item["headline"]


def _excerpt_text(item: sqlite3.Row) -> str | None:
    """item['excerpt'], tolerating rows selected without that column."""
    try:
        return item["excerpt"]
    except (IndexError, KeyError):
        return None


def _is_model_fallback(item: sqlite3.Row) -> bool:
    """True when this item's summary was produced by the fallback model.

    Tolerates rows selected without 'model_fallback' (older callers): absent
    means not a fallback.
    """
    try:
        return bool(item["model_fallback"])
    except (IndexError, KeyError):
        return False


def positive_int_setting(key: str, conn=None) -> int:
    """The int value of setting `key`, falling back to its DEFAULTS value.

    Falls back silently (no raise) when the stored value is missing, non-numeric,
    or less than 1.
    """
    value = get_setting(key, conn)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed < 1:
        parsed = int(DEFAULTS[key])
    return parsed


# Daily per-feed summary-count thresholds at which a warning article is prepended
# to the output feed, read from the PINTXOS_WARN_AT / PINTXOS_WARN_HARD_AT settings;
# warning_level() reports the highest one reached.
def warn_levels(conn=None) -> tuple[int, int]:
    """The (warn, hard) daily per-feed summary-count thresholds from settings.

    Falls back to the DEFAULTS values for either threshold when the stored value is
    missing, non-numeric, or less than 1. If the hard threshold ends up below the
    warn threshold, it is raised to match the warn threshold.
    """
    warn = positive_int_setting("PINTXOS_WARN_AT", conn)
    hard = positive_int_setting("PINTXOS_WARN_HARD_AT", conn)
    if hard < warn:
        hard = warn
    return (warn, hard)


def warning_level(summaries_today: int, levels: tuple[int, int] = (100, 180)) -> int | None:
    """The highest of `levels` (warn, hard) that summaries_today reached, else None."""
    warn, hard = levels
    if summaries_today >= hard:
        return hard
    if summaries_today >= warn:
        return warn
    return None


def warning_item(
    feed: sqlite3.Row,
    *,
    level: int,
    hard_level: int,
    summaries_today: int,
    day: str,
    feed_page_url: str,
    model: str,
    kept_today: int,
) -> dict:
    """Build the warning article for a feed that produced many summaries today.

    The guid encodes the tier ("warn" or "hard") reached, not the raw threshold
    value, so editing a threshold mid-day does not mint a new guid and re-fire
    the warning for the same feed and day.
    """
    title_esc = html.escape(str(feed["title"] or feed["url"]))
    model_esc = html.escape(str(model))
    url_esc = html.escape(str(feed_page_url))

    sentence = (
        f"{title_esc} produced {summaries_today} summaries today, "
        f"each one a call to {model_esc}; {kept_today} of them became new items."
    )
    if level >= hard_level:
        sentence += (
            " That is far more than anyone reads in a day; "
            "most of these summaries are paid for and never opened."
        )
    if kept_today * 2 < summaries_today:
        sentence += (
            " Paying for summaries that are then discarded usually means a "
            "re-summarize loop: check this feed's Filtered list and the container log."
        )
    description = (
        f"<p>{sentence}</p>"
        "<p>Open the feed's settings to set a daily budget, mute whole topics, "
        f'or turn this warning off: <a href="{url_esc}">{url_esc}</a></p>'
        "<p><em>Did you know? Pintxøs can skip ads, drop entries by keyword pattern, "
        "and mute whole topics per feed, before any summary is paid for.</em></p>"
    )

    tier = "hard" if level >= hard_level else "warn"
    return {
        "guid": f"pintxos-warning-{feed['id']}-{tier}-{day}",
        "title": f"Pintxøs: this feed produced {summaries_today} summaries today",
        "link": feed_page_url,
        "pub_date": datetime.now(UTC),
        "description": description,
    }


def pause_reason(error: str | None) -> str:
    """One plain-language sentence describing why the AI provider refused a request.

    Shared by pause_warning_item() and the index-page banner context, so the two
    surfaces never drift out of sync on wording.
    """
    text = error or ""
    if "402" in text or "credit balance" in text:
        return "Your AI provider reports that there is no credit left."
    if "401" in text or "403" in text:
        return "Your AI provider rejected the API key."
    return "Your AI provider refused the request."


def paused_since_display(paused_since: str) -> str:
    """`paused_since` (an aware ISO UTC string) rendered as "YYYY-MM-DD HH:MM UTC".

    Shared by pause_warning_item() and app._paused_context() (the index-page banner
    context), so the two surfaces render the same timestamp format.
    """
    since_dt = datetime.fromisoformat(paused_since)
    if since_dt.tzinfo is None:  # defensive: every value we write is already aware
        since_dt = since_dt.replace(tzinfo=UTC)
    return since_dt.strftime("%Y-%m-%d %H:%M UTC")


def pause_warning_item(
    feed: sqlite3.Row,
    *,
    paused_since: str,
    error: str,
    day: str,
    settings_url: str,
) -> dict:
    """Build the warning article shown while summarization is globally paused.

    The guid is keyed by the pause's start date and the current day, so it re-mints
    once per UTC day while the pause persists, and mints nothing once it clears
    (this function is simply not called then).
    """
    since_text = paused_since_display(paused_since)
    reason = pause_reason(error)
    settings_url_esc = html.escape(str(settings_url))

    description = (
        f"<p>Since {since_text}, Pintxøs has not been able to summarize any new "
        "articles.</p>"
        f"<p>{html.escape(reason)}</p>"
        "<p>Pintxøs checks again every 30 minutes and resumes by itself. "
        f'Top up or fix the key in Settings: <a href="{settings_url_esc}">{settings_url_esc}</a></p>'
    )

    return {
        "guid": f"pintxos-paused-{paused_since[:10]}-{day}",
        "title": "Pintxøs has stopped summarizing: your AI account needs attention",
        "link": settings_url,
        "pub_date": datetime.now(UTC),
        "description": description,
    }


def fallback_warning_item(
    feed: sqlite3.Row,
    *,
    used: int,
    total: int,
    fallback_model: str,
    day: str,
    settings_url: str,
) -> dict:
    """Build the warning article for a day when the fallback model was overused."""
    fallback_model_esc = html.escape(str(fallback_model))
    settings_url_esc = html.escape(str(settings_url))

    description = (
        f"<p>Pintxøs used {fallback_model_esc} instead of the default model on "
        f"{used} of {total} articles today.</p>"
        "<p>This costs differently than the default model, and may mean the "
        "default model is broken or unavailable.</p>"
        f'<p>Check Settings: <a href="{settings_url_esc}">{settings_url_esc}</a></p>'
    )

    return {
        "guid": f"pintxos-fallback-{day}",
        "title": f"Your default model failed on {used} of {total} articles today",
        "link": settings_url,
        "pub_date": datetime.now(UTC),
        "description": description,
    }


def text_lines(item: sqlite3.Row) -> list[str]:
    """Non-blank lines of item['text'], deduping a first line that repeats the title.

    Tolerates rows selected without the 'text' column: absent/falsy means no lines.
    A first non-blank line that (loosely) matches original_title is dropped, since
    the fetched article body often repeats its own headline as its first line.
    """
    text = item["text"] if "text" in item.keys() else None
    if not text:
        return []
    norm_original_title = _norm_title(item["original_title"] or "")
    lines: list[str] = []
    first_line_seen = False
    for line in text.splitlines():
        if not line.strip():
            continue
        if not first_line_seen:
            first_line_seen = True
            if norm_original_title and _norm_title(line) == norm_original_title:
                continue
        lines.append(line)
    return lines


def item_html(item: sqlite3.Row, *, full: bool) -> str:
    """The item's description rendered as HTML paragraphs, joined with "\\n".

    A stripped-down cousin of render_rss's per-item description: no auth/fetch
    notes, no "=== FULL TEXT BELOW ===" marker, no link to Pintxøs. Every
    interpolated value is html.escape()d; a paragraph is skipped entirely when its
    source value is falsy. When `full` is True, the fetched article's text lines
    (see text_lines()) are appended, one paragraph per line.
    """
    paragraphs: list[str] = []

    exhausted = _is_exhausted(item)
    headline = _exhausted_headline(item) if exhausted else item["headline"]
    if headline:
        paragraphs.append(
            f'<p><b style="font-size:1.15em">{html.escape(headline)}</b></p>'
        )

    if exhausted:
        note = _not_summarized_note(item)
        paragraphs.append(f"<p>{html.escape(note)}</p>")
        excerpt = _excerpt_text(item)
        if excerpt:
            paragraphs.append(f"<p>{html.escape(excerpt)}</p>")
    else:
        summary = item["summary"]
        if summary:
            model = item["model"] if "model" in item.keys() else None
            if model and _is_model_fallback(item):
                paragraphs.append(f"<p>{html.escape(summary)}</p>")
                model_esc = html.escape(model)
                paragraphs.append(
                    f"<p><strong>Note: Pintxøs used {model_esc} as a fallback "
                    "for this item.</strong></p>"
                )
            elif model:
                paragraphs.append(
                    f"<p>{html.escape(summary)} <small>({html.escape(model)})</small></p>"
                )
            else:
                paragraphs.append(f"<p>{html.escape(summary)}</p>")

    original_title = item["original_title"]
    if original_title:
        paragraphs.append(f"<p>Original: {html.escape(original_title)}</p>")

    words = item["word_count"]
    topic_name = _topic_name(item)
    if words and topic_name:
        paragraphs.append(
            f"<p><em>{html.escape(format_stats(words))} · {html.escape(topic_name)}</em></p>"
        )
    elif words:
        paragraphs.append(f"<p><em>{html.escape(format_stats(words))}</em></p>")
    elif topic_name:
        paragraphs.append(f"<p><em>{html.escape(topic_name)}</em></p>")

    link = item["link"]
    if link:
        link_esc = html.escape(link)
        paragraphs.append(f'<p><a href="{link_esc}">{link_esc}</a></p>')

    if full:
        for line in text_lines(item):
            paragraphs.append(f"<p>{html.escape(line)}</p>")

    return "\n".join(paragraphs)


def item_plain(item: sqlite3.Row, *, full: bool) -> str:
    """The item's description rendered as plain text paragraphs, joined with "\\n\\n".

    Same paragraphs, order, and skip rules as item_html(), but unescaped. When
    `full` is True, each of the fetched article's text lines (see text_lines())
    is appended as its own paragraph.
    """
    paragraphs: list[str] = []

    exhausted = _is_exhausted(item)
    headline = _exhausted_headline(item) if exhausted else item["headline"]
    if headline:
        paragraphs.append(headline)

    if exhausted:
        paragraphs.append(_not_summarized_note(item))
        excerpt = _excerpt_text(item)
        if excerpt:
            paragraphs.append(excerpt)
    else:
        summary = item["summary"]
        if summary:
            model = item["model"] if "model" in item.keys() else None
            if model and _is_model_fallback(item):
                paragraphs.append(summary)
                paragraphs.append(f"Note: Pintxøs used {model} as a fallback for this item.")
            elif model:
                paragraphs.append(f"{summary} ({model})")
            else:
                paragraphs.append(summary)

    original_title = item["original_title"]
    if original_title:
        paragraphs.append(f"Original: {original_title}")

    words = item["word_count"]
    topic_name = _topic_name(item)
    if words and topic_name:
        paragraphs.append(f"{format_stats(words)} · {topic_name}")
    elif words:
        paragraphs.append(format_stats(words))
    elif topic_name:
        paragraphs.append(topic_name)

    link = item["link"]
    if link:
        paragraphs.append(link)

    if full:
        paragraphs.extend(text_lines(item))

    return "\n\n".join(paragraphs)


def render_rss(
    feed: sqlite3.Row,
    items: Sequence[sqlite3.Row],
    *,
    full_text: bool = True,
    warnings: list[dict] | None = None,
    base_url: str | None = None,
) -> bytes:
    """Render a feed and its items as RSS 2.0 XML bytes.

    `warnings` are prepended as items ahead of the feed's real items, in list order
    (e.g. pause, then fallback-model, then volume warnings).
    """
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{feed['title'] or feed['url']} · Pintxøs"
    ET.SubElement(channel, "link").text = feed["url"]
    ET.SubElement(channel, "description").text = "Factual summaries by Pintxøs"

    for warning in warnings or []:
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = warning["title"]
        ET.SubElement(entry, "link").text = warning["link"]
        guid = ET.SubElement(entry, "guid", {"isPermaLink": "false"})
        guid.text = warning["guid"]
        ET.SubElement(entry, "pubDate").text = format_datetime(warning["pub_date"])
        ET.SubElement(entry, "description").text = warning["description"]

    for item in items:
        if _is_muted(item):  # muted topic: stored, but never published
            continue
        entry = ET.SubElement(channel, "item")
        exhausted = _is_exhausted(item)
        title_text = _exhausted_headline(item) if exhausted else item["headline"]
        ET.SubElement(entry, "title").text = title_text
        ET.SubElement(entry, "link").text = item["link"]
        guid = ET.SubElement(entry, "guid", {"isPermaLink": "false"})
        guid.text = item["guid"]
        pub_date = format_datetime(datetime.fromisoformat(item["published_at"]))
        ET.SubElement(entry, "pubDate").text = pub_date

        if exhausted:
            note = _not_summarized_note(item)
            description = f"<p>{html.escape(note)}</p>"
            # Show the excerpt whenever the full-text block below will not render
            # (full_text is off, or on but there is no fetched text to show).
            if not (full_text and item["text"]):
                excerpt = _excerpt_text(item)
                if excerpt:
                    description += f"<p>{html.escape(excerpt)}</p>"
        else:
            summary = item["summary"]
            model = item["model"] if "model" in item.keys() else None
            if summary is None:
                description = ""
            elif model and _is_model_fallback(item):
                model_esc = html.escape(model)
                description = f"<p>{summary}</p>"
                description += (
                    f"<p><strong>Note: Pintxøs used {model_esc} as a fallback "
                    "for this item.</strong></p>"
                )
            elif model:
                description = (
                    f"<p>{summary} "
                    f'<small style="color:#888">({html.escape(model)})</small></p>'
                )
            else:
                description = f"<p>{summary}</p>"
        auth = item["auth"]
        fetch_status = item["fetch_status"]
        if auth == "used":
            description += _AUTH_NOTES["used"]
        elif auth == "failed":
            description += _AUTH_NOTES["failed"]
        elif fetch_status in _FETCH_NOTES:
            description += _FETCH_NOTES[fetch_status]
        elif item["fallback"]:
            if auth == "missing":
                description += _AUTH_NOTES["missing"]
            elif auth is None:
                description += (
                    "<p><em>Note: article fetch failed; summarized from feed excerpt.</em></p>"
                )
        description += f"<p>Original: {item['original_title']}</p>"
        words = item["word_count"]
        topic_name = _topic_name(item)
        if words and topic_name:
            description += f"<p><em>{format_stats(words)} · {html.escape(topic_name)}</em></p>"
        elif words:
            description += f"<p><em>{format_stats(words)}</em></p>"
        elif topic_name:
            description += f"<p><em>{html.escape(topic_name)}</em></p>"
        if base_url is not None:
            item_url = html.escape(f"{base_url}/items/{item['id']}")
            description += f'<p><a href="{item_url}">Copy or share this article</a></p>'
        if full_text and item["text"]:
            description += "<p>=== FULL TEXT BELOW ===</p>"
            for line in text_lines(item):
                description += f"<p>{html.escape(line)}</p>"
        ET.SubElement(entry, "description").text = description

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
