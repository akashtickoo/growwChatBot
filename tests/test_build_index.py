"""Index build, pointer switch and failure handling (fake embedder)."""

import hashlib
from datetime import UTC, datetime

import pytest

from ingestion.build_index import build_index, read_build_info, read_chunks, read_current
from ingestion.lock import IngestLock, LockHeld
from ingestion.models import Chunk

T1 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def fake_embedder(texts):
    vectors = []
    for text in texts:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b / 255 for b in digest[:8]]
        norm = sum(x * x for x in raw) ** 0.5 or 1.0
        vectors.append([x / norm for x in raw])
    return vectors


def failing_embedder(texts):
    raise RuntimeError("model crashed")


def _chunks(n: int = 3) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"doc#{i:03d}",
            doc_id="doc",
            scheme_id="hdfc-mid-cap" if i else None,
            doc_type="KIM",
            section=f"Page {i}",
            text=f"[X | KIM | Page {i}]\nchunk number {i}",
            source_url="https://files.hdfcfund.com/kim.pdf",
            page=i,
        )
        for i in range(n)
    ]


def test_build_writes_a_version_and_switches_the_pointer(tmp_path):
    version = build_index(tmp_path, _chunks(), fake_embedder, doc_hashes={"doc": "h1"}, now=T1)

    current = read_current(tmp_path)
    assert current is not None and current.name == version == "v20261111T090000Z"
    info = read_build_info(current)
    assert info["chunk_count"] == 3 and info["documents"] == {"doc": "h1"}
    assert [c.chunk_id for c in read_chunks(current)] == ["doc#000", "doc#001", "doc#002"]


def test_chroma_collection_is_searchable_with_scheme_filter(tmp_path):
    import chromadb
    from chromadb.config import Settings

    build_index(tmp_path, _chunks(), fake_embedder, doc_hashes={"doc": "h1"}, now=T1)
    client = chromadb.PersistentClient(
        path=str(read_current(tmp_path) / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_collection("chunks")
    query = fake_embedder(["[X | KIM | Page 1]\nchunk number 1"])
    result = collection.query(
        query_embeddings=query, n_results=3, where={"scheme_id": "hdfc-mid-cap"}
    )
    assert result["ids"][0][0] == "doc#001"
    assert "doc#000" not in result["ids"][0]  # the general chunk is filtered out


def test_failed_build_keeps_the_current_index(tmp_path):
    first = build_index(tmp_path, _chunks(), fake_embedder, doc_hashes={"doc": "h1"}, now=T1)

    with pytest.raises(RuntimeError):
        build_index(tmp_path, _chunks(), failing_embedder, doc_hashes={"doc": "h2"}, now=T2)

    assert read_current(tmp_path).name == first
    assert not (tmp_path / "v20261111T100000Z").exists()


def test_empty_build_is_refused(tmp_path):
    with pytest.raises(ValueError):
        build_index(tmp_path, [], fake_embedder, doc_hashes={}, now=T1)
    assert read_current(tmp_path) is None


def test_second_run_waits_for_the_lock(tmp_path):
    """EC-ING-08."""
    lock_path = tmp_path / "ingest.lock"
    with IngestLock(lock_path):
        with pytest.raises(LockHeld, match="in progress"):
            with IngestLock(lock_path):
                pass
    with IngestLock(lock_path):  # released after the first run
        pass
    assert not lock_path.exists()
