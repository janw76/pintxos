"""Server-side OpenRouter model catalogue for the model dropdown.

The browser never calls openrouter.ai: we fetch the public catalogue (no API key),
keep it in memory for 24 h and serve a filtered slice of it.
"""

from __future__ import annotations

import time

import httpx

URL = "https://openrouter.ai/api/v1/models"
TTL = 24 * 3600
TIMEOUT = 20
LIMIT = 30
MAX_QUERY = 200

# ponytail: per-process cache, no lock; two concurrent cold requests may both fetch.
_cache: dict = {"models": None, "at": 0.0}


def _fetch() -> list[dict]:
    """Fetch the catalogue as [{"id", "name"}, ...]; raise RuntimeError with a short reason."""
    try:
        resp = httpx.get(URL, timeout=TIMEOUT)
    except httpx.TimeoutException:
        raise RuntimeError("OpenRouter timed out") from None
    except httpx.HTTPError:
        raise RuntimeError("Could not connect to OpenRouter") from None
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter answered {resp.status_code}")
    try:
        data = resp.json()["data"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("OpenRouter sent an unreadable answer") from None
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter sent an unreadable answer")
    return [
        {"id": m["id"], "name": m.get("name") or m["id"]}
        for m in data
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]
    ]


def get_models() -> tuple[list[dict], str | None]:
    """Return (catalogue, error). A failed refetch keeps serving the last good list."""
    if _cache["models"] is not None and time.monotonic() - _cache["at"] < TTL:
        return _cache["models"], None
    try:
        _cache["models"], _cache["at"] = _fetch(), time.monotonic()
    except RuntimeError as exc:
        return _cache["models"] or [], str(exc)
    return _cache["models"], None


def filter_models(models: list[dict], q: str) -> tuple[list[dict], bool]:
    """First LIMIT models matching every token of `q`, plus whether more matched."""
    tokens = q[:MAX_QUERY].lower().split()
    if not tokens:
        return [], False
    hits = []
    for m in models:
        hay = f"{m['id']}\n{m['name']}".lower()
        if "batch" not in hay and all(t in hay for t in tokens):
            hits.append(m)
    return hits[:LIMIT], len(hits) > LIMIT


def search(q: str) -> dict:
    models, error = get_models()
    found, more = filter_models(models, q)
    return {"models": found, "more": more, "error": error}
