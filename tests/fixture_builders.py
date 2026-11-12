"""Synthetic test fixtures: the scheme page HTML on disk, and PDFs generated with pymupdf.

Nothing here is copied from HDFC MF documents; layouts imitate them and values are made up.
"""

from pathlib import Path

import pymupdf

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FILLER = (
    "This Key Information Memorandum sets forth the information which a prospective investor "
    "ought to know before investing. For further details of the scheme, investors should refer "
    "to the Scheme Information Document and the Statement of Additional Information. "
)


def scheme_page_html() -> str:
    return (FIXTURES / "scheme_page.html").read_text(encoding="utf-8")


def _cell(page: pymupdf.Page, rect: pymupdf.Rect, text: str) -> None:
    page.draw_rect(rect, color=(0, 0, 0), width=0.8)
    page.insert_textbox(rect + (3, 3, -3, -3), text, fontsize=8)


def _table(
    page: pymupdf.Page,
    x0: float,
    y0: float,
    widths: list[float],
    rows: list[list[str]],
    height: float = 40,
) -> None:
    y = y0
    for row in rows:
        x = x0
        for width, text in zip(widths, row, strict=True):
            _cell(page, pymupdf.Rect(x, y, x + width, y + height), text)
            x += width
        y += height


def kim_pdf(scheme: str = "HDFC Mid Cap Fund") -> bytes:
    doc = pymupdf.open()

    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 40, 550, 160),
        f"KEY INFORMATION MEMORANDUM\n{scheme}\n"
        "(Formerly known as HDFC Mid-Cap Opportunities Fund)\n"
        "An open ended equity scheme predominantly investing in mid cap stocks",
        fontsize=10,
    )
    page.insert_textbox(pymupdf.Rect(50, 170, 550, 300), FILLER * 3, fontsize=9)
    _table(
        page,
        50,
        320,
        [250, 250],
        [
            ["This product is suitable for investors who are seeking*:", "Scheme Riskometer#"],
            ["- long-term capital appreciation", "Benchmark Riskometer#"],
        ],
    )

    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 40, 550, 120), FILLER * 2, fontsize=9)
    page.insert_textbox(
        pymupdf.Rect(50, 140, 150, 220),
        "11. Minimum Application Amount / Number of Units",
        fontsize=9,
    )
    _table(
        page,
        160,
        140,
        [130, 130, 130],
        [
            [
                "Purchase (including switch-in)",
                "Additional Purchase (including switch-in)",
                "Redemption (including switch-out)",
            ],
            [
                "Rs.100/- and any amount thereafter",
                "Rs.100/- and any amount thereafter",
                "Rs. 100 and in multiples of Re. 1/- thereafter.",
            ],
        ],
        height=50,
    )

    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 40, 550, 400),
        "Performance of the Scheme (as at September 30, 2025)\n"
        "Returns\nSince Inception* 18.50%\n"
        "Past performance may or may not be sustained in future.\n"
        "1. Load Structure\n"
        "Exit Load:\n"
        "In respect of each purchase / switch-in of Units, an Exit Load of 1.00% "
        "is payable if Units are redeemed / switched-out within 1 year from the "
        "date of allotment.",
        fontsize=9,
    )
    return doc.tobytes()


def simple_pdf(title: str, scheme: str = "HDFC Mid Cap Fund", repeat: int = 4) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 40, 550, 800), f"{title}\n{scheme}\n" + FILLER * repeat, fontsize=9
    )
    return doc.tobytes()


def sid_pdf() -> bytes:
    return simple_pdf("SCHEME INFORMATION DOCUMENT")


def fund_facts_pdf() -> bytes:
    return simple_pdf("Fund Facts", repeat=1)
