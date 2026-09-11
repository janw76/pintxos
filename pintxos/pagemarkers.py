"""Read the machine-readable "you may read this" markers off an HTML page.

`fetch_article()` treats any 2xx HTML page whose extracted text is under a
couple hundred characters as a paywall teaser (GitHub issue #9). That is the
right default -- FT and Economist teasers look exactly like that -- but it
also swallows pages that are legitimately short: a New Yorker cartoon, a
video or podcast episode page, a photo gallery.

This module reads the two markers publishers actually put in their markup:

* schema.org's ``isAccessibleForFree`` inside ``<script
  type="application/ld+json">`` blocks, which a publisher sets to true on
  pages it wants everyone (and Google) to see, and to false on soft-paywalled
  ones;
* the page's ``og:type`` and its JSON-LD ``@type`` values, which say whether
  the thing on the page is an article at all or a piece of media.

Only positive evidence rescues a page. Absence of markup stays `None`, so a
real teaser -- FT and the Economist carry no markers at all -- keeps today's
behaviour, and an explicit ``isAccessibleForFree: false`` is never overridden
by a media type sitting next to it on the same page.

Standard library only, and nothing from `pintxos`: this is a pure text-in,
verdict-out module so it can be tested with inline HTML fixtures.
"""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple

__all__ = ["Markers", "page_markers", "free_short_page", "PAYWALL_MARKERS", "paywall_markers"]

# <script type="application/ld+json"> ... </script>, tolerating other
# attributes, any attribute order and unquoted/single-quoted type values.
_LD_JSON_RE = re.compile(
    r"<script\b[^>]*\btype\s*=\s*['\"]?application/ld\+json['\"]?[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([\w:.\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
    re.IGNORECASE,
)

# JSON-LD @type values that mean "this page is a piece of media, not prose",
# in the order we prefer to report them.
_MEDIA_TYPES = (
    "VideoObject",
    "AudioObject",
    "PodcastEpisode",
    "ImageGallery",
    "MediaObject",
)

# og:type prefixes that mean the same thing ("video.other", "music.song", ...).
_MEDIA_OG_PREFIXES = ("video.", "music.")

class Markers(NamedTuple):
    """What a page says about itself.

    `is_free` is the schema.org ``isAccessibleForFree`` verdict: `False` wins
    over `True` (a page that declares any part paywalled is treated as
    paywalled), and `None` means the page said nothing at all.
    """

    is_free: bool | None
    og_type: str | None
    jsonld_types: frozenset[str]


def _is_true(value: Any) -> bool:
    # Identity for the real boolean (so a stray 1 does not count), plus the
    # string spellings publishers actually emit.
    return value is True or value in ("true", "True")


def _is_false(value: Any) -> bool:
    return value is False or value in ("false", "False")


def _walk(node: Any, types: set[str], flags: list[bool]) -> None:
    """Collect @type strings and isAccessibleForFree booleans, recursively.

    Nested nodes count as much as top-level ones: publishers hang the real
    article off "@graph", off "mainEntity", or inside a list, and the flag
    can live on any of them.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "@type":
                if isinstance(value, str):
                    types.add(value)
                elif isinstance(value, list):
                    types.update(item for item in value if isinstance(item, str))
            elif key == "isAccessibleForFree":
                if _is_true(value):
                    flags.append(True)
                elif _is_false(value):
                    flags.append(False)
                # Anything else (null, a number, an odd string) is ignored.
            _walk(value, types, flags)
    elif isinstance(node, list):
        for item in node:
            _walk(item, types, flags)


def _og_type(html: str) -> str | None:
    for tag in _META_RE.findall(html):
        attrs: dict[str, str] = {}
        for name, dq, sq, bare in _ATTR_RE.findall(tag):
            attrs[name.lower()] = dq or sq or bare
        if attrs.get("property", "").strip().lower() == "og:type":
            content = attrs.get("content")
            if content is not None:
                stripped = content.strip().lower()
                if stripped:
                    return stripped
    return None


def page_markers(html: str) -> Markers:
    """Extract the free/paywalled and media markers from a page's HTML.

    Never raises on malformed input: a JSON-LD block that does not parse is
    skipped, and the remaining blocks still count.
    """
    types: set[str] = set()
    flags: list[bool] = []

    for block in _LD_JSON_RE.findall(html or ""):
        try:
            _walk(json.loads(block), types, flags)
        except (ValueError, RecursionError):
            continue  # unparseable or absurdly deep: skip the block, keep the rest

    if False in flags:
        is_free: bool | None = False
    elif True in flags:
        is_free = True
    else:
        is_free = None

    return Markers(is_free=is_free, og_type=_og_type(html or ""), jsonld_types=frozenset(types))


def free_short_page(html: str) -> str | None:
    """Why a short page is short, when the page itself says so.

    Returns "free" when the page declares itself freely accessible,
    "media:<kind>" when it is a video/audio/gallery page rather than prose,
    and `None` when the markup gives us no reason to override the teaser
    heuristic -- including when the page declares itself paywalled.
    """
    markers = page_markers(html)

    if markers.is_free is True:
        return "free"
    if markers.is_free is False:
        # An explicit paywall flag beats any media marker on the same page.
        return None

    if markers.og_type and markers.og_type.startswith(_MEDIA_OG_PREFIXES):
        return f"media:{markers.og_type}"

    # A media object next to an Article/Posting is usually just the lead
    # image or an embedded clip, so it says nothing about the page itself.
    if any(t.endswith(("Article", "Posting")) for t in markers.jsonld_types):
        return None
    for media_type in _MEDIA_TYPES:
        if media_type in markers.jsonld_types:
            return f"media:{media_type}"

    return None


# ponytail: guessed from two teaser pages (FT, Economist); extend from teaser
# fingerprints in the logs before building Option B
PAYWALL_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("piano", re.compile(r"tp-modal|tp-container|piano\.io", re.IGNORECASE)),
    (
        "paywall",
        re.compile(r"""\b(?:class|id)\s*=\s*("[^"]*paywall[^"]*"|'[^']*paywall[^']*')""", re.IGNORECASE),
    ),
    ("subscribe-wall", re.compile(r"subscribe-wall|subscription-wall|meter-wall", re.IGNORECASE)),
    (
        "subscribe-copy",
        re.compile(
            r"subscribe to continue|subscribe to read|already a subscriber", re.IGNORECASE
        ),
    ),
    ("regwall", re.compile(r"regwall|registration-wall", re.IGNORECASE)),
]


def paywall_markers(html: str) -> list[str]:
    """Names of the paywall fingerprints found in `html`, in `PAYWALL_MARKERS`
    order, each name at most once. `[]` for empty input or no match."""
    if not html:
        return []
    return [name for name, pattern in PAYWALL_MARKERS if pattern.search(html)]
