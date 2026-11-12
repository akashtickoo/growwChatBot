"""Scheme-page and general-page parsing, on a synthetic fixture with the HDFC MF layout."""

import re
from datetime import date

from fixture_builders import scheme_page_html

from ingestion.parse_html import (
    normalize_sections,
    parse_generic_page,
    parse_scheme_page,
    visible_text,
)
from ingestion.sanity import check_generic_page, check_scheme_page

SCHEME_NAME = "HDFC Mid Cap Fund – Direct Growth"
BANNED = re.compile(
    r"since inception|value of investment|ideal for|suitable for|scheme returns|past performance"
    r"|why invest|who can consider|25\.10%|19\.30%",
    re.IGNORECASE,
)


def _sections(html: str) -> dict[str, str]:
    return dict(normalize_sections(parse_scheme_page(html).sections))


def test_reads_the_fact_boxes():
    facts = parse_scheme_page(scheme_page_html()).facts
    assert facts["expense_ratio"] == "0.74"
    assert facts["riskometer"] == "Very High"
    assert facts["min_sip"] == "₹ 100"
    assert facts["lock_in"] == "NA"
    assert facts["entry_load"] == "NA"
    assert facts["benchmark"] == "NIFTY Midcap 150 Index (Total Returns Index)"
    assert facts["aum"] == "₹100,687.07 Cr." and facts["aum_as_on"] == "31/10/2026"
    assert facts["scheme_type"].startswith("An open ended equity scheme")


def test_keeps_only_allowed_sections():
    sections = _sections(scheme_page_html())
    assert set(sections) == {
        "Key facts",
        "What is HDFC Mid Cap Fund?",
        "Fund Managers",
        "Exit Load",
        "FAQ: Can I invest in SIP and Lump Sum of HDFC Mid Cap Fund ?",
        "FAQ: What benchmark does HDFC Mid Cap Fund track?",
    }
    assert "Total expense ratio (TER): 0.74%" in sections["Key facts"]
    assert "Minimum SIP amount: ₹100" in sections["Key facts"]


def test_drops_performance_suitability_and_advice():
    """EC-PERF-07, EC-INT-06, EV-2-07: no returns, suitability or advisory text survives."""
    for heading, text in _sections(scheme_page_html()).items():
        assert not BANNED.search(heading), heading
        assert not BANNED.search(text), (heading, text)


def test_nav_is_never_kept():
    """NAV is not answered (architecture §2.1), so it isn't indexed."""
    text = "\n".join(_sections(scheme_page_html()).values())
    assert "123.4567" not in text


def test_fund_managers_without_a_title_prefix():
    managers = _sections(scheme_page_html())["Fund Managers"]
    assert "Mr. Chirag Setalvad" in managers and "Amar Kalkundrikar" in managers
    assert "Portfolio" not in managers


def test_exit_load_written_as_h3_headings():
    """Some pages (Multi Cap) put the exit-load bullet points in h3 tags."""
    html = scheme_page_html()
    start = html.index("<h2>Exit Load</h2>")
    end = html.index("<h3>Product Labelling</h3>")
    html = (
        html[:start]
        + (
            "<h2>Exit Load</h2>"
            "<h3>An Exit Load of 1.00% is payable if Units are redeemed within 1 year.</h3>"
            "<h3>No Exit Load is payable after 1 year from the date of allotment.</h3>"
        )
        + html[end:]
    )
    page = parse_scheme_page(html)
    assert page.has_exit_load
    exit_load = dict(normalize_sections(page.sections))["Exit Load"]
    assert "1.00%" in exit_load and "after 1 year" in exit_load


def test_content_ignores_nav_returns_and_timestamps():
    """EC-ING-03: values that change on every load don't change the parsed content."""
    html = scheme_page_html()
    changed = (
        html.replace("123.4567", "130.0001")
        .replace("19.30%", "20.10%")
        .replace("25.10%", "31.00%")
        .replace("2026-11-11T09:30:00Z", "2026-11-12T10:45:00Z")
    )
    assert _sections(html) == _sections(changed)


def test_real_fact_change_changes_the_content():
    html = scheme_page_html()
    assert _sections(html) != _sections(html.replace("<p>0.74</p>", "<p>0.81</p>"))


def test_sanity_passes_for_a_real_looking_page():
    html = scheme_page_html()
    assert check_scheme_page(parse_scheme_page(html), SCHEME_NAME, visible_text(html)) == []


def test_sanity_catches_the_wrong_scheme():
    html = scheme_page_html()
    problems = check_scheme_page(
        parse_scheme_page(html), "HDFC Small Cap Fund – Direct Growth", visible_text(html)
    )
    assert any("doesn't match" in p for p in problems)


def test_sanity_catches_a_block_page():
    """EC-ING-01: HTTP 200 with a captcha is still a failed fetch."""
    html = (
        "<html><body><h1>Access Denied</h1>"
        "<p>Please complete the captcha to continue.</p></body></html>"
    )
    problems = check_scheme_page(parse_scheme_page(html), SCHEME_NAME, visible_text(html))
    assert any("block page" in p for p in problems)
    assert any("missing facts" in p for p in problems)


GUIDE_HTML = """
<html><body><nav>Trending Searches</nav>
<h1>How to Get a Capital Gain Statement for Mutual Fund schemes</h1>
<p>Last Updated On: 3 Jul 2026</p><p>5 min read</p>
<h2>What is a Capital Gain Statement?</h2>
<p>A capital gain statement shows the gains or losses made on redeeming mutual fund units
during a financial year, split into short-term and long-term gains.</p>
<h2>4. Via CAS (Consolidated Account Statement)</h2>
<p>Request a CAS from the registrar's website by entering your email and choosing the period.</p>
<h2>Did you find this interesting?</h2><p>Yes</p><p>No</p>
<h2>OUR VISION</h2><p>To be the most respected asset manager.</p>
</body></html>
"""


def test_general_page_sections_and_date():
    page = parse_generic_page(GUIDE_HTML)
    sections = dict(normalize_sections(page.sections))
    assert page.title.startswith("How to Get a Capital Gain Statement")
    assert page.last_updated == date(2026, 7, 3)
    assert "What is a Capital Gain Statement?" in sections
    assert "4. Via CAS (Consolidated Account Statement)" in sections
    assert not any("interesting" in h for h in sections)
    assert not any("Last Updated" in t for t in sections.values())
    assert check_generic_page(page, visible_text(GUIDE_HTML)) == []


def test_general_page_too_short_fails():
    html = "<html><body><h1>Statement</h1><p>Loading…</p></body></html>"
    assert check_generic_page(parse_generic_page(html), visible_text(html))
