"""Retrieval evaluation (eval.md §8, EV-4B-01 to EV-4B-06) over the current index.

Deterministic, so one run is enough. Needs a built index (python -m ingestion.run).

Usage: python -m eval.run_retrieval [--threshold 0.6]   exit code 1 if a gate fails
"""

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.retrieval.fields import detect_fields
from app.retrieval.hybrid import retrieve
from app.retrieval.index_loader import LoadedIndex, load_index
from app.settings import get_settings
from ingestion.models import Chunk
from ingestion.paths import Paths

SET_PATH = Path(__file__).resolve().parent / "retrieval_set.yaml"
SWEEP = [round(0.30 + 0.05 * i, 2) for i in range(9)]  # 0.30 … 0.70


def matches(chunk: Chunk, gold: str | dict) -> bool:
    if isinstance(gold, str):
        return chunk.chunk_id == gold
    if chunk.doc_id != gold["doc"]:
        return False
    if "section" in gold:
        return gold["section"].lower() in chunk.section.lower()
    return " ".join(gold["text"].lower().split()) in " ".join(chunk.text.lower().split())


@dataclass
class RetrievalScores:
    threshold: float
    recall_at_5: float
    fact_hit_at_1: float
    mrr: float
    leaks: int
    answerable_above: float
    off_topic_below: float
    p95_ms: float
    sweep: list[tuple[float, float, float]]  # (threshold, answerable above, off-topic below)
    lowest_answerable: float = 0.0
    highest_off_topic: float = 0.0
    failures: list[str] = field(default_factory=list)

    def gate_failures(self) -> list[str]:
        gates = []
        if self.recall_at_5 < 0.95:
            gates.append(f"EV-4B-01 Recall@5 {self.recall_at_5:.0%} < 95%")
        if self.fact_hit_at_1 < 0.90:
            gates.append(f"EV-4B-02 fact-card Hit@1 {self.fact_hit_at_1:.0%} < 90%")
        if self.leaks:
            gates.append(f"EV-4B-03 {self.leaks} cross-scheme chunk(s) returned")
        if self.answerable_above < 0.98:
            gates.append(f"EV-4B-04 answerable above threshold {self.answerable_above:.0%} < 98%")
        if self.off_topic_below < 0.90:
            gates.append(f"EV-4B-05 off-topic below threshold {self.off_topic_below:.0%} < 90%")
        if self.p95_ms >= 150:
            gates.append(f"EV-4B-06 retrieval latency p95 {self.p95_ms:.0f} ms ≥ 150 ms")
        return gates


def evaluate(
    index: LoadedIndex, threshold: float | None = None, path: Path = SET_PATH
) -> RetrievalScores:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    threshold = get_settings().min_similarity if threshold is None else threshold
    failures: list[str] = []
    timings: list[float] = []

    def run(case: dict):
        start = time.perf_counter()
        result = retrieve(
            index,
            case["q"],
            scheme_id=case["scheme"],
            fields=detect_fields(case["q"]),
            top_k=5,
            min_similarity=0.0,  # thresholds are applied below, for the sweep
        )
        timings.append((time.perf_counter() - start) * 1000)
        return result

    retrieve(index, "warm up", scheme_id=None)  # first call loads the embedding model

    found = fact_cases = fact_hits = leaks = 0
    reciprocal_ranks: list[float] = []
    answerable_sims: list[float] = []
    for case in data["answerable"]:
        result = run(case)
        answerable_sims.append(result.best_similarity)
        leaks += sum(1 for s in result.chunks if s.chunk.scheme_id != case["scheme"])
        ranks = [
            i for i, s in enumerate(result.chunks) if any(matches(s.chunk, g) for g in case["gold"])
        ]
        found += bool(ranks)
        reciprocal_ranks.append(1 / (ranks[0] + 1) if ranks else 0.0)
        if not ranks:
            failures.append(f"not in top 5: {case['q']!r} got {result.chunk_ids}")
        cards = [g for g in case["gold"] if isinstance(g, str) and g.startswith("fact:")]
        if cards:
            fact_cases += 1
            top = result.chunk_ids[0] if result.chunks else None
            fact_hits += top in cards
            if top not in cards:
                failures.append(f"fact card not first: {case['q']!r} got {top}")

    off_sims = []
    for case in data["off_topic"]:
        result = run(case)
        off_sims.append(result.best_similarity)
        leaks += sum(1 for s in result.chunks if s.chunk.scheme_id != case["scheme"])

    def above(sims: list[float], t: float) -> float:
        return sum(s >= t for s in sims) / len(sims)

    for case, sim in zip(data["answerable"], answerable_sims, strict=True):
        if sim < threshold:
            failures.append(f"answerable below {threshold}: {case['q']!r} ({sim:.3f})")
    for case, sim in zip(data["off_topic"], off_sims, strict=True):
        if sim >= threshold:
            failures.append(f"off-topic above {threshold}: {case['q']!r} ({sim:.3f})")

    timings.sort()
    return RetrievalScores(
        threshold=threshold,
        recall_at_5=found / len(data["answerable"]),
        fact_hit_at_1=fact_hits / max(fact_cases, 1),
        mrr=statistics.fmean(reciprocal_ranks),
        leaks=leaks,
        answerable_above=above(answerable_sims, threshold),
        off_topic_below=1 - above(off_sims, threshold),
        p95_ms=statistics.quantiles(timings, n=20)[-1],
        sweep=[(t, above(answerable_sims, t), 1 - above(off_sims, t)) for t in SWEEP],
        lowest_answerable=min(answerable_sims),
        highest_off_topic=max(off_sims),
        failures=failures,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold", type=float, help="MIN_SIMILARITY to score (default: setting)"
    )
    args = parser.parse_args(argv)

    index = load_index(Paths.default().index)
    s = evaluate(index, args.threshold)
    print(f"index {index.version}, MIN_SIMILARITY {s.threshold}")
    print(f"EV-4B-01 Recall@5                   {s.recall_at_5:.1%}")
    print(f"EV-4B-02 fact-card Hit@1            {s.fact_hit_at_1:.1%}")
    print(f"EV-4B-03 cross-scheme leaks         {s.leaks}")
    print(f"EV-4B-04 answerable above threshold {s.answerable_above:.1%}")
    print(f"EV-4B-05 off-topic below threshold  {s.off_topic_below:.1%}")
    print(f"EV-4B-06 latency p95                {s.p95_ms:.0f} ms")
    print(f"         MRR                        {s.mrr:.3f}")
    print("\n| MIN_SIMILARITY | answerable above | off-topic below |\n|---|---|---|")
    for t, a, o in s.sweep:
        print(f"| {t:.2f} | {a:.1%} | {o:.1%} |")
    gap = (s.lowest_answerable + s.highest_off_topic) / 2
    print(
        f"\nlowest answerable {s.lowest_answerable:.3f}, highest off-topic "
        f"{s.highest_off_topic:.3f}, midpoint {gap:.3f}"
    )
    for failure in s.failures:
        print(f"  - {failure}")
    gates = s.gate_failures()
    print("\n" + ("PASS" if not gates else "FAIL: " + "; ".join(gates)))
    return 1 if gates else 0


if __name__ == "__main__":
    sys.exit(main())
