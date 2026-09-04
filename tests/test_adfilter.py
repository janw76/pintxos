"""Tests for pintxos.adfilter, using a real Wired feed snapshot as ground truth."""

from __future__ import annotations

import re
from pathlib import Path

import feedparser
import pytest

from pintxos.adfilter import compile_patterns, entry_tags, is_ad

FIXTURE = Path(__file__).parent / "fixtures" / "wired.xml"

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


@pytest.fixture(scope="module")
def wired_entries():
    parsed = feedparser.parse(FIXTURE.read_bytes())
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


def test_sponsored_tag_is_flagged():
    entry = {"title": "Weekend reads", "tags": [{"term": "Sponsored"}]}
    reason = is_ad(entry)
    assert reason is not None
    assert reason.startswith("tag:")


def test_percent_off_title_flagged_but_unrelated_discount_text_is_not():
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
    reason = is_ad({"title": "Coupon fraud ring busted by FBI"})
    assert reason is not None
    assert reason.startswith("title:")
