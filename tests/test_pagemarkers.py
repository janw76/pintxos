"""Tests for pintxos.pagemarkers, with HTML fixtures modelled on the pages
listed in GitHub issue #9: the short pages fetch_article() currently
misreads as paywall teasers, and the real teasers it must keep misreading
as such (there is nothing in FT's markup to tell us otherwise)."""

from __future__ import annotations

import pytest

from pintxos.pagemarkers import Markers, free_short_page, page_markers

# A New Yorker cartoon: an ImageObject hanging off a NewsArticle, explicitly
# flagged free. Short by nature -- the "article" is a caption.
NEW_YORKER_CARTOON = """
<html><head>
<meta property="og:type" content="article">
<meta property="og:title" content="Daily Cartoon: Monday, September 7th">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "NewsArticle",
  "headline": "Daily Cartoon: Monday, September 7th",
  "isAccessibleForFree": true,
  "image": {"@type": "ImageObject", "url": "https://media.newyorker.com/cartoon.jpg"}
}
</script>
</head><body><p>A cartoon by Roz Chast.</p></body></html>
"""

# An FT-style hard teaser: no JSON-LD, no Open Graph type, just a few
# paragraphs and a subscribe wall. Nothing here may rescue it.
FT_TEASER = """
<html><head><title>Something happened in the bond market</title></head>
<body>
<h1>Something happened in the bond market</h1>
<p>Yields moved. Subscribe to read the rest.</p>
<div class="barrier">Subscribe to unlock this article</div>
</body></html>
"""

# A soft paywall that says so in so many words.
SOFT_PAYWALL = """
<html><head>
<meta property="og:type" content="article">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "NewsArticle",
 "headline": "Members only", "isAccessibleForFree": false}
</script>
</head><body><p>The first two paragraphs, then nothing.</p></body></html>
"""

# The flag as the string "true", buried in an @graph array -- how Yoast and
# most WordPress SEO plugins emit it.
GRAPH_STRING_TRUE = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "name": "Example"},
  {"@type": ["WebPage", "ItemPage"], "isAccessibleForFree": "true"}
]}
</script>
</head><body><p>Short.</p></body></html>
"""

# A video page with nothing but Open Graph markup.
VIDEO_PAGE = """
<html><head>
<meta content="video.other" property="og:type">
<meta property="og:title" content="Watch: the whole thing in 90 seconds">
</head><body><div id="player"></div></body></html>
"""

# A podcast episode: JSON-LD only, no og:type at all.
PODCAST_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "PodcastEpisode",
 "name": "Episode 214: The bond market, again"}
</script>
</head><body><p>Listen now.</p></body></html>
"""

# An ordinary (unflagged) article whose JSON-LD carries a lead image. The
# ImageObject must not be mistaken for "this page is a gallery".
ARTICLE_WITH_LEAD_IMAGE = """
<html><head>
<meta property="og:type" content="article">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "NewsArticle",
 "headline": "A normal article",
 "image": {"@type": "ImageObject", "url": "https://example.com/lead.jpg"}}
</script>
</head><body><p>Body text.</p></body></html>
"""

# A truncated/invalid JSON-LD block sitting next to a valid one, which is
# common enough (ad tech, template bugs) that it must not cost us the flag.
BROKEN_PLUS_VALID_JSONLD = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Organization", "name": "Broken",
</script>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "NewsArticle",
 "isAccessibleForFree": true, "headline": "Still readable"}
</script>
</head><body><p>Short.</p></body></html>
"""

# A paywalled video page: the paywall flag must win over the media marker.
PAYWALLED_VIDEO = """
<html><head>
<meta property="og:type" content="video.other">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "VideoObject",
 "name": "Subscriber screening", "isAccessibleForFree": false}
</script>
</head><body><div id="player"></div></body></html>
"""


class TestPageMarkers:
    def test_empty_string_yields_no_markers(self):
        assert page_markers("") == Markers(
            is_free=None, og_type=None, jsonld_types=frozenset()
        )

    def test_cartoon_markers(self):
        markers = page_markers(NEW_YORKER_CARTOON)
        assert markers.is_free is True
        assert markers.og_type == "article"
        assert markers.jsonld_types == frozenset({"NewsArticle", "ImageObject"})

    def test_teaser_has_nothing_to_say(self):
        assert page_markers(FT_TEASER) == Markers(
            is_free=None, og_type=None, jsonld_types=frozenset()
        )

    def test_soft_paywall_flag_is_read_as_false(self):
        assert page_markers(SOFT_PAYWALL).is_free is False

    def test_string_true_inside_graph(self):
        markers = page_markers(GRAPH_STRING_TRUE)
        assert markers.is_free is True
        # A list-valued @type contributes every entry.
        assert markers.jsonld_types == frozenset({"WebSite", "WebPage", "ItemPage"})

    def test_og_type_is_found_in_either_attribute_order(self):
        assert page_markers(VIDEO_PAGE).og_type == "video.other"

    def test_og_type_is_lowercased_and_stripped(self):
        html = '<meta property="og:type" content="  Video.Other  ">'
        assert page_markers(html).og_type == "video.other"

    def test_broken_block_is_skipped_and_the_valid_one_still_counts(self):
        markers = page_markers(BROKEN_PLUS_VALID_JSONLD)
        assert markers.is_free is True
        assert markers.jsonld_types == frozenset({"NewsArticle"})

    def test_false_beats_true_when_a_page_carries_both(self):
        html = """
        <script type="application/ld+json">{"isAccessibleForFree": true}</script>
        <script type="application/ld+json">{"isAccessibleForFree": false}</script>
        """
        assert page_markers(html).is_free is False

    def test_non_boolean_flag_values_are_ignored(self):
        html = """
        <script type="application/ld+json">
        {"@type": "NewsArticle", "isAccessibleForFree": null}
        </script>
        """
        assert page_markers(html).is_free is None

    def test_script_tag_with_other_attributes_and_odd_spacing(self):
        html = (
            "<script data-x='1' TYPE = 'application/ld+json' id='seo'>"
            '{"@type": "PodcastEpisode"}'
            "</script>"
        )
        assert page_markers(html).jsonld_types == frozenset({"PodcastEpisode"})

    def test_non_jsonld_script_is_not_parsed(self):
        html = (
            '<script type="text/javascript">'
            'var d = {"@type": "VideoObject", "isAccessibleForFree": true};'
            "</script>"
        )
        assert page_markers(html) == Markers(
            is_free=None, og_type=None, jsonld_types=frozenset()
        )


class TestFreeShortPage:
    @pytest.mark.parametrize(
        "html, expected",
        [
            (NEW_YORKER_CARTOON, "free"),
            (FT_TEASER, None),
            (SOFT_PAYWALL, None),
            (GRAPH_STRING_TRUE, "free"),
            (VIDEO_PAGE, "media:video.other"),
            (PODCAST_PAGE, "media:PodcastEpisode"),
            (ARTICLE_WITH_LEAD_IMAGE, None),
            (BROKEN_PLUS_VALID_JSONLD, "free"),
            (PAYWALLED_VIDEO, None),
            ("", None),
        ],
    )
    def test_verdicts(self, html, expected):
        assert free_short_page(html) == expected

    def test_music_og_type_counts_as_media(self):
        html = '<meta property="og:type" content="music.song">'
        assert free_short_page(html) == "media:music.song"

    def test_plain_og_type_article_is_not_media(self):
        html = '<meta property="og:type" content="article">'
        assert free_short_page(html) is None

    def test_media_object_next_to_a_posting_is_ignored(self):
        html = """
        <script type="application/ld+json">
        {"@graph": [{"@type": "SocialMediaPosting"}, {"@type": "VideoObject"}]}
        </script>
        """
        assert free_short_page(html) is None

    def test_gallery_without_an_article_type_is_media(self):
        html = """
        <script type="application/ld+json">
        {"@type": "ImageGallery", "name": "The year in pictures"}
        </script>
        """
        assert free_short_page(html) == "media:ImageGallery"

    def test_video_object_wins_over_media_object_in_the_reported_order(self):
        html = """
        <script type="application/ld+json">
        {"@type": ["MediaObject", "VideoObject"]}
        </script>
        """
        assert free_short_page(html) == "media:VideoObject"
