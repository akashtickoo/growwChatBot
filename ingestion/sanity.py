"""Sanity checks on fetched content (architecture §5.1, EC-ING-01).

A captcha, "access denied" or empty page can come back with HTTP 200. Required content
decides pass or fail; block-page markers only explain a failure.
"""

import re

from ingestion.normalize import squash
from ingestion.parse_html import GenericPage, SchemePage, blocked_markers

MIN_SCHEME_PAGE_CHARS = 3000
MIN_GENERIC_PAGE_CHARS = 150
MIN_PDF_TEXT_CHARS = 300
REQUIRED_SCHEME_FACTS = ("riskometer", "min_sip", "expense_ratio", "benchmark")


def short_name(scheme_name: str) -> str:
    """'HDFC Mid Cap Fund – Direct Growth' -> 'HDFC Mid Cap Fund'."""
    return re.split(r"\s+[–-]\s+", scheme_name)[0]


def _with_block_reason(problems: list[str], visible: str) -> list[str]:
    if problems and (markers := blocked_markers(visible)):
        problems.append(f"looks like a block page ({', '.join(markers)})")
    return problems


def check_content_type(content_type: str, expected: str) -> list[str]:
    return [] if expected in content_type else [f"unexpected content type: {content_type!r}"]


def check_scheme_page(page: SchemePage, scheme_name: str, visible: str) -> list[str]:
    problems = []
    if len(visible) < MIN_SCHEME_PAGE_CHARS:
        problems.append(f"page text too short ({len(visible)} characters)")
    expected = short_name(scheme_name)
    if squash(page.title) != squash(expected):
        problems.append(f"page title {page.title!r} doesn't match {expected!r}")
    missing = [f for f in REQUIRED_SCHEME_FACTS if f not in page.facts]
    if missing:
        problems.append(f"missing facts: {', '.join(missing)}")
    if not page.has_exit_load:
        problems.append("no Exit Load section")
    return _with_block_reason(problems, visible)


def check_generic_page(page: GenericPage, visible: str) -> list[str]:
    problems = []
    text_chars = sum(len(text) for _, text in page.sections)
    if not page.title:
        problems.append("no page title (h1)")
    if text_chars < MIN_GENERIC_PAGE_CHARS:
        problems.append(f"page text too short ({text_chars} characters)")
    return _with_block_reason(problems, visible)


def check_pdf_text(text: str, doc_type: str, scheme_name: str) -> list[str]:
    """`text` should include section headings (table captions carry some labels)."""
    problems = []
    if len(text.strip()) < MIN_PDF_TEXT_CHARS:
        return [f"almost no text ({len(text.strip())} characters); scanned PDF?"]
    flat = " ".join(text.split()).lower()  # phrases can wrap across lines
    name_key = squash(re.sub(r"\s+Fund$", "", short_name(scheme_name)))
    if name_key not in squash(text):
        problems.append(f"scheme name {short_name(scheme_name)!r} not found in the PDF")
    if doc_type == "KIM" and "minimum application amount" not in flat:
        problems.append("KIM has no 'Minimum Application Amount' section")
    if doc_type == "SID" and "scheme information document" not in flat:
        problems.append("SID doesn't say 'Scheme Information Document'")
    return problems
