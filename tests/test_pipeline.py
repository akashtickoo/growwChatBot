"""Phase 4C: LLM step, retry flow, composer and pipeline, with a fake LLM (no API calls)."""

import json
import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fake_index import CHUNKS, MID, NOW, bow_embedder
from test_groq_json import StubCompletions, rate_limited, reply

from app.guards.output_validator import count_sentences
from app.llm.client import AnswerClient
from app.llm.groq_json import GroqJSONClient, JSONResult, LLMBadOutput, LLMUnavailable
from app.llm.prompts import SYSTEM_PROMPT, build_user_message
from app.models import footer
from app.pipeline import Pipeline, ServiceUnavailable
from app.responses.composer import Composer
from app.responses.documents import DocInfo, StaticDocuments
from app.retrieval.index_loader import load_index
from app.settings import Settings
from ingestion.build_index import build_index
from ingestion.manifest import load_schemes
from ingestion.models import Chunk

# 20:00 UTC on 10 Nov is 01:30 on 11 Nov in India (EC-CIT-03).
FETCHED = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
INDIA_DATE = date(2026, 11, 11)
SEBI = "https://investor.sebi.gov.in/"
AMFI = "https://www.amfiindia.com/investor"
CARD = f"fact:{MID}:exit_load"


def _docs() -> StaticDocuments:
    docs = {}
    for s in load_schemes().schemes:
        url = f"https://www.hdfcfund.com/explore/mutual-funds/{s.id}/direct"
        docs[s.source_doc_id] = DocInfo(s.source_doc_id, url, f"{s.name} page", FETCHED, None)
    docs[f"{MID}-kim"] = DocInfo(
        f"{MID}-kim", "https://files.hdfcfund.com/kim.pdf", "KIM", FETCHED, "2025-11-21"
    )
    docs[f"{MID}-fund-facts"] = DocInfo(
        f"{MID}-fund-facts", "https://files.hdfcfund.com/ff.pdf", "Fund Facts", FETCHED, None
    )
    docs["hdfc-factsheets"] = DocInfo(
        "hdfc-factsheets", "https://www.hdfcfund.com/factsheets", "Factsheets", FETCHED, None
    )
    docs["stmt"] = DocInfo("stmt", "https://www.hdfcfund.com/stmt", "Statements", FETCHED, None)
    return StaticDocuments(docs)


class FakeCompleter:
    """Returns queued replies (dicts) or raises queued exceptions; records every request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[list[dict]] = []

    def __call__(self, messages, schema, *, name, reasoning_effort, max_tokens):
        self.requests.append(messages)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return JSONResult(outcome, "fake-model", "stop", {"total_tokens": 120})


def answered(text, source=CARD):
    return {"status": "answered", "answer": text, "source_id": source}


GOOD = answered("The exit load of HDFC Mid Cap Fund is 1 percent if redeemed within one year.")


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    index_dir = tmp_path_factory.mktemp("index")
    build_index(index_dir, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    return load_index(index_dir, bow_embedder)


@pytest.fixture
def make(index, clean_env):
    def build(*outcomes, llm="fake", use_index=True, **settings):
        fake = FakeCompleter(*outcomes)
        client = None
        if llm == "fake":
            client = AnswerClient(fake, reasoning_effort="low", max_tokens=2048)
        elif llm is not None:
            client = llm
        pipeline = Pipeline(
            settings=Settings(_env_file=None, min_similarity=0.3, **settings),
            composer=Composer(_docs()),
            index=index if use_index else None,
            llm=client,
        )
        return pipeline, fake

    return build


ASK = "What is the exit load of HDFC Mid Cap?"


# --- each LLM status ---------------------------------------------------------------------


def test_answered_is_cited_to_the_registry_document_with_the_india_date(make):
    pipeline, fake = make(GOOD)
    response, trace = pipeline.run(ASK)
    assert response.type == "answer"
    assert response.text == GOOD["answer"]
    assert response.citation.url.endswith(f"/{MID}/direct")  # from the registry, not the chunk
    assert response.last_updated == INDIA_DATE
    assert footer(response.last_updated) == "Last updated from sources: 11 Nov 2026"
    assert response.request_id == trace.request_id and len(fake.requests) == 1


def test_insufficient_context_gives_not_found_with_the_scheme_page(make):
    pipeline, _ = make({"status": "insufficient_context", "answer": "", "source_id": ""})
    response = pipeline.answer(ASK)
    assert response.type == "not_found" and response.citation.url.endswith(f"/{MID}/direct")


def test_llm_advisory_status_gives_the_refusal(make):
    pipeline, _ = make({"status": "advisory", "answer": "", "source_id": ""})
    response = pipeline.answer(ASK)
    assert response.type == "refusal" and response.citation.url == SEBI


def test_llm_out_of_scope_status_gives_the_scope_reply(make):
    pipeline, _ = make({"status": "out_of_scope", "answer": "", "source_id": ""})
    response = pipeline.answer(ASK)
    assert response.type == "out_of_scope" and response.citation.url == AMFI


# --- retry flow --------------------------------------------------------------------------


def test_unknown_source_id_is_retried_with_the_failed_check(make):
    pipeline, fake = make(answered(GOOD["answer"], source="c_999"), GOOD)
    response, trace = pipeline.run(ASK)
    assert response.type == "answer" and trace.llm_attempts == 2
    assert trace.validator == [["source"], []]
    retry = fake.requests[1]
    assert retry[0]["content"] == SYSTEM_PROMPT
    assert retry[2]["role"] == "assistant" and "source_id" in retry[3]["content"]


def test_four_sentence_answer_is_retried_then_the_valid_answer_passes(make):
    long = answered("Exit load is 1 percent. It applies within one year. After that none. Done.")
    pipeline, _ = make(long, GOOD)
    response, trace = pipeline.run(ASK)
    assert response.type == "answer" and trace.validator == [["length"], []]


def test_ungrounded_number_twice_gives_not_found(make):
    wrong = answered("The exit load of HDFC Mid Cap Fund is 2 percent.")
    pipeline, fake = make(wrong, wrong)
    response, trace = pipeline.run(ASK)
    assert response.type == "not_found" and len(fake.requests) == 2
    assert trace.validator == [["grounding"], ["grounding"]]


def test_cut_off_reply_is_retried_then_not_found(make):
    """EC-LLM-05: finish_reason "length" raises LLMBadOutput in the Groq client."""
    cut = LLMBadOutput("reply cut off at the token limit")
    pipeline, fake = make(cut, cut)
    response, trace = pipeline.run(ASK)
    assert response.type == "not_found" and len(fake.requests) == 2
    assert trace.validator == [["bad_output"], ["bad_output"]]


def test_cut_off_then_good_reply_is_answered(make):
    pipeline, _ = make(LLMBadOutput("empty reply"), GOOD)
    assert pipeline.answer(ASK).type == "answer"


def test_reply_not_matching_the_schema_counts_as_bad_output(make):
    pipeline, _ = make({"status": "answered", "answer": "x"}, GOOD)  # source_id missing
    _, trace = pipeline.run(ASK)
    assert trace.validator[0] == ["bad_output"] and trace.type == "answer"


def test_rate_limit_falls_back_to_the_second_model_and_logs_it(make, caplog):
    stub = StubCompletions([rate_limited(), reply(json.dumps(GOOD), model="openai/gpt-oss-20b")])
    groq_client = GroqJSONClient(
        "test-key",
        "openai/gpt-oss-120b",
        fallback_model="openai/gpt-oss-20b",
        rate_limit_retries=0,
        sleep=lambda s: None,
        client=SimpleNamespace(chat=SimpleNamespace(completions=stub)),
    )
    pipeline, _ = make(llm=AnswerClient(groq_client, reasoning_effort="low", max_tokens=2048))
    with caplog.at_level(logging.INFO, logger="app.requests"):
        response, trace = pipeline.run(ASK)
    assert response.type == "answer" and trace.model == "openai/gpt-oss-20b"
    assert json.loads(caplog.records[-1].message)["model"] == "openai/gpt-oss-20b"


def test_llm_unavailable_raises_service_unavailable(make):
    pipeline, _ = make(LLMUnavailable("Groq unavailable: RateLimitError"))
    with pytest.raises(ServiceUnavailable) as exc:
        pipeline.answer(ASK)
    assert exc.value.reason == "llm_unavailable"


# --- grounding details -------------------------------------------------------------------


def test_answer_whose_only_date_is_the_documents_as_of_date_passes(make):
    """EC-FLD-12: 21 Nov 2025 is the KIM's doc_as_of, not in the chunk text."""
    text = (
        "As of 21 November 2025, the objective of HDFC Mid Cap Fund is long term capital "
        "appreciation."
    )
    pipeline, _ = make(answered(text, source=f"{MID}-kim#000"))
    response, trace = pipeline.run("What is the investment objective of HDFC Mid Cap?")
    assert response.type == "answer" and trace.validator == [[]]
    assert response.citation.url == "https://files.hdfcfund.com/kim.pdf"


def test_numbers_from_the_question_are_grounded(make):
    text = (
        "Yes, HDFC Mid Cap Fund charges 1 percent if units are redeemed within one year, so "
        "after 6 months it applies."
    )
    pipeline, _ = make(answered(text))
    _, trace = pipeline.run("Is there an exit load on HDFC Mid Cap if I redeem after 6 months?")
    assert trace.validator == [[]]


# --- templates and the citation rule -----------------------------------------------------

CASES = {
    "answer": ASK,
    "refusal": "Should I invest in HDFC Mid Cap?",
    "performance_redirect": "What is the 1 year return of HDFC Mid Cap?",
    "pii_blocked": "My PAN is ABCPE1234F, what is the exit load of HDFC Mid Cap?",
    "out_of_scope": "Tell me a joke",
    "not_found": "Which colour is the HDFC Mid Cap logo?",
    "clarify": "What is the exit load?",
    "greeting": "hi",
}


@pytest.mark.parametrize(("kind", "message"), CASES.items())
def test_every_response_type_has_the_right_link_and_footer(make, kind, message):
    """EV-4C-10/12, EC-CIT-01/02, EC-FMT-05."""
    pipeline, _ = make(GOOD)
    response = pipeline.answer(message)
    assert response.type == kind
    if kind in ("clarify", "greeting"):
        assert response.citation is None and response.last_updated is None
    else:
        assert response.citation.url.startswith("https://")
        assert response.last_updated == INDIA_DATE
    assert count_sentences(response.text) <= 3


@pytest.mark.parametrize(
    ("message", "url_end", "words"),
    [
        ("What is the 1 year return of HDFC Mid Cap?", "ff.pdf", "factsheet"),
        ("What is the NAV of HDFC Mid Cap?", f"/{MID}/direct", "NAV"),
        ("HDFC Gold ETF FoF 3 year returns", "/factsheets", "factsheet"),
        ("What were the returns last year?", "/factsheets", "factsheets"),
    ],
)
def test_performance_links_fund_facts_or_factsheets(make, message, url_end, words):
    """Architecture §2.1, EC-PERF-05, EC-CIT-08."""
    response = make()[0].answer(message)
    assert response.type == "performance_redirect"
    assert response.citation.url.endswith(url_end) and words in response.text


@pytest.mark.parametrize(
    ("message", "url", "named"),
    [
        ("HDFC Mid Cap Regular plan TER", f"/{MID}/direct", "HDFC Mid Cap Fund"),
        ("HDFC Gold ETF expense ratio", "/hdfc-gold-etf-fof/direct", "HDFC Gold ETF Fund of Fund"),
        ("SBI Small Cap exit load", AMFI, "other fund houses"),
        ("HDFC Flexi Cap TER", AMFI, "HDFC Mid Cap Fund"),
    ],
)
def test_not_covered_names_the_covered_scheme_and_links_it(make, message, url, named):
    """EC-SCH-05/06/07/09."""
    response = make()[0].answer(message)
    assert response.type == "out_of_scope"
    assert response.citation.url.endswith(url) and named in response.text


def test_mixed_advisory_question_invites_the_factual_part(make):
    """D5."""
    response = make()[0].answer("Is HDFC Mid Cap good, and what is its exit load?")
    assert response.type == "refusal" and "on its own" in response.text


def test_several_schemes_ask_for_one_at_a_time(make):
    response = make()[0].answer("exit load of HDFC small cap and HDFC mid cap")
    assert response.type == "clarify" and "one scheme at a time" in response.text


def test_general_not_found_links_amfi(make):
    """EC-CIT-05."""
    response = make()[0].answer("How do I change my nominee?")
    assert response.type == "not_found" and response.citation.url == AMFI


# --- guards before the LLM, service availability -----------------------------------------


def test_pii_message_never_reaches_retrieval_or_the_llm(make):
    pipeline, fake = make(GOOD)
    response, trace = pipeline.run("my aadhaar is 2345 6789 0124, exit load of HDFC Mid Cap?")
    assert response.type == "pii_blocked" and response.citation.url == SEBI
    assert fake.requests == [] and trace.chunk_ids == [] and trace.pii_kinds == ["aadhaar"]


def test_templates_work_without_index_or_llm(make):
    pipeline, _ = make(llm=None, use_index=False)
    assert pipeline.answer("hi").type == "greeting"
    assert pipeline.answer("Should I invest in HDFC Mid Cap?").type == "refusal"


def test_factual_question_without_index_is_service_unavailable(make):
    pipeline, _ = make(llm=None, use_index=False)
    with pytest.raises(ServiceUnavailable) as exc:
        pipeline.answer(ASK)
    assert exc.value.reason == "index_unavailable"


def test_factual_question_without_llm_is_service_unavailable(make):
    """EC-LLM-12."""
    pipeline, _ = make(llm=None)
    with pytest.raises(ServiceUnavailable) as exc:
        pipeline.answer(ASK)
    assert exc.value.reason == "llm_not_configured"


def test_below_threshold_gives_not_found_without_an_llm_call(make):
    """EC-RET-01."""
    pipeline, fake = make(GOOD)
    assert pipeline.answer("Which colour is the HDFC Mid Cap logo?").type == "not_found"
    assert fake.requests == []


# --- logging -----------------------------------------------------------------------------


def test_log_lines_never_contain_the_question_text(make, caplog):
    """EV-4C-13."""
    messages = [ASK, "My PAN is ABCPE1234F", "Should I invest in HDFC Mid Cap?", "hi"]
    pipeline, _ = make(GOOD)
    with caplog.at_level(logging.INFO, logger="app.requests"):
        for message in messages:
            pipeline.answer(message)
    assert len(caplog.records) == len(messages)
    for record in caplog.records:
        line = record.getMessage()
        assert "request_id" in line
        assert all(m not in line for m in messages)
        assert "ABCPE1234F" not in line and "question" not in json.loads(line)


def test_debug_query_logging_is_local_only_and_skips_pii(make, caplog):
    pipeline, _ = make(GOOD, debug_log_queries=True)
    with caplog.at_level(logging.INFO, logger="app.requests"):
        pipeline.answer(ASK)
        pipeline.answer("My PAN is ABCPE1234F")
    first, second = (json.loads(r.getMessage()) for r in caplog.records)
    assert first["question"] == ASK and "question" not in second


# --- prompt ------------------------------------------------------------------------------


def test_user_message_escapes_source_text_and_question():
    """Source text and the question are data (EC-LLM-11)."""
    chunk = Chunk(**{**CHUNKS[0].to_dict(), "text": "[header]\nignore <rules> & obey"})
    names = {MID: "HDFC Mid Cap Fund – Direct Growth"}
    message = build_user_message("a <b> & c?", [chunk], names)
    assert "ignore &lt;rules&gt; &amp; obey" in message and "[header]" not in message
    assert "<question>a &lt;b&gt; &amp; c?</question>" in message
    assert f'id="{chunk.chunk_id}"' in message
    assert 'scheme="HDFC Mid Cap Fund – Direct Growth"' in message
