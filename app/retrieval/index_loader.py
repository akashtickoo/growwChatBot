"""Load the index named by the `current` pointer (architecture §5.5, §6.5).

Loaded once at startup. A newer index built while the server runs is picked up on restart
(EC-RET-08). A missing or broken index raises `IndexUnavailable`; the app then reports
`index_loaded: false` on /healthz and factual questions get a 503 (EC-RET-07).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from rank_bm25 import BM25Okapi

from ingestion.build_index import COLLECTION, read_build_info, read_chunks, read_current
from ingestion.embed import MODEL_NAME, QUERY_PREFIX, Embedder, SentenceTransformerEmbedder
from ingestion.models import Chunk

GENERAL = ""  # scope key (and ChromaDB scheme_id value) for chunks with no scheme

# Common words carry no signal for BM25 over such a small, single-topic corpus.
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it its me my of on or the this "
    "to was what when where which who will with you your fund scheme hdfc".split()
)


class IndexUnavailable(RuntimeError):
    """No usable index: pointer missing, version folder missing, or files unreadable."""


_SUFFIXES = ("ings", "ing", "ers", "er", "es", "ed", "s")


def _stem(word: str) -> str:
    """Crude suffix stripping so "manages" and "managers" meet. Same function on both sides."""
    if word.isdigit():
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOPWORDS]


@dataclass
class _Scope:
    chunk_ids: list[str]
    bm25: BM25Okapi | None  # None when the scope has no indexable text


@dataclass
class LoadedIndex:
    version: str
    built_at: str
    embedding_model: str
    chunks: dict[str, Chunk]
    collection: object  # chromadb Collection
    embedder: Embedder
    scopes: dict[str, _Scope] = field(default_factory=dict)

    def scope_of(self, scheme_id: str | None) -> str:
        return scheme_id or GENERAL

    def embed_query(self, question: str) -> list[float]:
        return self.embedder([QUERY_PREFIX + question])[0]


def _build_scopes(chunks: list[Chunk]) -> dict[str, _Scope]:
    by_scope: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_scope.setdefault(chunk.scheme_id or GENERAL, []).append(chunk)
    scopes = {}
    for key, members in by_scope.items():
        corpus = [tokens(c.text) for c in members]
        bm25 = BM25Okapi(corpus) if any(corpus) else None
        scopes[key] = _Scope([c.chunk_id for c in members], bm25)
    return scopes


def load_index(index_dir: Path, embedder: Embedder | None = None) -> LoadedIndex:
    """Open the current index version. `embedder` defaults to the bge model used at build."""
    import chromadb
    from chromadb.config import Settings

    version_dir = read_current(index_dir)
    if version_dir is None:
        raise IndexUnavailable(f"no current index in {index_dir}")
    try:
        info = read_build_info(version_dir)
        chunks = read_chunks(version_dir)
        client = chromadb.PersistentClient(
            path=str(version_dir / "chroma"), settings=Settings(anonymized_telemetry=False)
        )
        collection = client.get_collection(COLLECTION, embedding_function=None)
        stored = collection.count()
    except Exception as exc:  # any unreadable file means the index can't be served
        raise IndexUnavailable(f"index {version_dir.name} is unreadable: {exc}") from exc
    if not chunks or stored != len(chunks):
        raise IndexUnavailable(
            f"index {version_dir.name} is inconsistent: {len(chunks)} chunks, {stored} vectors"
        )

    model_name = info.get("embedding_model", MODEL_NAME)
    return LoadedIndex(
        version=info.get("version", version_dir.name),
        built_at=info.get("built_at", ""),
        embedding_model=model_name,
        chunks={c.chunk_id: c for c in chunks},
        collection=collection,
        embedder=embedder or SentenceTransformerEmbedder(model_name),
        scopes=_build_scopes(chunks),
    )
