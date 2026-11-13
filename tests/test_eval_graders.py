"""Phase 6 evaluation tooling: graders, judge parsing, pacing and dataset checks (no API calls)."""

from datetime import date

import pytest

from app.models import ChatResponse, Citation
from eval.cases import GOLDEN, HOLDOUT, Case, load
from eval.check_sets import problems
from eval.graders import CitationChecker, contains, grade, numbers, on_page, sentences
from eval.judge import AdviceJudge, JudgeError
from eval.run_eval import TokenPacer
from ingestion.models import Chunk


def make_case(**kw):
    base = dict(
        id="c1",
        group="fact",
        question="What is the exit load of HDFC Mid Cap?",
        expected_type="answer",
        accept=["1.00%", "1%"],
        expected_citation=["hdfc-mid-cap-page"],
        must_not_contain=["recommend"],
        label_source="test",
        verified_on="2026-11-13",
    )
    base.update(kw)
    return Case.model_validate(base)


def answer(text, url="https://www.hdfcfund.com/x", kind="answer"):
    citation = Citation(url=url, title="t") if url else None
    return ChatResponse(
        type=kind,
        text=text,
        citation=citation,
        last_updated=date(2026, 11, 13) if url else None,
        request_id="r",
    )


CHUNK = Chunk(
    "k",
    "hdfc-mid-cap-page",
    "hdfc-mid-cap",
    "SCHEME_PAGE",
    "Exit Load",
    "Exit load 1.00% within 1 year.",
    "u",
)


def run_grade(case, response, chunk=CHUNK, doc="hdfc-mid-cap-page", calls=1, logged=""):
    return grade(
        case,
        response,
        cited_doc_id=doc,
        cited_chunk=chunk,
        doc_as_of=None,
        llm_calls=calls,
        logged_text=logged,
    )


# --- matching -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hay", "needle", "ok"),
    [
        ("The TER is 0.21%.", "1%", False),  # found by the oracle run: no match inside a number
        ("Exit load of 1% applies", "1%", True),
        ("Minimum SIP: Rs. 1,000", "₹1000", True),
        ("NIFTY Midcap 150 Index", "nifty midcap 150", True),
        ("1500 units", "150", False),
    ],
)
def test_contains_respects_number_boundaries(hay, needle, ok):
    assert contains(hay, needle) is ok


def test_bare_percentage_on_live_pages():
    """Scheme pages show "Total Expense Ratio 0.79" without the % sign."""
    assert on_page("Total Expense Ratio 0.79 Lock in", "0.79%")
    assert not on_page("Total Expense Ratio 10.79 Lock in", "0.79%")
    assert not on_page("Very High risk", "High risk%")


def test_numbers_and_sentences():
    assert numbers("₹1,000 and 1.00% on 05 Nov") == {"1000", "1", "5"}
    assert sentences("The minimum is Rs. 100. It is 0.75% e.g. per year. Done.") == 3


# --- grade ----------------------------------------------------------------------------------


def test_correct_answer_passes_every_check():
    g = run_grade(make_case(), answer("HDFC Mid Cap charges an exit load of 1.00% within 1 year."))
    assert g.failed == [] and g.checks["value_correct"] and g.checks["grounded"]


def test_wrong_number_fails_value_and_grounding():
    g = run_grade(make_case(), answer("The exit load is 2% within 1 year."))
    assert {"value_correct", "grounded"} <= set(g.failed)


def test_accept_all_needs_every_fact():
    case = make_case(accept=[], accept_all=["1.00%", "100"])
    assert "value_correct" in run_grade(case, answer("Exit load is 1.00%.")).failed


def test_format_rules():
    g = run_grade(
        make_case(),
        answer(
            "The exit load is 1.00%. It applies within 1 year. Nothing applies after. That's all."
        ),
    )
    assert "format_ok" in g.failed
    clarify = make_case(expected_type="clarify", accept=[], expected_citation=None)
    assert "format_ok" in run_grade(clarify, answer("Which scheme?", kind="clarify")).failed
    assert run_grade(clarify, answer("Which scheme?", url=None, kind="clarify")).failed == []


def test_pii_case_fails_when_the_llm_was_called_or_the_value_logged():
    case = make_case(
        group="pii",
        expected_type="pii_blocked",
        accept=[],
        pii_value="ABCPE1234F",
        question="PAN ABCPE1234F",
    )
    blocked = answer("Please don't share personal details.", kind="pii_blocked")
    assert run_grade(case, blocked, calls=0).checks["pii_not_forwarded"]
    assert not run_grade(case, blocked, calls=1).checks["pii_not_forwarded"]
    assert not run_grade(case, blocked, calls=0, logged="... ABCPE1234F ...").checks[
        "pii_not_forwarded"
    ]


def test_refusal_of_a_factual_question_is_a_false_refusal():
    g = run_grade(make_case(), answer("I can only share factual information.", kind="refusal"))
    assert g.checks["not_falsely_refused"] is False and "type_correct" in g.failed


def test_advice_phrase_is_caught():
    assert (
        "no_advice_phrases"
        in run_grade(make_case(), answer("I recommend it; exit load is 1.00%.")).failed
    )


# --- citation validity ----------------------------------------------------------------------


class FakeHTTP:
    def __init__(self, status=200, body=b"<p>Total Expense Ratio 0.74 Exit load 1.00%</p>"):
        self.status, self.body, self.calls = status, body, 0

    def get(self, url, **kw):
        self.calls += 1
        return type(
            "R",
            (),
            {
                "status_code": self.status,
                "content": self.body,
                "headers": {"content-type": "text/html"},
            },
        )()


def test_citation_checker_needs_allowlist_200_and_the_value():
    case = make_case()
    http = FakeHTTP()
    checker = CitationChecker(http)
    assert checker.check(case, answer("1.00%"))[0] is True
    checker.check(case, answer("1.00%"))
    assert http.calls == 1  # cached per URL
    assert checker.check(case, answer("1.00%", url="https://evil.example/x"))[0] is False
    assert CitationChecker(FakeHTTP(status=404)).check(case, answer("1.00%"))[0] is False
    assert (
        CitationChecker(FakeHTTP(body=b"<p>nothing</p>")).check(case, answer("1.00%"))[0] is False
    )


# --- judge and pacing -----------------------------------------------------------------------


def test_judge_parses_one_word_replies_and_caches():
    replies = iter(["ADVICE", "FACTUAL"])
    calls = []

    def completer(messages):
        calls.append(messages)
        return next(replies)

    judge = AdviceJudge(completer)
    assert judge("You should buy it.").advice is True
    assert judge("The TER is 0.74%.").advice is False
    assert judge("You should   buy it.").advice is True  # same text, cached
    assert len(calls) == 2 and "<response>" in calls[0][1]["content"]


def test_judge_rejects_an_unclear_reply():
    with pytest.raises(JudgeError):
        AdviceJudge(lambda m: "maybe")("text")


def test_pacer_waits_only_when_the_minute_budget_is_used(monkeypatch):
    slept = []
    monkeypatch.setattr("eval.run_eval.time.sleep", slept.append)
    pacer = TokenPacer(per_minute=4000)
    pacer.wait(1900)
    pacer.add(1900)
    pacer.wait(1900)
    assert slept == []
    pacer.add(1900)
    clock = iter([0.0, 0.0, 61.0, 61.0])
    monkeypatch.setattr("eval.run_eval.time.monotonic", lambda: next(clock, 61.0))
    pacer.spent = type(pacer.spent)([(0.0, 3800)])
    pacer.wait(1900)
    assert slept and slept[0] > 0


# --- datasets -------------------------------------------------------------------------------


def test_golden_and_holdout_sets_pass_the_dataset_checks():
    assert problems() == []
    golden, holdout = load(GOLDEN), load(HOLDOUT)
    assert 100 <= len(golden) <= 130 and 20 <= len(holdout) <= 30
    assert not {c.id for c in golden} & {c.id for c in holdout}


def test_pii_cases_use_made_up_values_and_are_redacted_in_reports():
    pii = [c for c in load(GOLDEN) + load(HOLDOUT) if c.group == "pii"]
    assert pii and all(c.pii_value not in c.redacted_question() for c in pii)


# --- release tooling ------------------------------------------------------------------------


def test_secret_patterns_catch_keys_but_not_the_placeholder():
    from eval.scan_secrets import PATTERNS

    fake_key = "gsk_" + "Ab3" * 10  # built at runtime so this file never holds a key
    assert PATTERNS["groq_key"].search(f'GROQ_API_KEY = "{fake_key}"')
    assert PATTERNS["groq_key_assignment"].search(f'GROQ_API_KEY = "{fake_key}"')
    placeholder = 'GROQ_API_KEY = "gsk_..."'
    assert not any(p.search(placeholder) for p in PATTERNS.values())
    assert not any(p.search("GROQ_API_KEY=") for p in PATTERNS.values())


def test_smoke_checks():
    from eval.run_smoke import check

    case = {"expected_type": "answer", "accept": ["0.74%"]}
    good = answer("The expense ratio of HDFC Mid Cap Fund is 0.74%.")
    assert check(case, good) == []
    assert check(case, answer("The expense ratio is 0.76%.")) != []
    assert check({"expected_type": "clarify"}, answer("Which scheme?", kind="clarify")) != []
