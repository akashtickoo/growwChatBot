"""Fact-card extraction, review gating and index withdrawal (fake LLM, no API calls)."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.llm.groq_json import JSONResult, LLMUnavailable
from ingestion.fact_cards import (
    CARD_SCHEMA,
    ExtractedCard,
    Extractor,
    check_card,
    fact_card_chunks,
    for_match,
    load_scheme_sources,
    pick_candidates,
)
from ingestion.manifest import load_schemes
from ingestion.models import Chunk
from ingestion.paths import Paths
from ingestion.registry import Registry

SCHEMES = {s.id: s for s in load_schemes().schemes}
MID_CAP = SCHEMES["hdfc-mid-cap"]
PAGE_URL = "https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct"
KIM_URL = "https://files.hdfcfund.com/kim.pdf"

KEY_FACTS = (
    "Riskometer: Very High.\nMinimum SIP amount: ₹100.\n"
    "Total expense ratio (TER): 0.74%, including additional expenses and goods and service tax "
    "on management fees, if any.\nLock-in period: NA.\n"
    "Benchmark: NIFTY Midcap 150 Index (Total Returns Index)."
)
EXIT_LOAD = (
    "In respect of each purchase/switch-in of Units, an Exit Load of 1.00% is payable if Units "
    "are redeemed/switched-out within 1 year from the date of allotment.\nNo Exit Load is "
    "payable if Units are redeemed/switched-out after 1 year from the date of allotment."
)
KIM_TABLE = (
    "Purchase (including switch-in): ₹100/- and any amount thereafter; Additional Purchase "
    "(including switch-in): ₹100/- and any amount thereafter."
)


def _chunk(doc_id, doc_type, section, body, url, i):
    return Chunk(
        chunk_id=f"{doc_id}#{i:03d}",
        doc_id=doc_id,
        scheme_id="hdfc-mid-cap",
        doc_type=doc_type,
        section=section,
        text=f"[{MID_CAP.name} | {doc_type} | {section}]\n{body}",
        source_url=url,
        doc_as_of="2025-11-21" if doc_type == "KIM" else None,
    )


def write_sources(paths: Paths, key_facts: str = KEY_FACTS, page_hash: str = "page-v1"):
    """Processed chunks and registry rows for the Mid Cap page and KIM, as Phase 2 leaves them."""
    paths.processed.mkdir(parents=True, exist_ok=True)
    page = [
        _chunk("hdfc-mid-cap-page", "SCHEME_PAGE", "Key facts", key_facts, PAGE_URL, 0),
        _chunk("hdfc-mid-cap-page", "SCHEME_PAGE", "Exit Load", EXIT_LOAD, PAGE_URL, 1),
    ]
    kim = [
        _chunk(
            "hdfc-mid-cap-kim",
            "KIM",
            "Page 18 table: 11. Minimum Application Amount",
            KIM_TABLE,
            KIM_URL,
            0,
        ),
        _chunk("hdfc-mid-cap-kim", "KIM", "Page 5", "Investment strategy text. " * 20, KIM_URL, 1),
    ]
    for doc_id, chunks, digest in (
        ("hdfc-mid-cap-page", page, page_hash),
        ("hdfc-mid-cap-kim", kim, "kim-v1"),
    ):
        data = {"doc_id": doc_id, "content_hash": digest, "chunks": [c.to_dict() for c in chunks]}
        (paths.processed / f"{doc_id}.json").write_text(json.dumps(data), "utf-8")
        with Registry(paths.registry) as registry:
            registry.upsert_document(
                doc_id,
                scheme_id="hdfc-mid-cap",
                doc_type=chunks[0].doc_type,
                title=doc_id,
                url=chunks[0].source_url,
                fetch_mode="auto",
                indexed=1,
                content_hash=digest,
            )


class FakeLLM:
    """Answers per field from `replies`; a list gives successive replies for retries."""

    def __init__(self, replies: dict):
        self.replies = {k: list(v) if isinstance(v, list) else [v] for k, v in replies.items()}
        self.calls: list[str] = []

    def __call__(self, messages, schema, *, name, reasoning_effort, max_tokens):
        assert schema is CARD_SCHEMA and name == "fact_card"
        field = re.search(r"<field>(\w+):", messages[1]["content"]).group(1)
        self.calls.append(field)
        sources = dict(
            re.findall(r'<source id="(S\d+)"[^>]*section="([^"]*)"', messages[1]["content"])
        )
        queue = self.replies[field]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        reply = dict(reply)
        if "section" in reply:  # pick the source id by section name
            wanted = reply.pop("section")
            reply["source_id"] = next(sid for sid, sec in sources.items() if sec.startswith(wanted))
        return JSONResult(reply, "openai/gpt-oss-120b", "stop")


def found(value, quote, section, conflicting=False):
    return {
        "status": "found",
        "value": value,
        "verbatim_quote": quote,
        "section": section,
        "conflicting": conflicting,
    }


GOOD = {
    "expense_ratio": found("0.74%", "Total expense ratio (TER): 0.74%", "Key facts"),
    "exit_load": found(
        "1.00% if redeemed within 1 year from the date of allotment; nil after 1 year",
        EXIT_LOAD.replace("\n", " "),
        "Exit Load",
    ),
    "min_sip": found("₹100", "Minimum SIP amount: ₹100", "Key facts"),
    "min_lumpsum": found(
        "₹100/- and any amount thereafter",
        "Purchase (including switch-in): ₹100/- and any amount thereafter",
        "Page 18 table",
    ),
    "lock_in": found("NA", "Lock-in period: NA", "Key facts"),
    "riskometer": found("Very High", "Riskometer: Very High", "Key facts"),
    "benchmark": found(
        "NIFTY Midcap 150 Index (Total Returns Index)",
        "Benchmark: NIFTY Midcap 150 Index (Total Returns Index)",
        "Key facts",
    ),
}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)

    def __call__(self):
        self.t += timedelta(minutes=1)
        return self.t


@pytest.fixture
def paths(tmp_path):
    p = Paths.under(tmp_path / "data")
    write_sources(p)
    return p


def extract(paths, llm, fields=None, force=False):
    extractor = Extractor(
        paths,
        llm,
        reasoning_effort="medium",
        model_name="openai/gpt-oss-120b",
        force=force,
        now=Clock(),
    )
    report = extractor.run(["hdfc-mid-cap"], fields)
    extractor.registry.close()
    return report


def card(paths, field):
    with Registry(paths.registry) as registry:
        return registry.get_card("hdfc-mid-cap", field)


def approve(paths, *fields):
    with Registry(paths.registry) as registry:
        for f in fields:
            registry.set_review("hdfc-mid-cap", f, "approved", "2026-11-13T10:00:00+00:00")


def indexed_cards(paths):
    with Registry(paths.registry) as registry:
        return fact_card_chunks(registry, SCHEMES)


# --- extraction --------------------------------------------------------------------------


def test_extracts_every_field_as_pending(paths):
    report = extract(paths, FakeLLM(GOOD))

    assert {o.field: o.status for o in report.outcomes} == {f: "found" for f in GOOD}
    exit_card = card(paths, "exit_load")
    assert exit_card["review_status"] == "pending"
    assert exit_card["source_doc_id"] == "hdfc-mid-cap-page"
    assert exit_card["source_content_hash"] == "page-v1"
    lumpsum = card(paths, "min_lumpsum")
    assert lumpsum["source_doc_id"] == "hdfc-mid-cap-kim" and lumpsum["doc_as_of"] == "2025-11-21"


def test_quote_not_in_source_is_retried_then_rejected(paths):
    """Plan test: a card whose quote isn't in the source is rejected."""
    bad = found("0.65%", "TER is 0.65%", "Key facts")
    llm = FakeLLM({"expense_ratio": [bad, bad]})

    report = extract(paths, llm, ["expense_ratio"])

    assert llm.calls == ["expense_ratio", "expense_ratio"]  # one retry
    assert report.outcomes[0].status == "rejected"
    stored = card(paths, "expense_ratio")
    assert stored["status"] == "rejected" and "not in the source" in stored["note"]


def test_retry_can_fix_a_bad_quote(paths):
    bad = found("0.74%", "TER 0.74 percent", "Key facts")
    llm = FakeLLM({"expense_ratio": [bad, GOOD["expense_ratio"]]})
    assert extract(paths, llm, ["expense_ratio"]).outcomes[0].status == "found"


def test_not_found_is_stored_without_a_source(paths):
    reply = {
        "status": "not_found",
        "value": "",
        "verbatim_quote": "",
        "source_id": "",
        "conflicting": False,
    }
    extract(paths, FakeLLM({"lock_in": reply}), ["lock_in"])
    stored = card(paths, "lock_in")
    assert stored["status"] == "not_found" and stored["source_doc_id"] is None


def test_unchanged_sources_are_not_re_extracted(paths):
    extract(paths, FakeLLM(GOOD))
    llm = FakeLLM(GOOD)

    report = extract(paths, llm)

    assert llm.calls == []
    assert {o.status for o in report.outcomes} == {"skipped"}


def test_changed_value_appears_in_the_diff_report(paths):
    """Plan test: change a value in a fixture and confirm it appears in the diff report."""
    extract(paths, FakeLLM(GOOD))
    write_sources(paths, KEY_FACTS.replace("0.74%", "0.81%"), page_hash="page-v2")
    changed = dict(
        GOOD, expense_ratio=found("0.81%", "Total expense ratio (TER): 0.81%", "Key facts")
    )

    report = extract(paths, FakeLLM(changed))

    ter = next(o for o in report.outcomes if o.field == "expense_ratio")
    assert ter.value == "0.81%" and ter.previous_value == "0.74%"
    text = report.path.read_text(encoding="utf-8")
    changes = text.split("## Changed or new values")[1].split("## All cards")[0]
    assert "| hdfc-mid-cap | expense_ratio | found | 0.81% | 0.74% |" in changes


def test_label_mismatch_is_flagged(paths):
    wrong = found("Very High", "Riskometer: Very High", "Key facts")
    replies = dict(GOOD, riskometer=wrong, benchmark=found("NIFTY 50", "NIFTY", "Key facts"))
    report = extract(paths, FakeLLM(replies), ["riskometer"])
    assert report.outcomes[0].label_match is True
    # the Phase 1 label for Mid Cap TER is 0.74%; a different value is flagged
    write_sources(paths, KEY_FACTS.replace("0.74%", "0.99%"), page_hash="page-v3")
    ter = found("0.99%", "Total expense ratio (TER): 0.99%", "Key facts")
    report = extract(paths, FakeLLM({"expense_ratio": ter}), ["expense_ratio"])
    assert report.outcomes[0].label_match is False
    assert "**no**" in report.path.read_text(encoding="utf-8")


# --- review gating and withdrawal --------------------------------------------------------


def test_unreviewed_cards_are_not_indexed(paths):
    """Plan test."""
    extract(paths, FakeLLM(GOOD))
    chunks, withdrawn = indexed_cards(paths)
    assert chunks == [] and withdrawn == 0


def test_approved_cards_are_indexed_with_their_source(paths):
    extract(paths, FakeLLM(GOOD))
    approve(paths, "exit_load", "min_lumpsum")

    chunks, _ = indexed_cards(paths)

    by_id = {c.chunk_id: c for c in chunks}
    assert set(by_id) == {"fact:hdfc-mid-cap:exit_load", "fact:hdfc-mid-cap:min_lumpsum"}
    exit_chunk = by_id["fact:hdfc-mid-cap:exit_load"]
    assert exit_chunk.doc_type == "FACT_CARD" and exit_chunk.source_url == PAGE_URL
    assert exit_chunk.text.startswith(f"[{MID_CAP.name} | Fact card | Exit load]\n")
    assert by_id["fact:hdfc-mid-cap:min_lumpsum"].source_url == KIM_URL


def test_approved_card_is_withdrawn_when_its_source_changes(paths):
    """Plan test / EC-RET-04: a reviewed card whose source hash changed leaves the index."""
    extract(paths, FakeLLM(GOOD))
    approve(paths, "expense_ratio")
    with Registry(paths.registry) as registry:
        registry.upsert_document("hdfc-mid-cap-page", content_hash="page-v2")

    chunks, withdrawn = indexed_cards(paths)

    assert chunks == [] and withdrawn == 1


def test_approval_carries_over_when_value_and_quote_are_unchanged(paths):
    extract(paths, FakeLLM(GOOD))
    approve(paths, "riskometer", "expense_ratio")
    write_sources(paths, KEY_FACTS.replace("0.74%", "0.81%"), page_hash="page-v2")
    changed = dict(
        GOOD, expense_ratio=found("0.81%", "Total expense ratio (TER): 0.81%", "Key facts")
    )

    extract(paths, FakeLLM(changed))

    assert card(paths, "riskometer")["review_status"] == "approved"  # same value and quote
    assert card(paths, "expense_ratio")["review_status"] == "pending"  # new value
    ids = {c.chunk_id for c in indexed_cards(paths)[0]}
    assert ids == {"fact:hdfc-mid-cap:riskometer"}


def test_llm_outage_leaves_the_previous_card(paths):
    extract(paths, FakeLLM(GOOD), ["min_sip"])

    def down(*args, **kwargs):
        raise LLMUnavailable("Groq unavailable: RateLimitError")

    report = extract(paths, down, ["min_sip"], force=True)
    assert report.outcomes[0].status == "error"
    assert card(paths, "min_sip")["value"] == "₹100"


# --- helpers -----------------------------------------------------------------------------


def test_candidates_pin_key_facts_and_find_the_kim_table(paths):
    sources = load_scheme_sources(paths, MID_CAP)
    lumpsum = pick_candidates(sources, "min_lumpsum")
    assert "Minimum Application Amount" in lumpsum[0].section
    exit_load = pick_candidates(sources, "exit_load")
    assert [c.section for c in exit_load[:2]] == ["Key facts", "Exit Load"]


def test_check_card_catches_numbers_missing_from_the_quote():
    chunk = Chunk("c#0", "d", "s", "KIM", "x", "TER: 0.74%", "u")
    card = ExtractedCard(
        status="found",
        value="0.74% (was 0.80%)",
        verbatim_quote="TER: 0.74%",
        source_id="S1",
        conflicting=False,
    )
    assert any("numbers not in the quote" in p for p in check_card(card, {"S1": chunk}))


def test_for_match_ignores_spacing_dashes_and_currency_style():
    assert for_match("Rs.100/-  and any\namount – thereafter") == for_match(
        "₹100/- and any amount - thereafter"
    )
