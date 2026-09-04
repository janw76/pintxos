"""Heuristics for skipping ad/coupon posts before we bother summarizing them.

Wired's RSS feed (and feeds like it) mix real journalism with recurring
coupon-code roundups ("Groupon Promo Codes: 60% Off in September 2026",
"ExpressVPN Coupons: 73% Off", ...). These are near-identical, low-value
posts that get republished with a new percentage every few days, and we
don't want to spend an LLM call summarizing each refresh. The rules below
were tuned on both Wired and Tom's Guide: the bare "N% off" title rule was
removed in pintxos-rqb.3 on Wired-only evidence (it never fired there) and
is reinstated here because Tom's Guide's Labor Day deal posts ("Skechers
are up to 44% off", "up to 20% off") need it to be caught.

`is_ad` first checks `keep_patterns` against the title, alongside the
built-in `KEEP_TITLE_PATTERNS`: a match from either wins over every block
rule below, built-ins included, and the entry is kept. Otherwise it checks
three signals, in order of confidence:

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

Known limitation: the loose title rule (bare "coupons?") is broad enough
to occasionally skip a real story at entry time -- e.g. "Coupon clipping
is back in fashion". When that happens, only that entry's summary is lost
for this poll; the entry is re-evaluated on the next poll (it is never
marked seen), and nothing is ever deleted from the database. Built-in
`KEEP_TITLE_PATTERNS` rescue headlines with obvious news markers (fraud,
lawsuits, arrests, and the like), and the user-configurable
`PINTXOS_AD_KEEP_PATTERNS` (see `is_ad`'s `keep_patterns`) add to them,
letting you rescue specific titles from this and every other block rule,
built-ins included.
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
        "sales events",
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
    re.compile(r"\bsponsored\b", re.IGNORECASE),
    re.compile(r"\b\d+%\s+off\b", re.IGNORECASE),
    re.compile(r"\bsale\b", re.IGNORECASE),
    re.compile(r"\bsave up to\b", re.IGNORECASE),
)

KEEP_TITLE_PATTERNS: tuple[re.Pattern, ...] = (
    # stock sale, arms sale, home sales
    re.compile(r"\b(stock|share|shares|equity|asset|arms|weapons|land|home|house|property)\s+sales?\b", re.IGNORECASE),
    # up for sale, not for sale, sale of TikTok, sale to Musk
    re.compile(r"\b(for|of|to)\s+sale\b", re.IGNORECASE),
    # sale of/to a company, sale talks, sale deadline/agreement/ban, sale price falls/collapses/blocked
    re.compile(r"\bsale\s+(of|to|talks|deadline|agreement|ban|price|falls?|collapses?|blocked)\b", re.IGNORECASE),
    # fire sale, bake sale, garage/yard/estate sale, point-of-sale
    re.compile(r"\b(fire|bake|garage|yard|estate|point[- ]of)[- ]sale\b", re.IGNORECASE),
    # judge blocks the sale
    re.compile(r"\b(approv\w*|block\w*|halt\w*|forc\w*|reject\w*|clear\w*|complete\w*)\b.*\bsale\b", re.IGNORECASE),
    # the sale is approved/blocked/halted (same idea, reversed word order)
    re.compile(r"\bsale\b.*\b(approv\w*|block\w*|halt\w*|forc\w*|reject\w*|clear\w*|complete\w*)\b", re.IGNORECASE),
    # tickets go on sale Friday: event news, not a deal
    re.compile(r"\btickets?\b.*\bon sale\b", re.IGNORECASE),
    # 20% off its all-time high
    re.compile(r"\d+%\s+off\s+(its|their|the|all-time|record)\b", re.IGNORECASE),
    # news markers; ads never say these. Deliberately excludes bare "probe"
    # and "charged" -- a meat probe deal or "charged my phone in 20
    # minutes" would otherwise be un-filtered.
    re.compile(
        r"\b(fraud|scam|lawsuit|sues?|sued|suing|banned|investigation|arrest\w*|indict\w*|charged with|guilty|court|ruling|regulator\w*)\b",
        re.IGNORECASE,
    ),
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


def is_ad(
    entry,
    extra_title_patterns: Sequence[re.Pattern] = (),
    keep_patterns: Sequence[re.Pattern] = (),
) -> str | None:
    """Return a short reason string if `entry` looks like an ad, else None.

    Checks `keep_patterns` against the title first: built-in keep rules
    (KEEP_TITLE_PATTERNS) run alongside the caller's `keep_patterns`, which
    add to them, and a match from either wins over every block rule below,
    built-ins included, short-circuiting to None. Otherwise checks, in
    order: tags (AD_TAGS), title (AD_TITLE_PATTERNS then
    extra_title_patterns), then the link's last path segment
    (AD_LINK_PATTERN). `entry` may be a plain dict or a feedparser
    FeedParserDict; missing title/link/tags are treated as absent.
    """
    title = entry.get("title") or ""
    for pattern in (*KEEP_TITLE_PATTERNS, *keep_patterns):
        if pattern.search(title):
            return None

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
