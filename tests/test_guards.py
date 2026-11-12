"""Phase 4A guards: PII, intent, field detection, scheme resolver."""

import pytest

from app.guards.intent import classify_intent
from app.guards.pii import contains_pii, detect_pii, verhoeff_check_digit, verhoeff_valid
from app.retrieval.fields import detect_fields, is_general_topic
from app.retrieval.scheme_resolver import resolve
from eval.run_guards import evaluate

# --- PII ---------------------------------------------------------------------------------


def test_verhoeff_round_trip():
    base = "23456789012"
    number = base + verhoeff_check_digit(base)
    assert verhoeff_valid(number)
    assert not verhoeff_valid(base + str((int(number[-1]) + 1) % 10))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("my pan is ABCPE1234F", "pan"),
        ("aadhaar 2345 6789 0124", "aadhaar"),
        ("call +91 98765 43210", "phone"),
        ("mail me test.user@example.com", "email"),
        ("my OTP is 482913", "otp"),
    ],
)
def test_plan_true_positives(text, kind):
    assert kind in detect_pii(text)


@pytest.mark.parametrize(
    "text", ["₹500 SIP", "1 year lock-in", "expense ratio 0.75%", "launched in 2013"]
)
def test_plan_false_positives(text):
    assert not contains_pii(text)


@pytest.mark.parametrize("text", ["₹1000000000", "100000000 rupees"])
def test_amounts_are_not_account_numbers(text):
    """EC-PII-13."""
    assert not contains_pii(text)


def test_checksum_failing_12_digits_still_blocked_as_account():
    """EC-PII-04."""
    assert detect_pii("234567890125") == ["account"]


def test_only_kinds_are_returned_never_the_text():
    kinds = detect_pii("PAN ABCPE1234F and phone 9876543210")
    assert kinds == ["pan", "phone"]
    assert all("ABCPE" not in k and "9876" not in k for k in kinds)


def test_fullwidth_characters_are_normalized():
    """EC-IN-08."""
    assert "pan" in detect_pii("ＡＢＣＰＥ１２３４Ｆ")


# --- intent ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Should I invest in this fund?", "Which fund is better?"])
def test_problem_statement_examples_are_advisory(text):
    assert classify_intent(text).intent == "advisory"


@pytest.mark.parametrize(
    "text",
    [
        "Hi! What's the exit load of HDFC Mid Cap?",
        "Should I pay exit load if I redeem after 6 months?",
    ],
)
def test_plan_factual_cases(text):
    """EC-INT-16, EC-INT-02."""
    assert classify_intent(text).intent == "factual"


def test_priority_advisory_over_performance():
    assert classify_intent("Which fund has better returns?").intent == "advisory"


def test_greeting_only_when_whole_message():
    assert classify_intent("thanks!").intent == "greeting"
    assert classify_intent("thanks, what is the TER of HDFC Mid Cap?").intent == "factual"


def test_rule_name_is_reported_not_text():
    result = classify_intent("What is the CAGR of HDFC Small Cap?")
    assert result.intent == "performance" and result.rule == "metrics"


# --- fields ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fields"),
    [
        ("What is the exit load of HDFC Mid Cap?", ["exit_load"]),
        ("minimum SIP for HDFC Small Cap", ["min_sip"]),
        ("What is the riskometer?", ["riskometer"]),
        ("risk level of the gold fund", ["riskometer"]),
        ("lock-in period of ELSS", ["lock_in"]),
        ("expense ratio and exit load", ["expense_ratio", "exit_load"]),
        ("minimum lumpsum investment", ["min_lumpsum"]),
        ("which index is the benchmark", ["benchmark"]),
    ],
)
def test_field_detection(text, fields):
    assert detect_fields(text) == fields


def test_general_topics():
    assert is_general_topic("How do I download my capital gains statement?")
    assert not is_general_topic("What is the exit load?")


# --- resolver ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "scheme"),
    [
        ("HDFC Small Cap exit load", "hdfc-small-cap"),
        ("hdfc smallcap min sip", "hdfc-small-cap"),
        ("HDFC mid-cap riskometer", "hdfc-mid-cap"),
        ("hdfc smal cap expense ratio", "hdfc-small-cap"),
    ],
)
def test_resolver_aliases_and_typos(text, scheme):
    result = resolve(text)
    assert result.kind == "one" and result.scheme_id == scheme


def test_resolver_two_schemes_and_no_scheme():
    assert resolve("exit load of small cap and mid cap").kind == "several"
    assert resolve("What is the exit load?").kind == "needs_scheme"


@pytest.mark.parametrize(
    "text",
    [
        "SBI Small Cap exit load",
        "HDFC Flexi Cap TER",
        "HDFC Mid Cap Regular plan TER",
        "HDFC Gold ETF TER",
    ],
)
def test_not_covered_never_resolves_to_a_corpus_scheme(text):
    """EC-SCH-05/06/07/09."""
    result = resolve(text)
    assert result.kind == "not_covered" and result.scheme_ids == []


def test_not_covered_points_to_the_closest_corpus_scheme():
    assert resolve("HDFC Gold ETF expense ratio").closest_scheme_id == "hdfc-gold-etf-fof"
    assert resolve("HDFC Mid Cap Regular plan TER").closest_scheme_id == "hdfc-mid-cap"


# --- the labeled guard set (EV-4A gates) -------------------------------------------------


def test_guard_set_meets_the_eval_gates():
    scores = evaluate()
    assert scores.gate_failures() == [], scores.failures
