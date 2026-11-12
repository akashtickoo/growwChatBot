"""Chunk audit for the current index (eval.md EV-2-07 to EV-2-10).

Usage: python -m ingestion.audit     exit code 1 if any check fails
"""

import re
import statistics
import sys

from ingestion.build_index import read_chunks, read_current
from ingestion.paths import Paths
from ingestion.registry import Registry

MAX_TOKENS = 600
WORDS_TO_TOKENS = 1.3  # rough: ~300 words ≈ 400 embedding tokens
BANNED = (
    "since inception",
    "value of investment of rs",
    "ideal for",
    "suitable for investors",
    "benchmark returns",
    "past performance",
    "cagr",
)
HEADER = re.compile(r"^\[.+ \| .+ \| .+\]\n")


def audit(paths: Paths) -> list[str]:
    """Returns failure messages; empty means every check passed."""
    current = read_current(paths.index)
    if current is None:
        return ["no index: run python -m ingestion.run"]
    chunks = read_chunks(current)
    with Registry(paths.registry) as registry:
        schemes = {
            c.doc_id: (registry.get_document(c.doc_id) or {}).get("scheme_id") for c in chunks
        }

    failures = []
    untagged = [c.chunk_id for c in chunks if not (c.doc_id and c.source_url and c.doc_type)]
    mismatched = [c.chunk_id for c in chunks if c.scheme_id != schemes[c.doc_id]]
    if untagged or mismatched:
        failures.append(f"EV-2-08: untagged {untagged[:5]}, scheme mismatch {mismatched[:5]}")

    headerless = [c.chunk_id for c in chunks if not HEADER.match(c.text)]
    if headerless:
        failures.append(f"EV-2-09: chunks without context header {headerless[:5]}")

    words = {c.chunk_id: len(c.text.split("\n", 1)[-1].split()) for c in chunks}
    empty = [cid for cid, n in words.items() if n == 0]
    oversized = [
        c.chunk_id
        for c in chunks
        if not c.is_table and words[c.chunk_id] * WORDS_TO_TOKENS > MAX_TOKENS
    ]
    if empty or oversized:
        failures.append(f"EV-2-10: empty {empty[:5]}, over ~{MAX_TOKENS} tokens {oversized[:5]}")

    banned = [(c.chunk_id, b) for c in chunks for b in BANNED if b in c.text.lower()]
    if banned:
        failures.append(f"EV-2-07: banned phrases {banned[:5]}")

    sizes = list(words.values())
    print(
        f"Index {current.name}: {len(chunks)} chunks from {len(schemes)} documents; "
        f"words per chunk min {min(sizes)}, median {statistics.median(sizes):.0f}, "
        f"max {max(sizes)}"
    )
    return failures


def main() -> int:
    failures = audit(Paths.default())
    for failure in failures:
        print(f"FAIL  {failure}")
    if not failures:
        print("PASS  EV-2-07 banned phrases, EV-2-08 tagging, EV-2-09 headers, EV-2-10 sizes")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
