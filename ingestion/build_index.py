"""Build a versioned search index and switch the `current` pointer (architecture §5.5).

Layout of data/index/:
    v20261111T093000Z/chroma/       ChromaDB collection "chunks" (cosine)
    v20261111T093000Z/chunks.jsonl  every chunk; BM25 is built from this file at load time
    v20261111T093000Z/build.json    version, model, document hashes, counts
    current                         name of the version in use

The pointer is written last, through a temp file and os.replace, so a failed build never
changes the index in use.
"""

import json
import os
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ingestion.embed import MODEL_NAME, Embedder
from ingestion.models import Chunk

COLLECTION = "chunks"
POINTER = "current"
CHROMA_BATCH = 1000


def read_current(index_dir: Path) -> Path | None:
    pointer = index_dir / POINTER
    if not pointer.exists():
        return None
    version_dir = index_dir / pointer.read_text(encoding="utf-8").strip()
    return version_dir if version_dir.is_dir() else None


def read_build_info(version_dir: Path) -> dict:
    return json.loads((version_dir / "build.json").read_text(encoding="utf-8"))


def read_chunks(version_dir: Path) -> list[Chunk]:
    with (version_dir / "chunks.jsonl").open(encoding="utf-8") as f:
        return [Chunk.from_dict(json.loads(line)) for line in f if line.strip()]


def _write_pointer(index_dir: Path, version: str) -> None:
    tmp = index_dir / f"{POINTER}.tmp"
    tmp.write_text(version, encoding="utf-8")
    os.replace(tmp, index_dir / POINTER)


def _metadata(chunk: Chunk) -> dict:
    # ChromaDB rejects None, so missing values are stored as "" or -1.
    return {
        "doc_id": chunk.doc_id,
        "scheme_id": chunk.scheme_id or "",
        "doc_type": chunk.doc_type,
        "section": chunk.section,
        "source_url": chunk.source_url,
        "doc_as_of": chunk.doc_as_of or "",
        "page": chunk.page if chunk.page is not None else -1,
        "is_table": chunk.is_table,
    }


def build_index(
    index_dir: Path,
    chunks: Sequence[Chunk],
    embedder: Embedder,
    *,
    doc_hashes: dict[str, str],
    now: datetime,
    model_name: str = MODEL_NAME,
) -> str:
    """Build a new version from `chunks` and make it current. Returns the version name."""
    import chromadb
    from chromadb.config import Settings

    if not chunks:
        raise ValueError("no chunks to index")
    version = "v" + now.strftime("%Y%m%dT%H%M%SZ")
    target = index_dir / version
    if target.exists():
        raise FileExistsError(f"index version already exists: {target}")
    target.mkdir(parents=True)
    try:
        vectors = embedder([c.text for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("embedder returned the wrong number of vectors")

        client = chromadb.PersistentClient(
            path=str(target / "chroma"), settings=Settings(anonymized_telemetry=False)
        )
        collection = client.create_collection(
            COLLECTION, embedding_function=None, configuration={"hnsw": {"space": "cosine"}}
        )
        for start in range(0, len(chunks), CHROMA_BATCH):
            batch = chunks[start : start + CHROMA_BATCH]
            collection.add(
                ids=[c.chunk_id for c in batch],
                embeddings=vectors[start : start + CHROMA_BATCH],
                documents=[c.text for c in batch],
                metadatas=[_metadata(c) for c in batch],
            )

        with (target / "chunks.jsonl").open("w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
        info = {
            "version": version,
            "built_at": now.isoformat(),
            "embedding_model": model_name,
            "chunk_count": len(chunks),
            "doc_count": len({c.doc_id for c in chunks}),
            "documents": doc_hashes,
        }
        (target / "build.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise

    _write_pointer(index_dir, version)
    return version
