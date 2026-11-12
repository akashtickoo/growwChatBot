"""Read citation details from the source registry at response time (architecture §6.8, §9).

The citation URL, title and footer date always come from here by `doc_id`, never from model
text. Reading per request (not from chunk metadata) means a page re-fetched with an unchanged
hash shows its new `fetched_at` without an index rebuild.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DocInfo:
    doc_id: str
    url: str
    title: str
    fetched_at: datetime | None  # UTC
    doc_as_of: str | None  # ISO date printed on the document


class DocumentLookup(Protocol):
    def get(self, doc_id: str) -> DocInfo | None: ...

    def latest_fetch(self) -> datetime | None: ...

    def stale(self, cutoff: datetime) -> list[str]: ...


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class RegistryDocuments:
    """Read-only view of the `documents` table. One short connection per call (thread-safe)."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, doc_id: str) -> DocInfo | None:
        if not self.path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT doc_id, url, title, fetched_at, doc_as_of FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        return DocInfo(
            row["doc_id"],
            row["url"],
            row["title"] or row["doc_id"],
            _parse_time(row["fetched_at"]),
            row["doc_as_of"],
        )

    def latest_fetch(self) -> datetime | None:
        if not self.path.exists():
            return None
        with self._connect() as conn:
            (value,) = conn.execute(
                "SELECT MAX(fetched_at) FROM documents WHERE fetched_at IS NOT NULL"
            ).fetchone()
        return _parse_time(value)

    def stale(self, cutoff: datetime) -> list[str]:
        """Documents not fetched successfully since `cutoff` (EC-CIT-06), for /healthz."""
        if not self.path.exists():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, fetched_at FROM documents ORDER BY doc_id"
            ).fetchall()
        return [
            r["doc_id"]
            for r in rows
            if r["fetched_at"] is None or datetime.fromisoformat(r["fetched_at"]) < cutoff
        ]


class StaticDocuments:
    """In-memory lookup for tests and tools."""

    def __init__(self, docs: dict[str, DocInfo]):
        self.docs = docs

    def get(self, doc_id: str) -> DocInfo | None:
        return self.docs.get(doc_id)

    def latest_fetch(self) -> datetime | None:
        times = [d.fetched_at for d in self.docs.values() if d.fetched_at]
        return max(times, default=None)

    def stale(self, cutoff: datetime) -> list[str]:
        return sorted(
            d.doc_id for d in self.docs.values() if d.fetched_at is None or d.fetched_at < cutoff
        )
