import anthropic
import pytest

from pintxos import llm


class FakeResponse:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _patch_post(monkeypatch, response):
    """Patch httpx.post to record its kwargs and return `response`."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr("pintxos.llm.httpx.post", fake_post)
    return calls


def _forbid_post(monkeypatch):
    """Patch httpx.post so any call fails the test."""

    def fake_post(*args, **kwargs):
        pytest.fail("httpx.post was called")

    monkeypatch.setattr("pintxos.llm.httpx.post", fake_post)


def _ok_response(content="the answer"):
    return FakeResponse(payload={"choices": [{"message": {"content": content}}]})


# --- provider -------------------------------------------------------------


def test_provider_slash_means_openrouter():
    assert llm.provider("openai/gpt-5") == "openrouter"
    assert llm.provider("anthropic/claude-haiku-4.5") == "openrouter"


def test_provider_bare_name_means_anthropic():
    assert llm.provider("claude-haiku-4-5-20251001") == "anthropic"


# --- missing keys ---------------------------------------------------------


def test_openrouter_without_key_raises_before_any_request(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _forbid_post(monkeypatch)
    with pytest.raises(llm.MissingApiKey, match="OPENROUTER_API_KEY not set"):
        llm.complete("sys", "user", 100, "openai/gpt-5")


def test_anthropic_without_key_raises_before_constructing_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fake_anthropic(*args, **kwargs):
        pytest.fail("anthropic.Anthropic was constructed")

    monkeypatch.setattr("pintxos.llm.anthropic.Anthropic", fake_anthropic)
    with pytest.raises(llm.MissingApiKey, match="ANTHROPIC_API_KEY not set"):
        llm.complete("sys", "user", 100, "claude-haiku-4-5-20251001")


def test_missing_api_key_is_an_llm_error():
    assert issubclass(llm.MissingApiKey, llm.LLMError)


# --- OpenRouter request shape --------------------------------------------


def test_openrouter_request_shape(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    calls = _patch_post(monkeypatch, _ok_response("sport"))

    assert llm.complete("sys prompt", "user prompt", 123, "openai/gpt-5") == "sport"

    ((url, kwargs),) = calls
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer or-key"
    assert kwargs["timeout"] == 60
    body = kwargs["json"]
    assert body["model"] == "openai/gpt-5"
    assert body["max_tokens"] == 123
    assert body["reasoning"] == {"enabled": False}
    assert body["messages"] == [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    assert "response_format" not in body


def test_openrouter_asks_for_json_object_only_when_requested(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    calls = _patch_post(monkeypatch, _ok_response('{"headline": "x"}'))

    llm.complete("sys", "user", 10, "openai/gpt-5", json=True)

    ((_url, kwargs),) = calls
    assert kwargs["json"]["response_format"] == {"type": "json_object"}
    assert kwargs["json"]["reasoning"] == {"enabled": False}


# --- OpenRouter failures --------------------------------------------------


def test_openrouter_http_500_raises_llm_error_with_the_status(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    _patch_post(monkeypatch, FakeResponse(status_code=500, text="upstream exploded"))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("sys", "user", 10, "openai/gpt-5")
    assert "500" in str(excinfo.value)
    assert "upstream exploded" in str(excinfo.value)


def test_openrouter_transport_error_raises_llm_error(monkeypatch):
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("pintxos.llm.httpx.post", fake_post)

    with pytest.raises(llm.LLMError, match="no route to host"):
        llm.complete("sys", "user", 10, "openai/gpt-5")


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {},
    ],
)
def test_openrouter_empty_or_missing_content_raises(monkeypatch, payload):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    _patch_post(monkeypatch, FakeResponse(payload=payload))

    with pytest.raises(llm.LLMError, match="empty response"):
        llm.complete("sys", "user", 10, "openai/gpt-5")


# --- reasoning-mandatory fallback ------------------------------------------


def _patch_post_reasoning_aware(monkeypatch, mandatory_model="openai/gpt-5-mini"):
    """Patch httpx.post to imitate a provider where `mandatory_model` rejects
    reasoning:{"enabled": False} with HTTP 400, but accepts effort:minimal;
    any other model accepts enabled:False. Records every call."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        body = kwargs["json"]
        reasoning = body["reasoning"]
        if body["model"] == mandatory_model and reasoning == {"enabled": False}:
            return FakeResponse(
                status_code=400,
                text=(
                    '{"error":{"message":"Reasoning is mandatory for this endpoint '
                    'and cannot be disabled.","code":400}}'
                ),
            )
        return _ok_response("OK")

    monkeypatch.setattr("pintxos.llm.httpx.post", fake_post)
    return calls


def test_reasoning_mandatory_model_retries_with_effort_minimal(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm, "_REASONING_MANDATORY", set())
    calls = _patch_post_reasoning_aware(monkeypatch)

    result = llm.complete("sys", "user", 50, "openai/gpt-5-mini")

    assert result == "OK"
    assert len(calls) == 2
    assert calls[0][1]["json"]["reasoning"] == {"enabled": False}
    assert calls[1][1]["json"]["reasoning"] == {"effort": "minimal"}
    assert "openai/gpt-5-mini" in llm._REASONING_MANDATORY


def test_reasoning_mandatory_model_remembered_across_calls(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm, "_REASONING_MANDATORY", {"openai/gpt-5-mini"})
    calls = _patch_post_reasoning_aware(monkeypatch)

    result = llm.complete("sys", "user", 50, "openai/gpt-5-mini")

    assert result == "OK"
    assert len(calls) == 1
    assert calls[0][1]["json"]["reasoning"] == {"effort": "minimal"}


def test_other_model_still_sends_enabled_false_after_a_mandatory_model_learned(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm, "_REASONING_MANDATORY", {"openai/gpt-5-mini"})
    calls = _patch_post_reasoning_aware(monkeypatch)

    # google/gemini-2.5-flash-lite is not in the learned set: enabled:False
    # is sent and succeeds (this fake only 400s the mandatory model).
    result = llm.complete("sys", "user", 50, "google/gemini-2.5-flash-lite")

    assert result == "OK"
    assert len(calls) == 1
    assert calls[0][1]["json"]["reasoning"] == {"enabled": False}
    assert "google/gemini-2.5-flash-lite" not in llm._REASONING_MANDATORY


def test_400_with_unrelated_body_raises_without_retry_or_learning(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(llm, "_REASONING_MANDATORY", set())
    calls = _patch_post(monkeypatch, FakeResponse(status_code=400, text="invalid model"))

    with pytest.raises(llm.LLMError, match="400"):
        llm.complete("sys", "user", 50, "openai/gpt-5-mini")

    assert len(calls) == 1
    assert "openai/gpt-5-mini" not in llm._REASONING_MANDATORY


# --- Anthropic branch -----------------------------------------------------


class _FakeMessages:
    def __init__(self, text=None, error=None):
        self._text = text
        self._error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return type("R", (), {"content": [type("C", (), {"text": self._text})()]})()


def _patch_anthropic(monkeypatch, text=None, error=None):
    messages = _FakeMessages(text, error)
    init_kwargs = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            init_kwargs.update(kwargs)
            self.messages = messages

    monkeypatch.setattr("pintxos.llm.anthropic.Anthropic", FakeAnthropic)
    return messages, init_kwargs


def test_anthropic_request_shape_and_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    messages, init_kwargs = _patch_anthropic(monkeypatch, text="sport")

    assert llm.complete("sys", "user", 55, "claude-haiku-4-5-20251001") == "sport"

    assert init_kwargs == {"api_key": "an-key", "max_retries": 2}
    (call,) = messages.calls
    assert call["model"] == "claude-haiku-4-5-20251001"
    assert call["max_tokens"] == 55
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "user"}]


def test_anthropic_api_error_becomes_llm_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    _patch_anthropic(monkeypatch, error=anthropic.APIError("boom", request=None, body=None))

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("sys", "user", 10, "claude-haiku-4-5-20251001")
    assert "boom" in str(excinfo.value)
    assert not isinstance(excinfo.value, llm.MissingApiKey)


def test_anthropic_branch_never_touches_httpx(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    _forbid_post(monkeypatch)
    _patch_anthropic(monkeypatch, text="sport")

    assert llm.complete("sys", "user", 10, "claude-haiku-4-5-20251001") == "sport"
