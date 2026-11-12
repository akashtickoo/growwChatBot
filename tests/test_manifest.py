"""EV-1-01 / EV-1-02: the corpus is official HDFC MF pages only; Groww only identifies (D1)."""

from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from ingestion.manifest import (
    EducationLinks,
    ManifestEntry,
    SourceManifest,
    host_allowed,
    linked_doc_id,
    load_education_links,
    load_schemes,
    load_sources,
)

# The 7 Groww URLs from the problem statement. They identify the schemes and nothing more.
PROBLEM_STATEMENT_GROWW_URLS = {
    "hdfc-large-cap": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
    "hdfc-mid-cap": "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth",
    "hdfc-small-cap": "https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth",
    "hdfc-multi-cap": "https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth",
    "hdfc-elss-tax-saver": (
        "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
    ),
    "hdfc-gold-etf-fof": (
        "https://groww.in/mutual-funds/hdfc-gold-etf-fund-of-fund-direct-plan-growth"
    ),
    "hdfc-silver-etf-fof": "https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth",
}

# The official HDFC MF Direct-plan page for each scheme (Phase 1 corpus table).
OFFICIAL_SCHEME_PAGES = {
    "hdfc-large-cap": "https://www.hdfcfund.com/explore/mutual-funds/hdfc-large-cap-fund/direct",
    "hdfc-mid-cap": "https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct",
    "hdfc-small-cap": "https://www.hdfcfund.com/explore/mutual-funds/hdfc-small-cap-fund/direct",
    "hdfc-multi-cap": "https://www.hdfcfund.com/explore/mutual-funds/hdfc-multi-cap-fund/direct",
    "hdfc-elss-tax-saver": (
        "https://www.hdfcfund.com/explore/mutual-funds/hdfc-elss-tax-saver-fund/direct"
    ),
    "hdfc-gold-etf-fof": (
        "https://www.hdfcfund.com/explore/mutual-funds/hdfc-gold-etf-fund-fund/direct"
    ),
    "hdfc-silver-etf-fof": (
        "https://www.hdfcfund.com/explore/mutual-funds/hdfc-silver-etf-fund-fund/direct"
    ),
}

OFFICIAL_INGESTION_HOSTS = {"www.hdfcfund.com", "files.hdfcfund.com"}
OFFICIAL_LINK_HOSTS = {"investor.sebi.gov.in", "www.sebi.gov.in", "www.amfiindia.com"}


def test_groww_refs_are_the_problem_statement_urls():
    schemes = load_schemes().schemes
    assert {s.id: str(s.groww_ref) for s in schemes} == PROBLEM_STATEMENT_GROWW_URLS


def test_no_groww_url_is_a_source():
    """D1: Groww pages are never fetched, indexed or cited."""
    manifest = load_sources()
    for domain in manifest.allowed_domains:
        assert "groww" not in domain
    for doc in manifest.documents:
        assert "groww" not in (urlsplit(str(doc.url)).hostname or ""), doc.id


def test_only_official_hdfc_hosts_are_allowed():
    assert set(load_sources().allowed_domains) <= OFFICIAL_INGESTION_HOSTS


def test_each_scheme_has_its_official_direct_page():
    schemes = load_schemes().schemes
    docs = {d.id: d for d in load_sources().documents}
    pages = {}
    for scheme in schemes:
        doc = docs[scheme.source_doc_id]
        assert doc.doc_type == "SCHEME_PAGE"
        assert doc.scheme_id == scheme.id
        pages[scheme.id] = str(doc.url)
    assert pages == OFFICIAL_SCHEME_PAGES


def test_every_scheme_page_is_the_direct_plan():
    """D11: only Direct plans are covered."""
    for doc in load_sources().documents:
        if doc.doc_type == "SCHEME_PAGE":
            assert str(doc.url).endswith("/direct"), doc.id


def test_every_scheme_page_links_its_sid_and_kim():
    for doc in load_sources().documents:
        if doc.doc_type == "SCHEME_PAGE":
            types = {spec.doc_type for spec in doc.linked_documents}
            assert {"SID", "KIM"} <= types, doc.id


def test_statement_pages_and_factsheet_index_are_present():
    types = [d.doc_type for d in load_sources().documents]
    assert types.count("SERVICE_PAGE") >= 1
    assert types.count("FACTSHEET_INDEX") == 1


def test_education_links_are_official():
    education = load_education_links()
    assert set(education.allowed_domains) <= OFFICIAL_LINK_HOSTS
    used_in = {use for link in education.links.values() for use in link.used_in}
    assert {"refusal", "pii_blocked"} <= used_in


def test_every_alias_contains_hdfc():
    """EC-SCH-07: generic aliases like 'small cap' could match another AMC's fund."""
    for scheme in load_schemes().schemes:
        for alias in scheme.aliases:
            assert "hdfc" in alias.lower(), (scheme.id, alias)


def test_aliases_are_unique_across_schemes():
    seen: dict[str, str] = {}
    for scheme in load_schemes().schemes:
        for alias in scheme.aliases:
            key = alias.lower()
            assert key not in seen, f"{alias!r} used by {seen.get(key)} and {scheme.id}"
            seen[key] = scheme.id


def test_linked_doc_id():
    assert linked_doc_id("hdfc-mid-cap", "FUND_FACTS") == "hdfc-mid-cap-fund-facts"
    assert linked_doc_id("hdfc-mid-cap", "KIM") == "hdfc-mid-cap-kim"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.hdfcfund.com/explore/x", True),
        ("https://WWW.HDFCFUND.COM/explore/x", True),
        ("https://files.hdfcfund.com/a.pdf", True),
        ("https://hdfcfund.com/explore/x", False),
        ("https://www.hdfcfund.com.example.com/x", False),
        ("https://example.com/www.hdfcfund.com", False),
    ],
)
def test_host_allowed_is_an_exact_match(url, allowed):
    assert host_allowed(url, ["www.hdfcfund.com", "files.hdfcfund.com"]) is allowed


def _entry(**overrides) -> dict:
    entry = {
        "id": "d1",
        "scheme_id": "s1",
        "doc_type": "SCHEME_PAGE",
        "url": "https://www.hdfcfund.com/a",
        "format": "html",
        "refresh": "weekly",
    }
    return {**entry, **overrides}


def _manifest(*entries: dict) -> dict:
    return {"allowed_domains": ["www.hdfcfund.com"], "documents": list(entries)}


@pytest.mark.parametrize(
    "url",
    ["https://groww.in/mutual-funds/x", "http://www.hdfcfund.com/a"],
    ids=["groww", "plain-http"],
)
def test_manifest_rejects_bad_urls(url):
    with pytest.raises(ValidationError):
        SourceManifest.model_validate(_manifest(_entry(url=url)))


def test_manifest_rejects_duplicate_ids():
    with pytest.raises(ValidationError, match="duplicate"):
        SourceManifest.model_validate(_manifest(_entry(), _entry(url="https://www.hdfcfund.com/b")))


def test_scheme_page_needs_a_scheme():
    with pytest.raises(ValidationError, match="scheme_id"):
        ManifestEntry.model_validate(_entry(scheme_id=None))


def test_only_scheme_pages_link_documents():
    spec = [{"doc_type": "SID", "link_text_prefix": "SID - "}]
    with pytest.raises(ValidationError, match="linked documents"):
        ManifestEntry.model_validate(
            _entry(scheme_id=None, doc_type="SERVICE_PAGE", linked_documents=spec)
        )


def test_education_links_reject_other_sites():
    data = {
        "allowed_domains": ["investor.sebi.gov.in"],
        "links": {"x": {"title": "X", "url": "https://example.com/", "used_in": ["refusal"]}},
    }
    with pytest.raises(ValidationError):
        EducationLinks.model_validate(data)
