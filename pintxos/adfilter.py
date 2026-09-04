"""Heuristics for skipping ad/coupon posts before we bother summarizing them.

Wired's RSS feed (and feeds like it) mix real journalism with recurring
coupon-code roundups ("Groupon Promo Codes: 60% Off in September 2026",
"ExpressVPN Coupons: 73% Off", ...). These are near-identical, low-value
posts that get republished with a new percentage every few days, and we
don't want to spend an LLM call summarizing each refresh.

`is_ad` checks three signals, in order of confidence:

1. RSS category tags. Feedparser's `tags` list is the most reliable signal:
   coupon roundups are consistently tagged with terms like "coupons" or
   "Gear / Deals". We match on AD_TAGS.
2. Title patterns. Coupon posts have a very regular title shape ("X Promo
   Codes: N% Off", "X Coupons: N% Off", etc). AD_TITLE_PATTERNS matches
   that shape.
3. Link slug. As a last resort, the last path segment of the article URL
   often encodes the same signal (".../groupon-promo-code/").

Note that "deals" is deliberately a TAG-only rule and never a title
pattern. Plenty of legitimate journalism has "deals" in the title or talks
about deals without being a coupon post -- e.g. "Amazon's 2026 Holiday
Deals Are About to Look Better Than They Are" is an article critiquing
retail deal season, not a coupon roundup, and would be a false positive if
matched by title text. The *tag* "deals" (as in Wired's "Gear / Deals"
category) is a much stronger, curated signal than the word appearing
somewhere in a headline.

Known limitation: the loose title rules (bare "coupons?", "N% off") are
broad enough to occasionally skip a real story at entry time -- e.g.
"Coupon fraud ring busted by FBI". When that happens, only that entry's
summary is lost for this poll; the entry is re-evaluated on the next poll
(it is never marked seen), and nothing is ever deleted from the database.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from urllib.parse import urlparse

AD_TAGS: frozenset[str] = frozenset(
    {
        "coupons",
        "coupon",
        "deals",
        "promo codes",
        "promo code",
        "sponsored",
        "sponsored content",
        "paid content",
        "partner content",
        "advertisement",
        "advertorial",
        "affiliate",
    }
)

AD_TITLE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(?:promo|coupon|discount)\s+codes?\b", re.IGNORECASE),
    re.compile(r"\bcoupons?\b", re.IGNORECASE),
    re.compile(r"\b\d+%\s+off\b", re.IGNORECASE),
    re.compile(r"\bsponsored\b", re.IGNORECASE),
)

# Applied to the last non-empty path segment of the link, e.g.
# "groupon-promo-code" or "expressvpn-coupons". A bare final segment like
# "code" (as in the malformed /story/instacart-promo/code/ slug) does not
# match here -- that entry is caught by its tags/title instead.
AD_LINK_PATTERN: re.Pattern = re.compile(
    r"(?:^|-)(?:promo-?codes?|coupons?|discount-?codes?)$", re.IGNORECASE
)


def entry_tags(entry) -> list[str]:
    """Lower-cased tag terms for `entry`, including ' / '-separated segments.

    feedparser gives `tags` as a list of dict-like objects with a 'term'
    key (also accessible as `.term`). For a term like "Gear / Deals" this
    yields ["gear / deals", "gear", "deals"] -- the full term first, then
    each slash-separated segment. Never raises; missing/odd input yields [].
    """
    try:
        raw_tags = entry.get("tags") or []
    except AttributeError:
        return []

    tags: list[str] = []
    for tag in raw_tags:
        try:
            term = tag.get("term") if hasattr(tag, "get") else getattr(tag, "term", None)
        except Exception:
            term = None
        if not term:
            continue
        term = str(term).strip()
        if not term:
            continue
        term_lower = term.lower()
        tags.append(term_lower)
        if " / " in term:
            for segment in term.split(" / "):
                segment = segment.strip().lower()
                if segment:
                    tags.append(segment)
    return tags


def _match_title(
    title: str | None,
    extra_title_patterns: Sequence[re.Pattern] = (),
) -> str | None:
    """Return a `"title:<pattern>"` reason if `title` matches a title rule, else None."""
    title = title or ""
    for pattern in (*AD_TITLE_PATTERNS, *extra_title_patterns):
        if pattern.search(title):
            return f"title:{pattern.pattern}"
    return None


def _match_link(link: str | None) -> str | None:
    """Return `"link"` if the last path segment of `link` matches AD_LINK_PATTERN, else None."""
    link = link or ""
    if link:
        path = urlparse(link).path
        segments = [seg for seg in path.split("/") if seg]
        if segments and AD_LINK_PATTERN.search(segments[-1]):
            return "link"
    return None


def is_ad(entry, extra_title_patterns: Sequence[re.Pattern] = ()) -> str | None:
    """Return a short reason string if `entry` looks like an ad, else None.

    Checks, in order: tags (AD_TAGS), title (AD_TITLE_PATTERNS then
    extra_title_patterns), then the link's last path segment
    (AD_LINK_PATTERN). `entry` may be a plain dict or a feedparser
    FeedParserDict; missing title/link/tags are treated as absent.
    """
    for term in entry_tags(entry):
        if term in AD_TAGS:
            return f"tag:{term}"

    reason = _match_title(entry.get("title"), extra_title_patterns=extra_title_patterns)
    if reason is not None:
        return reason

    return _match_link(entry.get("link"))


def compile_patterns(
    text: str | None, on_error: Callable[[int, str, re.error], None] | None = None
) -> list[re.Pattern]:
    """Compile one case-insensitive regex per non-blank line of `text`.

    By default, raises ValueError with the offending 1-based line number and
    text if a line fails to compile as a regex. If `on_error` is given, bad
    lines are skipped instead: `on_error(lineno, line, err)` is called for
    each one and compilation continues.
    """
    if not text:
        return []

    patterns: list[re.Pattern] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as err:
            if on_error is None:
                raise ValueError(f"line {lineno}: {line!r}: {err}") from err
            on_error(lineno, line, err)
    return patterns
