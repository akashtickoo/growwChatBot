"""Find a scheme page's SID, KIM and Fund Facts PDFs by the start of their link text.

Their URLs carry dates and version IDs that change when HDFC MF publishes a new version,
so they are looked up on the page each time instead of being stored in the manifest.
"""

from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ingestion.manifest import LinkedDocumentSpec


@dataclass(frozen=True)
class LinkedDocument:
    doc_type: str
    title: str  # the link text, e.g. "KIM - HDFC Mid Cap Fund"
    url: str


def find_linked_documents(
    html: str, page_url: str, specs: list[LinkedDocumentSpec]
) -> tuple[list[LinkedDocument], list[LinkedDocumentSpec]]:
    """Return (found, missing). Takes the first PDF link whose text starts with each prefix."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = [
        (a.get_text(" ", strip=True), urljoin(page_url, a["href"]))
        for a in soup.find_all("a", href=True)
    ]

    found: list[LinkedDocument] = []
    missing: list[LinkedDocumentSpec] = []
    for spec in specs:
        match = next(
            (
                (text, url)
                for text, url in anchors
                if text.startswith(spec.link_text_prefix) and ".pdf" in url.lower()
            ),
            None,
        )
        if match is None:
            missing.append(spec)
        else:
            found.append(LinkedDocument(spec.doc_type, match[0], match[1]))
    return found, missing
