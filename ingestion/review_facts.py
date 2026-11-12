"""Review fact cards against their official sources (architecture §5.4).

    python -m ingestion.review_facts                    go through pending cards one by one
    python -m ingestion.review_facts --list             show every card and its review status
    python -m ingestion.review_facts --approve hdfc-mid-cap:exit_load [...]
    python -m ingestion.review_facts --reject hdfc-mid-cap:exit_load --note "wrong slab"

Open the source link, check the value and quote, then approve or reject. Only approved cards
whose source hasn't changed since extraction are indexed; run
`python -m ingestion.run --index-only` afterwards to rebuild the index.
"""

import argparse
import sys
from datetime import UTC, datetime

from ingestion.fact_cards import FIELDS, label_matches, load_labels
from ingestion.manifest import load_schemes
from ingestion.paths import Paths
from ingestion.registry import Registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key(value: str) -> tuple[str, str]:
    scheme_id, sep, field = value.partition(":")
    if not sep or field not in FIELDS:
        raise argparse.ArgumentTypeError(f"expected SCHEME_ID:FIELD, got {value!r}")
    return scheme_id, field


def describe(card: dict, registry: Registry, names: dict[str, str], labels: dict) -> str:
    doc = registry.get_document(card["source_doc_id"] or "") or {}
    label = labels.get((card["scheme_id"], card["field"]))
    match = label_matches(label, card["value"])
    lines = [
        f"{names.get(card['scheme_id'], card['scheme_id'])} · {FIELDS[card['field']]['label']}",
        f"  Status:  {card['status']}   Review: {card['review_status']}",
        f"  Value:   {card['value'] or '—'}",
        f'  Quote:   "{card["verbatim_quote"] or "—"}"',
    ]
    if doc:
        fetched = (doc.get("fetched_at") or "")[:10]
        lines.append(f"  Source:  {doc.get('title')} — {doc.get('url')} (fetched {fetched})")
    if label:
        verdict = {True: "matches", False: "DIFFERS", None: "—"}[match]
        lines.append(f"  Phase 1 label: {label['expected']}  ({verdict})")
    if card["conflicting"] and str(card["conflicting"]) != "0":
        lines.append("  Flag:    the sources gave different values")
    if card["note"]:
        lines.append(f"  Note:    {card['note']}")
    return "\n".join(lines)


def interactive(registry: Registry, names: dict[str, str], labels: dict) -> int:
    pending = [
        c
        for c in registry.list_cards()
        if c["review_status"] == "pending" and c["status"] in ("found", "not_found")
    ]
    rejected_by_check = [c for c in registry.list_cards() if c["status"] == "rejected"]
    if rejected_by_check:
        print(
            f"{len(rejected_by_check)} card(s) failed the quote check and can't be approved; "
            "re-run python -m ingestion.fact_cards --force for them.\n"
        )
    if not pending:
        print("No pending cards.")
        return 0
    for i, card in enumerate(pending, start=1):
        print(f"\n[{i}/{len(pending)}] " + describe(card, registry, names, labels))
        while True:
            choice = input("  [a]pprove  [r]eject  [s]kip  [q]uit > ").strip().lower()
            if choice in ("a", "r", "s", "q"):
                break
        if choice == "q":
            break
        if choice == "s":
            continue
        note = None
        if choice == "r":
            note = input("  Reason (optional) > ").strip() or None
        status = "approved" if choice == "a" else "rejected"
        registry.set_review(card["scheme_id"], card["field"], status, _now(), note)
    print("\nDone. Rebuild the index with: python -m ingestion.run --index-only")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review fact cards")
    parser.add_argument("--list", action="store_true", help="show every card")
    parser.add_argument("--approve", nargs="+", type=_key, metavar="SCHEME:FIELD", default=[])
    parser.add_argument("--reject", nargs="+", type=_key, metavar="SCHEME:FIELD", default=[])
    parser.add_argument("--note", help="reason, stored with --reject")
    args = parser.parse_args(argv)

    names = {s.id: s.name for s in load_schemes().schemes}
    labels = load_labels()
    with Registry(Paths.default().registry) as registry:
        if args.list:
            for card in registry.list_cards():
                print(describe(card, registry, names, labels) + "\n")
            return 0
        if args.approve or args.reject:
            for scheme_id, field in args.approve:
                card = registry.get_card(scheme_id, field)
                if card is None or card["status"] != "found":
                    status = card["status"] if card else "missing"
                    print(f"skip {scheme_id}:{field}: card is {status}", file=sys.stderr)
                    continue
                registry.set_review(scheme_id, field, "approved", _now())
                print(f"approved {scheme_id}:{field}")
            for scheme_id, field in args.reject:
                registry.set_review(scheme_id, field, "rejected", _now(), args.note)
                print(f"rejected {scheme_id}:{field}")
            print("Rebuild the index with: python -m ingestion.run --index-only")
            return 0
        return interactive(registry, names, labels)


if __name__ == "__main__":
    sys.exit(main())
