"""Render feed items as RSS 2.0 XML."""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime
from email.utils import format_datetime

from pintxos.stats import format_stats


def render_rss(
    feed: sqlite3.Row, items: Sequence[sqlite3.Row]
) -> bytes:
    """Render a feed and its items as RSS 2.0 XML bytes."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{feed['title'] or feed['url']} · Pintxøs"
    ET.SubElement(channel, "link").text = feed["url"]
    ET.SubElement(channel, "description").text = "Factual summaries by Pintxøs"

    for item in items:
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item["headline"]
        ET.SubElement(entry, "link").text = item["link"]
        guid = ET.SubElement(entry, "guid", {"isPermaLink": "false"})
        guid.text = item["guid"]
        pub_date = format_datetime(datetime.fromisoformat(item["published_at"]))
        ET.SubElement(entry, "pubDate").text = pub_date

        description = f"<p>{item['summary']}</p>"
        words = item["word_count"]
        if words:
            description += f"<p><em>{format_stats(words)}</em></p>"
        if item["fallback"]:
            description += (
                "<p><em>Note: article fetch failed; "
                "summarized from feed excerpt.</em></p>"
            )
        description += f"<p>Original: {item['original_title']}</p>"
        ET.SubElement(entry, "description").text = description

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
