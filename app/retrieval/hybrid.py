"""Hybrid retrieval: dense + BM25, fused with Reciprocal Rank Fusion (architecture §6.5).

1. Dense top 10 by cosine similarity, filtered by scheme (statement pages for general questions).
2. BM25 top 10 over the same scope.
3. RRF with k = 60.
4. When the question names a fact-card field, that scheme's card for the field is added and
   ranked first (EC-RET-02). Cards for fields the question didn't name take at most one slot:
   they are short, so they score well against any question about the scheme and would
   otherwise crowd out the passage that answers it.
5. Top `top_k` (5) chunks are returned.
6. `relevant` is False when the best dense similarity is below `MIN_SIMILARITY`; the pipeline
   then answers `not_found` without calling the LLM (EC-RET-01).

The dense query has the scheme's name replaced by "the fund". Every chunk in a scheme's scope
carries that name, so leaving it in lifts the similarity of any question that mentions it,
including off-topic ones ("Which colour is the HDFC Mid Cap logo?"), and the threshold could
no longer tell them apart.

The scope filter is applied in both searches, so one scheme's question can never return another
scheme's chunk (EC-RET-03).
"""

import re
from dataclasses import dataclass, field

from app.retrieval.index_loader import LoadedIndex, tokens
from app.retrieval.scheme_resolver import normalize, scheme_names
from ingestion.models import Chunk

CANDIDATES = 10
RRF_K = 60
# Larger than any fused score (at most 2 / (RRF_K + 1)), so a matching card always ranks first.
FACT_CARD_BOOST = 1.0
MAX_UNREQUESTED_CARDS = 1


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float  # fused score (with boost)
    similarity: float | None  # dense cosine similarity, None if only found by BM25 or boost


@dataclass
class RetrievalResult:
    chunks: list[ScoredChunk] = field(default_factory=list)
    best_similarity: float = 0.0
    relevant: bool = False

    @property
    def chunk_ids(self) -> list[str]:
        return [s.chunk.chunk_id for s in self.chunks]


def topic_query(question: str, scheme_id: str | None) -> str:
    """The question with the scheme's name replaced by "the fund" (unchanged if none given)."""
    if not scheme_id:
        return question
    text = normalize(question)
    for name in scheme_names(scheme_id):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?: fund)?(?!\w)", " the fund ", text)
    return " ".join(text.split())


def _dense(index: LoadedIndex, query: str, scope: str, n: int) -> list[tuple[str, float]]:
    result = index.collection.query(
        query_embeddings=[index.embed_query(query)],
        n_results=n,
        where={"scheme_id": scope},
        include=["distances"],
    )
    ids, distances = result["ids"][0], result["distances"][0]
    return [(cid, 1.0 - dist) for cid, dist in zip(ids, distances, strict=True)]


def _bm25(index: LoadedIndex, question: str, scope: str, n: int) -> list[str]:
    entry = index.scopes[scope]
    query = tokens(question)
    if entry.bm25 is None or not query:
        return []
    scores = entry.bm25.get_scores(query)
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [entry.chunk_ids[i] for i in ranked[:n] if scores[i] > 0]


def retrieve(
    index: LoadedIndex,
    question: str,
    *,
    scheme_id: str | None,
    fields: list[str] | None = None,
    top_k: int = 5,
    min_similarity: float = 0.63,
) -> RetrievalResult:
    """Search one scheme's chunks, or the statement pages when `scheme_id` is None."""
    scope = index.scope_of(scheme_id)
    if scope not in index.scopes:
        return RetrievalResult()
    n = min(CANDIDATES, len(index.scopes[scope].chunk_ids))

    dense = _dense(index, topic_query(question, scheme_id), scope, n)
    similarity = dict(dense)
    fused: dict[str, float] = {}
    for ranking in ([cid for cid, _ in dense], _bm25(index, question, scope, n)):
        for rank, cid in enumerate(ranking):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

    requested = {f"fact:{scheme_id}:{f}" for f in fields or []} if scheme_id else set()
    for card_id in requested & index.chunks.keys():
        fused[card_id] = fused.get(card_id, 0.0) + FACT_CARD_BOOST

    ranked, unrequested_cards = [], 0
    for cid in sorted(fused, key=lambda c: fused[c], reverse=True):
        if index.chunks[cid].doc_type == "FACT_CARD" and cid not in requested:
            if unrequested_cards >= MAX_UNREQUESTED_CARDS:
                continue
            unrequested_cards += 1
        ranked.append(cid)
        if len(ranked) == top_k:
            break

    best = max(similarity.values(), default=0.0)
    return RetrievalResult(
        chunks=[ScoredChunk(index.chunks[c], fused[c], similarity.get(c)) for c in ranked],
        best_similarity=best,
        relevant=best >= min_similarity,
    )
