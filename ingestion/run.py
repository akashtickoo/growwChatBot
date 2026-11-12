"""Ingestion: turn config/sources.yaml into a versioned search index (architecture §5).

Usage:
    python -m ingestion.run                 all sources
    python -m ingestion.run --only DOC_ID   one manifest entry (a scheme page includes its PDFs)
    python -m ingestion.run --force         re-process even if content is unchanged

Every run writes data/reports/ingest_<time>.md listing each document as fetched, unchanged,
failed or skipped. A failed document keeps its last good version in the index.
"""

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ingestion.build_index import build_index, read_build_info, read_current
from ingestion.chunk import chunk_document
from ingestion.embed import MODEL_NAME, Embedder, SentenceTransformerEmbedder
from ingestion.fact_cards import cards_digest, fact_card_chunks
from ingestion.fetch import Fetched, FetchError, fetch, load_manual, make_client, save_raw
from ingestion.linked_docs import LinkedDocument, find_linked_documents
from ingestion.lock import IngestLock, LockHeld
from ingestion.manifest import (
    LinkedDocumentSpec,
    ManifestEntry,
    Scheme,
    linked_doc_id,
    load_schemes,
    load_sources,
)
from ingestion.models import Chunk, ParsedDocument, Section
from ingestion.parse_html import (
    normalize_sections,
    parse_generic_page,
    parse_scheme_page,
    visible_text,
)
from ingestion.parse_pdf import dated_from, full_text, is_pdf, parse_kim, text_layer_chars
from ingestion.paths import Paths
from ingestion.registry import Registry
from ingestion.sanity import (
    MIN_PDF_TEXT_CHARS,
    check_content_type,
    check_generic_page,
    check_pdf_text,
    check_scheme_page,
)

GENERAL_SUBJECT = "HDFC Mutual Fund"
FACT_CARDS_KEY = "fact-cards"  # build.json entry for the approved fact cards


@dataclass
class DocResult:
    doc_id: str
    doc_type: str
    status: str  # fetched | unchanged | failed | skipped
    detail: str = ""


@dataclass
class RunReport:
    started_at: datetime
    results: list[DocResult] = field(default_factory=list)
    index: str = ""
    report_path: Path | None = None

    @property
    def failed(self) -> list[DocResult]:
        return [r for r in self.results if r.status == "failed"]


def _decode(fetched: Fetched) -> str:
    charset = "utf-8"
    for part in fetched.content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip().strip('"')
    try:
        return fetched.content.decode(charset, errors="replace")
    except LookupError:
        return fetched.content.decode("utf-8", errors="replace")


def content_hash(parsed: ParsedDocument) -> str:
    return hashlib.sha256(parsed.full_text().encode("utf-8")).hexdigest()


class Ingestor:
    def __init__(
        self,
        paths: Paths,
        *,
        client: httpx.Client,
        embedder: Embedder,
        force: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.paths = paths
        self.client = client
        self.embedder = embedder
        self.force = force
        self.sleep = sleep
        self.now = now
        self.manifest = load_sources()
        self.schemes = {s.id: s for s in load_schemes().schemes}
        self.registry = Registry(paths.registry)

    # --- processed chunk store -------------------------------------------------------------

    def _processed_path(self, doc_id: str) -> Path:
        return self.paths.processed / f"{doc_id}.json"

    def _write_processed(self, doc_id: str, digest: str, chunks: list[Chunk]) -> None:
        self.paths.processed.mkdir(parents=True, exist_ok=True)
        data = {"doc_id": doc_id, "content_hash": digest, "chunks": [c.to_dict() for c in chunks]}
        self._processed_path(doc_id).write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def _read_processed(self, doc_id: str) -> dict | None:
        path = self._processed_path(doc_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    # --- registry helpers ------------------------------------------------------------------

    def _fail(self, doc_id: str, doc_type: str, url: str, error: str, **meta: object) -> DocResult:
        """Record a failure. A document seen before keeps its last good content and fetch time."""
        if self.registry.get_document(doc_id) is None:
            meta = {"fetch_mode": "auto", "indexed": 1, **meta}
            self.registry.upsert_document(doc_id, doc_type=doc_type, url=url, **meta)
        self.registry.upsert_document(
            doc_id,
            last_attempt_at=self.now().isoformat(),
            last_status="failed",
            last_error=error,
        )
        return DocResult(doc_id, doc_type, "failed", error)

    def _skip_linked(self, entry: ManifestEntry, reason: str) -> list[DocResult]:
        return [
            DocResult(linked_doc_id(entry.scheme_id, s.doc_type), s.doc_type, "skipped", reason)
            for s in entry.linked_documents
        ]

    def _store(
        self,
        parsed: ParsedDocument,
        fetched: Fetched,
        *,
        indexed: bool,
        fetch_mode: str,
        subject: str,
    ) -> DocResult:
        digest = content_hash(parsed)
        previous = self.registry.get_document(parsed.doc_id)
        have_chunks = self._read_processed(parsed.doc_id) is not None
        unchanged = (
            not self.force
            and previous is not None
            and previous["content_hash"] == digest
            and (have_chunks or not indexed)
        )
        detail = "same content as the last run"
        if not unchanged:
            if indexed:
                chunks = chunk_document(parsed, subject)
                self._write_processed(parsed.doc_id, digest, chunks)
                detail = f"{len(chunks)} chunks"
            else:
                detail = "registered (not indexed)"
        if not indexed:
            self._processed_path(parsed.doc_id).unlink(missing_ok=True)

        self.registry.upsert_document(
            parsed.doc_id,
            scheme_id=parsed.scheme_id,
            doc_type=parsed.doc_type,
            title=parsed.title,
            url=parsed.url,
            fetch_mode=fetch_mode,
            indexed=int(indexed),
            content_hash=digest,
            doc_as_of=parsed.doc_as_of.isoformat() if parsed.doc_as_of else None,
            fetched_at=fetched.fetched_at.isoformat(),
            last_attempt_at=self.now().isoformat(),
            last_status="unchanged" if unchanged else "fetched",
            last_error=None,
        )
        return DocResult(
            parsed.doc_id, parsed.doc_type, "unchanged" if unchanged else "fetched", detail
        )

    # --- one manifest entry ----------------------------------------------------------------

    def _get(self, url: str, doc_id: str, fetch_mode: str, ext: str) -> Fetched:
        if fetch_mode == "manual":
            return load_manual(self.paths.raw, doc_id, url, ext)
        return fetch(self.client, url, self.manifest.allowed_domains, sleep=self.sleep)

    def process_entry(self, entry: ManifestEntry) -> list[DocResult]:
        url = str(entry.url)
        meta = {
            "scheme_id": entry.scheme_id,
            "fetch_mode": entry.fetch,
            "indexed": int(entry.index),
        }
        try:
            fetched = self._get(url, entry.id, entry.fetch, "html")
        except FetchError as exc:
            result = self._fail(entry.id, entry.doc_type, url, f"fetch: {exc}", **meta)
            return [result, *self._skip_linked(entry, "scheme page failed; last good version kept")]

        problems = check_content_type(fetched.content_type, "text/html")
        html = _decode(fetched)
        visible = visible_text(html)
        scheme = self.schemes.get(entry.scheme_id) if entry.scheme_id else None

        if entry.doc_type == "SCHEME_PAGE":
            page = parse_scheme_page(html)
            problems += check_scheme_page(page, scheme.name, visible)
            parsed = ParsedDocument(
                doc_id=entry.id,
                scheme_id=entry.scheme_id,
                doc_type=entry.doc_type,
                title=f"{scheme.name} (HDFC Mutual Fund)",
                url=url,
                sections=[Section(h, t) for h, t in normalize_sections(page.sections)],
                facts=page.facts,
            )
        else:
            page = parse_generic_page(html)
            problems += check_generic_page(page, visible)
            parsed = ParsedDocument(
                doc_id=entry.id,
                scheme_id=None,
                doc_type=entry.doc_type,
                title=page.title or entry.id,
                url=url,
                sections=[Section(h, t) for h, t in normalize_sections(page.sections)],
                doc_as_of=page.last_updated,
            )

        if problems:
            result = self._fail(entry.id, entry.doc_type, url, "; ".join(problems), **meta)
            return [result, *self._skip_linked(entry, "scheme page failed; last good version kept")]

        if entry.fetch == "auto":
            save_raw(self.paths.raw, entry.id, fetched, "html")
        subject = scheme.name if scheme else GENERAL_SUBJECT
        results = [
            self._store(
                parsed, fetched, indexed=entry.index, fetch_mode=entry.fetch, subject=subject
            )
        ]

        if entry.linked_documents:
            found, missing = find_linked_documents(html, fetched.final_url, entry.linked_documents)
            for spec in missing:
                pdf_id = linked_doc_id(entry.scheme_id, spec.doc_type)
                error = f"no PDF link starting {spec.link_text_prefix!r} on the scheme page"
                pdf_meta = {"scheme_id": entry.scheme_id, "indexed": int(spec.index)}
                results.append(self._fail(pdf_id, spec.doc_type, url, error, **pdf_meta))
            specs = {s.doc_type: s for s in entry.linked_documents}
            for linked in found:
                results.append(self.process_pdf(scheme, specs[linked.doc_type], linked))
        return results

    def process_pdf(
        self, scheme: Scheme, spec: LinkedDocumentSpec, linked: LinkedDocument
    ) -> DocResult:
        doc_id = linked_doc_id(scheme.id, spec.doc_type)
        meta = {"scheme_id": scheme.id, "fetch_mode": "auto", "indexed": int(spec.index)}
        try:
            fetched = fetch(
                self.client, linked.url, self.manifest.allowed_domains, sleep=self.sleep
            )
        except FetchError as exc:
            return self._fail(doc_id, spec.doc_type, linked.url, f"fetch: {exc}", **meta)

        content = fetched.content
        if not is_pdf(content):
            return self._fail(doc_id, spec.doc_type, linked.url, "not a PDF", **meta)
        if spec.doc_type in ("KIM", "SID") and text_layer_chars(content) < MIN_PDF_TEXT_CHARS:
            return self._fail(doc_id, spec.doc_type, linked.url, "no text layer (scanned?)", **meta)

        if spec.doc_type == "KIM":
            sections = parse_kim(content)
            check_text = "\n".join(f"{s.heading}\n{s.text}" for s in sections)
        else:
            check_text = full_text(content)
            sections = [Section("Full text", check_text)]
        if spec.doc_type in ("KIM", "SID"):
            problems = check_pdf_text(check_text, spec.doc_type, scheme.name)
            if problems:
                return self._fail(doc_id, spec.doc_type, linked.url, "; ".join(problems), **meta)

        save_raw(self.paths.raw, doc_id, fetched, "pdf")
        parsed = ParsedDocument(
            doc_id=doc_id,
            scheme_id=scheme.id,
            doc_type=spec.doc_type,
            title=linked.title,
            url=linked.url,
            sections=sections,
            doc_as_of=dated_from(linked.title, linked.url),
        )
        return self._store(
            parsed, fetched, indexed=spec.index, fetch_mode="auto", subject=scheme.name
        )

    # --- index -----------------------------------------------------------------------------

    def indexed_doc_ids(self) -> list[str]:
        ids = []
        for entry in self.manifest.documents:
            if entry.index:
                ids.append(entry.id)
            ids += [
                linked_doc_id(entry.scheme_id, s.doc_type)
                for s in entry.linked_documents
                if s.index
            ]
        return ids

    def update_index(self) -> str:
        chunks: list[Chunk] = []
        hashes: dict[str, str] = {}
        for doc_id in self.indexed_doc_ids():
            data = self._read_processed(doc_id)
            if data is None:
                continue
            hashes[doc_id] = data["content_hash"]
            chunks += [Chunk.from_dict(c) for c in data["chunks"]]
        if not chunks:
            return "not built: no processed documents"

        # Approved fact cards whose source is unchanged since extraction (Phase 3).
        card_chunks, withdrawn = fact_card_chunks(self.registry, self.schemes)
        if card_chunks:
            hashes[FACT_CARDS_KEY] = cards_digest(card_chunks)
            chunks += card_chunks
        cards_note = f"; {len(card_chunks)} fact cards" if card_chunks or withdrawn else ""
        if withdrawn:
            cards_note += f" ({withdrawn} withdrawn: source changed since review)"

        current = read_current(self.paths.index)
        if current and not self.force and read_build_info(current).get("documents") == hashes:
            return f"unchanged ({current.name}){cards_note}"

        built_at = self.now()
        try:
            version = build_index(
                self.paths.index, chunks, self.embedder, doc_hashes=hashes, now=built_at
            )
        except Exception as exc:
            version = "v" + built_at.strftime("%Y%m%dT%H%M%SZ")
            self.registry.record_build(
                version,
                built_at.isoformat(),
                "failed",
                len(hashes),
                len(chunks),
                MODEL_NAME,
                f"{type(exc).__name__}: {exc}",
            )
            return f"not built: {type(exc).__name__}: {exc} (current index unchanged)"
        self.registry.record_build(
            version, built_at.isoformat(), "ok", len(hashes), len(chunks), MODEL_NAME
        )
        doc_count = len([k for k in hashes if k != FACT_CARDS_KEY])
        return f"built {version}: {len(chunks)} chunks from {doc_count} documents{cards_note}"

    # --- whole run -------------------------------------------------------------------------

    def run(self, only: str | None = None, index_only: bool = False) -> RunReport:
        report = RunReport(started_at=self.now())
        entries = [e for e in self.manifest.documents if only is None or e.id == only]
        if only is not None and not entries:
            raise SystemExit(f"--only: no manifest entry with id {only!r}")
        for entry in [] if index_only else entries:
            report.results += self.process_entry(entry)
        report.index = self.update_index()
        report.report_path = write_report(self.paths.reports, report)
        return report


def write_report(reports_dir: Path, report: RunReport) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.strftime("%Y%m%dT%H%M%SZ")
    path = reports_dir / f"ingest_{stamp}.md"
    counts: dict[str, int] = {}
    for r in report.results:
        counts[r.status] = counts.get(r.status, 0) + 1
    lines = [
        f"# Ingestion run {stamp}",
        "",
        "Statuses: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        "",
        f"Index: {report.index}",
        "",
        "| Document | Type | Status | Detail |",
        "|---|---|---|---|",
        *(f"| {r.doc_id} | {r.doc_type} | {r.status} | {r.detail} |" for r in report.results),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the search index from config/sources.yaml")
    parser.add_argument("--only", help="process one manifest entry by id")
    parser.add_argument("--force", action="store_true", help="re-process unchanged documents")
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="don't fetch; rebuild the index from processed documents and approved fact cards",
    )
    args = parser.parse_args(argv)

    paths = Paths.default()
    try:
        with IngestLock(paths.lock), make_client() as client:
            ingestor = Ingestor(
                paths, client=client, embedder=SentenceTransformerEmbedder(), force=args.force
            )
            report = ingestor.run(only=args.only, index_only=args.index_only)
    except LockHeld as exc:
        print(exc, file=sys.stderr)
        return 2

    width = max((len(r.doc_id) for r in report.results), default=10)
    for r in report.results:
        print(f"{r.status.upper():9} {r.doc_id:<{width}}  {r.detail}")
    print(f"\nIndex: {report.index}\nReport: {report.report_path}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
