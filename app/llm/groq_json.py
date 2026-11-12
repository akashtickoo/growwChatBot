"""Groq chat completions with strict JSON-schema output (architecture §6.6).

Shared by offline fact extraction (Phase 3) and the answer step (Phase 4C). Strict mode uses
constrained decoding, so the reply always matches the schema; every property must be listed in
`required` and objects must set `additionalProperties: false`.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

DEFAULT_RATE_LIMIT_WAIT_S = 5.0
MAX_RATE_LIMIT_WAIT_S = 60.0


class LLMError(Exception):
    """A request the API rejected (bad request, auth). Retrying won't help."""


class LLMUnavailable(LLMError):
    """Rate limits, timeouts, server or connection errors that outlasted the retries."""


class LLMBadOutput(LLMError):
    """A reply that can't be used: cut off at the token limit, empty, or not JSON."""


@dataclass
class JSONResult:
    data: dict
    model: str  # the model that actually served the reply
    finish_reason: str
    usage: dict | None = None
    waited_s: float = 0.0  # time spent waiting out rate limits for this reply


class JSONCompleter(Protocol):
    def __call__(
        self,
        messages: list[dict],
        schema: dict,
        *,
        name: str,
        reasoning_effort: str,
        max_tokens: int,
    ) -> JSONResult: ...


def _retry_after(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after", "") if response is not None else ""
    try:
        return min(float(header), MAX_RATE_LIMIT_WAIT_S)
    except ValueError:
        return DEFAULT_RATE_LIMIT_WAIT_S


def parse_reply(response: object, requested_model: str) -> JSONResult:
    choice = response.choices[0]
    content = choice.message.content or ""
    if choice.finish_reason == "length":
        raise LLMBadOutput("reply cut off at the token limit")
    if not content.strip():
        raise LLMBadOutput("empty reply")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(f"reply is not valid JSON ({exc.msg})") from None
    if not isinstance(data, dict):
        raise LLMBadOutput("reply is not a JSON object")
    usage = response.usage.model_dump() if getattr(response, "usage", None) else None
    return JSONResult(
        data, getattr(response, "model", None) or requested_model, choice.finish_reason, usage
    )


class GroqJSONClient:
    """Calls Groq; waits out rate limits; tries `fallback_model` once if the main model fails."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        fallback_model: str | None = None,
        timeout_s: float = 60.0,
        rate_limit_retries: int = 3,
        min_interval_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        client: object | None = None,
    ):
        if client is None:
            from groq import Groq

            client = Groq(api_key=api_key, timeout=timeout_s, max_retries=1)
        self.client = client
        self.model = model
        self.fallback_model = fallback_model
        self.rate_limit_retries = rate_limit_retries
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self._last_call = 0.0

    def _throttle(self) -> None:
        if self.min_interval_s:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = time.monotonic()

    def __call__(
        self,
        messages: list[dict],
        schema: dict,
        *,
        name: str,
        reasoning_effort: str,
        max_tokens: int,
    ) -> JSONResult:
        import groq

        response_format = {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        }
        models = [self.model] + ([self.fallback_model] if self.fallback_model else [])
        last_error: Exception | None = None
        waited = 0.0
        for model in models:
            for attempt in range(self.rate_limit_retries + 1):
                self._throttle()
                try:
                    response = self.client.chat.completions.create(
                        model=model,
                        messages=messages,
                        response_format=response_format,
                        reasoning_effort=reasoning_effort,
                        max_completion_tokens=max_tokens,
                    )
                except groq.RateLimitError as exc:
                    last_error = exc
                    if attempt < self.rate_limit_retries:
                        wait = _retry_after(exc)
                        waited += wait
                        self.sleep(wait)
                        continue
                    break  # try the fallback model
                except (
                    groq.APITimeoutError,
                    groq.APIConnectionError,
                    groq.InternalServerError,
                ) as exc:
                    last_error = exc
                    break  # try the fallback model
                except groq.APIStatusError as exc:
                    raise LLMError(f"Groq API error {exc.status_code}") from None
                result = parse_reply(response, model)
                result.waited_s = waited
                return result
        raise LLMUnavailable(f"Groq unavailable: {type(last_error).__name__}")
