"""Generation evaluation (eval.md §9.2, EV-4C-03 to EV-4C-09). Calls the real Groq model.

Retrieval is held fixed: each question gets its gold passages from the current index, then goes
through the production prompt, validator, retry and composer. Release runs use --reps 3.

Usage: python -m eval.run_generation [--reps 1] [--limit N]   exit code 1 if a gate fails
"""

import argparse
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.guards.output_validator import count_sentences, numbers_in
from app.llm.client import AnswerClient
from app.llm.groq_json import GroqJSONClient
from app.pipeline import Pipeline
from app.responses.composer import Composer
from app.responses.documents import RegistryDocuments
from app.retrieval.index_loader import load_index
from app.settings import get_settings
from eval.run_retrieval import matches
from ingestion.paths import Paths

SET_PATH = Path(__file__).resolve().parent / "generation_set.yaml"
LABELS_PATH = Path(__file__).resolve().parent / "fact_labels.yaml"


def normalize(text: str) -> str:
    """Lowercase, no currency marks, commas or spaces: "Rs. 1,000/-" → "1000"."""
    text = text.lower().replace("₹", "").replace("/-", "")
    text = re.sub(r"\b(?:rs|inr|re)\b\.?", "", text)
    return re.sub(r"[\s,]+", "", text).replace("–", "-")


def accepted(answer: str, accept: list[str]) -> bool:
    norm = normalize(answer)
    return any(normalize(a) in norm for a in accept)


@dataclass
class GenerationScores:
    runs: int
    value_correct: float
    format_ok: float
    grounded: float
    retry_rate: float
    not_found_on_answerable: float
    truncated_or_empty: int
    mean_tokens: float
    rate_limit_wait_s: float
    p95_latency_s: float
    failures: list[str] = field(default_factory=list)

    def gate_failures(self) -> list[str]:
        gates = []
        if self.value_correct < 0.97:
            gates.append(f"EV-4C-03 correct value {self.value_correct:.1%} < 97%")
        if self.format_ok < 1:
            gates.append(f"EV-4C-04 format compliance {self.format_ok:.1%} < 100%")
        if self.grounded < 1:
            gates.append(f"EV-4C-05 grounding {self.grounded:.1%} < 100%")
        if self.retry_rate > 0.10:
            gates.append(f"EV-4C-06 retry rate {self.retry_rate:.1%} > 10%")
        if self.not_found_on_answerable > 0.03:
            gates.append(
                f"EV-4C-07 not_found on answerable {self.not_found_on_answerable:.1%} > 3%"
            )
        return gates


def run(reps: int = 1, limit: int | None = None, path: Path = SET_PATH) -> GenerationScores:
    settings = get_settings()
    if settings.groq_api_key is None:
        raise SystemExit("GROQ_API_KEY is not set")
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"][:limit]
    labels = {
        f"{lab['scheme_id']}:{lab['field']}": lab
        for lab in yaml.safe_load(LABELS_PATH.read_text(encoding="utf-8"))["labels"]
    }
    index = load_index(Paths.default().index)
    chunks = list(index.chunks.values())

    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        time.sleep(seconds)

    completer = GroqJSONClient(
        settings.groq_api_key.get_secret_value(),
        settings.groq_model,
        fallback_model=settings.groq_fallback_model,
        timeout_s=60,
        rate_limit_retries=6,  # the eval waits out rate limits instead of failing (eval.md §11)
        min_interval_s=2.0,
        sleep=sleep,
    )
    pipeline = Pipeline(
        settings=settings,
        composer=Composer(RegistryDocuments(Paths.default().registry)),
        index=index,
        llm=AnswerClient(
            completer,
            reasoning_effort=settings.llm_reasoning_effort,
            max_tokens=settings.llm_max_tokens,
        ),
    )

    failures: list[str] = []
    valued = correct = answers = fmt_ok = grounded = retried = answerable = not_found = 0
    bad_output = 0
    tokens: list[int] = []
    latencies: list[float] = []
    for rep in range(reps):
        for case in cases:
            gold = []
            for g in case["gold"]:
                gold += [c for c in chunks if matches(c, g) and c not in gold][:1]
            if not gold:
                failures.append(f"no gold chunk in the index: {case['q']!r}")
                continue
            waited_before = sum(waits)
            response, trace = pipeline.answer_from_chunks(case["q"], case["scheme"], gold[:5])
            latencies.append(trace.latency_ms / 1000 - (sum(waits) - waited_before))
            tokens += [u.get("total_tokens", 0) for u in trace.usage]
            retried += trace.llm_attempts > 1
            bad_output += sum(1 for v in trace.validator if v == ["bad_output"])
            tag = f"[rep {rep + 1}] {case['q']!r}"

            want = case.get("expect_type", "answer")
            if want == "answer":
                answerable += 1
                not_found += response.type == "not_found"
            if response.type != want:
                failures.append(f"{tag}: expected {want}, got {response.type} {trace.validator}")
            if response.type == "answer":
                answers += 1
                ok_format = (
                    count_sentences(response.text) <= 3
                    and response.citation is not None
                    and response.last_updated is not None
                )
                fmt_ok += bool(ok_format)
                allowed = set().union(*(numbers_in(c.text) for c in gold)) | numbers_in(case["q"])
                allowed |= {p.lstrip("0") for c in gold for p in (c.doc_as_of or "").split("-")}
                ok_ground = numbers_in(response.text) <= allowed
                grounded += ok_ground
                if not ok_ground:
                    failures.append(f"{tag}: ungrounded numbers in {response.text!r}")
            accept = list(case.get("also_accept", []))
            if "label" in case:
                accept += labels[case["label"]]["accept"]
            if accept and want == "answer":
                valued += 1
                if response.type == "answer" and accepted(response.text, accept):
                    correct += 1
                else:
                    failures.append(f"{tag}: value not matched {accept} in {response.text!r}")

    return GenerationScores(
        runs=reps * len(cases),
        value_correct=correct / max(valued, 1),
        format_ok=fmt_ok / max(answers, 1),
        grounded=grounded / max(answers, 1),
        retry_rate=retried / max(reps * len(cases), 1),
        not_found_on_answerable=not_found / max(answerable, 1),
        truncated_or_empty=bad_output,
        mean_tokens=statistics.fmean(tokens) if tokens else 0.0,
        rate_limit_wait_s=sum(waits),
        p95_latency_s=statistics.quantiles(latencies, n=20)[-1] if len(latencies) > 1 else 0.0,
        failures=failures,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--limit", type=int, help="only the first N cases (smoke run)")
    args = parser.parse_args(argv)

    started = datetime.now(UTC)
    s = run(args.reps, args.limit)
    settings = get_settings()
    print(
        f"model {settings.groq_model}, effort {settings.llm_reasoning_effort}, "
        f"{s.runs} runs, started {started:%Y-%m-%d %H:%M} UTC"
    )
    print(f"EV-4C-03 correct value            {s.value_correct:.1%}")
    print(f"EV-4C-04 format compliance        {s.format_ok:.1%}")
    print(f"EV-4C-05 grounding                {s.grounded:.1%}")
    print(f"EV-4C-06 retry rate               {s.retry_rate:.1%}")
    print(f"EV-4C-07 not_found on answerable  {s.not_found_on_answerable:.1%}")
    print(f"EV-4C-08 truncated or empty       {s.truncated_or_empty}")
    print(f"EV-4C-09 mean tokens per call     {s.mean_tokens:.0f}")
    print(
        f"         pacing + rate-limit waits {s.rate_limit_wait_s:.0f} s (not counted as latency)"
    )
    print(f"         latency p95              {s.p95_latency_s:.1f} s")
    for failure in s.failures:
        print(f"  - {failure}")
    gates = s.gate_failures()
    print("\n" + ("PASS" if not gates else "FAIL: " + "; ".join(gates)))
    return 1 if gates else 0


if __name__ == "__main__":
    sys.exit(main())
