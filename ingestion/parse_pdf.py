"""Parse HDFC MF PDFs (architecture §5.2).

KIMs are parsed into page text and tables with pdfplumber. Table areas are cut out of the page
text so table content isn't repeated in jumbled form. The suitability/riskometer table, and the
"Performance of the Scheme" block with its return tables, are dropped (EC-PERF-07, EC-INT-06).
pymupdf is used for the quick text-layer check and for full-text scans of SIDs.
"""

import io
import re
from datetime import date, datetime
from urllib.parse import unquote

import pdfplumber
import pymupdf

from ingestion.models import Section
from ingestion.normalize import normalize_text

# Tables mentioning returns, performance or suitability are dropped. "Total Returns Index"
# (a benchmark name) is not a returns figure, so it doesn't count.
SKIP_TABLE = re.compile(
    r"\breturns?\b(?!\s+index)|\bCAGR\b|since inception|past performance|suitable for"
    r"|riskometer|value of investment",
    re.IGNORECASE,
)
PERFORMANCE_START = re.compile(r"^\s*Performance of the Scheme", re.IGNORECASE)
PERFORMANCE_END = re.compile(
    r"^\s*(\d+\.\s*)?(Exit Load|Load Structure|For Riskometer)", re.IGNORECASE
)
BANNED_LINE = re.compile(
    r"suitable for|consult their financial advis|since inception|past performance"
    r"|\bCAGR\b|returns greater than one year",
    re.IGNORECASE,
)
_DATED = re.compile(r"dated\s+([A-Z][a-z]+ \d{1,2}, \d{4})")


def is_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def text_layer_chars(content: bytes, pages: int = 5) -> int:
    """Characters of extractable text in the first pages. Near zero means a scanned PDF."""
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return sum(len(doc[i].get_text().strip()) for i in range(min(pages, len(doc))))


def full_text(content: bytes) -> str:
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def dated_from(*texts: str) -> date | None:
    """The 'dated November 21, 2025' date in a KIM/SID link title or file name."""
    for text in texts:
        match = _DATED.search(unquote(text))
        if match:
            try:
                return datetime.strptime(match.group(1), "%B %d, %Y").date()
            except ValueError:
                continue
    return None


def _clean_cell(cell: str | None) -> str:
    # "switch-\nin" is one word broken across lines: keep the hyphen, drop the break.
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2", cell or "")
    return " ".join(text.split())


def table_to_text(rows: list[list[str | None]]) -> str:
    """Readable sentences from a table. With a header row: 'Header: value; Header: value'."""
    rows = [[_clean_cell(c) for c in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    header = rows[0]
    if len(rows) >= 2 and len(header) > 1 and all(header):
        lines = []
        for row in rows[1:]:
            parts = [f"{h}: {v}" for h, v in zip(header, row, strict=False) if v]
            if parts:
                lines.append("; ".join(parts) + ".")
        return "\n".join(lines)
    return "\n".join(" | ".join(c for c in row if c) for row in rows)


def _caption(page: pdfplumber.page.Page, bbox: tuple[float, float, float, float]) -> str:
    """The label for a table: text to its left on the same rows, or else just above it."""
    x0, top, _x1, bottom = bbox
    candidates = []
    if x0 > 40:
        candidates.append((0, top, x0, bottom))
    if top > 5:
        candidates.append((0, max(0, top - 30), page.width, top))
    for box in candidates:
        try:
            text = page.crop(box).extract_text() or ""
        except ValueError:
            continue
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        if lines:
            return " ".join(lines)[:120] if box[2] == x0 else lines[-1][:120]
    return ""


def parse_kim(content: bytes) -> list[Section]:
    sections: list[Section] = []
    in_performance = False
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            outside = page
            for table in tables:
                try:
                    outside = outside.outside_bbox(table.bbox)
                except ValueError:
                    continue

            kept_lines = []
            for line in (outside.extract_text() or "").splitlines():
                if PERFORMANCE_START.match(line):
                    in_performance = True
                    continue
                if in_performance:
                    if PERFORMANCE_END.match(line):
                        in_performance = False
                    else:
                        continue
                if not BANNED_LINE.search(line):
                    kept_lines.append(line)
            text = normalize_text("\n".join(kept_lines))
            if text:
                sections.append(Section(heading=f"Page {number}", text=text, page=number))

            for table in tables:
                rows = table.extract()
                flat = " ".join(_clean_cell(c) for row in rows for c in row)
                if not flat or SKIP_TABLE.search(flat) or in_performance:
                    continue
                caption = _caption(page, table.bbox)
                heading = f"Page {number} table" + (f": {caption}" if caption else "")
                table_text = normalize_text(table_to_text(rows))
                if table_text:
                    sections.append(
                        Section(heading=heading, text=table_text, is_table=True, page=number)
                    )
    return sections
