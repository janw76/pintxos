"""Turn raw article text into a factual headline + short summary via Claude."""

from __future__ import annotations

import json
import re

import anthropic

from pintxos.config import get_setting

MAX_INPUT_WORDS = 6000
MAX_SUMMARY_WORDS = 100

SYSTEM_PROMPT = (
    "You rewrite clickbait into facts. Output headline: one sentence, states "
    "the concrete fact (names, numbers, what happened), no teasers, no "
    "questions, no 'this', <=15 words. Summary: <=100 words, factual, no "
    "opinion, no 'the article says'. Same language as the article. "
    "If the text is not an article (paywall notice, cookie banner, error "
    "page), still produce the most factual headline possible from what is "
    "given. Reply as JSON {\"headline\": ..., \"summary\": ...} only."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class SummarizeError(Exception):
    """Raised when summarization fails (API error or unparseable response)."""


class MissingApiKey(SummarizeError):
    """Raised when no ANTHROPIC_API_KEY is configured at all."""


def _client() -> anthropic.Anthropic:
    api_key = get_setting("ANTHROPIC_API_KEY")
    if not api_key:
        raise MissingApiKey("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key, max_retries=2)


def _parse(raw: str) -> tuple[str, str]:
    stripped = raw.strip()
    stripped = _FENCE_RE.sub("", stripped).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise SummarizeError(f"could not parse response as JSON: {e}") from e

    if not isinstance(data, dict):
        raise SummarizeError("response JSON was not an object")

    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        raise SummarizeError("response JSON missing non-empty 'headline'")
    headline = " ".join(headline.split())

    summary = data.get("summary")
    if not isinstance(summary, str):
        summary = ""
    summary_words = summary.split()
    if len(summary_words) > MAX_SUMMARY_WORDS:
        summary = " ".join(summary_words[:MAX_SUMMARY_WORDS]) + "…"

    return headline, summary


def summarize(text: str, original_title: str, url: str) -> tuple[str, str]:
    """Return (headline, summary) for the given article text."""
    truncated = " ".join(text.split()[:MAX_INPUT_WORDS])
    user_message = (
        f"Original title: {original_title}\nURL: {url}\n\nArticle text:\n{truncated}"
    )

    client = _client()
    try:
        response = client.messages.create(
            model=get_setting("PINTXOS_MODEL"),
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        raise SummarizeError(str(e)) from e

    raw = response.content[0].text
    return _parse(raw)
