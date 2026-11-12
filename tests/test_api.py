"""Phase 5 API: contract, error mapping, no input echo, rate limit, static page (EV-5-01 to 03)."""

import logging
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fake_index import CHUNKS, NOW, bow_embedder
from fastapi.testclient import TestClient
from test_pipeline import GOOD, FakeCompleter, _docs

from app.llm.client import AnswerClient
from app.main import create_app
from app.models import ChatResponse, Citation
from app.pipeline import Pipeline, ServiceUnavailable
from app.responses.composer import Composer
from app.responses.documents import DocInfo, StaticDocuments
from app.retrieval.index_loader import load_index
from app.settings import Settings, get_settings
from ingestion.build_index import build_index

PAN = "ABCPE1234F"
OK = ChatResponse(
    type="answer",
    text="The exit load is 1%.",
    citation=Citation(url="https://www.hdfcfund.com/x", title="Page"),
    last_updated=date(2026, 11, 11),
    request_id="r1",
)


class FakePipeline:
    """Stands in for app.pipeline.Pipeline: returns OK, raises, or sleeps."""

    def __init__(self, outcome=OK, delay=0.0, docs=None):
        self.outcome, self.delay, self.calls = outcome, delay, []
        self.index, self.llm = None, None
        self.composer = SimpleNamespace(documents=docs or StaticDocuments({}))

    def answer(self, message):
        self.calls.append(message)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def client_for(clean_env):
    def build(pipeline=None, **env):
        for key, value in env.items():
            clean_env.setenv(key.upper(), str(value))
        get_settings.cache_clear()
        pipeline = pipeline or FakePipeline()
        client = TestClient(create_app(pipeline=pipeline), raise_server_exceptions=False)
        client.__enter__()
        return client, pipeline

    yield build
    get_settings.cache_clear()


# --- /api/chat contract ------------------------------------------------------------------


def test_chat_returns_the_chat_response(client_for):
    client, pipeline = client_for()
    response = client.post("/api/chat", json={"message": "  exit load of HDFC Mid Cap?  "})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"type", "text", "citation", "last_updated", "request_id"}
    assert body["last_updated"] == "2026-11-11"
    assert pipeline.calls == ["exit load of HDFC Mid Cap?"]  # trimmed (EC-IN-02)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"message": ""}, "message"),  # EC-IN-01
        ({"message": "  \n\t "}, "message"),  # EC-IN-02
        ({"message": "x" * 501}, "message"),  # EC-IN-03
        ({}, "message"),  # EC-API-01
        ({"message": 42}, "message"),
        ({"message": "hi", "extra": "field"}, "extra"),
    ],
)
def test_invalid_requests_are_422_with_field_names_only(client_for, payload, field):
    client, pipeline = client_for()
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "fields": [field]}
    assert pipeline.calls == []


def test_malformed_json_and_wrong_content_type_are_422(client_for):
    """EC-API-02."""
    client, _ = client_for()
    bad_json = client.post(
        "/api/chat", content=b'{"message": "hi"', headers={"Content-Type": "application/json"}
    )
    plain = client.post("/api/chat", content=b"message=hi", headers={"Content-Type": "text/plain"})
    assert bad_json.status_code == 422 and plain.status_code == 422


def test_invalid_request_never_echoes_pii(client_for, caplog):
    """EV-5-02, EC-PII-15: a 600-character message with a PAN."""
    client, _ = client_for()
    with caplog.at_level(logging.DEBUG):
        response = client.post("/api/chat", json={"message": f"my pan {PAN} " + "x" * 600})
        extra = client.post("/api/chat", json={"message": "hi", "pan": PAN})
    for r in (response, extra):
        assert r.status_code == 422 and PAN not in r.text
    assert all(PAN not in rec.getMessage() for rec in caplog.records)


def test_500_unicode_characters_are_accepted(client_for):
    """EC-IN-04: counted in characters, not bytes."""
    client, _ = client_for()
    assert client.post("/api/chat", json={"message": "म" * 500}).status_code == 200
    assert client.post("/api/chat", json={"message": "🤔" * 500}).status_code == 200
    assert client.post("/api/chat", json={"message": "🤔" * 501}).status_code == 422


def test_get_chat_is_405(client_for):
    """EC-API-03."""
    client, _ = client_for()
    assert client.get("/api/chat").status_code == 405


# --- rate limit, unavailability, errors --------------------------------------------------


def test_rate_limit_gives_429_with_retry_after(client_for):
    """EV-5-03, EC-API-04."""
    client, _ = client_for(rate_limit="2/minute")
    codes = [client.post("/api/chat", json={"message": "hi"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = client.post("/api/chat", json={"message": "hi"})
    assert limited.json() == {"error": "rate_limited"}
    assert int(limited.headers["Retry-After"]) > 0


def test_service_unavailable_is_503(client_for):
    client, _ = client_for(FakePipeline(ServiceUnavailable("llm_unavailable")))
    response = client.post("/api/chat", json={"message": "exit load"})
    assert response.status_code == 503
    assert response.json() == {"error": "service_unavailable", "reason": "llm_unavailable"}


def test_slow_request_times_out_with_503(client_for):
    """EC-API-08."""
    client, _ = client_for(FakePipeline(delay=1.0), request_timeout_s=0.2)
    response = client.post("/api/chat", json={"message": "exit load"})
    assert response.status_code == 503 and response.json()["reason"] == "timeout"


def test_unexpected_error_is_500_without_details(client_for, caplog):
    client, _ = client_for(FakePipeline(RuntimeError(f"boom {PAN}")))
    with caplog.at_level(logging.ERROR):
        response = client.post("/api/chat", json={"message": "exit load"})
    assert response.status_code == 500 and response.json() == {"error": "internal_error"}
    assert all(PAN not in rec.getMessage() for rec in caplog.records)


# --- /api/meta, /healthz -----------------------------------------------------------------


def test_meta_has_the_page_content(client_for):
    client, _ = client_for()
    meta = client.get("/api/meta").json()
    assert meta["disclaimer"] == "Facts-only. No investment advice."
    assert len(meta["examples"]) == 3 and meta["welcome"]
    assert meta["max_message_chars"] == 500
    assert [s["id"] for s in meta["schemes"]][:2] == ["hdfc-large-cap", "hdfc-mid-cap"]
    assert len(meta["schemes"]) == 7


def test_healthz_reports_llm_and_stale_sources(client_for):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    docs = StaticDocuments(
        {
            "fresh": DocInfo("fresh", "u", "t", datetime.now(UTC), None),
            "old": DocInfo("old", "u", "t", old, None),
        }
    )
    client, _ = client_for(FakePipeline(docs=docs))
    assert client.get("/healthz").json() == {
        "index_loaded": False,
        "llm_configured": False,
        "stale_sources": ["old"],
    }


# --- static page and headers -------------------------------------------------------------


def test_json_responses_carry_security_headers_and_no_page(client_for):
    """The UI is Streamlit; this API serves JSON only."""
    client, _ = client_for()
    response = client.post("/api/chat", json={"message": "hi"})
    assert (
        response.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    )
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/").status_code == 404


def test_other_origins_get_no_cors_permission(client_for):
    """EC-API-06."""
    client, _ = client_for()
    preflight = client.options(
        "/api/chat",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers
    post = client.post(
        "/api/chat", json={"message": "hi"}, headers={"Origin": "https://evil.example"}
    )
    assert "access-control-allow-origin" not in post.headers


def test_no_api_docs_are_exposed(client_for):
    client, _ = client_for()
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


# --- end to end with the real pipeline and a fake LLM ------------------------------------


@pytest.fixture
def real_client(tmp_path, client_for):
    build_index(tmp_path, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    fake = FakeCompleter(GOOD)
    pipeline = Pipeline(
        settings=Settings(_env_file=None, min_similarity=0.3),
        composer=Composer(_docs()),
        index=load_index(tmp_path, bow_embedder),
        llm=AnswerClient(fake, reasoning_effort="low", max_tokens=2048),
    )
    client, _ = client_for(pipeline)
    return client, fake


def test_end_to_end_answer_has_one_citation_and_india_date(real_client):
    client, _ = real_client
    body = client.post(
        "/api/chat", json={"message": "What is the exit load of HDFC Mid Cap?"}
    ).json()
    assert body["type"] == "answer" and body["citation"]["url"].startswith("https://")
    assert body["last_updated"] == "2026-11-11"
    health = client.get("/healthz").json()
    assert (
        health["index_loaded"] and health["llm_configured"] and health["chunk_count"] == len(CHUNKS)
    )


def test_end_to_end_pii_is_blocked_even_without_the_client_warning(real_client):
    """EC-PII-14, EC-UI-07: a direct API call with a PAN."""
    client, fake = real_client
    body = client.post("/api/chat", json={"message": f"PAN {PAN} exit load HDFC Mid Cap"}).json()
    assert body["type"] == "pii_blocked" and PAN not in body["text"]
    assert fake.requests == []


def test_end_to_end_html_in_the_question_is_plain_text(real_client):
    """EC-IN-06."""
    client, _ = real_client
    body = client.post(
        "/api/chat", json={"message": "<script>alert(1)</script> exit load of HDFC Mid Cap"}
    ).json()
    assert body["type"] == "answer" and "<script>" not in body["text"]
