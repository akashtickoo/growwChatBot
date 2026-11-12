"""Groq JSON client: rate limits, fallback model, unusable replies (stubbed SDK client)."""

from types import SimpleNamespace

import groq
import httpx
import pytest

from app.llm.groq_json import GroqJSONClient, LLMBadOutput, LLMError, LLMUnavailable

REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def reply(content='{"ok": true}', finish="stop", model="openai/gpt-oss-120b"):
    message = SimpleNamespace(content=content)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)], model=model, usage=None
    )


def rate_limited(retry_after="2"):
    response = httpx.Response(429, request=REQUEST, headers={"retry-after": retry_after})
    return groq.RateLimitError("rate limited", response=response, body=None)


def server_error():
    response = httpx.Response(503, request=REQUEST)
    return groq.InternalServerError("unavailable", response=response, body=None)


class StubCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(outcomes, **kwargs):
    stub = StubCompletions(outcomes)
    waits: list[float] = []
    c = GroqJSONClient(
        "test-key",
        "openai/gpt-oss-120b",
        sleep=waits.append,
        client=SimpleNamespace(chat=SimpleNamespace(completions=stub)),
        **kwargs,
    )
    return c, stub, waits


def call(c):
    return c(
        [{"role": "user", "content": "hi"}],
        {"type": "object"},
        name="t",
        reasoning_effort="low",
        max_tokens=100,
    )


def test_sends_strict_json_schema_and_parses_the_reply():
    c, stub, _ = client([reply()])
    result = call(c)
    assert result.data == {"ok": True} and result.model == "openai/gpt-oss-120b"
    sent = stub.calls[0]
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["reasoning_effort"] == "low" and sent["max_completion_tokens"] == 100


def test_waits_out_a_rate_limit():
    c, stub, waits = client([rate_limited("2"), reply()])
    assert call(c).data == {"ok": True}
    assert waits == [2.0] and len(stub.calls) == 2


def test_uses_the_fallback_model_after_a_server_error():
    c, stub, _ = client(
        [server_error(), reply(model="openai/gpt-oss-20b")], fallback_model="openai/gpt-oss-20b"
    )
    result = call(c)
    assert [k["model"] for k in stub.calls] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert result.model == "openai/gpt-oss-20b"


def test_gives_up_when_rate_limits_persist():
    c, _, _ = client([rate_limited()] * 4, rate_limit_retries=3)
    with pytest.raises(LLMUnavailable):
        call(c)


def test_bad_request_is_not_retried():
    error = groq.BadRequestError("bad", response=httpx.Response(400, request=REQUEST), body=None)
    c, stub, _ = client([error])
    with pytest.raises(LLMError, match="400"):
        call(c)
    assert len(stub.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (reply(finish="length"), "token limit"),
        (reply(content=""), "empty"),
        (reply(content="not json"), "not valid JSON"),
    ],
)
def test_unusable_replies(outcome, message):
    c, _, _ = client([outcome])
    with pytest.raises(LLMBadOutput, match=message):
        call(c)
