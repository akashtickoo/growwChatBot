"""Guard evaluation (eval.md §7, EV-4A-01 to EV-4A-08). Deterministic, so one run is enough.

Usage: python -m eval.run_guards        exit code 1 if a gate fails
"""

import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.guards.intent import classify_intent
from app.guards.pii import detect_pii
from app.retrieval.scheme_resolver import resolve

SET_PATH = Path(__file__).resolve().parent / "guards_set.yaml"


@dataclass
class GuardScores:
    pii_recall: float
    pii_false_positive_rate: float
    not_covered_misroutes: int
    false_refusals: float  # factual inputs given a refusal/redirect intent
    advisory_caught: float  # advisory inputs caught by rules alone (target ≥ 80%; LLM is backup)
    greeting_with_question_ok: bool
    resolver_accuracy: float
    intent_accuracy: float
    p95_ms: float
    failures: list[str] = field(default_factory=list)

    def gate_failures(self) -> list[str]:
        gates = []
        if self.pii_recall < 1.0:
            gates.append(f"EV-4A-01 PII recall {self.pii_recall:.0%} < 100%")
        if self.pii_false_positive_rate > 0.02:
            gates.append(f"EV-4A-02 PII false positives {self.pii_false_positive_rate:.0%} > 2%")
        if self.not_covered_misroutes:
            gates.append(f"EV-4A-03 {self.not_covered_misroutes} not-covered input(s) misrouted")
        if self.false_refusals > 0.05:
            gates.append(f"EV-4A-04 false refusals {self.false_refusals:.0%} > 5%")
        if not self.greeting_with_question_ok:
            gates.append("EV-4A-06 greeting plus question not classified as factual")
        if self.resolver_accuracy < 0.98:
            gates.append(f"EV-4A-07 resolver accuracy {self.resolver_accuracy:.0%} < 98%")
        if self.p95_ms >= 10:
            gates.append(f"EV-4A-08 guard latency p95 {self.p95_ms:.1f} ms ≥ 10 ms")
        return gates


def evaluate(path: Path = SET_PATH) -> GuardScores:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    timings: list[float] = []

    def timed(fn, text):
        start = time.perf_counter()
        result = fn(text)
        timings.append((time.perf_counter() - start) * 1000)
        return result

    caught = 0
    for case in data["pii_positive"]:
        kinds = timed(detect_pii, case["text"])
        if set(case["kinds"]) <= set(kinds):
            caught += 1
        else:
            failures.append(f"PII missed: {case['kinds']} got {kinds}")
    flagged = 0
    for text in data["pii_negative"]:
        if kinds := timed(detect_pii, text):
            flagged += 1
            failures.append(f"PII false positive: {kinds} on a harmless input")

    intent_ok = advisory_total = advisory_rules = factual_total = factual_refused = 0
    greeting_ok = True
    for case in data["intent"]:
        got = timed(classify_intent, case["text"]).intent
        want = case["intent"]
        intent_ok += got == want
        if got != want:
            failures.append(f"intent: {case['text']!r} expected {want}, got {got}")
        if want == "advisory":
            advisory_total += 1
            advisory_rules += got == "advisory"
        if want == "factual":
            factual_total += 1
            factual_refused += got in ("advisory", "performance", "out_of_scope")
            if case["text"].lower().startswith(("hi", "hello")) and got != "factual":
                greeting_ok = False

    resolver_ok = misroutes = 0
    for case in data["resolver"]:
        result = timed(resolve, case["text"])
        ok = result.kind == case["kind"]
        if "scheme" in case:
            ok = ok and result.scheme_id == case["scheme"]
        if "reason" in case:
            ok = ok and result.reason == case["reason"]
        resolver_ok += ok
        if case["kind"] == "not_covered" and result.kind in ("one", "several"):
            misroutes += 1
        if not ok:
            failures.append(
                f"resolver: {case['text']!r} expected {case['kind']} {case.get('scheme', '')}"
                f"{case.get('reason', '')}, got {result.kind} {result.scheme_ids} {result.reason}"
            )

    timings.sort()
    return GuardScores(
        pii_recall=caught / len(data["pii_positive"]),
        pii_false_positive_rate=flagged / len(data["pii_negative"]),
        not_covered_misroutes=misroutes,
        false_refusals=factual_refused / max(factual_total, 1),
        advisory_caught=advisory_rules / max(advisory_total, 1),
        greeting_with_question_ok=greeting_ok,
        resolver_accuracy=resolver_ok / len(data["resolver"]),
        intent_accuracy=intent_ok / len(data["intent"]),
        p95_ms=statistics.quantiles(timings, n=20)[-1] if len(timings) > 1 else timings[0],
        failures=failures,
    )


def main() -> int:
    s = evaluate()
    print(f"EV-4A-01 PII recall               {s.pii_recall:.0%}")
    print(f"EV-4A-02 PII false positives      {s.pii_false_positive_rate:.0%}")
    print(f"EV-4A-03 not-covered misroutes    {s.not_covered_misroutes}")
    print(f"EV-4A-04 false refusals           {s.false_refusals:.0%}")
    print(f"EV-4A-05 advisory caught by rules {s.advisory_caught:.0%} (target ≥ 80%)")
    print(f"EV-4A-06 greeting + question      {'ok' if s.greeting_with_question_ok else 'FAIL'}")
    print(f"EV-4A-07 resolver accuracy        {s.resolver_accuracy:.0%}")
    print(f"         intent accuracy          {s.intent_accuracy:.0%}")
    print(f"EV-4A-08 latency p95              {s.p95_ms:.2f} ms")
    for failure in s.failures:
        print(f"  - {failure}")
    gates = s.gate_failures()
    print("\n" + ("PASS" if not gates else "FAIL: " + "; ".join(gates)))
    return 1 if gates else 0


if __name__ == "__main__":
    sys.exit(main())
