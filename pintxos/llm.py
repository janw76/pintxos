"""One LLM call, two providers: Anthropic directly or any model via OpenRouter.

The model name is the router: a slash in it ("openai/gpt-5", "anthropic/claude-...")
means OpenRouter, a bare name ("claude-haiku-4-5-20251001") means the Anthropic SDK.
"""

from __future__ import annotations

import anthropic
import httpx

from pintxos.config import get_setting

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Generous but finite: a hung provider must not hold a poll worker forever.
REQUEST_TIMEOUT = 60


class LLMError(Exception):
    """Raised when a completion fails (transport, HTTP status or unusable response)."""


class MissingApiKey(LLMError):
    """Raised when the API key for the chosen provider is not configured at all."""


def provider(model: str) -> str:
    """Return "openrouter" for slash-qualified model names, else "anthropic"."""
    return "openrouter" if "/" in (model or "") else "anthropic"


def complete(
    system: str, user: str, max_tokens: int, model: str, json: bool = False
) -> str:
    """Return the model's reply text for one system + one user message.

    `json` asks the provider for a JSON object reply where it supports that
    (OpenRouter's response_format); the Anthropic branch relies on the prompt.
    """
    if provider(model) == "openrouter":
        return _complete_openrouter(system, user, max_tokens, model, json)
    return _complete_anthropic(system, user, max_tokens, model)


def _complete_anthropic(system: str, user: str, max_tokens: int, model: str) -> str:
    api_key = get_setting("ANTHROPIC_API_KEY")
    if not api_key:
        raise MissingApiKey("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key, max_retries=2)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        raise LLMError(str(e)) from e
    return response.content[0].text


def _complete_openrouter(
    system: str, user: str, max_tokens: int, model: str, json: bool
) -> str:
    api_key = get_setting("OPENROUTER_API_KEY")
    if not api_key:
        raise MissingApiKey("OPENROUTER_API_KEY not set")

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Reasoning tokens are billed and eat the max_tokens budget without
        # improving a rewrite-this-headline task; keep it off.
        "reasoning": {"enabled": False},
    }
    if json:
        body["response_format"] = {"type": "json_object"}

    try:
        # ponytail: no retry here, the poll loop re-tries un-stored items on the next poll
        response = httpx.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise LLMError(str(e)) from e

    if not 200 <= response.status_code < 300:
        raise LLMError(f"OpenRouter HTTP {response.status_code}: {response.text[:500]}")

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError("empty response") from e
    if not content:
        raise LLMError("empty response")
    return content
