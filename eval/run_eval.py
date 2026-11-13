"""System evaluation (eval.md §11): the real pipeline, production settings, separate graders.

Usage:
    python -m eval.run_eval --set golden --reps 1          quick tuning run
    python -m eval.run_eval --set golden --reps 3          release run
    python -m eval.run_eval --set holdout --reps 3         final check only
    python -m eval.run_eval --oracle                       grader check: expected answers pass
    python -m eval.run_eval --null                         grader check: empty answers fail
    python -m eval.run_eval ... --resume data/reports/eval_<stamp>_golden

Writes data/reports/eval_<stamp>_<set>/: results.jsonl (one record per case and rep),
errors.jsonl (infra failures, not scored) and scorecard.md. Exit code 1 if a target fails.
Questions with made-up PII are stored redacted.
"""

import argparse
import json
import logging
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import httpx

from app.llm.client import AnswerClient
from app.llm.groq_json import GroqJSONClient, JSONResult
from app.llm.prompts import SYSTEM_PROMPT
from app.models import ChatResponse, Citation
from app.pipeline import Pipeline, ServiceUnavailable
from app.responses.composer import Composer
from app.responses.documents import RegistryDocuments
from app.retrieval.index_loader import LoadedIndex, load_index
from app.settings import get_settings
from eval.cases import GOLDEN, HOLDOUT, Case, load
from eval.check_sets import problems as set_problems
from eval.graders import CitationChecker, Grades, contains, grade
from eval.judge import AdviceJudge, JudgeError, make_judge
from ingestion.models import Chunk
from ingestion.paths import Paths

TOKENS_PER_MINUTE = 7200  # stay under the key's 8,000 so the eval doesn't hit 429s
EXPECTED_TOKENS = 1900  # per answered question, measured in Phase 4C/5


# --- instrumentation ------------------------------------------------------------------------


class RecordingCompleter:
    """Wraps the Groq client and records every call (the per-case LLM call count)."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    def __call__(self, messages, schema, *, name, reasoning_effort, max_tokens) -> JSONResult:
        result = self.inner(
            messages, schema, name=name, reasoning_effort=reasoning_effort, max_tokens=max_tokens
        )
        self.calls.append(
            {
                "model": result.model,
                "finish_reason": result.finish_reason,
                "tokens": (result.usage or {}).get("total_tokens", 0),
                "waited_s": result.waited_s,
            }
        )
        return result


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class TokenPacer:
    """Waits (outside the timed request) so tokens in the last minute stay under the budget."""

    def __init__(self, per_minute: int = TOKENS_PER_MINUTE):
        self.per_minute = per_minute
        self.spent: deque[tuple[float, int]] = deque()
        self.waited_s = 0.0

    def wait(self, upcoming: int = EXPECTED_TOKENS) -> None:
        while True:
            now = time.monotonic()
            while self.spent and now - self.spent[0][0] > 60:
                self.spent.popleft()
            if sum(t for _, t in self.spent) + upcoming <= self.per_minute:
                return
            pause = 60 - (now - self.spent[0][0]) + 0.5
            self.waited_s += pause
            time.sleep(pause)

    def add(self, tokens: int) -> None:
        if tokens:
            self.spent.append((time.monotonic(), tokens))


# --- environment ----------------------------------------------------------------------------


@dataclass
class Env:
    pipeline: Pipeline
    index: LoadedIndex
    recorder: RecordingCompleter
    url_to_doc: dict[str, str]
    doc_as_of: dict[str, str | None]
    capture: LogCapture


def build_env() -> Env:
    settings = get_settings()
    if settings.groq_api_key is None:
        raise SystemExit("GROQ_API_KEY is not set")
    paths = Paths.default()
    index = load_index(paths.index)
    recorder = RecordingCompleter(
        GroqJSONClient(
            settings.groq_api_key.get_secret_value(),
            settings.groq_model,
            fallback_model=settings.groq_fallback_model,
            timeout_s=60,
            rate_limit_retries=6,
        )
    )
    pipeline = Pipeline(
        settings=settings,
        composer=Composer(RegistryDocuments(paths.registry), timezone=settings.display_timezone),
        index=index,
        llm=AnswerClient(
            recorder,
            reasoning_effort=settings.llm_reasoning_effort,
            max_tokens=settings.llm_max_tokens,
        ),
    )
    with sqlite3.connect(paths.registry) as conn:
        rows = conn.execute("SELECT doc_id, url, doc_as_of FROM documents").fetchall()
    capture = LogCapture()
    requests_log = logging.getLogger("app.requests")
    requests_log.addHandler(capture)
    requests_log.setLevel(logging.INFO)
    requests_log.propagate = False
    return Env(
        pipeline,
        index,
        recorder,
        {url: doc_id for doc_id, url, _ in rows},
        {doc_id: as_of for doc_id, _, as_of in rows},
        capture,
    )


# --- one case -------------------------------------------------------------------------------


@dataclass
class Outcome:
    record: dict
    grades: Grades | None = None
    error: dict | None = None
    model_dependent: bool = False


def run_case(case: Case, rep: int, env: Env, checker, judge, pacer) -> Outcome:
    env.recorder.calls.clear()
    env.capture.lines.clear()
    base = {
        "case_id": case.id,
        "group": case.group,
        "rep": rep,
        "question": case.redacted_question(),
    }
    pacer.wait()
    started = time.perf_counter()
    try:
        response, trace = env.pipeline.run(case.question)
    except ServiceUnavailable as exc:
        return Outcome(base, error={**base, "type": "service_unavailable", "reason": exc.reason})
    elapsed_ms = (time.perf_counter() - started) * 1000
    calls = list(env.recorder.calls)
    tokens = sum(c["tokens"] for c in calls)
    pacer.add(tokens)
    waited = sum(c["waited_s"] for c in calls)
    logged = "\n".join(env.capture.lines)

    cited_doc = env.url_to_doc.get(response.citation.url) if response.citation else None
    cited_chunk = env.index.chunks.get(trace.source_id) if trace.source_id else None
    grades = grade(
        case,
        response,
        cited_doc_id=cited_doc,
        cited_chunk=cited_chunk,
        doc_as_of=env.doc_as_of.get(cited_doc or ""),
        llm_calls=len(calls),
        logged_text=logged,
    )
    error = None
    if checker is not None and response.citation is not None:
        ok, note = checker.check(case, response)
        grades.set("citation_valid", ok, note)
        if ok is None and note.startswith("BLOCKED"):
            error = {**base, "type": "citation_blocked", "detail": note}
    if judge is not None and response.type == "answer":
        try:
            verdict = judge(response.text)
            grades.set("judge_no_advice", not verdict.advice, f"judge: {verdict.reason}")
        except JudgeError as exc:
            error = {**base, "type": "judge_unavailable", "detail": str(exc)}

    status = "ok"
    if trace.validator and all(v == ["bad_output"] for v in trace.validator):
        status = "truncated"
    record = {
        **base,
        "status": status,
        "type": response.type,
        "text": response.text
        if not case.pii_value
        else response.text.replace(case.pii_value, "[PII]"),
        "citation": response.citation.url if response.citation else None,
        "cited_doc": cited_doc,
        "source_id": trace.source_id,
        "last_updated": response.last_updated.isoformat() if response.last_updated else None,
        "chunk_ids": trace.chunk_ids,
        "best_similarity": trace.best_similarity,
        "llm_status": trace.llm_status,
        "attempts": trace.llm_attempts,
        "validator": trace.validator,
        "llm_calls": len(calls),
        "served_models": sorted({c["model"] for c in calls}),
        "finish_reasons": [c["finish_reason"] for c in calls],
        "tokens": tokens,
        "latency_ms": round(elapsed_ms - waited * 1000, 1),
        "rate_limit_wait_s": round(waited, 2),
        "checks": grades.checks,
        "notes": grades.notes,
    }
    return Outcome(record, grades, error, model_dependent=bool(calls) or trace.llm_attempts > 0)


# --- oracle and null runs -------------------------------------------------------------------


def synthetic(case: Case, env: Env, mode: str) -> tuple[ChatResponse, Chunk | None, str | None]:
    if mode == "null":
        return (
            ChatResponse(
                type="answer",
                text="I don't know.",
                citation=None,
                last_updated=None,
                request_id="n",
            ),
            None,
            None,
        )
    kind = case.expected_type[0]
    values = case.accept_all or case.accept[:1]
    text = " ".join(f"It is {v}." for v in values) if values else "This is the reply."
    if kind == "answer" and len(values) > 3:
        text = "It is " + ", ".join(values) + "."
    citation, cited_doc, chunk = None, None, None
    if kind not in ("clarify", "greeting"):
        if case.expected_citation_url:
            citation = Citation(url=case.expected_citation_url, title="Education link")
        else:
            cited_doc = (case.expected_citation or ["hdfc-mid-cap-page"])[0]
            url = next(
                (u for u, d in env.url_to_doc.items() if d == cited_doc),
                "https://www.hdfcfund.com/",
            )
            citation = Citation(url=url, title=cited_doc)
    if kind == "answer":
        chunk = next(
            (
                c
                for c in env.index.chunks.values()
                if c.doc_id == cited_doc and all(contains(c.text, v) for v in values)
            ),
            Chunk("oracle", cited_doc or "", None, "SCHEME_PAGE", "oracle", text, ""),
        )
    response = ChatResponse(
        type=kind,
        text=text,
        citation=citation,
        last_updated=date.today() if citation else None,
        request_id="o",
    )
    return response, chunk, cited_doc


def run_synthetic(cases: list[Case], env: Env, checker, mode: str) -> list[Outcome]:
    outcomes = []
    for case in cases:
        response, chunk, cited_doc = synthetic(case, env, mode)
        grades = grade(
            case,
            response,
            cited_doc_id=cited_doc,
            cited_chunk=chunk,
            doc_as_of=None,
            llm_calls=0,
            logged_text="",
        )
        if checker is not None and response.citation is not None:
            ok, note = checker.check(case, response)
            grades.set("citation_valid", ok, note)
        record = {
            "case_id": case.id,
            "group": case.group,
            "rep": 1,
            "type": response.type,
            "checks": grades.checks,
            "notes": grades.notes,
        }
        outcomes.append(Outcome(record, grades))
    return outcomes


# --- scoring --------------------------------------------------------------------------------


TARGETS = [
    # (metric, check name or None, target description, fail_on_any / threshold)
    ("Factual accuracy", "value_correct", "≥ 95%", 0.95),
    ("Grounding", "grounded", "100%", 1.0),
    ("Citation correctness", "citation_correct", "100%", 1.0),
    ("Citation validity", "citation_valid", "100% (BLOCKED excluded)", 1.0),
    ("Format compliance", "format_ok", "100%", 1.0),
    ("Advisory refusal recall", "refused", "100%", 1.0),
    ("Not-covered misroutes", "not_misrouted", "0", 1.0),
    ("PII forwarded or logged", "pii_not_forwarded", "0", 1.0),
    ("Type correct", "type_correct", "reported", None),
]


@dataclass
class Score:
    rows: list[tuple[str, str, str, str]] = field(
        default_factory=list
    )  # metric, value, target, verdict
    failed: list[str] = field(default_factory=list)


def rate(records: list[dict], check: str) -> tuple[float | None, int, int]:
    vals = [r["checks"].get(check) for r in records if r["checks"].get(check) is not None]
    if not vals:
        return None, 0, 0
    return sum(vals) / len(vals), sum(vals), len(vals)


def score(records: list[dict], errors: list[dict], attempts: int, reps: int) -> Score:
    s = Score()
    for metric, check, target, threshold in TARGETS:
        value, ok, n = rate(records, check)
        if value is None:
            s.rows.append((metric, "n/a", target, "n/a"))
            continue
        shown = f"{value:.1%} ({ok}/{n})"
        if metric in ("Not-covered misroutes", "PII forwarded or logged"):
            shown = f"{n - ok} of {n}"
        verdict = "info" if threshold is None else ("PASS" if value >= threshold else "FAIL")
        if verdict == "FAIL":
            s.failed.append(metric)
        s.rows.append((metric, shown, target, verdict))

    # Advice in any response: phrase check or judge.
    advice = [
        r
        for r in records
        if r["checks"].get("no_advice_phrases") is False
        or r["checks"].get("judge_no_advice") is False
    ]
    judged = sum(1 for r in records if r["checks"].get("judge_no_advice") is not None)
    verdict = "PASS" if not advice else "FAIL"
    s.rows.append(
        ("Advice in any response", f"{len(advice)} (judge ran on {judged})", "0", verdict)
    )
    if advice:
        s.failed.append("Advice in any response")

    # False refusals on factual questions.
    _, ok, n = rate(records, "not_falsely_refused")
    false_refusals = (n - ok) / n if n else 0.0
    verdict = "PASS" if false_refusals <= 0.05 else "FAIL"
    s.rows.append(("False refusals", f"{false_refusals:.1%} ({n - ok}/{n})", "≤ 5%", verdict))
    if verdict == "FAIL":
        s.failed.append("False refusals")

    # Consistency across reps (model-dependent cases passing in every rep).
    if reps > 1:
        by_case: dict[str, list[bool]] = defaultdict(list)
        for r in records:
            if r.get("llm_calls"):
                by_case[r["case_id"]].append(not [k for k, v in r["checks"].items() if v is False])
        consistent = [all(v) for v in by_case.values() if len(v) == reps]
        value = sum(consistent) / len(consistent) if consistent else 1.0
        verdict = "PASS" if value >= 0.95 else "FAIL"
        s.rows.append(
            (
                "Consistency (all reps pass)",
                f"{value:.1%} ({sum(consistent)}/{len(consistent)})",
                "≥ 95%",
                verdict,
            )
        )
        if verdict == "FAIL":
            s.failed.append("Consistency")

    infra = len([e for e in errors if e["type"] == "service_unavailable"])
    infra_rate = infra / attempts if attempts else 0.0
    s.rows.append(
        (
            "Infra error rate",
            f"{infra_rate:.1%} ({infra}/{attempts})",
            "≤ 2% (run validity)",
            "PASS" if infra_rate <= 0.02 else "INCOMPLETE",
        )
    )

    latencies = sorted(r["latency_ms"] for r in records if "latency_ms" in r)
    if len(latencies) > 1:
        p95 = statistics.quantiles(latencies, n=20)[-1] / 1000
        verdict = "PASS" if p95 <= 5 else "FAIL"
        s.rows.append(
            (
                "Latency p95",
                f"{p95:.2f} s (p50 {statistics.median(latencies) / 1000:.2f} s)",
                "≤ 5 s",
                verdict,
            )
        )
        if verdict == "FAIL":
            s.failed.append("Latency")
    answered = [r["tokens"] for r in records if r.get("type") == "answer" and r.get("tokens")]
    if answered:
        s.rows.append(
            (
                "Tokens per answered question",
                f"{statistics.fmean(answered):.0f}",
                "reported",
                "info",
            )
        )
    return s


# --- reports --------------------------------------------------------------------------------


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def previous_results(out_dir: Path, set_name: str) -> dict[str, bool]:
    runs = sorted(
        p
        for p in out_dir.parent.glob(f"eval_*_{set_name}")
        if p != out_dir and (p / "results.jsonl").exists()
    )
    if not runs:
        return {}
    passed: dict[str, bool] = {}
    for line in (runs[-1] / "results.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        ok = not [k for k, v in r["checks"].items() if v is False]
        passed[r["case_id"]] = passed.get(r["case_id"], True) and ok
    return passed


def write_scorecard(out_dir: Path, header: dict, s: Score, records, errors, previous) -> Path:
    lines = [f"# Eval scorecard: {header['set']} set", ""]
    lines += [f"- **{k}:** {v}" for k, v in header.items()]
    lines += ["", "## Targets", "", "| Metric | Value | Target | Result |", "|---|---|---|---|"]
    lines += [f"| {m} | {v} | {t} | {r} |" for m, v, t, r in s.rows]
    lines += ["", "## Failures", ""]
    failures = [(r, k) for r in records for k, v in r["checks"].items() if v is False]
    if not failures:
        lines.append("None.")
    else:
        lines += ["| Case | Rep | Check | Detail | Response |", "|---|---|---|---|---|"]
        for r, k in failures:
            detail = r.get("notes", {}).get(k, "").replace("|", "/")[:160]
            text = (r.get("text") or "").replace("|", "/").replace("\n", " ")[:120]
            lines.append(f"| {r['case_id']} | {r['rep']} | {k} | {detail} | [{r['type']}] {text} |")
    lines += ["", "## Errors and blocked cases", ""]
    lines += [
        f"- {e['case_id']} rep {e['rep']}: {e['type']} {e.get('reason', e.get('detail', ''))}"
        for e in errors
    ] or ["None."]
    now = {r["case_id"]: not [k for k, v in r["checks"].items() if v is False] for r in records}
    if previous:
        newly_pass = sorted(c for c, ok in now.items() if ok and previous.get(c) is False)
        newly_fail = sorted(c for c, ok in now.items() if not ok and previous.get(c) is True)
        lines += [
            "",
            "## Change vs previous run",
            "",
            f"- Newly passing: {', '.join(newly_pass) or 'none'}",
            f"- Newly failing: {', '.join(newly_fail) or 'none'}",
        ]
    path = out_dir / "scorecard.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- main -----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", choices=["golden", "holdout"], default="golden")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--limit", type=int, help="first N cases only (smoke run)")
    parser.add_argument("--only", nargs="+", help="case ids to run")
    parser.add_argument(
        "--oracle", action="store_true", help="grade expected answers (grader check)"
    )
    parser.add_argument("--null", action="store_true", help="grade empty answers (grader check)")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--no-citations", action="store_true", help="skip the live citation check")
    parser.add_argument("--resume", type=Path, help="continue a run directory")
    parser.add_argument(
        "--score-only", action="store_true", help="score --resume results, no new calls"
    )
    args = parser.parse_args(argv)

    if issues := set_problems():
        print("dataset checks failed:\n  " + "\n  ".join(issues))
        return 1
    cases = load(GOLDEN if args.set == "golden" else HOLDOUT)
    if args.only:
        cases = [c for c in cases if c.id in set(args.only)]
    cases = cases[: args.limit]
    settings = get_settings()
    env = build_env()
    http = None if args.no_citations else httpx.Client(headers={"User-Agent": "faq-eval/0.1"})
    checker = CitationChecker(http) if http else None

    mode = "oracle" if args.oracle else "null" if args.null else "pipeline"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (
        args.resume
        or Paths.default().reports
        / f"eval_{stamp}_{args.set}{'' if mode == 'pipeline' else '_' + mode}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    if mode != "pipeline":
        outcomes = run_synthetic(cases, env, checker, mode)
        records = [o.record for o in outcomes]
        passing = sum(1 for r in records if not [k for k, v in r["checks"].items() if v is False])
        print(f"{mode} run: {passing}/{len(records)} cases pass every check")
        for r in records:
            bad = [k for k, v in r["checks"].items() if v is False]
            if (mode == "oracle" and bad) or (mode == "null" and not bad):
                print(f"  - {r['case_id']}: {bad or 'passes everything'} {r.get('notes', {})}")
        (out_dir / "results.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
        )
        return 0

    if args.score_only and not args.resume:
        parser.error("--score-only needs --resume <run directory>")
    judge: AdviceJudge | None = None if args.no_judge or args.score_only else make_judge()
    results_path, errors_path = out_dir / "results.jsonl", out_dir / "errors.jsonl"
    done = set()
    if results_path.exists():
        done = {
            (json.loads(line)["case_id"], json.loads(line)["rep"])
            for line in results_path.read_text(encoding="utf-8").splitlines()
            if line
        }
    deterministic: set[str] = set()
    if args.resume and results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("llm_calls") and not r.get("attempts"):
                deterministic.add(r["case_id"])
    pacer = TokenPacer()
    attempts = 0
    started = time.monotonic()
    with (
        results_path.open("a", encoding="utf-8") as results,
        errors_path.open("a", encoding="utf-8") as errs,
    ):
        for rep in range(1, args.reps + 1):
            for case in cases:
                if (
                    args.score_only
                    or (case.id, rep) in done
                    or (rep > 1 and case.id in deterministic)
                ):
                    continue
                attempts += 1
                outcome = run_case(case, rep, env, checker, judge, pacer)
                if outcome.error:
                    errs.write(json.dumps(outcome.error, ensure_ascii=False) + "\n")
                    errs.flush()
                if outcome.grades is not None:
                    results.write(json.dumps(outcome.record, ensure_ascii=False) + "\n")
                    results.flush()
                    if not outcome.model_dependent:
                        deterministic.add(case.id)
                    bad = outcome.grades.failed
                    verdict = "ok" if not bad else "FAIL " + ",".join(bad)
                    kind = outcome.record["type"]
                    print(f"[rep {rep}] {case.id:28s} {kind:20s} {verdict}", flush=True)
                else:
                    print(f"[rep {rep}] {case.id:28s} ERROR {outcome.error['reason']}", flush=True)

    records = [
        json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line
    ]
    errors = [
        json.loads(line) for line in errors_path.read_text(encoding="utf-8").splitlines() if line
    ]
    s = score(records, errors, attempts or len(records), args.reps)
    header = {
        "set": args.set,
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "git commit": git_commit(),
        "index": env.index.version,
        "model requested": settings.groq_model,
        "models served": ", ".join(sorted({m for r in records for m in r.get("served_models", [])}))
        or "none",
        "reasoning effort": settings.llm_reasoning_effort,
        "MIN_SIMILARITY": settings.min_similarity,
        "prompt hash": sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12],
        "set hash": sha256((GOLDEN if args.set == "golden" else HOLDOUT).read_bytes()).hexdigest()[
            :12
        ],
        "reps": args.reps,
        "cases": len(cases),
        "judge": f"{settings.judge_model} ({judge.calls} calls)" if judge else "off",
        "pacing waits": f"{pacer.waited_s:.0f} s (not counted as latency)",
        "wall time": f"{(time.monotonic() - started) / 60:.1f} min",
    }
    path = write_scorecard(out_dir, header, s, records, errors, previous_results(out_dir, args.set))
    for metric, value, target, verdict in s.rows:
        print(f"{metric:32s} {value:28s} {target:24s} {verdict}")
    print(f"\nScorecard: {path}")
    return 1 if s.failed else 0


if __name__ == "__main__":
    sys.exit(main())
