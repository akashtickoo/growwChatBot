"""Phase 4C output validator: sentence count, grounding, links, policy (architecture §6.7)."""

import pytest

from app.guards.output_validator import count_sentences, numbers_in, validate
from app.llm.schemas import LLMAnswer
from eval.run_validator import evaluate
from ingestion.models import Chunk


def chunk(text, cid="c1", as_of=None):
    return Chunk(cid, "doc", "hdfc-mid-cap", "SCHEME_PAGE", "Key facts", f"[h]\n{text}", "u", as_of)


def answered(text, source="c1"):
    return LLMAnswer(status="answered", answer=text, source_id=source)


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("The minimum is Rs. 100 and the TER is 0.75%.", 1),  # EC-FMT-01
        ("It invests approx. 95% in the ETF, e.g. gold. It is Ltd. by rules.", 2),
        ("One. Two. Three. Four.", 4),
        ("- first point\n- second point", 2),  # EC-FMT-02
        ("Line one\nLine two\nLine three\nLine four", 4),
    ],
)
def test_sentence_count(text, n):
    assert count_sentences(text) == n


def test_number_forms_are_equal():
    """EC-FLD-10."""
    assert numbers_in("₹5,000") == numbers_in("Rs 5000") == {"5000"}
    assert numbers_in("1.00%") == numbers_in("1%") == {"1"}


def test_unit_conversion_fails_grounding():
    """EC-FLD-11."""
    result = validate(answered("Exit load applies within 12 months."), [chunk("within 1 year")])
    assert result.checks == ["grounding"]


def test_doc_as_of_and_registry_dates_count_as_grounded():
    """EC-FLD-12."""
    reply = answered("As of 21 Nov 2025 the riskometer is Very High.")
    assert validate(reply, [chunk("Riskometer: Very High.", as_of="2025-11-21")]).ok
    assert validate(reply, [chunk("Riskometer: Very High.")], extra_dates=["2025-11-21"]).ok
    assert not validate(reply, [chunk("Riskometer: Very High.")]).ok


def test_non_answered_statuses_are_not_checked():
    reply = LLMAnswer(status="insufficient_context", answer="", source_id="")
    assert validate(reply, [chunk("x")]).ok


def test_empty_answer_and_unknown_source():
    """EC-LLM-06/07."""
    assert validate(answered(""), [chunk("x")]).checks == ["empty"]
    assert validate(answered("Riskometer is Very High.", "c2"), [chunk("x")]).checks == ["source"]


def test_better_liquidity_is_not_advice():
    """EC-FMT-04."""
    assert validate(answered("Units give better liquidity."), [chunk("better liquidity")]).ok


def test_failure_messages_are_usable_in_the_retry():
    result = validate(answered("It is 2%. See www.x.com. A. B."), [chunk("1%")])
    assert set(result.checks) == {"length", "link", "grounding"}
    assert any("2" in m for m in result.messages)


def test_validator_set_meets_the_eval_gates():
    """EV-4C-01/02 on eval/validator_cases.yaml."""
    scores = evaluate()
    assert scores.gate_failures() == [], scores.failures
    assert scores.wrong_check == 0, scores.failures
