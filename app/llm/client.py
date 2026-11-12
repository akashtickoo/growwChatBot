"""The answer step's LLM client (architecture §6.6).

`AnswerClient` sits on top of any `JSONCompleter`: the Groq client in production, a fake in
tests. It sends the fixed system prompt plus the conversation so far and returns a validated
`LLMAnswer`.

Errors:
- `LLMBadOutput` (cut off at the token limit, empty, not JSON, or not matching the schema):
  the pipeline counts it as a failed attempt, retries once, then answers `not_found`.
- `LLMUnavailable` / `LLMError` (rate limits that outlasted the waits and the fallback model,
  timeouts, API errors, a retired model id): passed up, the API answers 503 (EC-LLM-01/02/13).
"""

import json
from dataclasses import dataclass

from pydantic import ValidationError

from app.llm.groq_json import GroqJSONClient, JSONCompleter, LLMBadOutput
from app.llm.prompts import SYSTEM_PROMPT
from app.llm.schemas import ANSWER_SCHEMA, ANSWER_SCHEMA_NAME, LLMAnswer
from app.settings import Settings


@dataclass
class AnswerResult:
    answer: LLMAnswer
    raw: str  # the reply as JSON text, sent back as the assistant turn on a retry
    model: str  # the model that served it (the fallback model, if it was used)
    usage: dict | None
    waited_s: float = 0.0  # rate-limit waits inside this call


class AnswerClient:
    def __init__(self, completer: JSONCompleter, *, reasoning_effort: str, max_tokens: int):
        self.completer = completer
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens

    def complete(self, turns: list[dict]) -> AnswerResult:
        """`turns` are the user/assistant messages after the system prompt."""
        result = self.completer(
            [{"role": "system", "content": SYSTEM_PROMPT}, *turns],
            ANSWER_SCHEMA,
            name=ANSWER_SCHEMA_NAME,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        )
        try:
            answer = LLMAnswer.model_validate(result.data)
        except ValidationError:
            raise LLMBadOutput("reply does not match the answer schema") from None
        return AnswerResult(
            answer, json.dumps(result.data), result.model, result.usage, result.waited_s
        )


def make_answer_client(settings: Settings) -> AnswerClient | None:
    """None when no API key is set: the app still starts, factual questions get 503 (EC-LLM-12)."""
    if settings.groq_api_key is None:
        return None
    completer = GroqJSONClient(
        settings.groq_api_key.get_secret_value(),
        settings.groq_model,
        fallback_model=settings.groq_fallback_model,
        timeout_s=settings.request_timeout_s,
        rate_limit_retries=1,  # wait for retry-after once, then the fallback model (§6.6)
    )
    return AnswerClient(
        completer,
        reasoning_effort=settings.llm_reasoning_effort,
        max_tokens=settings.llm_max_tokens,
    )
