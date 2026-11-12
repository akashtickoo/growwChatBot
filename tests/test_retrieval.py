"""Phase 4B: index loader and hybrid retrieval over a small synthetic index (fake_index.py)."""

import pytest
from fake_index import CHUNKS, MID, NOW, SMALL, bow_embedder
from fastapi.testclient import TestClient

from app.main import create_app
from app.retrieval.fields import detect_fields
from app.retrieval.hybrid import retrieve, topic_query
from app.retrieval.index_loader import IndexUnavailable, load_index, tokens
from ingestion.build_index import build_index, read_current

THRESHOLD = 0.63


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    index_dir = tmp_path_factory.mktemp("index")
    build_index(index_dir, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    return load_index(index_dir, bow_embedder)


def ask(index, question, scheme_id):
    return retrieve(
        index,
        question,
        scheme_id=scheme_id,
        fields=detect_fields(question),
        min_similarity=THRESHOLD,
    )


# --- plan tests --------------------------------------------------------------------------


def test_exit_load_mid_cap_returns_the_card_or_page_chunk_in_top_3(index):
    result = ask(index, "exit load mid cap", MID)
    assert {f"fact:{MID}:exit_load", f"{MID}-page#001"} & set(result.chunk_ids[:3])
    assert result.chunk_ids[0] == f"fact:{MID}:exit_load"  # boost puts the card first (EC-RET-02)
    assert result.relevant


@pytest.mark.parametrize(
    ("question", "scheme"),
    [
        ("What is the exit load of HDFC Small Cap?", SMALL),
        ("exit load redemption within one year", SMALL),
        ("What is the exit load of HDFC Mid Cap?", MID),
        ("How do I download a capital gain statement?", None),
    ],
)
def test_one_scheme_never_returns_another_schemes_chunks(index, question, scheme):
    """EC-RET-03, EV-4B-03. Both schemes have identical exit-load text."""
    result = ask(index, question, scheme)
    assert result.chunks
    assert {s.chunk.scheme_id for s in result.chunks} == {scheme}


def test_unrelated_question_falls_below_the_threshold(index):
    """EC-RET-01."""
    assert not ask(index, "what is the weather", None).relevant


# --- behavior details --------------------------------------------------------------------


def test_off_topic_question_naming_a_scheme_is_not_lifted_by_the_name(index):
    assert topic_query("Which colour is the HDFC Mid Cap Fund logo?", MID) == (
        "which colour is the the fund logo"
    )
    assert not ask(index, "Which colour is the HDFC Mid Cap Fund logo?", MID).relevant


def test_topic_query_leaves_general_questions_alone():
    assert topic_query("How do I get a CAS?", None) == "How do I get a CAS?"


def test_cards_for_fields_not_asked_take_at_most_one_slot(index):
    result = ask(index, "investment objective and asset allocation pattern", MID)
    cards = [s for s in result.chunks if s.chunk.doc_type == "FACT_CARD"]
    assert len(cards) <= 1
    assert f"{MID}-kim#000" in result.chunk_ids and f"{MID}-kim#001" in result.chunk_ids


def test_several_requested_fields_put_each_card_first(index):
    result = ask(index, "expense ratio and exit load of HDFC Mid Cap", MID)
    assert set(result.chunk_ids[:2]) == {f"fact:{MID}:expense_ratio", f"fact:{MID}:exit_load"}


def test_bm25_stemming_matches_word_forms(index):
    assert tokens("who manages") == tokens("who managers")
    assert f"{MID}-page#002" in ask(index, "Who manages HDFC Mid Cap?", MID).chunk_ids


def test_scheme_with_no_chunks_returns_nothing(index):
    result = ask(index, "exit load", "hdfc-multi-cap")
    assert result.chunks == [] and not result.relevant


def test_results_carry_scores_and_top_k(index):
    result = retrieve(index, "exit load", scheme_id=MID, top_k=2, min_similarity=THRESHOLD)
    assert len(result.chunks) == 2
    assert result.best_similarity >= max(s.similarity or 0 for s in result.chunks)


# --- loader and /healthz -----------------------------------------------------------------


def test_missing_index_raises(tmp_path):
    with pytest.raises(IndexUnavailable):
        load_index(tmp_path, bow_embedder)


def test_inconsistent_index_raises(tmp_path):
    build_index(tmp_path, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    chunks_file = read_current(tmp_path) / "chunks.jsonl"
    lines = chunks_file.read_text(encoding="utf-8").splitlines()
    chunks_file.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(IndexUnavailable, match="inconsistent"):
        load_index(tmp_path, bow_embedder)


def test_healthz_reports_the_loaded_index(tmp_path, clean_env):
    version = build_index(tmp_path, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    with TestClient(create_app(index_dir=tmp_path, embedder=bow_embedder)) as client:
        body = client.get("/healthz").json()
    assert body["index_loaded"] is True
    assert body["index_version"] == version and body["index_built_at"] == NOW.isoformat()
    assert body["chunk_count"] == len(CHUNKS)


def test_healthz_without_an_index_still_serves(tmp_path, clean_env):
    """EC-RET-07."""
    with TestClient(create_app(index_dir=tmp_path, embedder=bow_embedder)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200 and response.json()["index_loaded"] is False
    assert "index_version" not in response.json()
