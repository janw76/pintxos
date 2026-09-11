"""Pure helper: turn per-feed fetch-outcome counts into a terse status summary.

No DB access here — callers (the feeds table, the feed page) are responsible
for counting items and passing in plain integers.
"""

from __future__ import annotations

_PAYWALL_HREF = "/settings#paywall"


def summarize(
    counts: dict,
    *,
    total: int,
    domain: str,
    cookies_loaded: bool,
    cookie_expiry: str | None,
) -> list[dict]:
    """Build the list of status entries for one feed.

    counts keys (missing keys count as 0):
      - "paywalled": teaser or blocked items with no login cookies saved for the site.
      - "login_failed": teaser or blocked items while cookies ARE saved (expired cookies or articles outside the subscription).
      - "unreadable": fetch_status "error" or NULL among fallback items.
      - "used": items fetched using saved login cookies (auth "used").

    cookies_loaded is accepted for callers' convenience only; the buckets above
    already encode whether cookies were present, so it does not change the
    output here.

    Returns a list of {"text", "tooltip", "link", "ok"} dicts. "link" is either
    None or {"href": "/settings#paywall", "label": ...}.
    """
    if total == 0:
        return [{"text": "-", "tooltip": "No items yet.", "link": None, "ok": True}]

    paywalled = counts.get("paywalled", 0)
    login_failed = counts.get("login_failed", 0)
    unreadable = counts.get("unreadable", 0)
    used = counts.get("used", 0)

    if not paywalled and not login_failed and not unreadable:
        if used:
            tooltip = f"All {total} articles read in full, {used} with your login."
        else:
            tooltip = f"All {total} articles read in full."
        return [{"text": "OK", "tooltip": tooltip, "link": None, "ok": True}]

    entries: list[dict] = []

    if paywalled:
        entries.append(
            {
                "text": f"{paywalled} paywalled",
                "tooltip": (
                    f"{paywalled} of {total} articles came back as a teaser or were "
                    f"blocked and no login cookies are saved for {domain}. Add them "
                    "under Settings, Accessing Pay-Walled Content."
                ),
                "link": {"href": _PAYWALL_HREF, "label": "add login"},
                "ok": False,
            }
        )

    if login_failed:
        expiry = cookie_expiry or "unknown"
        entries.append(
            {
                "text": f"{login_failed} unreadable with login",
                "tooltip": (
                    f"Cookies for {domain} are saved but {login_failed} articles "
                    "still came back as a teaser or blocked. Either the cookies "
                    f"expired (earliest expiry {expiry}) or those articles are not "
                    "part of your subscription."
                ),
                "link": {"href": _PAYWALL_HREF, "label": "check login"},
                "ok": False,
            }
        )

    if unreadable:
        entries.append(
            {
                "text": f"{unreadable} unreadable",
                "tooltip": (
                    f"{unreadable} articles could not be fetched (timeout, server "
                    "error or not an HTML page); Pintxøs summarized the feed "
                    "excerpt instead."
                ),
                "link": None,
                "ok": False,
            }
        )

    return entries
