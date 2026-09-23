"""One LLM call, two providers: Anthropic directly or any model via OpenRouter.

The model name is the router: a slash in it ("openai/gpt-5", "anthropic/claude-...")
means OpenRouter, a bare name ("claude-haiku-4-5-20251001") means the Anthropic SDK.
"""

from __future__ import annotations

from typing import NamedTuple

import anthropic
import httpx

from pintxos.config import get_setting

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Generous but finite: a hung provider must not hold a poll worker forever.
REQUEST_TIMEOUT = 60

# Models that reject reasoning: {"enabled": False} with HTTP 400; learned at runtime.
_REASONING_MANDATORY: set[str] = set()

# OpenRouter statuses that mean "your account", not "your request": no key, no
# credit, key not allowed to use this model.
_ACCOUNT_STATUS = {401, 402, 403}

# Anthropic says this in the message body, not in a dedicated status code.
_ANTHROPIC_CREDIT_MARKER = "credit balance is too low"


class LLMError(Exception):
    """Raised when a completion fails (transport, HTTP status or unusable response)."""


class MissingApiKey(LLMError):
    """Raised when the API key for the chosen provider is not configured at all."""


class AccountError(LLMError):
    """Raised when the account, not the request, is the problem.

    No credit, a rejected or unauthorized key, a model the key may not use: retrying
    the same call cannot help, only a human can fix it. Every other failure (429,
    5xx, timeouts, an unusable body) stays a plain LLMError and is worth a retry.
    """


class Completion(NamedTuple):
    """One reply: the text, and the model that actually produced it.

    The answering model can differ from the requested one when OpenRouter falls
    back to the next entry of the "models" list.
    """

    text: str
    model: str


def provider(model: str) -> str:
    """Return "openrouter" for slash-qualified model names, else "anthropic"."""
    return "openrouter" if "/" in (model or "") else "anthropic"


def complete(
    system: str,
    user: str,
    max_tokens: int,
    model: str,
    json: bool = False,
    fallback: bool = True,
) -> Completion:
    """Return the model's reply for one system + one user message.

    `json` asks the provider for a JSON object reply where it supports that
    (OpenRouter's response_format); the Anthropic branch relies on the prompt.

    `fallback` controls whether PINTXOS_FALLBACK_MODEL is added to OpenRouter's
    "models" list; pass False when the caller specifically wants to know
    whether `model` itself works (e.g. the settings health check), since a
    configured fallback would otherwise mask a broken primary model.
    """
    if provider(model) == "openrouter":
        return _complete_openrouter(system, user, max_tokens, model, json, fallback)
    return _complete_anthropic(system, user, max_tokens, model)


def _complete_anthropic(
    system: str, user: str, max_tokens: int, model: str
) -> Completion:
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
        status = getattr(e, "status_code", None)
        if status in (401, 403) or _ANTHROPIC_CREDIT_MARKER in str(e).lower():
            raise AccountError(str(e)) from e
        raise LLMError(str(e)) from e
    return Completion(response.content[0].text, getattr(response, "model", None) or model)


def _one_line(text: str, limit: int) -> str:
    """First `limit` characters of `text`, newlines turned into spaces."""
    return (text or "")[:limit].replace("\r", " ").replace("\n", " ")


def _empty_response_error(response: httpx.Response) -> LLMError:
    """LLMError for a 2xx with no usable content, naming finish_reason and the body.

    A provider that returns 200 with an empty message is the hardest failure to
    debug from a log line, so the reason it stopped and a slice of the raw body
    travel with the exception instead of being dropped.
    """
    finish_reason = "n/a"
    try:
        choices = response.json()["choices"]
        finish_reason = choices[0].get("finish_reason") or "n/a"
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        pass
    return LLMError(
        f"empty response (finish_reason={finish_reason}): {_one_line(response.text, 200)}"
    )


def _complete_openrouter(
    system: str, user: str, max_tokens: int, model: str, json: bool, fallback: bool = True
) -> Completion:
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
        "reasoning": {"effort": "minimal"} if model in _REASONING_MANDATORY else {"enabled": False},
    }
    # OpenRouter routes to the first model of "models" that answers, so a dead or
    # rate-limited primary degrades to the fallback instead of failing the item.
    # `fallback=False` callers (the settings health check) want to know whether
    # `model` itself works, so the fallback would only mask a broken primary.
    fallback_model = get_setting("PINTXOS_FALLBACK_MODEL") if fallback else None
    if fallback_model and fallback_model != model:
        body["models"] = [model, fallback_model]
    if json:
        body["response_format"] = {"type": "json_object"}

    def _post(body: dict) -> httpx.Response:
        try:
            # ponytail: no retry here, the poll loop re-tries un-stored items on the next poll
            return httpx.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise LLMError(str(e)) from e

    response = _post(body)

    if (
        response.status_code == 400
        and "reasoning is mandatory" in response.text.lower()
        and model not in _REASONING_MANDATORY
    ):
        # ponytail: learn-on-400 instead of a vendor allowlist; one extra request per model per process
        _REASONING_MANDATORY.add(model)
        body = {**body, "reasoning": {"effort": "minimal"}}
        response = _post(body)

    if not 200 <= response.status_code < 300:
        message = f"OpenRouter HTTP {response.status_code}: {response.text[:500]}"
        if response.status_code in _ACCOUNT_STATUS:
            raise AccountError(message)
        raise LLMError(message)

    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise _empty_response_error(response) from e
    if not content:
        raise _empty_response_error(response)
    answered = payload.get("model") if isinstance(payload, dict) else None
    return Completion(content, answered or model)
