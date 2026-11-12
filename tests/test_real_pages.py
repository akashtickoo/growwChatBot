"""Parser check on the real HDFC MF pages saved locally in data/raw/ (gitignored).

Skipped when no saved pages exist, e.g. on a fresh clone. Run `python -m ingestion.run` first.
Compares parsed facts with the Phase 1 labels in eval/fact_labels.yaml.
"""

from pathlib import Path

import pytest
import yaml

from ingestion.manifest import load_schemes
from ingestion.normalize import normalize_text
from ingestion.parse_html import parse_scheme_page

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
LABELS = yaml.safe_load((ROOT / "eval" / "fact_labels.yaml").read_text(encoding="utf-8"))["labels"]
PAGE_FIELDS = {"expense_ratio", "min_sip", "lock_in", "riskometer", "benchmark"}


def _saved_page(scheme_id: str) -> Path | None:
    copies = sorted((RAW / f"{scheme_id}-page").glob("*.html"))
    return copies[-1] if copies else None


SCHEMES = [s.id for s in load_schemes().schemes]


@pytest.mark.parametrize("scheme_id", SCHEMES)
def test_saved_page_facts_match_the_labels(scheme_id):
    page_path = _saved_page(scheme_id)
    if page_path is None:
        pytest.skip("no saved page in data/raw")
    facts = parse_scheme_page(page_path.read_text(encoding="utf-8")).facts
    for label in LABELS:
        if label["scheme_id"] != scheme_id or label["field"] not in PAGE_FIELDS:
            continue
        value = normalize_text(facts.get(label["field"], ""))
        if label["field"] == "expense_ratio":
            value += "%"
        accepted = [normalize_text(a).lower() for a in label["accept"]]
        assert any(a in value.lower() for a in accepted), (label["field"], value, label["accept"])
