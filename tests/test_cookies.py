"""Tests for pintxos.cookies: loading and caching a Netscape cookies.txt.

The module cache (`pintxos.cookies._cache`) is keyed on
(str(path), st_mtime_ns, st_size), and `path` comes from
`data_dir() / "cookies.txt"`. Since `tests/conftest.py`'s autouse
`_isolated_data_dir` fixture points PINTXOS_DATA_DIR at a fresh tmp_path
per test, every test naturally gets its own path and therefore its own
cache key -- no explicit cache-clearing fixture is needed. We still reset
`pintxos.cookies._cache` to None in an autouse fixture below, purely for
extra safety/clarity (so a stale cache entry can never leak across tests
even if paths were ever to collide).
"""

from __future__ import annotations

import os

import pytest

import pintxos.cookies as cookies_mod
from pintxos.cookies import cookie_path, expiry_for, get_jar, has_cookies_for, load_jar, summary
from conftest import FUTURE_EXPIRY, write_cookies

PAST_EXPIRY = 946684800  # 2000-01-01T00:00:00Z, well in the past


@pytest.fixture(autouse=True)
def _reset_cache():
    cookies_mod._cache = None
    yield
    cookies_mod._cache = None


def test_no_file_load_jar_and_get_jar_are_none():
    assert load_jar() is None
    assert get_jar() is None


def test_two_domains_loaded_and_summarized():
    write_cookies(
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n"
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tuid\tdef\n"
        f".economist.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tghi\n"
    )

    jar = get_jar()
    assert jar is not None
    assert len(jar) == 3

    result = summary(jar)
    assert result == [
        {"domain": ".economist.com", "count": 1, "expires": "2100-01-01"},
        {"domain": ".ft.com", "count": 2, "expires": "2100-01-01"},
    ]


def test_malformed_file_logs_one_warning_without_contents(caplog):
    path = cookie_path()
    path.write_text('{"not": "cookies"}')

    with caplog.at_level("WARNING"):
        result = load_jar()

    assert result is None

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "cookies.txt" in message
    assert '{"not": "cookies"}' not in message  # file contents must never be logged


def test_reload_on_mtime_change_and_removal_clears_cache():
    path = cookie_path()
    write_cookies(
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n"
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tuid\tdef\n"
        f".economist.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tghi\n"
    )

    jar = get_jar()
    assert jar is not None
    assert len(jar) == 3

    t = path.stat().st_mtime
    write_cookies(
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n"
        f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tuid\tdef\n"
        f".economist.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tghi\n"
        f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tjkl\n"
    )
    os.utime(path, (t + 2, t + 2))

    jar = get_jar()
    assert jar is not None
    assert len(jar) == 4

    path.unlink()
    assert get_jar() is None


@pytest.mark.parametrize(
    "expiry_field, loaded, expires_is_none",
    [
        (str(PAST_EXPIRY), False, None),  # past expiry: dropped entirely
        ("0", True, True),  # 0 means "no expiry" (session cookie) by convention
        ("", True, True),  # empty field: session cookie
        (str(FUTURE_EXPIRY), True, False),  # future expiry: kept, reports its date
    ],
)
def test_expiry_rules(expiry_field, loaded, expires_is_none):
    write_cookies(f".ft.com\tTRUE\t/\tFALSE\t{expiry_field}\tsid\tabc\n")

    jar = get_jar()
    if not loaded:
        assert jar is not None
        assert len(jar) == 0
        return

    assert jar is not None
    assert len(jar) == 1
    cookie = jar._cookies[".ft.com"]["/"]["sid"]
    if expires_is_none:
        assert cookie.expires is None
    else:
        assert cookie.expires == FUTURE_EXPIRY


@pytest.mark.parametrize(
    "cookie_line, url, expected",
    [
        (f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc", "https://www.ft.com/a", True),
        (f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc", "https://ft.com/a", True),
        (f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc", "https://notft.com/a", False),
        (
            f"www.economist.com\tFALSE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc",
            "https://www.economist.com/a",
            True,
        ),
        (
            f"www.economist.com\tFALSE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc",
            "https://economist.com/a",
            False,
        ),
        (f".ft.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc", "not-a-url", False),
    ],
)
def test_has_cookies_for(cookie_line, url, expected):
    write_cookies(cookie_line + "\n")
    jar = get_jar()
    assert has_cookies_for(jar, url) is expected


def test_has_cookies_for_none_or_empty_jar_is_false():
    assert has_cookies_for(None, "https://www.ft.com/a") is False

    write_cookies("")
    jar = get_jar()
    assert has_cookies_for(jar, "https://www.ft.com/a") is False


def test_expiry_for_prefers_most_specific_matching_domain():
    write_cookies(
        f".example.com\tTRUE\t/\tFALSE\t{FUTURE_EXPIRY}\tsid\tabc\n"
        f"www.example.com\tFALSE\t/\tFALSE\t{FUTURE_EXPIRY + 86400}\tuid\tdef\n"
    )
    jar = get_jar()
    assert expiry_for(jar, "www.example.com") == "2100-01-02"
