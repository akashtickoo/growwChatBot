"""eval/fact_labels.yaml stays complete: one label per scheme x field (EV-1-06)."""

from pathlib import Path

import yaml

from ingestion.manifest import load_schemes

LABELS_PATH = Path(__file__).resolve().parent.parent / "eval" / "fact_labels.yaml"
FIELDS = {
    "expense_ratio",
    "exit_load",
    "min_sip",
    "min_lumpsum",
    "lock_in",
    "riskometer",
    "benchmark",
}


def _labels() -> list[dict]:
    return yaml.safe_load(LABELS_PATH.read_text(encoding="utf-8"))["labels"]


def test_one_label_per_scheme_and_field():
    scheme_ids = {s.id for s in load_schemes().schemes}
    pairs = [(label["scheme_id"], label["field"]) for label in _labels()]

    assert len(pairs) == len(set(pairs)), "duplicate scheme/field label"
    assert set(pairs) == {(s, f) for s in scheme_ids for f in FIELDS}


def test_every_label_has_an_expected_value_and_accept_list():
    for label in _labels():
        assert label["expected"], label
        assert label["accept"], label
        assert label["source"] in {"page", "kim"}, label
