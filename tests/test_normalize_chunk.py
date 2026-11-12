"""Normalization and chunking."""

from ingestion.chunk import MAX_WORDS, OVERLAP_WORDS, chunk_document
from ingestion.models import ParsedDocument, Section
from ingestion.normalize import normalize_text, squash


def test_currency_forms_become_rupee_sign():
    text = normalize_text("Rs.100/- or Rs 500 or INR 1000 or Re. 1/- or ₹ 100")
    assert text == "₹100/- or ₹500 or ₹1000 or ₹1/- or ₹100"


def test_rs_without_a_number_is_left_alone():
    assert normalize_text("Rs. per unit") == "Rs. per unit"


def test_nfkc_and_whitespace():
    assert normalize_text("Ｍｉｄ Cap\t\tFund  \n\n\n  next") == "Mid Cap Fund\nnext"


def test_line_break_hyphen_is_joined():
    assert normalize_text("switch-\nin of Units") == "switch-in of Units"


def test_squash():
    assert squash("HDFC ELSS - Tax Saver Fund") == squash("HDFC ELSS Tax Saver Fund")


def _doc(sections: list[Section]) -> ParsedDocument:
    return ParsedDocument(
        doc_id="hdfc-mid-cap-kim",
        scheme_id="hdfc-mid-cap",
        doc_type="KIM",
        title="KIM - HDFC Mid Cap Fund",
        url="https://files.hdfcfund.com/kim.pdf",
        sections=sections,
    )


def test_long_section_is_split_with_overlap():
    words = [f"w{i}" for i in range(700)]
    chunks = chunk_document(_doc([Section("Page 5", " ".join(words), page=5)]), "HDFC Mid Cap")
    bodies = [c.text.split("\n", 1)[1].split() for c in chunks]

    assert len(chunks) == 3
    assert all(len(b) <= MAX_WORDS for b in bodies)
    assert bodies[0][-OVERLAP_WORDS:] == bodies[1][:OVERLAP_WORDS]
    assert bodies[-1][-1] == "w699"


def test_tables_are_never_split():
    long_table = " ".join(f"cell{i}" for i in range(MAX_WORDS * 2))
    chunks = chunk_document(_doc([Section("Page 2 table", long_table, is_table=True)]), "X")
    assert len(chunks) == 1 and chunks[0].is_table


def test_chunks_carry_header_and_metadata():
    doc = _doc([Section("Page 1", "Exit load text."), Section("Page 2 table", "a: b", True, 2)])
    chunks = chunk_document(doc, "HDFC Mid Cap Fund – Direct Growth")

    assert chunks[0].text.startswith("[HDFC Mid Cap Fund – Direct Growth | KIM | Page 1]\n")
    assert [c.chunk_id for c in chunks] == ["hdfc-mid-cap-kim#000", "hdfc-mid-cap-kim#001"]
    assert all(c.scheme_id == "hdfc-mid-cap" and c.source_url.endswith("kim.pdf") for c in chunks)
    assert chunks[1].page == 2 and chunks[1].is_table
