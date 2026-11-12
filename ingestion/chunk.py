"""Split parsed sections into chunks (architecture §5.3).

Sizes are counted in words: about 300 words is roughly 400 embedding tokens, and a 45-word
overlap is roughly 60 tokens. Tables are never split.
"""

from ingestion.models import Chunk, ParsedDocument

MAX_WORDS = 300
OVERLAP_WORDS = 45

DOC_LABELS = {
    "SCHEME_PAGE": "Scheme page",
    "KIM": "KIM",
    "SID": "SID",
    "FUND_FACTS": "Fund Facts",
    "SERVICE_PAGE": "Service page",
    "FACTSHEET_INDEX": "Factsheets",
}


def _windows(words: list[str]) -> list[list[str]]:
    if len(words) <= MAX_WORDS:
        return [words]
    step = MAX_WORDS - OVERLAP_WORDS
    windows = []
    for start in range(0, len(words), step):
        windows.append(words[start : start + MAX_WORDS])
        if start + MAX_WORDS >= len(words):
            break
    return windows


def chunk_document(doc: ParsedDocument, subject: str) -> list[Chunk]:
    """`subject` is the scheme name, or "HDFC Mutual Fund" for general pages."""
    label = DOC_LABELS.get(doc.doc_type, doc.doc_type)
    chunks: list[Chunk] = []
    for section in doc.sections:
        if section.is_table:
            pieces = [section.text]
        else:
            pieces = [" ".join(w) for w in _windows(section.text.split())]
        for piece in pieces:
            header = f"[{subject} | {label} | {section.heading}]"
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}#{len(chunks):03d}",
                    doc_id=doc.doc_id,
                    scheme_id=doc.scheme_id,
                    doc_type=doc.doc_type,
                    section=section.heading,
                    text=f"{header}\n{piece}",
                    source_url=doc.url,
                    doc_as_of=doc.doc_as_of.isoformat() if doc.doc_as_of else None,
                    page=section.page,
                    is_table=section.is_table,
                )
            )
    return chunks
