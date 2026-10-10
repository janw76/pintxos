"""Load a Netscape-format cookies.txt from the data directory."""

from __future__ import annotations

import http.cookiejar
import logging
import os
import tempfile
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


def save_jar(jar: http.cookiejar.MozillaCookieJar, path: Path | None = None) -> bool:
    """Write `jar` back to `path` (default cookie_path()) atomically. Never raises.

    # ponytail: save after every authenticated fetch; ceiling is one small write per
    # article. Same-size rewrite within one mtime tick is still missed, same ceiling
    # as get_jar().
    """
    global _cache

    if path is None:
        path = cookie_path()

    if _cache is not None and _cache[1] is jar:
        cached_path = Path(_cache[0][0])
        try:
            cached_stat = cached_path.stat()
        except OSError:
            log.info("cookies.txt changed on disk since it was loaded; not overwriting")
            return False
        cached_key = (str(cached_path), cached_stat.st_mtime_ns, cached_stat.st_size)
        if cached_key != _cache[0]:
            log.info("cookies.txt changed on disk since it was loaded; not overwriting")
            return False

    tmp_path: Path | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)  # 0600 by default
        tmp_path = Path(tmp.name)
        tmp.close()
        # MozillaCookieJar.save writes session cookies (expires None) with an empty
        # expiry field; load_jar() reads that back as a session cookie, so no special
        # handling is needed here.
        jar.save(str(tmp_path), ignore_discard=True, ignore_expires=True)
        os.replace(tmp_path, path)  # os.replace keeps the tmp file's 0600 mode
    except OSError as e:
        log.warning("could not save cookies.txt to %s: %s", path, e)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return False

    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    _cache = (key, jar)
    return True


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


def domain_covers(cookie_domain: str, host: str) -> bool:
    """Whether a cookie for `cookie_domain` (leading dot ignored) applies to `host`."""
    cookie_domain = cookie_domain.lstrip(".")
    return host == cookie_domain or host.endswith("." + cookie_domain)


def expiry_for(jar: http.cookiejar.MozillaCookieJar | None, host: str) -> str | None:
    """Earliest expiry (or None for session-only) among the cookies covering `host`."""
    if jar is None or not host:
        return None
    best_match_len = -1
    expiry = None
    for entry in summary(jar):
        cookie_domain = entry["domain"].lstrip(".")
        if domain_covers(cookie_domain, host):
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


def site_cookies_text(jar: http.cookiejar.MozillaCookieJar | None, host: str) -> str:
    """The cookies covering `host` as Netscape cookies.txt lines (no header)."""
    if jar is None or not host:
        return ""
    sub = http.cookiejar.MozillaCookieJar()
    for cookie in jar:
        if domain_covers(cookie.domain, host):
            sub.set_cookie(cookie)
    if not len(sub):
        return ""
    with tempfile.TemporaryDirectory() as d:
        sub.save(str(Path(d) / "c.txt"), ignore_discard=True, ignore_expires=True)
        lines = (Path(d) / "c.txt").read_text().splitlines()
    # Keep "#HttpOnly_" cookie lines; drop the header comments.
    return "\n".join(ln for ln in lines if ln and not ln.startswith("# ")) + "\n"


def replace_site_cookies(host: str, text: str) -> int:
    """Replace the cookies covering `host` in cookies.txt with those parsed from `text`.

    Other sites' cookies are kept. Returns the number of cookies saved from the paste
    (0 = site removed by a blank `text`). Raises ValueError with a user-facing message
    (file left unchanged) if the existing file or `text` cannot be parsed, or `text` holds
    no live cookie; OSError if the file cannot be written.
    """
    path = cookie_path()
    jar = load_jar(path) if path.exists() else http.cookiejar.MozillaCookieJar(str(path))
    if jar is None:
        raise ValueError("The saved cookies file could not be read; nothing changed")
    pasted: list = []
    if text.strip():
        # Browsers send CRLF; Cookie-Editor may omit the header MozillaCookieJar requires.
        text = "\n".join(text.splitlines()) + "\n"
        if "HTTP Cookie File" not in text:
            text = "# Netscape HTTP Cookie File\n" + text
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "paste.txt"
            tmp.write_text(text)
            new = load_jar(tmp)
        if new is None:
            raise ValueError("Not a Netscape cookies.txt file")
        pasted = list(new)
        if not pasted:
            raise ValueError(
                "No valid cookies in the pasted text (expired or empty export); nothing changed"
            )
    for cookie in list(jar):
        if domain_covers(cookie.domain, host):
            jar.clear(cookie.domain, cookie.path, cookie.name)
    for cookie in pasted:
        jar.set_cookie(cookie)
    if len(jar) == 0:
        path.unlink(missing_ok=True)
    elif not save_jar(jar, path):
        raise OSError("could not write cookies.txt")
    return len(pasted)
