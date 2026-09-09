import json
from types import SimpleNamespace

import pytest

from pintxos.summarize import (
    LANGUAGE_RULE_OFF,
    LANGUAGE_RULE_ON,
    MissingApiKey,
    SummarizeError,
    summarize,
)


class FakeMessages:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


def _patch_client(monkeypatch, text):
    fake = FakeClient(text)
    monkeypatch.setattr("pintxos.summarize._client", lambda: fake)
    return fake


def test_plain_json(monkeypatch):
    raw = json.dumps({"headline": "Netflix renews Supacell for season 2", "summary": "Short summary."})
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("some article text", "Original Title", "https://x")
    assert headline == "Netflix renews Supacell for season 2"
    assert summary == "Short summary."


def test_fenced_json(monkeypatch):
    raw = '```json\n{"headline": "Fact happened", "summary": "It happened."}\n```'
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Fact happened"
    assert summary == "It happened."


def test_fenced_without_json_tag(monkeypatch):
    raw = '```\n{"headline": "Fact happened", "summary": "It happened."}\n```'
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Fact happened"


def test_long_summary_passes_through_verbatim(monkeypatch):
    words = [f"word{i}" for i in range(150)]
    long_summary = " ".join(words)
    raw = json.dumps({"headline": "Headline", "summary": long_summary})
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert summary == long_summary
    assert not summary.endswith("…")
    assert summary.split() == words
    assert len(summary.split()) == 150


def test_summary_whitespace_is_normalised(monkeypatch):
    raw = json.dumps(
        {"headline": "Headline", "summary": "First  sentence.\n\nSecond\tsentence."}
    )
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert summary == "First sentence. Second sentence."


def test_garbage_raises_summarize_error(monkeypatch):
    _patch_client(monkeypatch, "not json at all")
    with pytest.raises(SummarizeError):
        summarize("text", "Title", "https://x")


def test_missing_headline_raises(monkeypatch):
    raw = json.dumps({"summary": "Only a summary."})
    _patch_client(monkeypatch, raw)
    with pytest.raises(SummarizeError):
        summarize("text", "Title", "https://x")


def test_empty_headline_raises(monkeypatch):
    raw = json.dumps({"headline": "   ", "summary": "Summary."})
    _patch_client(monkeypatch, raw)
    with pytest.raises(SummarizeError):
        summarize("text", "Title", "https://x")


def test_missing_summary_defaults_to_empty(monkeypatch):
    raw = json.dumps({"headline": "Headline only"})
    _patch_client(monkeypatch, raw)
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
    fake = _patch_client(monkeypatch, raw)

    long_text = " ".join(f"w{i}" for i in range(7000))
    summarize(long_text, "My Original Title", "https://example.com/article")

    assert len(fake.messages.calls) == 1
    kwargs = fake.messages.calls[0]
    user_message = kwargs["messages"][0]["content"]
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
    fake = _patch_client(monkeypatch, raw)

    summarize("Der Bundestag hat ...", "Titel", "https://x")

    kwargs = fake.messages.calls[0]
    system_prompt = kwargs["system"]
    user_message = kwargs["messages"][0]["content"]

    assert LANGUAGE_RULE_ON in system_prompt
    assert LANGUAGE_RULE_ON in user_message

    article_index = user_message.index("Article text:\n")
    rule_index = user_message.index(LANGUAGE_RULE_ON)
    assert rule_index > article_index


def test_respect_language_off_forces_english(monkeypatch):
    monkeypatch.setenv("PINTXOS_RESPECT_LANGUAGE", "0")
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_client(monkeypatch, raw)

    summarize("Der Bundestag hat ...", "Titel", "https://x")

    kwargs = fake.messages.calls[0]
    system_prompt = kwargs["system"]
    user_message = kwargs["messages"][0]["content"]

    assert LANGUAGE_RULE_OFF in system_prompt
    assert LANGUAGE_RULE_OFF in user_message
    assert LANGUAGE_RULE_ON not in system_prompt
    assert LANGUAGE_RULE_ON not in user_message


def test_explicit_respect_language_overrides_global_setting(monkeypatch):
    # Global setting says "respect the article's language" (env unset, default "1"),
    # but an explicit False argument must win and force English.
    monkeypatch.delenv("PINTXOS_RESPECT_LANGUAGE", raising=False)
    raw = json.dumps({"headline": "Headline", "summary": "Summary."})
    fake = _patch_client(monkeypatch, raw)

    summarize("Der Bundestag hat ...", "Titel", "https://x", respect_language=False)

    kwargs = fake.messages.calls[0]
    system_prompt = kwargs["system"]
    user_message = kwargs["messages"][0]["content"]
    assert LANGUAGE_RULE_OFF in system_prompt
    assert LANGUAGE_RULE_OFF in user_message
    assert LANGUAGE_RULE_ON not in system_prompt

    fake2 = _patch_client(monkeypatch, raw)
    monkeypatch.setenv("PINTXOS_RESPECT_LANGUAGE", "0")

    summarize("Der Bundestag hat ...", "Titel", "https://x", respect_language=True)

    kwargs2 = fake2.messages.calls[0]
    system_prompt2 = kwargs2["system"]
    user_message2 = kwargs2["messages"][0]["content"]
    assert LANGUAGE_RULE_ON in system_prompt2
    assert LANGUAGE_RULE_ON in user_message2
    assert LANGUAGE_RULE_OFF not in system_prompt2


def test_reply_with_language_field_parses_headline_and_summary(monkeypatch):
    raw = json.dumps(
        {"language": "de", "headline": "Bundestag beschließt X", "summary": "Kurz."}
    )
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    assert headline == "Bundestag beschließt X"
    assert summary == "Kurz."
