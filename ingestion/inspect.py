"""Print chunks from the current index so they can be checked by eye.

Usage:
    python -m ingestion.inspect --scheme hdfc-mid-cap
    python -m ingestion.inspect --scheme hdfc-mid-cap --doc-type KIM --grep "Minimum Application"
    python -m ingestion.inspect --general            statement pages (no scheme)
"""

import argparse
import sys

from ingestion.build_index import read_build_info, read_chunks, read_current
from ingestion.paths import Paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show chunks from the current index")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scheme", help="scheme id, e.g. hdfc-mid-cap")
    group.add_argument("--general", action="store_true", help="chunks with no scheme")
    parser.add_argument("--doc-type", help="SCHEME_PAGE, KIM, SERVICE_PAGE …")
    parser.add_argument("--grep", help="only chunks containing this text (case-insensitive)")
    parser.add_argument("--full", action="store_true", help="print whole chunks")
    args = parser.parse_args(argv)

    current = read_current(Paths.default().index)
    if current is None:
        print("No index yet. Run: python -m ingestion.run", file=sys.stderr)
        return 1
    info = read_build_info(current)
    print(f"Index {info['version']} built {info['built_at']}, {info['chunk_count']} chunks\n")

    wanted_scheme = None if args.general else args.scheme
    shown = 0
    for chunk in read_chunks(current):
        if chunk.scheme_id != wanted_scheme:
            continue
        if args.doc_type and chunk.doc_type != args.doc_type:
            continue
        if args.grep and args.grep.lower() not in chunk.text.lower():
            continue
        body = chunk.text
        if not args.full and len(body) > 400:
            body = body[:400] + "…"
        print(f"--- {chunk.chunk_id}  ({chunk.doc_type}, page {chunk.page})\n{body}\n")
        shown += 1
    print(f"{shown} chunks shown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
