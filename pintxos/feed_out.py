"""Render feed items as RSS 2.0 XML."""

from __future__ import annotations

import html
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import format_datetime

from pintxos.stats import format_stats

# Daily per-feed summary-count thresholds at which a warning article is prepended
# to the output feed. Ascending order; warning_level() reports the highest one reached.
WARN_LEVELS = (50, 100)

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


def warning_level(summaries_today: int) -> int | None:
    """The highest WARN_LEVELS threshold that summaries_today reached, else None."""
    level = None
    for threshold in WARN_LEVELS:
        if summaries_today >= threshold:
            level = threshold
    return level


def warning_item(
    feed: sqlite3.Row,
    *,
    level: int,
    summaries_today: int,
    day: str,
    feed_page_url: str,
    model: str,
) -> dict:
    """Build the warning article for a feed that produced many summaries today."""
    title_esc = html.escape(str(feed["title"] or feed["url"]))
    model_esc = html.escape(str(model))
    url_esc = html.escape(str(feed_page_url))

    sentence = (
        f"{title_esc} produced {summaries_today} summaries today, "
        f"each one a call to {model_esc}."
    )
    if level >= 100:
        sentence += (
            " That is far more than anyone reads in a day; "
            "most of these summaries are paid for and never opened."
        )
    description = (
        f"<p>{sentence}</p>"
        "<p>Open the feed's settings to set a daily budget, mute whole topics, "
        f'or turn this warning off: <a href="{url_esc}">{url_esc}</a></p>'
        "<p><em>Did you know? Pintxøs can skip ads, drop entries by keyword pattern, "
        "and mute whole topics per feed, before any summary is paid for.</em></p>"
    )

    return {
        "guid": f"pintxos-warning-{feed['id']}-{level}-{day}",
        "title": f"Pintxøs: this feed produced {summaries_today} summaries today",
        "link": feed_page_url,
        "pub_date": datetime.now(UTC),
        "description": description,
    }


def render_rss(
    feed: sqlite3.Row,
    items: Sequence[sqlite3.Row],
    *,
    full_text: bool = True,
    warning: dict | None = None,
) -> bytes:
    """Render a feed and its items as RSS 2.0 XML bytes."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{feed['title'] or feed['url']} · Pintxøs"
    ET.SubElement(channel, "link").text = feed["url"]
    ET.SubElement(channel, "description").text = "Factual summaries by Pintxøs"

    if warning is not None:
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
        ET.SubElement(entry, "title").text = item["headline"]
        ET.SubElement(entry, "link").text = item["link"]
        guid = ET.SubElement(entry, "guid", {"isPermaLink": "false"})
        guid.text = item["guid"]
        pub_date = format_datetime(datetime.fromisoformat(item["published_at"]))
        ET.SubElement(entry, "pubDate").text = pub_date

        model = item["model"] if "model" in item.keys() else None
        if model:
            description = (
                f"<p>{item['summary']} "
                f'<small style="color:#888">({html.escape(model)})</small></p>'
            )
        else:
            description = f"<p>{item['summary']}</p>"
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
        if words:
            description += f"<p><em>{format_stats(words)}</em></p>"
        if full_text and item["text"]:
            description += "<p>=== FULL TEXT BELOW ===</p>"
            norm_original_title = _norm_title(item["original_title"] or "")
            first_line_seen = False
            for line in item["text"].splitlines():
                if not line.strip():
                    continue
                if not first_line_seen:
                    first_line_seen = True
                    if norm_original_title and _norm_title(line) == norm_original_title:
                        continue
                description += f"<p>{html.escape(line)}</p>"
        ET.SubElement(entry, "description").text = description

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
