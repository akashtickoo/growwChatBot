"""Check every source: manifest pages, the PDFs they link to, and the fixed education links.

For each URL: on its allowlist (every redirect hop too), HTTP 200, and the expected type
(HTML or PDF). Pages are read only to find their PDF links; nothing is saved. PDFs are not
downloaded: only their response headers are read.

Usage: python -m ingestion.check_sources
"""

import sys
from dataclasses import dataclass
from typing import Literal

import httpx

from ingestion.linked_docs import find_linked_documents
from ingestion.manifest import host_allowed, linked_doc_id, load_education_links, load_sources

USER_AGENT = "faq-assistant-check/0.1"
TIMEOUT_S = 20.0
EXPECTED_TYPE = {"html": "text/html", "pdf": "application/pdf"}


@dataclass
class CheckResult:
    item_id: str
    url: str
    ok: bool
    detail: str
    status: int | None = None


def check_url(
    client: httpx.Client,
    item_id: str,
    url: str,
    allowed_domains: list[str],
    expect: Literal["html", "pdf"],
) -> tuple[CheckResult, str | None]:
    """Check one URL. Returns the result and, for an HTML page that passed, its text."""

    def fail(detail: str, status: int | None = None) -> tuple[CheckResult, None]:
        return CheckResult(item_id, url, False, detail, status), None

    if not host_allowed(url, allowed_domains):
        return fail("host not on the allowlist (not requested)")

    try:
        with client.stream("GET", url) as response:
            for hop in [*response.history, response]:
                if not host_allowed(str(hop.url), allowed_domains):
                    return fail(f"redirected off the allowlist to {hop.url.host}", hop.status_code)
            if response.status_code != 200:
                return fail(f"HTTP {response.status_code}", response.status_code)
            content_type = response.headers.get("content-type", "")
            if EXPECTED_TYPE[expect] not in content_type:
                return fail(f"not {expect.upper()} ({content_type or 'no content type'})", 200)
            body = None
            if expect == "html":
                response.read()
                body = response.text
            final_url = str(response.url)
    except httpx.HTTPError as exc:
        return fail(f"request failed: {type(exc).__name__}")

    detail = "OK" if final_url == url else f"OK (redirected to {final_url})"
    return CheckResult(item_id, url, True, detail, 200), body


def run(client: httpx.Client | None = None) -> list[CheckResult]:
    manifest = load_sources()
    education = load_education_links()
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True
        )

    results: list[CheckResult] = []
    try:
        for doc in manifest.documents:
            page_url = str(doc.url)
            result, html = check_url(client, doc.id, page_url, manifest.allowed_domains, "html")
            results.append(result)
            if not doc.linked_documents:
                continue

            if html is None:
                for spec in doc.linked_documents:
                    pdf_id = linked_doc_id(doc.scheme_id, spec.doc_type)
                    results.append(CheckResult(pdf_id, "", False, "not checked: page failed"))
                continue

            found, missing = find_linked_documents(html, page_url, doc.linked_documents)
            for spec in missing:
                results.append(
                    CheckResult(
                        linked_doc_id(doc.scheme_id, spec.doc_type),
                        "",
                        False,
                        f"no PDF link starting {spec.link_text_prefix!r} on {doc.id}",
                    )
                )
            for pdf in found:
                pdf_id = linked_doc_id(doc.scheme_id, pdf.doc_type)
                pdf_result, _ = check_url(client, pdf_id, pdf.url, manifest.allowed_domains, "pdf")
                results.append(pdf_result)

        for name, link in education.links.items():
            result, _ = check_url(
                client, f"link:{name}", str(link.url), education.allowed_domains, "html"
            )
            results.append(result)
    finally:
        if owns_client:
            client.close()
    return results


def main() -> int:
    results = run()
    width = max(len(r.item_id) for r in results)
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"{mark}  {r.item_id:<{width}}  {r.detail}")
    passed = sum(r.ok for r in results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
