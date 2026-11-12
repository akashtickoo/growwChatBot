"""Deployment support: rate limiter, fact-card export/import, startup bootstrap."""

from pathlib import Path

import pytest
from fake_index import CHUNKS, NOW, bow_embedder

import app.bootstrap as bootstrap
from app.ratelimit import SlidingWindowLimiter, parse_rate
from app.settings import Settings
from ingestion.build_index import build_index
from ingestion.card_export import EXPORT_PATH, export_cards, import_cards
from ingestion.paths import Paths
from ingestion.registry import CARD_COLUMNS, Registry

# --- rate limiter -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "parsed"),
    [("20/minute", (20, 60)), ("5 per second", (5, 1)), ("100/hour", (100, 3600))],
)
def test_parse_rate(rate, parsed):
    assert parse_rate(rate) == parsed


def test_parse_rate_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_rate("lots")


def test_sliding_window_frees_slots_as_time_passes():
    now = [0.0]
    limiter = SlidingWindowLimiter("2/minute", clock=lambda: now[0])
    assert limiter.try_acquire() == 0 and limiter.try_acquire() == 0
    assert limiter.try_acquire() == pytest.approx(60)
    now[0] = 30
    assert limiter.try_acquire() == pytest.approx(30)
    now[0] = 61
    assert limiter.try_acquire() == 0


# --- fact-card export and import --------------------------------------------------------------


def _card(review="approved", status="found", value="0.74%"):
    values = dict.fromkeys(CARD_COLUMNS)
    values.update(
        status=status,
        value=value,
        verbatim_quote=f"TER: {value}",
        source_doc_id="hdfc-mid-cap-page",
        source_content_hash="h1",
        sources_hash="s1",
        conflicting=0,
        review_status=review,
        extracted_at="2026-11-12T00:00:00+00:00",
    )
    return values


def test_export_keeps_only_approved_found_cards_and_import_restores_them(tmp_path):
    out = tmp_path / "cards.yaml"
    with Registry(tmp_path / "a.sqlite") as source:
        source.upsert_card("hdfc-mid-cap", "expense_ratio", **_card())
        source.upsert_card("hdfc-mid-cap", "exit_load", **_card(review="pending"))
        source.upsert_card("hdfc-mid-cap", "min_sip", **_card(status="not_found"))
        assert export_cards(source, out) == 1
    with Registry(tmp_path / "b.sqlite") as target:
        assert import_cards(target, out) == 1
        card = target.get_card("hdfc-mid-cap", "expense_ratio")
        assert card["value"] == "0.74%" and card["review_status"] == "approved"
        assert card["source_content_hash"] == "h1"  # withdrawal still works after a fresh fetch
        assert import_cards(target, out) == 0  # existing cards are left alone


def test_import_never_overwrites_a_local_review(tmp_path):
    out = tmp_path / "cards.yaml"
    with Registry(tmp_path / "a.sqlite") as source:
        source.upsert_card("hdfc-mid-cap", "expense_ratio", **_card(value="0.74%"))
        export_cards(source, out)
    with Registry(tmp_path / "b.sqlite") as target:
        target.upsert_card("hdfc-mid-cap", "expense_ratio", **_card(value="0.80%"))
        import_cards(target, out)
        assert target.get_card("hdfc-mid-cap", "expense_ratio")["value"] == "0.80%"


def test_committed_export_holds_the_reviewed_cards():
    """config/reviewed_fact_cards.yaml: 49 approved cards, each tied to a source hash."""
    import yaml

    cards = yaml.safe_load(EXPORT_PATH.read_text(encoding="utf-8"))["cards"]
    assert len(cards) == 49
    assert all(c["review_status"] == "approved" and c["source_content_hash"] for c in cards)


# --- bootstrap --------------------------------------------------------------------------------


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(bootstrap, "SentenceTransformerEmbedder", lambda: bow_embedder)


def test_existing_index_is_reused_without_fetching(tmp_path, no_model, monkeypatch, clean_env):
    paths = Paths.under(tmp_path)
    build_index(paths.index, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    monkeypatch.setattr(bootstrap, "build_index_from_sources", _must_not_run)
    startup = bootstrap.start(Settings(_env_file=None), paths)
    assert startup.pipeline.index is not None and not startup.built_index


def test_missing_index_is_built_from_sources(tmp_path, no_model, monkeypatch, clean_env):
    paths = Paths.under(tmp_path)

    def fake_build(p: Path, embedder) -> str:
        build_index(p.index, CHUNKS, embedder, doc_hashes={"x": "h"}, now=NOW)
        return "built"

    monkeypatch.setattr(bootstrap, "build_index_from_sources", fake_build)
    startup = bootstrap.start(Settings(_env_file=None), paths)
    assert startup.built_index and startup.pipeline.index is not None


def test_failed_build_still_serves_templates(tmp_path, no_model, monkeypatch, clean_env):
    """EC-RET-07: e.g. the HDFC MF site unreachable from the host."""
    paths = Paths.under(tmp_path)

    def broken(p, embedder):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(bootstrap, "build_index_from_sources", broken)
    startup = bootstrap.start(Settings(_env_file=None), paths)
    assert startup.pipeline.index is None and "ConnectionError" in startup.message
    assert startup.pipeline.answer("hi").type == "greeting"


def _must_not_run(*args, **kwargs):
    raise AssertionError("should not fetch when an index exists")
