"""Developer CLI: run one question through the full pipeline.

Usage: python -m app.cli ask "What is the expense ratio of HDFC Mid Cap Fund?"

Prints the response as the UI would show it, then the debug details: intent, scheme, retrieved
chunk ids, validator results per attempt, model and tokens.
"""

import argparse
import json
import logging
import sys

from app.models import footer
from app.pipeline import ServiceUnavailable, build_pipeline
from app.retrieval.index_loader import IndexUnavailable, load_index
from app.settings import get_settings
from ingestion.paths import Paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="answer one question")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true", help="print the raw ChatResponse JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    try:
        index = load_index(Paths.default().index)
    except IndexUnavailable as exc:
        print(f"(index not loaded: {exc})", file=sys.stderr)
        index = None
    pipeline = build_pipeline(settings, index)

    try:
        response, trace = pipeline.run(args.question)
    except ServiceUnavailable as exc:
        print(f"503: service unavailable ({exc.reason})")
        return 2

    if args.json:
        print(response.model_dump_json(indent=2))
    else:
        print(f"[{response.type}] {response.text}")
        if response.citation:
            print(f"\nSource: {response.citation.url}")
        if response.last_updated:
            print(footer(response.last_updated))
    debug = {
        "intent": f"{trace.intent} ({trace.intent_rule})",
        "resolver": trace.resolver,
        "scheme_id": trace.scheme_id,
        "fields": trace.fields,
        "chunk_ids": trace.chunk_ids,
        "best_similarity": trace.best_similarity,
        "llm_status": trace.llm_status,
        "attempts": trace.llm_attempts,
        "failed_checks_per_attempt": trace.validator,
        "model": trace.model,
        "tokens": [u.get("total_tokens") for u in trace.usage],
        "latency_ms": trace.latency_ms,
    }
    print("\n--- debug ---\n" + json.dumps(debug, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
