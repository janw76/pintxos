"""Tests for pintxos.adfilter, using real Wired, Tom's Guide and Verge feed
snapshots as ground truth."""

from __future__ import annotations

import re
from pathlib import Path

import feedparser
import pytest

from pintxos.adfilter import compile_patterns, entry_tags, is_ad

FIXTURE = Path(__file__).parent / "fixtures" / "wired.xml"
TOMSGUIDE_FIXTURE = Path(__file__).parent / "fixtures" / "tomsguide.xml"
VERGE_FIXTURE = Path(__file__).parent / "fixtures" / "verge.xml"

# The full set of titles that are coupon/promo posts in tests/fixtures/wired.xml,
# verified by eye: every one of these is a recurring "X Promo/Coupon/Discount
# Code(s): N% Off" roundup, not journalism.
EXPECTED_AD_TITLES = {
    "Foreo Discount Codes and Deals: Up to 50% Off",
    "Sportsman's Warehouse Promo Code: Save in September 2026",
    "Medicube Coupon Code: 40% Off for September 2026",
    "Mattress Firm Coupons: Save up to $700",
    "Bartesian Discount Codes: 35% Off",
    "30% Off Tempur-Pedic Promo Codes | September 2026",
    "Purple Promo Codes and Deals: Up to 30% Off",
    "Tuft & Needle Promo Codes: 30% Off | September 2026",
    "Dermstore Coupons: 25% Off for September 2026",
    "Groupon Promo Codes: 60% Off in September 2026",
    "HBO Max Promo Code: 50% Off | September 2026",
    "Instacart Promo Code: $15 Off | September 2026",
    "Lowe’s Promo Codes and Deals: Up to $300 Off Appliances",
    "Vimeo Promo Codes and Discounts: Up to 40% Off This September 2026",
    "Expedia Coupons: 40% Off",
    "We-Vibe Discount Codes and Deals: Up to 60% Off",
    "Total Wireless Promo Codes & Deals: 50% Off Select Plans",
    "Lovehoney Coupon Offers: Toys, Lingerie, and Gift Set Discounts",
    "Home Chef Promo Codes for September 2026",
    "ABT Promo Codes & Discounts for September 2026",
    "ExpressVPN Coupons: 73% Off",
    "KitchenAid Promo Codes: Save Up to 20%",
}


# The 7 (of 50) titles in tests/fixtures/tomsguide.xml that are Labor Day
# deal/sale posts, verified by eye. Copied verbatim from the fixture (curly
# apostrophes, em/en dashes and all).
EXPECTED_TOMSGUIDE_AD_TITLES = {
    "There are hundreds of luxury mattresses on sale this Labor Day, but this is the only hotel-style hybrid I'd buy",
    "I found 12 of the best tool deals in Lowe's Labor Day sale — prices start at $15",
    "Sony, Canon, and Nikon camera prices slashed in Best Buy’s Labor Day sale– up to 20% off",
    "REI’s Labor Day sale is here: save up to 50% on Vuori, Patagonia Arc'teryx and more",
    "Amazon's Labor Day sale is live — 41 deals that top my list for the weekend from $5",
    "The ultimate Labor Day sales shopping guide: Live updates by our deals experts",
    "Skechers are up to 44% off — step into fall with 11 deals on walking shoes and sandals",
}


@pytest.fixture(scope="module")
def wired_entries():
    parsed = feedparser.parse(FIXTURE.read_bytes())
    assert parsed.entries, "fixture failed to parse"
    return parsed.entries


@pytest.fixture(scope="module")
def tomsguide_entries():
    parsed = feedparser.parse(TOMSGUIDE_FIXTURE.read_bytes())
    assert parsed.entries, "fixture failed to parse"
    return parsed.entries


@pytest.fixture(scope="module")
def verge_entries():
    parsed = feedparser.parse(VERGE_FIXTURE.read_bytes())
    assert parsed.entries, "fixture failed to parse"
    return parsed.entries


def _entry_by_title_substring(entries, substring):
    for entry in entries:
        if substring in entry.get("title", ""):
            return entry
    raise AssertionError(f"no entry with title containing {substring!r}")


def test_fixture_flags_exactly_the_expected_ad_titles(wired_entries):
    flagged_titles = {e.get("title") for e in wired_entries if is_ad(e)}
    assert flagged_titles == EXPECTED_AD_TITLES
    assert len(flagged_titles) == 22


def test_amazon_holiday_deals_story_is_not_an_ad(wired_entries):
    entry = _entry_by_title_substring(wired_entries, "Holiday Deals Are About to Look Better")
    assert is_ad(entry) is None


def test_video_doorbell_buying_guide_is_not_an_ad(wired_entries):
    entry = _entry_by_title_substring(wired_entries, "5 Best Video Doorbell Cameras")
    assert is_ad(entry) is None


def test_tomsguide_fixture_flags_exactly_the_expected_ad_titles(tomsguide_entries):
    flagged_titles = {e.get("title") for e in tomsguide_entries if is_ad(e)}
    assert flagged_titles == EXPECTED_TOMSGUIDE_AD_TITLES
    assert len(flagged_titles) == 7


def test_tomsguide_mattress_toppers_sales_plural_is_a_known_miss(tomsguide_entries):
    # Plural "sales" ("...in the Labor Day sales") is deliberately not a title
    # rule -- it also matches business news and mattress reviews -- so this
    # entry is a known false negative, not a bug.
    entry = _entry_by_title_substring(tomsguide_entries, "5 mattress toppers")
    assert is_ad(entry) is None


def test_verge_fixture_flags_exactly_the_alienware_woot_entry(verge_entries):
    flagged = {e.get("title"): is_ad(e) for e in verge_entries if is_ad(e)}
    assert len(flagged) == 1
    (title, reason), = flagged.items()
    assert "Alienware" in title
    assert reason == "tag:deals"


def test_sponsored_tag_is_flagged():
    entry = {"title": "Weekend reads", "tags": [{"term": "Sponsored"}]}
    reason = is_ad(entry)
    assert reason is not None
    assert reason.startswith("tag:")


def test_percent_off_title_is_flagged_but_discount_text_is_not():
    ad_entry = {"title": "Save 40% off this weekend"}
    reason = is_ad(ad_entry)
    assert reason is not None
    assert reason.startswith("title:")

    non_ad_entry = {"title": "The discount rate debate at the Fed"}
    assert is_ad(non_ad_entry) is None


def test_link_slug_flags_promo_code_but_not_investigation_story():
    ad_entry = {"title": "x", "link": "https://example.com/deals/acme-promo-code/"}
    assert is_ad(ad_entry) == "link"

    non_ad_entry = {
        "title": "x",
        "link": "https://example.com/story/promo-code-scandal-investigation/",
    }
    assert is_ad(non_ad_entry) is None


def test_extra_title_pattern_flags_custom_phrase():
    extra = compile_patterns("best .* deals")
    entry = {"title": "Best Labor Day Deals 2026"}
    reason = is_ad(entry, extra_title_patterns=extra)
    assert reason is not None
    assert reason.startswith("title:")


def test_compile_patterns_reports_bad_line_number():
    with pytest.raises(ValueError) as excinfo:
        compile_patterns("ok\n(\n")
    assert "line 2" in str(excinfo.value)


def test_is_ad_handles_empty_and_none_fields():
    assert is_ad({}) is None
    assert is_ad({"tags": None, "title": None, "link": None}) is None


def test_entry_tags_splits_slash_separated_segments():
    entry = {"tags": [{"term": "Gear / Deals"}]}
    assert entry_tags(entry) == ["gear / deals", "gear", "deals"]


def test_is_ad_still_flags_bare_coupon_mention_at_entry_time():
    """The loose patterns still apply to is_ad (entry-time filtering is unchanged)."""
    reason = is_ad({"title": "Coupon clipping is back in fashion"})
    assert reason is not None
    assert reason.startswith("title:")


def test_keep_pattern_rescues_a_title_that_would_otherwise_be_flagged():
    entry = {"title": "Coupon clipping is back in fashion"}
    assert is_ad(entry) is not None
    assert is_ad(entry, keep_patterns=compile_patterns("clipping")) is None


@pytest.mark.parametrize(
    "title",
    [
        "Musk’s $10 billion stock sale rattles Tesla investors",
        "TikTok is up for sale again after court ruling",
        "Mr Trump, the White House is not for sale",
        "Judge blocks the sale of Chrome to OpenAI",
        "US approves $2 billion arms sale to Taiwan",
        "Nvidia is now 20% off its all-time high",
        "Coupon fraud ring busted by FBI",
        "Regulators open investigation into Amazon's Big Deal Days pricing",
        "Taylor Swift tickets go on sale Friday, here’s how to get them",
        "Bake sale raises $5,000 for school robotics team",
        "The sale of Warner Bros. to Paramount is complete",
    ],
)
def test_keep_title_patterns_rescue_news_headlines(title):
    assert is_ad({"title": title}) is None


def test_keep_title_patterns_still_flag_real_deal_titles():
    assert is_ad({"title": "Save 40% off this weekend"}) is not None
    mattress_title = next(t for t in EXPECTED_TOMSGUIDE_AD_TITLES if t.startswith("There are hundreds"))
    assert is_ad({"title": mattress_title}) is not None
    assert is_ad({"title": "This meat probe is 30% off for Labor Day"}) is not None


def test_keep_pattern_does_not_rescue_unrelated_titles(wired_entries):
    """A non-matching keep pattern leaves the fixture's ad detection untouched."""
    keep = compile_patterns("this pattern matches nothing in the fixture")
    flagged_titles = {e.get("title") for e in wired_entries if is_ad(e, keep_patterns=keep)}
    assert flagged_titles == EXPECTED_AD_TITLES
