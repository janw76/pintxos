import json
from types import SimpleNamespace

import pytest

from pintxos.summarize import MissingApiKey, SummarizeError, summarize


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


def test_summary_truncated_to_100_words(monkeypatch):
    long_summary = " ".join(f"word{i}" for i in range(150))
    raw = json.dumps({"headline": "Headline", "summary": long_summary})
    _patch_client(monkeypatch, raw)
    headline, summary = summarize("text", "Title", "https://x")
    words = summary[:-1].split()  # strip trailing ellipsis before counting
    assert summary.endswith("…")
    assert len(words) == 100
    assert words == [f"word{i}" for i in range(100)]


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
    assert len(article_text.split()) == 6000
