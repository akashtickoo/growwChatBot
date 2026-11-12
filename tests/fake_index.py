"""A small synthetic index for retrieval and pipeline tests. All text is made up.

The fake embedder is a hashed bag of words, so similarity means shared words: deterministic, no
model download, and an unrelated question really scores near zero.
"""

import hashlib
import math
from datetime import UTC, datetime

from app.retrieval.index_loader import tokens
from ingestion.embed import QUERY_PREFIX
from ingestion.models import Chunk

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
DIM = 256
MID, SMALL = "hdfc-mid-cap", "hdfc-small-cap"


def bow_embedder(texts):
    vectors = []
    for text in texts:
        vec = [0.0] * DIM
        for tok in tokens(text.removeprefix(QUERY_PREFIX)):
            vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        vectors.append([x / norm for x in vec])
    return vectors


def make_chunk(cid, scheme, doc_type, section, body, doc=None, doc_as_of=None):
    doc = doc or cid.split("#")[0]
    return Chunk(
        chunk_id=cid,
        doc_id=doc,
        scheme_id=scheme,
        doc_type=doc_type,
        section=section,
        text=f"[{scheme or 'General'} | {doc_type} | {section}]\n{body}",
        source_url=f"https://www.hdfcfund.com/{doc}",
        doc_as_of=doc_as_of,
    )


CHUNKS = [
    make_chunk(
        f"{MID}-page#000", MID, "SCHEME_PAGE", "Key facts", "expense ratio 0.50 minimum sip 100"
    ),
    make_chunk(
        f"{MID}-page#001",
        MID,
        "SCHEME_PAGE",
        "Exit Load",
        "exit load 1 percent within one year redemption",
    ),
    make_chunk(
        f"{MID}-page#002", MID, "SCHEME_PAGE", "Fund Managers", "managers named alpha and beta"
    ),
    make_chunk(
        f"{MID}-kim#000",
        MID,
        "KIM",
        "Page 2",
        "investment objective long term capital appreciation midsize companies",
        doc_as_of="2025-11-21",
    ),
    make_chunk(
        f"{MID}-kim#001",
        MID,
        "KIM",
        "Page 3",
        "asset allocation pattern equity instruments debt instruments",
        doc_as_of="2025-11-21",
    ),
    make_chunk(
        f"fact:{MID}:exit_load",
        MID,
        "FACT_CARD",
        "Exit load",
        "exit load 1 percent",
        doc=f"{MID}-page",
    ),
    make_chunk(
        f"fact:{MID}:expense_ratio",
        MID,
        "FACT_CARD",
        "Expense ratio",
        "expense ratio 0.50",
        doc=f"{MID}-page",
    ),
    make_chunk(
        f"fact:{MID}:min_sip", MID, "FACT_CARD", "Minimum SIP", "minimum sip 100", doc=f"{MID}-page"
    ),
    make_chunk(
        f"{SMALL}-page#000",
        SMALL,
        "SCHEME_PAGE",
        "Exit Load",
        "exit load 1 percent within one year redemption",
    ),
    make_chunk(
        f"fact:{SMALL}:exit_load",
        SMALL,
        "FACT_CARD",
        "Exit load",
        "exit load 1 percent",
        doc=f"{SMALL}-page",
    ),
    make_chunk(
        "stmt#000",
        None,
        "SERVICE_PAGE",
        "Capital gain statement",
        "download capital gain statement email pan",
    ),
    make_chunk(
        "stmt#001",
        None,
        "SERVICE_PAGE",
        "Account statement",
        "account statement folio transactions download",
    ),
]
