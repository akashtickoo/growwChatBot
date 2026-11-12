"""Source checker and linked-PDF finder, with a fake network (no real requests)."""

import httpx

from ingestion.check_sources import check_url
from ingestion.linked_docs import find_linked_documents
from ingestion.manifest import LinkedDocumentSpec

ALLOWED = ["www.hdfcfund.com", "files.hdfcfund.com"]
PAGE_URL = "https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct"
PDF_URL = "https://files.hdfcfund.com/s3fs-public/KIM/KIM%20-%20HDFC%20Mid%20Cap%20Fund.pdf"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _html(_request: httpx.Request, body: str = "<html>page</html>") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)


def _pdf(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7")


# --- check_url ---------------------------------------------------------------------------


def test_html_page_passes_and_returns_text():
    result, body = check_url(_client(_html), "p", PAGE_URL, ALLOWED, "html")
    assert result.ok and result.status == 200
    assert body == "<html>page</html>"


def test_pdf_passes_without_returning_a_body():
    result, body = check_url(_client(_pdf), "k", PDF_URL, ALLOWED, "pdf")
    assert result.ok and body is None


def test_wrong_type_fails():
    result, _ = check_url(_client(_html), "k", PDF_URL, ALLOWED, "pdf")
    assert not result.ok and "not PDF" in result.detail


def test_not_found_fails():
    result, _ = check_url(_client(lambda r: httpx.Response(404)), "p", PAGE_URL, ALLOWED, "html")
    assert not result.ok and result.detail == "HTTP 404"


def test_redirect_off_the_allowlist_fails():
    """EV-2-06 / EC-ING-05: every redirect hop is checked, not only the first URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.hdfcfund.com":
            return httpx.Response(302, headers={"location": "https://example.com/elsewhere"})
        return _html(request)

    result, body = check_url(_client(handler), "p", PAGE_URL, ALLOWED, "html")
    assert not result.ok and "off the allowlist" in result.detail and body is None


def test_redirect_within_the_allowlist_passes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/direct"):
            return httpx.Response(301, headers={"location": "https://www.hdfcfund.com/new"})
        return _html(request)

    result, _ = check_url(_client(handler), "p", PAGE_URL, ALLOWED, "html")
    assert result.ok and "redirected" in result.detail


def test_connection_error_fails_cleanly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    result, _ = check_url(_client(handler), "p", PAGE_URL, ALLOWED, "html")
    assert not result.ok and "ConnectError" in result.detail


def test_off_allowlist_url_is_never_requested():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return _html(request)

    url = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
    result, _ = check_url(_client(handler), "p", url, ALLOWED, "html")
    assert not result.ok and requested == []


# --- find_linked_documents ---------------------------------------------------------------

SPECS = [
    LinkedDocumentSpec(doc_type="SID", link_text_prefix="SID - "),
    LinkedDocumentSpec(doc_type="KIM", link_text_prefix="KIM - "),
    LinkedDocumentSpec(doc_type="FUND_FACTS", link_text_prefix="Fund Facts - "),
]

PAGE_HTML = """
<a href="https://files.hdfcfund.com/SID/SID%20-%20HDFC%20Mid%20Cap.pdf?VersionId=1">
  SID - HDFC Mid Cap Fund</a>
<a href="/s3fs-public/KIM/kim.pdf">KIM - HDFC Mid Cap Fund</a>
<a href="https://files.hdfcfund.com/Others/leaflet.pdf">Leaflet - HDFC Mid Cap Fund</a>
<a href="https://www.hdfcfund.com/fund-facts">Fund Facts - HDFC Mid Cap Fund</a>
"""


def test_finds_documents_by_link_text_and_resolves_relative_links():
    found, _ = find_linked_documents(PAGE_HTML, PAGE_URL, SPECS)
    by_type = {d.doc_type: d for d in found}

    assert by_type["SID"].title == "SID - HDFC Mid Cap Fund"
    assert by_type["SID"].url.startswith("https://files.hdfcfund.com/SID/")
    assert by_type["KIM"].url == "https://www.hdfcfund.com/s3fs-public/KIM/kim.pdf"


def test_a_link_that_is_not_a_pdf_counts_as_missing():
    found, missing = find_linked_documents(PAGE_HTML, PAGE_URL, SPECS)
    assert "FUND_FACTS" not in {d.doc_type for d in found}
    assert [m.doc_type for m in missing] == ["FUND_FACTS"]


def test_unrelated_links_are_ignored():
    found, missing = find_linked_documents(PAGE_HTML, PAGE_URL, SPECS[:2])
    assert {d.doc_type for d in found} == {"SID", "KIM"} and missing == []
