"""Validator evaluation (eval.md §9.1, EV-4C-01/02). Deterministic, no API calls.

Usage: python -m eval.run_validator        exit code 1 if a gate fails
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.guards.output_validator import validate
from app.llm.schemas import LLMAnswer
from ingestion.models import Chunk

SET_PATH = Path(__file__).resolve().parent / "validator_cases.yaml"


def _chunk(chunk_id: str, text: str, as_of: str | None = None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc",
        scheme_id="hdfc-mid-cap",
        doc_type="SCHEME_PAGE",
        section="Key facts",
        text=f"[HDFC Mid Cap Fund – Direct Growth | Scheme page | Key facts]\n{text}",
        source_url="https://www.hdfcfund.com/x",
        doc_as_of=as_of,
    )


def check(case: dict) -> list[str]:
    """The failed checks for one case."""
    chunks = [_chunk("c1", case["source"], case.get("as_of"))]
    if "second" in case:
        # A cited "c2" holds the `second` text; otherwise it is just another retrieved chunk.
        if case.get("source_id") == "c2":
            chunks = [_chunk("c1", case["source"]), _chunk("c2", case["second"])]
        else:
            chunks.append(_chunk("c2", case["second"]))
    reply = LLMAnswer(
        status="answered", answer=case["answer"], source_id=case.get("source_id", "c1")
    )
    return validate(reply, chunks, question=case.get("question", "")).checks


@dataclass
class ValidatorScores:
    bad_accepted: int
    good_rejected_rate: float
    wrong_check: int  # bad outputs rejected, but not for (all) the expected checks
    failures: list[str] = field(default_factory=list)

    def gate_failures(self) -> list[str]:
        gates = []
        if self.bad_accepted:
            gates.append(f"EV-4C-01 {self.bad_accepted} bad output(s) accepted")
        if self.good_rejected_rate > 0.05:
            gates.append(f"EV-4C-02 good outputs rejected {self.good_rejected_rate:.0%} > 5%")
        return gates


def evaluate(path: Path = SET_PATH) -> ValidatorScores:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    rejected = 0
    for case in data["good"]:
        if got := check(case):
            rejected += 1
            failures.append(f"good rejected: {case['id']} failed {got}")
    accepted = wrong = 0
    for case in data["bad"]:
        got = check(case)
        if not got:
            accepted += 1
            failures.append(f"bad accepted: {case['id']} (expected {case['expect']})")
        elif not set(case["expect"]) <= set(got):
            wrong += 1
            failures.append(
                f"bad caught by other checks: {case['id']} {got}, expected {case['expect']}"
            )
    return ValidatorScores(accepted, rejected / len(data["good"]), wrong, failures)


def main() -> int:
    s = evaluate()
    print(f"EV-4C-01 bad outputs accepted     {s.bad_accepted}")
    print(f"EV-4C-02 good outputs rejected    {s.good_rejected_rate:.1%}")
    print(f"         caught by another check  {s.wrong_check}")
    for failure in s.failures:
        print(f"  - {failure}")
    gates = s.gate_failures()
    print("\n" + ("PASS" if not gates else "FAIL: " + "; ".join(gates)))
    return 1 if gates else 0


if __name__ == "__main__":
    sys.exit(main())
