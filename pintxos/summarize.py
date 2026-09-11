"""Turn raw article text into a factual headline + short summary via Claude."""

from __future__ import annotations

import json
import re

from pintxos import llm
from pintxos.config import get_setting, is_truthy

MAX_INPUT_WORDS = 6000

# Language rules: swapped into the system prompt and repeated in the user message
# depending on PINTXOS_RESPECT_LANGUAGE. Kept as named constants (rather than inline
# strings) so tests can assert on them instead of duplicating the wording.
LANGUAGE_RULE_ON = (
    "Headline and summary MUST be written in the same language as the article "
    "text (never translate)."
)
LANGUAGE_RULE_OFF = "Write headline and summary in English regardless of the article's language."

_SYSTEM_PROMPT_TEMPLATE = (
    "You rewrite clickbait into facts. Output headline: one sentence, states "
    "the concrete fact (names, numbers, what happened), no teasers, no "
    "questions, no 'this', <=15 words. Summary: <=100 words, factual, no "
    "opinion, no 'the article says'. {rule} "
    "If the text is not an article (paywall notice, cookie banner, error "
    "page), still produce the most factual headline possible from what is "
    "given. Reply as JSON {{\"language\": ..., \"headline\": ..., \"summary\": ...}} only, "
    "with \"language\" naming the article's language."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class SummarizeError(Exception):
    """Raised when summarization fails (API error or unparseable response)."""


# Re-export, not a subclass: poll.py catches summarize.MissingApiKey by name and
# pintxos.llm raises it, so both names must be the very same exception object.
MissingApiKey = llm.MissingApiKey


def _parse(raw: str) -> tuple[str, str]:
    stripped = raw.strip()
    stripped = _FENCE_RE.sub("", stripped).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise SummarizeError(f"could not parse response as JSON: {e}") from e

    if not isinstance(data, dict):
        raise SummarizeError("response JSON was not an object")

    # "language" is optional metadata requested when PINTXOS_RESPECT_LANGUAGE is on;
    # it is not part of the (headline, summary) result, so it is simply ignored here.
    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        raise SummarizeError("response JSON missing non-empty 'headline'")
    headline = " ".join(headline.split())

    summary = data.get("summary")
    if not isinstance(summary, str):
        summary = ""
    summary = " ".join(summary.split())

    return headline, summary


def summarize(
    text: str,
    original_title: str,
    url: str,
    respect_language: bool | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Return (headline, summary) for the given article text.

    `respect_language`, when given, overrides the global PINTXOS_RESPECT_LANGUAGE
    setting (e.g. with a per-feed choice); None (the default) falls back to it.
    `model`, when given, overrides the global PINTXOS_MODEL the same way.
    """
    if respect_language is None:
        respect_language = is_truthy(get_setting("PINTXOS_RESPECT_LANGUAGE"))
    rule = LANGUAGE_RULE_ON if respect_language else LANGUAGE_RULE_OFF
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(rule=rule)

    truncated = " ".join(text.split()[:MAX_INPUT_WORDS])
    # ponytail: the language rule is repeated here, after the article text, because
    # models weigh instructions placed near the end of the prompt (recency) more
    # heavily than ones stated only once at the very start of the system prompt.
    user_message = (
        f"Original title: {original_title}\nURL: {url}\n\nArticle text:\n{truncated}"
        f"\n\n{rule}"
    )

    model = model or get_setting("PINTXOS_MODEL")
    try:
        # 400 tokens is only ~300 words of JSON; a long summary would be cut
        # mid-JSON and raise SummarizeError, silently re-introducing a length limit.
        raw = llm.complete(system_prompt, user_message, 1024, model, json=True)
    except llm.MissingApiKey:
        raise
    except llm.LLMError as e:
        raise SummarizeError(str(e)) from e

    return _parse(raw)
