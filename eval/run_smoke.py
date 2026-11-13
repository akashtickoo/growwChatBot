"""Smoke test (eval.md §12, EV-7-04): 10 questions, each with its type, link and footer rules.

Usage:
    python -m eval.run_smoke --local                    in-process, production settings
    python -m eval.run_smoke --base-url http://host:8000   the JSON API

Exit code 1 if any case fails. The private Streamlit app has no public HTTP API, so on Streamlit
Community Cloud the same 10 questions are asked by hand (README, "Deploy").
"""

import argparse
import sys
from pathlib import Path

import httpx
import yaml

from app.models import NO_CITATION_TYPES, ChatResponse
from eval.graders import contains, sentences

SMOKE_SET = Path(__file__).resolve().parent / "smoke_set.yaml"


def check(case: dict, response: ChatResponse) -> list[str]:
    problems = []
    if response.type != case["expected_type"]:
        problems.append(f"type {response.type}, expected {case['expected_type']}")
    accept = case.get("accept", [])
    if accept and not any(contains(response.text, a) for a in accept):
        problems.append(f"none of {accept} in the reply")
    if sentences(response.text) > 3:
        problems.append("more than 3 sentences")
    if response.type in NO_CITATION_TYPES:
        if response.citation is not None:
            problems.append("clarify/greeting must have no link")
    elif response.citation is None or response.last_updated is None:
        problems.append("missing link or footer")
    return problems


def local_asker():
    from app.pipeline import build_pipeline
    from app.retrieval.index_loader import load_index
    from app.settings import get_settings
    from ingestion.paths import Paths

    settings = get_settings()
    pipeline = build_pipeline(settings, load_index(Paths.default().index))
    return pipeline.answer


def api_asker(base_url: str):
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=40)

    def ask(question: str) -> ChatResponse:
        r = client.post("/api/chat", json={"message": question})
        r.raise_for_status()
        return ChatResponse.model_validate(r.json())

    return ask


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", action="store_true")
    target.add_argument("--base-url")
    args = parser.parse_args(argv)

    ask = local_asker() if args.local else api_asker(args.base_url)
    cases = yaml.safe_load(SMOKE_SET.read_text(encoding="utf-8"))["cases"]
    passed = 0
    for case in cases:
        try:
            problems = check(case, ask(case["question"]))
        except Exception as exc:  # unavailable service, HTTP error
            problems = [f"error: {type(exc).__name__}"]
        passed += not problems
        print(f"{case['id']} {'ok  ' if not problems else 'FAIL'} {'; '.join(problems)}")
    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
