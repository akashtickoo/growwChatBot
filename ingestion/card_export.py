"""Carry reviewed fact cards to a fresh environment (Streamlit Community Cloud, a new clone).

The registry (data/registry.sqlite) is local and gitignored, so a deployment starts without the
human review done in Phase 3. `export` writes the approved cards to config/reviewed_fact_cards.yaml
(committed); `import` loads them into an empty registry before the first index build.

Each card keeps the content hash of the document it was read from. After a fresh fetch, a card
whose source has changed since review is withdrawn from the index, exactly as locally
(EC-RET-04), so an imported card can never serve a value its page no longer shows.

Usage:
    python -m ingestion.card_export export     # after approving cards
    python -m ingestion.card_export import     # done automatically by app/bootstrap.py
"""

import argparse
import sys
from pathlib import Path

import yaml

from ingestion.paths import REPO_ROOT, Paths
from ingestion.registry import CARD_COLUMNS, Registry

EXPORT_PATH = REPO_ROOT / "config" / "reviewed_fact_cards.yaml"
HEADER = """\
# Approved fact cards (Phase 3 human review), exported from data/registry.sqlite.
# Regenerate with: python -m ingestion.card_export export
# A fresh deployment imports these before its first index build. A card whose source document has
# changed since review (different content hash) is withdrawn until it is reviewed again.
"""


def export_cards(registry: Registry, path: Path = EXPORT_PATH) -> int:
    cards = [
        {"scheme_id": c["scheme_id"], "field": c["field"], **{k: c[k] for k in CARD_COLUMNS}}
        for c in registry.list_cards()
        if c["status"] == "found" and c["review_status"] == "approved"
    ]
    body = yaml.safe_dump({"cards": cards}, allow_unicode=True, sort_keys=False, width=100)
    path.write_text(HEADER + body, encoding="utf-8")
    return len(cards)


def import_cards(registry: Registry, path: Path = EXPORT_PATH) -> int:
    """Load exported cards. Cards already in the registry are left alone (local review wins)."""
    if not path.exists():
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    added = 0
    for card in data.get("cards", []):
        if registry.get_card(card["scheme_id"], card["field"]) is not None:
            continue
        values = {k: card.get(k) for k in CARD_COLUMNS}
        registry.upsert_card(card["scheme_id"], card["field"], **values)
        added += 1
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["export", "import"])
    args = parser.parse_args(argv)
    with Registry(Paths.default().registry) as registry:
        if args.action == "export":
            print(f"exported {export_cards(registry)} approved cards to {EXPORT_PATH}")
        else:
            print(f"imported {import_cards(registry)} cards into the registry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
