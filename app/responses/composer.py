"""Build the final `ChatResponse` (architecture §6.8).

Every type except `clarify` and `greeting` carries exactly one link and a footer date
(EC-CIT-01/02):
- a registry document (answer, not_found, performance, not covered): its `fetched_at`
- a fixed education link (refusal, pii_blocked, out_of_scope, not_found without a scheme):
  the newest `fetched_at` in the registry, i.e. when the sources were last checked.
Dates are stored in UTC and shown as the Asia/Kolkata date (EC-CIT-03).
"""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app.models import ChatResponse, Citation, ResponseType
from app.responses.documents import DocInfo, DocumentLookup
from ingestion.manifest import (
    EducationLinks,
    Scheme,
    linked_doc_id,
    load_education_links,
    load_schemes,
)
from ingestion.models import Chunk

TEMPLATES_PATH = Path(__file__).resolve().parents[2] / "config" / "templates.yaml"
FACTSHEETS_DOC_ID = "hdfc-factsheets"  # for the two FoFs, which have no Fund Facts PDF


def load_templates(path: Path = TEMPLATES_PATH) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {key: " ".join(str(text).split()) for key, text in data.items()}


def short_name(scheme: Scheme) -> str:
    """'HDFC Mid Cap Fund – Direct Growth' → 'HDFC Mid Cap Fund'."""
    return scheme.name.split(" – ")[0]


class Composer:
    def __init__(
        self,
        documents: DocumentLookup,
        *,
        timezone: str = "Asia/Kolkata",
        templates: dict[str, str] | None = None,
        education: EducationLinks | None = None,
        schemes: list[Scheme] | None = None,
    ):
        self.documents = documents
        self.tz = ZoneInfo(timezone)
        self.templates = templates or load_templates()
        self.education = education or load_education_links()
        self.schemes = {s.id: s for s in (schemes or load_schemes().schemes)}
        self.scheme_list = ", ".join(short_name(s) for s in self.schemes.values())

    # --- helpers -------------------------------------------------------------------------

    def local_date(self, moment: datetime | None) -> date | None:
        return moment.astimezone(self.tz).date() if moment else None

    def _text(self, key: str, scheme_id: str | None = None) -> str:
        scheme = self.schemes.get(scheme_id or "")
        return self.templates[key].format(
            scheme=short_name(scheme) if scheme else "", schemes=self.scheme_list
        )

    def _response(
        self, kind: ResponseType, text: str, citation: Citation | None, updated: date | None
    ) -> ChatResponse:
        return ChatResponse(
            type=kind, text=text, citation=citation, last_updated=updated, request_id=""
        )

    def _education(self, kind: ResponseType, text: str, link: str) -> ChatResponse:
        edu = self.education.links[link]
        return self._response(
            kind,
            text,
            Citation(url=str(edu.url), title=edu.title),
            self.local_date(self.documents.latest_fetch()),
        )

    def _document(self, kind: ResponseType, text: str, doc: DocInfo) -> ChatResponse:
        moment = doc.fetched_at or self.documents.latest_fetch()
        return self._response(
            kind, text, Citation(url=doc.url, title=doc.title), self.local_date(moment)
        )

    def _scheme_page(self, scheme_id: str | None) -> DocInfo | None:
        scheme = self.schemes.get(scheme_id or "")
        return self.documents.get(scheme.source_doc_id) if scheme else None

    def _with_doc(
        self, kind: ResponseType, text: str, doc: DocInfo | None, fallback_link: str
    ) -> ChatResponse:
        # A registry row should always exist; if not, keep the one-link rule with a fixed link.
        if doc is None:
            return self._education(kind, text, fallback_link)
        return self._document(kind, text, doc)

    # --- response types ------------------------------------------------------------------

    def answer(self, text: str, chunk: Chunk) -> ChatResponse | None:
        """The model's answer, cited to the document the chunk came from. None if unknown."""
        doc = self.documents.get(chunk.doc_id)
        if doc is None:
            return None
        return self._document("answer", " ".join(text.split()), doc)

    def refusal(self, *, mixed: bool = False) -> ChatResponse:
        key = "refusal_mixed" if mixed else "refusal"
        return self._education("refusal", self._text(key), "sebi_investor")

    def pii_blocked(self) -> ChatResponse:
        return self._education("pii_blocked", self._text("pii_blocked"), "sebi_investor")

    def out_of_scope(self) -> ChatResponse:
        return self._education("out_of_scope", self._text("out_of_scope"), "amfi_investor")

    def performance(self, scheme_id: str | None, *, nav: bool = False) -> ChatResponse:
        if nav:  # EC-PERF-05: no figure, link the scheme page
            if scheme_id in self.schemes:
                text = self._text("nav", scheme_id)
                return self._with_doc(
                    "performance_redirect", text, self._scheme_page(scheme_id), "amfi_investor"
                )
            doc = self.documents.get(FACTSHEETS_DOC_ID)
            return self._with_doc(
                "performance_redirect", self._text("nav_general"), doc, "amfi_investor"
            )
        if scheme_id in self.schemes:
            doc = self.documents.get(linked_doc_id(scheme_id, "FUND_FACTS"))
            doc = doc or self.documents.get(FACTSHEETS_DOC_ID)
            text = self._text("performance_redirect", scheme_id)
        else:
            doc = self.documents.get(FACTSHEETS_DOC_ID)
            text = self._text("performance_redirect_general")
        return self._with_doc("performance_redirect", text, doc, "amfi_investor")

    def not_covered(self, reason: str, closest: str | None) -> ChatResponse:
        """Regular / IDCW / ETF name the covered scheme and link its page (EC-SCH-05/09)."""
        if reason in ("regular_plan", "idcw", "etf") and closest in self.schemes:
            text = self._text(f"not_covered_{reason}", closest)
            return self._with_doc("out_of_scope", text, self._scheme_page(closest), "amfi_investor")
        key = {
            "regular_plan": "not_covered_regular_plan_general",
            "idcw": "not_covered_idcw_general",
            "other_amc": "not_covered_other_amc",
            "other_hdfc_scheme": "not_covered_other_hdfc_scheme",
        }.get(reason, "out_of_scope")
        return self._education("out_of_scope", self._text(key), "amfi_investor")

    def not_found(self, scheme_id: str | None) -> ChatResponse:
        if scheme_id in self.schemes:
            text = self._text("not_found", scheme_id)
            return self._with_doc("not_found", text, self._scheme_page(scheme_id), "amfi_investor")
        return self._education("not_found", self._text("not_found_general"), "amfi_investor")

    def clarify(self, *, several: bool) -> ChatResponse:
        key = "clarify_several" if several else "clarify_needs_scheme"
        return self._response("clarify", self._text(key), None, None)

    def greeting(self) -> ChatResponse:
        return self._response("greeting", self._text("greeting"), None, None)
