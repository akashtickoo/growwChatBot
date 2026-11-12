"""SQLite source registry (architecture §9): documents, fact cards and index builds."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    scheme_id       TEXT,
    doc_type        TEXT NOT NULL,
    title           TEXT,
    url             TEXT NOT NULL,
    fetch_mode      TEXT NOT NULL,
    indexed         INTEGER NOT NULL,
    content_hash    TEXT,           -- hash of extracted text, not raw bytes (EC-ING-03)
    doc_as_of       TEXT,           -- ISO date printed on the document, if any
    fetched_at      TEXT,           -- UTC ISO time of the last good fetch; drives the footer
    last_attempt_at TEXT,
    last_status     TEXT,           -- fetched | unchanged | failed | skipped
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS fact_cards (
    scheme_id           TEXT NOT NULL,
    field               TEXT NOT NULL,
    status              TEXT NOT NULL,     -- found | not_found | rejected (failed the quote check)
    value               TEXT,
    verbatim_quote      TEXT,
    source_doc_id       TEXT,              -- document the quote came from
    source_chunk_id     TEXT,
    source_content_hash TEXT,              -- that document's hash; card withdrawn when it changes
    sources_hash        TEXT NOT NULL,     -- scheme's sources at extraction; re-extract on change
    doc_as_of           TEXT,
    conflicting         INTEGER NOT NULL DEFAULT 0,  -- sources gave different values
    note                TEXT,
    review_status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    reviewed_at         TEXT,
    model               TEXT,
    extracted_at        TEXT NOT NULL,
    PRIMARY KEY (scheme_id, field)
);

CREATE TABLE IF NOT EXISTS index_builds (
    version         TEXT PRIMARY KEY,
    built_at        TEXT NOT NULL,
    status          TEXT NOT NULL,     -- ok | failed
    doc_count       INTEGER,
    chunk_count     INTEGER,
    embedding_model TEXT,
    error           TEXT
);
"""

DOCUMENT_COLUMNS = (
    "scheme_id",
    "doc_type",
    "title",
    "url",
    "fetch_mode",
    "indexed",
    "content_hash",
    "doc_as_of",
    "fetched_at",
    "last_attempt_at",
    "last_status",
    "last_error",
)


CARD_COLUMNS = (
    "status",
    "value",
    "verbatim_quote",
    "source_doc_id",
    "source_chunk_id",
    "source_content_hash",
    "sources_hash",
    "doc_as_of",
    "conflicting",
    "note",
    "review_status",
    "reviewed_at",
    "model",
    "extracted_at",
)


class Registry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate_fact_cards()
        self.conn.executescript(SCHEMA)

    def _migrate_fact_cards(self) -> None:
        """Bring a Phase 2 fact_cards table up to the Phase 3 columns."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(fact_cards)")}
        if not columns or set(CARD_COLUMNS) <= columns:
            return
        (rows,) = self.conn.execute("SELECT COUNT(*) FROM fact_cards").fetchone()
        if rows == 0:
            self.conn.execute("DROP TABLE fact_cards")  # recreated by SCHEMA
            return
        for column in CARD_COLUMNS:
            if column not in columns:
                self.conn.execute(f"ALTER TABLE fact_cards ADD COLUMN {column} TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_document(self, doc_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def upsert_document(self, doc_id: str, **fields: object) -> None:
        unknown = set(fields) - set(DOCUMENT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown document columns: {sorted(unknown)}")
        existing = self.get_document(doc_id)
        if existing is None:
            columns = ["doc_id", *fields]
            placeholders = ", ".join("?" for _ in columns)
            self.conn.execute(
                f"INSERT INTO documents ({', '.join(columns)}) VALUES ({placeholders})",
                (doc_id, *fields.values()),
            )
        elif fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            self.conn.execute(
                f"UPDATE documents SET {assignments} WHERE doc_id = ?",
                (*fields.values(), doc_id),
            )
        self.conn.commit()

    # --- fact cards --------------------------------------------------------------------------

    def get_card(self, scheme_id: str, field: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM fact_cards WHERE scheme_id = ? AND field = ?", (scheme_id, field)
        ).fetchone()
        return dict(row) if row else None

    def list_cards(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM fact_cards ORDER BY scheme_id, field")
        return [dict(row) for row in rows]

    def upsert_card(self, scheme_id: str, field: str, **values: object) -> None:
        unknown = set(values) - set(CARD_COLUMNS)
        if unknown:
            raise ValueError(f"unknown fact card columns: {sorted(unknown)}")
        columns = ["scheme_id", "field", *values]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c} = excluded.{c}" for c in values)
        self.conn.execute(
            f"INSERT INTO fact_cards ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT (scheme_id, field) DO UPDATE SET {updates}",
            (scheme_id, field, *values.values()),
        )
        self.conn.commit()

    def set_review(
        self, scheme_id: str, field: str, status: str, reviewed_at: str, note: str | None = None
    ) -> None:
        if status not in ("approved", "rejected", "pending"):
            raise ValueError(f"bad review status: {status}")
        if self.get_card(scheme_id, field) is None:
            raise KeyError(f"no fact card for {scheme_id}:{field}")
        fields: dict[str, object] = {"review_status": status, "reviewed_at": reviewed_at}
        if note is not None:
            fields["note"] = note
        assignments = ", ".join(f"{c} = ?" for c in fields)
        self.conn.execute(
            f"UPDATE fact_cards SET {assignments} WHERE scheme_id = ? AND field = ?",
            (*fields.values(), scheme_id, field),
        )
        self.conn.commit()

    def record_build(
        self,
        version: str,
        built_at: str,
        status: str,
        doc_count: int,
        chunk_count: int,
        embedding_model: str,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO index_builds VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, built_at, status, doc_count, chunk_count, embedding_model, error),
        )
        self.conn.commit()
