"""Tests for the OpenRouter model catalogue endpoint (cache, filter, failure modes)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from pintxos import openrouter_models as om
from pintxos.app import app

CATALOGUE = [
    {"id": "google/gemini-2.5-flash", "name": "Google: Gemini 2.5 Flash"},
    {"id": "google/gemini-2.5-pro", "name": "Google: Gemini 2.5 Pro"},
    {"id": "openai/gpt-5", "name": "OpenAI: GPT-5"},
    {"id": "acme/m1", "name": "Acme Wonder"},
    {"id": "google/gemini-batch", "name": "Google: Gemini 2.5 Flash"},
    {"id": "acme/m2", "name": "Acme Batch Runner"},
    {"id": "deepseek/r1:free", "name": "DeepSeek R1 (free)"},
    {"id": "anthropic/claude:thinking", "name": "Claude (thinking)"},
    {"id": "meta/llama:extended", "name": "Llama (extended)"},
]


class FakeResponse:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


@pytest.fixture(autouse=True)
def _reset_cache():
    om._cache.update(models=None, at=0.0)


def _stub(monkeypatch, result):
    """Patch httpx.get; `result` is a response or exception, or a callable returning one."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        r = result() if callable(result) else result
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(om.httpx, "get", fake_get)
    return calls


def _ok(models=CATALOGUE):
    return FakeResponse(payload={"data": models})


def _ids(body):
    return [m["id"] for m in body["models"]]


def _get(q):
    with TestClient(app) as c:
        resp = c.get("/api/models", params={"q": q})
    assert resp.status_code == 200
    return resp.json()


def test_gemini_fl_matches_flash_only(monkeypatch):
    calls = _stub(monkeypatch, _ok())
    body = _get("gemini fl")
    assert _ids(body) == ["google/gemini-2.5-flash"]
    assert body["error"] is None and body["more"] is False
    assert calls[0][0] == "https://openrouter.ai/api/v1/models"
    assert calls[0][1]["timeout"] == 20
    assert "headers" not in calls[0][1]


def test_tokens_in_any_order(monkeypatch):
    _stub(monkeypatch, _ok())
    assert _ids(_get("fl gemini")) == ["google/gemini-2.5-flash"]


def test_match_on_name_only(monkeypatch):
    _stub(monkeypatch, _ok())
    assert _ids(_get("wonder")) == ["acme/m1"]
    assert _ids(_get("ACME m1")) == ["acme/m1"]  # one token per field


def test_batch_excluded_by_id_and_name(monkeypatch):
    _stub(monkeypatch, _ok())
    assert _ids(_get("gemini flash")) == ["google/gemini-2.5-flash"]  # id batch dropped
    assert _get("acme")["models"] == [{"id": "acme/m1", "name": "Acme Wonder"}]  # name batch dropped


def test_variants_kept(monkeypatch):
    _stub(monkeypatch, _ok())
    assert _ids(_get("r1")) == ["deepseek/r1:free"]
    assert _ids(_get("claude")) == ["anthropic/claude:thinking"]
    assert _ids(_get("llama")) == ["meta/llama:extended"]


def test_cap_30_with_more(monkeypatch):
    _stub(monkeypatch, _ok([{"id": f"x/m{i}", "name": f"M{i}"} for i in range(45)]))
    body = _get("x/m")
    assert len(body["models"]) == 30 and body["more"] is True
    assert _ids(body)[0] == "x/m0"
    # a narrower query under the cap is not "more"
    assert _get("x/m1")["more"] is False


@pytest.mark.parametrize("q", ["", "   ", "\t\n"])
def test_empty_query(monkeypatch, q):
    _stub(monkeypatch, _ok())
    body = _get(q)
    assert body["models"] == [] and body["more"] is False and body["error"] is None


def test_overlong_query_does_not_500(monkeypatch):
    _stub(monkeypatch, _ok())
    assert _get("a" * 5000)["models"] == []


def test_cached_for_24h(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(om.time, "monotonic", lambda: now[0])
    calls = _stub(monkeypatch, _ok())
    _get("gemini")
    now[0] += 24 * 3600 - 1
    _get("gpt")
    assert len(calls) == 1
    now[0] += 2
    _get("gpt")
    assert len(calls) == 2


def test_failure_without_cache(monkeypatch):
    _stub(monkeypatch, FakeResponse(status_code=502))
    body = _get("gemini")
    assert body["models"] == [] and body["error"] == "OpenRouter answered 502"


@pytest.mark.parametrize(
    "result, expected",
    [
        (httpx.ReadTimeout("slow"), "timed out"),
        (httpx.ConnectError("no route"), "connect"),
        (FakeResponse(bad_json=True), "unreadable"),
        (FakeResponse(payload={"data": "nope"}), "unreadable"),
        (FakeResponse(payload={"other": []}), "unreadable"),
        (FakeResponse(payload=[]), "unreadable"),
    ],
)
def test_failure_modes_report_error_not_crash(monkeypatch, result, expected):
    _stub(monkeypatch, result)
    body = _get("gemini")
    assert body["models"] == [] and expected in body["error"]


def test_stale_list_served_when_refetch_fails(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(om.time, "monotonic", lambda: now[0])
    state = {"r": _ok()}
    calls = _stub(monkeypatch, lambda: state["r"])
    assert _ids(_get("gemini fl")) == ["google/gemini-2.5-flash"]
    now[0] += 24 * 3600 + 1
    state["r"] = FakeResponse(status_code=503)
    body = _get("gemini fl")
    assert _ids(body) == ["google/gemini-2.5-flash"]
    assert body["error"] == "OpenRouter answered 503"
    # no back-off: next request retries, and a recovery clears the error
    state["r"] = _ok()
    assert _get("gemini fl")["error"] is None
    assert len(calls) == 3


def test_entries_missing_name_or_id(monkeypatch):
    _stub(monkeypatch, _ok([{"id": "a/noname"}, {"name": "No id"}, "junk", {"id": "b/ok", "name": "Ok"}]))
    assert _get("a/")["models"] == [{"id": "a/noname", "name": "a/noname"}]
    assert _ids(_get("no id")) == []
