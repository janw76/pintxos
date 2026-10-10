"""Tests for pintxos.fetch_status.summarize and its _fetch_status.html partial."""

from __future__ import annotations

from pintxos.app import templates
from pintxos.fetch_status import summarize


def _base_kwargs(**overrides):
    kwargs = dict(total=10, domain="example.com", cookies_loaded=True, cookie_expiry=None, feed_id=7)
    kwargs.update(overrides)
    return kwargs


def test_summarize_empty_feed_shows_dash():
    entries = summarize({}, **_base_kwargs(total=0))
    assert entries == [
        {
            "text": "-",
            "tooltip": "No items yet.",
            "link": None,
            "ok": True,
        }
    ]


def test_ok_when_no_bad_buckets_and_no_used():
    entries = summarize({}, **_base_kwargs(total=10))
    assert entries == [
        {
            "text": "OK",
            "tooltip": "All 10 articles read in full.",
            "link": None,
            "ok": True,
        }
    ]


def test_ok_mentions_used_when_used_is_nonzero():
    entries = summarize({"used": 4}, **_base_kwargs(total=10))
    assert entries == [
        {
            "text": "OK",
            "tooltip": "All 10 articles read in full, 4 with your login.",
            "link": None,
            "ok": True,
        }
    ]


def test_paywalled_bucket():
    entries = summarize({"paywalled": 3}, **_base_kwargs(total=10, domain="ft.com"))
    assert entries == [
        {
            "text": "3 paywalled",
            "tooltip": (
                "3 of 10 articles came back as a teaser or were blocked and no "
                "login cookies are saved for ft.com. Add them "
                "on the feed's Paywall tab."
            ),
            "link": {"href": "/feeds/7#paywall", "label": "add login"},
            "ok": False,
        }
    ]


def test_login_failed_bucket_with_cookie_expiry():
    entries = summarize(
        {"login_failed": 2},
        **_base_kwargs(total=10, domain="ft.com", cookie_expiry="2100-01-01"),
    )
    assert entries == [
        {
            "text": "2 unreadable with login",
            "tooltip": (
                "Cookies for ft.com are saved but 2 articles still came back as "
                "a teaser or blocked. Either the cookies expired (earliest "
                "expiry 2100-01-01) or those articles are not part of your "
                "subscription."
            ),
            "link": {"href": "/feeds/7#paywall", "label": "check login"},
            "ok": False,
        }
    ]


def test_login_failed_bucket_without_cookie_expiry_says_unknown():
    entries = summarize(
        {"login_failed": 2}, **_base_kwargs(total=10, domain="ft.com", cookie_expiry=None)
    )
    assert entries[0]["tooltip"] == (
        "Cookies for ft.com are saved but 2 articles still came back as a "
        "teaser or blocked. Either the cookies expired (earliest expiry "
        "unknown) or those articles are not part of your subscription."
    )


def test_unreadable_bucket():
    entries = summarize({"unreadable": 5}, **_base_kwargs(total=10))
    assert entries == [
        {
            "text": "5 unreadable",
            "tooltip": (
                "5 articles could not be fetched (timeout, server error or not "
                "an HTML page); Pintxøs summarized the feed excerpt instead."
            ),
            "link": None,
            "ok": False,
        }
    ]


def test_missing_keys_count_as_zero():
    # Only "used" supplied; the other buckets default to 0, so this is still OK.
    entries = summarize({"used": 1}, **_base_kwargs(total=1))
    assert entries[0]["text"] == "OK"


def test_ordering_when_all_three_buckets_nonzero():
    entries = summarize(
        {"paywalled": 1, "login_failed": 2, "unreadable": 3},
        **_base_kwargs(total=10),
    )
    assert [e["text"] for e in entries] == [
        "1 paywalled",
        "2 unreadable with login",
        "3 unreadable",
    ]
    assert [e["ok"] for e in entries] == [False, False, False]


def _render(fetch_status):
    return templates.env.get_template("_fetch_status.html").render(fetch_status=fetch_status)


def test_partial_renders_info_component_separator_link_and_muted_ok():
    entries = [
        {"text": "OK", "tooltip": "All 10 articles read in full.", "link": None, "ok": True},
        {
            "text": "3 paywalled",
            "tooltip": "3 of 10 articles came back as a teaser <b>&",
            "link": {"href": "/settings#paywall", "label": "add login"},
            "ok": False,
        },
    ]
    html = templates.env.get_template("_fetch_status.html").render(
        fetch_status=entries, info_prefix="fs-7"
    )

    assert "title=" not in html
    assert "info-icon" not in html
    for n in (1, 2):
        assert f'aria-expanded="false" aria-controls="fs-7-{n}"' in html
    assert html.count('<button type="button" class="info-btn"') == 2
    assert '<span class="info-box" id="fs-7-1" role="note">All 10 articles read in full.</span>' in html
    assert (
        '<span class="info-box" id="fs-7-2" role="note">'
        "3 of 10 articles came back as a teaser &lt;b&gt;&amp;</span>"
    ) in html
    assert " · " in html
    assert 'href="/settings#paywall"' in html
    assert ">add login<" in html
    assert '<span class="muted">OK</span>' in html
    assert "<span>3 paywalled</span>" in html


def test_partial_prefixes_keep_ids_distinct_between_feeds():
    entry = [{"text": "OK", "tooltip": "t", "link": None, "ok": True}]
    tpl = templates.env.get_template("_fetch_status.html")
    a = tpl.render(fetch_status=entry, info_prefix="fs-1")
    b = tpl.render(fetch_status=entry, info_prefix="fs-2")
    assert 'id="fs-1-1"' in a and 'id="fs-2-1"' in b and 'id="fs-1-1"' not in b


def test_partial_empty_tooltip_renders_no_button():
    html = _render([{"text": "-", "tooltip": "", "link": None, "ok": True}])
    assert "info-btn" not in html and ">-<" in html


def test_partial_single_entry_has_no_separator():
    html = _render(
        [{"text": "OK", "tooltip": "All 1 articles read in full.", "link": None, "ok": True}]
    )
    assert " · " not in html
