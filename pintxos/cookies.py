"""Load a Netscape-format cookies.txt from the data directory."""

from __future__ import annotations

import http.cookiejar
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pintxos.config import data_dir

log = logging.getLogger("pintxos")

# Cache key: (str(path), st_mtime_ns, st_size) -> cached jar (or None on failed load).
_cache: tuple[tuple[str, int, int], http.cookiejar.MozillaCookieJar | None] | None = None


def cookie_path() -> Path:
    """Path to the cookies.txt file in the data directory."""
    return data_dir() / "cookies.txt"


def load_jar(path: Path | None = None) -> http.cookiejar.MozillaCookieJar | None:
    """Load a Netscape cookies file fresh from disk. None if missing or unparseable."""
    if path is None:
        path = cookie_path()
    if not path.exists():
        return None
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError, UnicodeDecodeError):
        log.warning("cookies.txt at %s could not be loaded as a Netscape cookie file", path)
        return None

    # ponytail: expiry 0 means "session cookie" here, not an expired epoch timestamp.
    now = int(time.time())
    for cookie in list(jar):
        if cookie.expires == 0:
            cookie.expires = None
            cookie.discard = True
        elif cookie.expires is not None and cookie.expires < now:
            jar.clear(cookie.domain, cookie.path, cookie.name)

    return jar


def get_jar() -> http.cookiejar.MozillaCookieJar | None:
    """Cached cookie jar, reloaded when cookies.txt changes (by mtime/size)."""
    # ponytail: reload when mtime/size change; ceiling: a same-size rewrite inside
    # one mtime tick is missed.
    global _cache

    path = cookie_path()
    try:
        stat = path.stat()
    except OSError:
        _cache = None
        return None

    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _cache is not None and _cache[0] == key:
        return _cache[1]

    jar = load_jar()
    _cache = (key, jar)
    return jar


def summary(jar: http.cookiejar.MozillaCookieJar | None) -> list[dict]:
    """Per-domain cookie counts and earliest non-session expiry."""
    if jar is None:
        return []

    counts: dict[str, int] = {}
    earliest: dict[str, int] = {}
    for cookie in jar:
        counts[cookie.domain] = counts.get(cookie.domain, 0) + 1
        current = earliest.get(cookie.domain)
        if cookie.expires is not None and (current is None or cookie.expires < current):
            earliest[cookie.domain] = cookie.expires

    result = []
    for domain in sorted(counts):
        epoch = earliest.get(domain)
        expires = None
        if epoch is not None:
            expires = datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()
        result.append({"domain": domain, "count": counts[domain], "expires": expires})
    return result


def expiry_for(jar: http.cookiejar.MozillaCookieJar | None, host: str) -> str | None:
    """Earliest expiry (or None for session-only) among the cookies covering `host`."""
    if jar is None or not host:
        return None
    best_match_len = -1
    expiry = None
    for entry in summary(jar):
        cookie_domain = entry["domain"].lstrip(".")
        if host == cookie_domain or host.endswith("." + cookie_domain):
            if len(cookie_domain) > best_match_len:
                best_match_len = len(cookie_domain)
                expiry = entry["expires"]
    return expiry


def has_cookies_for(jar: http.cookiejar.MozillaCookieJar | None, url: str) -> bool:
    """Whether `jar` holds at least one cookie that would be sent with a request to `url`."""
    if jar is None:
        return False
    if not urlparse(url).hostname:
        return False
    req = urllib.request.Request(url)
    jar.add_cookie_header(req)
    return req.has_header("Cookie")
