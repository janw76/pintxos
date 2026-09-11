import json

import pytest

from pintxos import llm
from pintxos.summarize import (
    LANGUAGE_RULE_OFF,
    LANGUAGE_RULE_ON,
    MissingApiKey,
    SummarizeError,
    summarize,
)


class FakeComplete:
    """Stands in for llm.complete: records every call, returns `text` or raises `error`."""

    def __init__(self, text=None, error=None):
        self._text = text
        self._error = error
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._text


def _patch_complete(monkeypatch, text=None, error=None):
    fake = FakeComplete(text, error)
    # summarize() calls llm.complete through the module object, so patch it at the source.
    monkeypatch.setattr("pintxos.llm.complete", fake)
    return fake


def _one_call(fake):
    """The single recorded complete() call, as a keyword-style dict."""
    ((args, kwargs),) = fake.calls
    system, user, max_tokens, model = args
    return {
        "system": system,
        "user": user,
        "max_tokens": max_tokens,
        "model": model,
        **kwargs,
    }


def test_plain_json(monkeypatch):
    raw = json.dumps({"headline": "Netflix renews Supacell for season 2", "summary": "Short summary."})
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("some article text", "Original Title", "https://x")
    assert headline == "Netflix renews Supacell for season 2"
    assert summary == "Short summary."


def test_fenced_json(monkeypatch):
    raw = '```json\n{"headline": "Fact happened", "summary": "It happened."}\n```'
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Fact happened"
    assert summary == "It happened."


def test_fenced_without_json_tag(monkeypatch):
    raw = '```\n{"headline": "Fact happened", "summary": "It happened."}\n```'
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Fact happened"


def test_long_summary_passes_through_verbatim(monkeypatch):
    words = [f"word{i}" for i in range(150)]
    long_summary = " ".join(words)
    raw = json.dumps({"headline": "Headline", "summary": long_summary})
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert summary == long_summary
    assert not summary.endswith("…")
    assert summary.split() == words
    assert len(summary.split()) == 150


def test_summary_whitespace_is_normalised(monkeypatch):
    raw = json.dumps(
        {"headline": "Headline", "summary": "First  sentence.\n\nSecond\tsentence."}
    )
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert summary == "First sentence. Second sentence."


def test_garbage_raises_summarize_error(monkeypatch):
    _patch_complete(monkeypatch, "not json at all")
    with pytest.raises(SummarizeError):
        summarize("text", "Title", "https://x")


def test_missing_headline_raises(monkeypatch):
    raw = json.dumps({"summary": "Only a summary."})
    _patch_complete(monkeypatch, raw)
    with pytest.raises(SummarizeError):
        summarize("text", "Title", "https://x")


def test_empty_headline_raises(monkeypatch):
    raw = json.dumps({"headline": "   ", "summary": "Summary."})
    _patch_complete(monkeypatch, raw)
    with pytest.raises(SummarizeError):
        summarize("text", "Title", "https://x")


def test_missing_summary_defaults_to_empty(monkeypatch):
    raw = json.dumps({"headline": "Headline only"})
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Headline only"
    assert summary == ""


def test_missing_api_key_raises(monkeypatch, tmp_path):
    from pintxos.db import init_db

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    init_db()
    with pytest.raises(MissingApiKey):
        summarize("text", "Title", "https://x")


def test_user_message_contains_title_and_truncates_long_text(monkeypatch):
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_complete(monkeypatch, raw)

    long_text = " ".join(f"w{i}" for i in range(7000))
    summarize(long_text, "My Original Title", "https://example.com/article")

    call = _one_call(fake)
    user_message = call["user"]
    assert "My Original Title" in user_message
    assert "https://example.com/article" in user_message

    article_text = user_message.split("Article text:\n", 1)[1]
    # The article body is followed by a trailing language-instruction line (see
    # summarize()); strip it off before counting the truncated body's words.
    article_text = article_text.split("\n\n", 1)[0]
    assert len(article_text.split()) == 6000


def test_respect_language_default_enforces_same_language(monkeypatch):
    monkeypatch.delenv("PINTXOS_RESPECT_LANGUAGE", raising=False)
    raw = json.dumps(
        {"language": "de", "headline": "Bundestag beschließt X", "summary": "Kurz."}
    )
    fake = _patch_complete(monkeypatch, raw)

    summarize("Der Bundestag hat ...", "Titel", "https://x")

    call = _one_call(fake)
    system_prompt = call["system"]
    user_message = call["user"]

    assert LANGUAGE_RULE_ON in system_prompt
    assert LANGUAGE_RULE_ON in user_message

    article_index = user_message.index("Article text:\n")
    rule_index = user_message.index(LANGUAGE_RULE_ON)
    assert rule_index > article_index


def test_respect_language_off_forces_english(monkeypatch):
    monkeypatch.setenv("PINTXOS_RESPECT_LANGUAGE", "0")
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_complete(monkeypatch, raw)

    summarize("Der Bundestag hat ...", "Titel", "https://x")

    call = _one_call(fake)
    system_prompt = call["system"]
    user_message = call["user"]

    assert LANGUAGE_RULE_OFF in system_prompt
    assert LANGUAGE_RULE_OFF in user_message
    assert LANGUAGE_RULE_ON not in system_prompt
    assert LANGUAGE_RULE_ON not in user_message


def test_explicit_respect_language_overrides_global_setting(monkeypatch):
    # Global setting says "respect the article's language" (env unset, default "1"),
    # but an explicit False argument must win and force English.
    monkeypatch.delenv("PINTXOS_RESPECT_LANGUAGE", raising=False)
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_complete(monkeypatch, raw)

    summarize("Der Bundestag hat ...", "Titel", "https://x", respect_language=False)

    call = _one_call(fake)
    system_prompt = call["system"]
    user_message = call["user"]
    assert LANGUAGE_RULE_OFF in system_prompt
    assert LANGUAGE_RULE_OFF in user_message
    assert LANGUAGE_RULE_ON not in system_prompt

    fake2 = _patch_complete(monkeypatch, raw)
    monkeypatch.setenv("PINTXOS_RESPECT_LANGUAGE", "0")

    summarize("Der Bundestag hat ...", "Titel", "https://x", respect_language=True)

    call2 = _one_call(fake2)
    system_prompt2 = call2["system"]
    user_message2 = call2["user"]
    assert LANGUAGE_RULE_ON in system_prompt2
    assert LANGUAGE_RULE_ON in user_message2
    assert LANGUAGE_RULE_OFF not in system_prompt2


def test_reply_with_language_field_parses_headline_and_summary(monkeypatch):
    raw = json.dumps(
        {"language": "de", "headline": "Bundestag beschließt X", "summary": "Kurz."}
    )
    _patch_complete(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Bundestag beschließt X"
    assert summary == "Kurz."


def test_api_error_becomes_summarize_error(monkeypatch):
    _patch_complete(monkeypatch, error=llm.LLMError("boom"))
    with pytest.raises(SummarizeError, match="boom"):
        summarize("text", "Title", "https://x")


def test_asks_for_json_and_uses_the_configured_model(monkeypatch):
    monkeypatch.setenv("PINTXOS_MODEL", "claude-test-model")
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_complete(monkeypatch, raw)

    summarize("text", "Title", "https://x")

    call = _one_call(fake)
    assert call["json"] is True
    assert call["model"] == "claude-test-model"
    assert call["max_tokens"] == 1024


def test_explicit_model_overrides_the_setting(monkeypatch):
    monkeypatch.setenv("PINTXOS_MODEL", "claude-test-model")
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_complete(monkeypatch, raw)

    summarize("text", "Title", "https://x", model="x/y")

    assert _one_call(fake)["model"] == "x/y"
