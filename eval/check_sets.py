"""Dataset checks before every run (eval.md §11.1).

Usage: python -m eval.check_sets        exit code 1 on any problem

Checks: schema valid, no duplicate questions, every group present in the golden set, every
edge_case_ref exists in docs/edge-case.md, no case in both the golden and held-out sets, every
fact label still matches eval/fact_labels.yaml, every cited doc id is in config/sources.yaml.
"""

import re
import sys
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from eval.cases import GOLDEN, HOLDOUT, Group, load
from ingestion.manifest import linked_doc_id, load_sources

EDGE_CASES = Path(__file__).resolve().parents[1] / "docs" / "edge-case.md"


def known_doc_ids() -> set[str]:
    ids = set()
    for doc in load_sources().documents:
        ids.add(doc.id)
        for spec in doc.linked_documents:
            ids.add(linked_doc_id(doc.scheme_id, spec.doc_type))
    return ids


def problems() -> list[str]:
    found: list[str] = []
    try:
        golden, holdout = load(GOLDEN), load(HOLDOUT)
    except (ValidationError, FileNotFoundError, KeyError) as exc:
        return [f"cannot load the sets: {exc}"]

    edge_ids = set(
        re.findall(r"^\| (EC-[A-Z]+-\d+) \|", EDGE_CASES.read_text(encoding="utf-8"), re.M)
    )
    docs = known_doc_ids()
    seen_questions: dict[str, str] = {}
    ids = [c.id for c in golden + holdout]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        found.append(f"duplicate case id: {dup}")
    overlap = {c.id for c in golden} & {c.id for c in holdout}
    for cid in sorted(overlap):
        found.append(f"case in both golden and held-out sets: {cid}")

    for c in golden + holdout:
        key = " ".join(c.question.lower().split())
        if key in seen_questions:
            found.append(f"duplicate question: {c.id} and {seen_questions[key]}")
        seen_questions[key] = c.id
        if c.edge_case_ref and c.edge_case_ref not in edge_ids:
            found.append(f"{c.id}: unknown edge case {c.edge_case_ref}")
        for doc in c.expected_citation or []:
            if doc not in docs:
                found.append(f"{c.id}: unknown cited document {doc}")
        if "answer" in c.expected_type and not c.has_value and c.answerable:
            found.append(f"{c.id}: answer case without accept values")
        if c.group == "pii" and not (c.pii_value and c.pii_value in c.question):
            found.append(f"{c.id}: PII case must name its made-up value")

    missing = set(get_args(Group)) - {c.group for c in golden}
    for group in sorted(missing):
        found.append(f"group missing from the golden set: {group}")
    return found


def main() -> int:
    issues = problems()
    golden, holdout = load(GOLDEN), load(HOLDOUT)
    print(f"golden {len(golden)} cases, held-out {len(holdout)} cases")
    for issue in issues:
        print(f"  - {issue}")
    print("PASS" if not issues else f"FAIL: {len(issues)} problem(s)")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
