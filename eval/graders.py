"""Graders for the system evaluation (eval.md §11.2). One property per check, no blended score.

Grounding, value matching and sentence counting are written here independently of the app's
validator (app/guards/output_validator.py), so a validator bug is not hidden by the same bug in
the grader.
"""

import io
import re
import warnings
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.models import NO_CITATION_TYPES, ChatResponse, footer
from eval.cases import Case
from ingestion.models import Chunk

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    import pysbd

_SEGMENTER = pysbd.Segmenter(language="en", clean=False)
_FOOTER = re.compile(r"^Last updated from sources: \d{1,2} [A-Z][a-z]{2} \d{4}$")
_URLISH = re.compile(r"https?://|\bwww\.|\]\(", re.IGNORECASE)
ALLOWED_HOSTS = {
    "www.hdfcfund.com",
    "files.hdfcfund.com",
    "investor.sebi.gov.in",
    "www.amfiindia.com",
}


# --- normalization --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Case, spacing, currency marks, thousands separators and dashes don't matter."""
    text = text.lower().replace("₹", " ").replace("/-", " ").replace("–", "-").replace("‑", "-")
    text = re.sub(r"\b(?:rs|inr|re)\b\.?", " ", text)
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    return re.sub(r"\s+", "", text)


def contains(haystack: str, needle: str) -> bool:
    """Normalized substring match that respects number boundaries: "1%" is not in "0.21%"."""
    hay, want = normalize(haystack), normalize(needle)
    if not want:
        return False
    before = r"(?<![\d.])" if want[0].isdigit() else ""
    after = r"(?!\d)" if want[-1].isdigit() else ""
    return re.search(before + re.escape(want) + after, hay) is not None


def on_page(page_text: str, value: str) -> bool:
    """A value on a live page. Scheme pages show the TER as a bare number ("0.79") in its box,
    so a percentage also counts without its "%"."""
    if contains(page_text, value):
        return True
    bare = value.strip().removesuffix("%")
    return (
        bare != value.strip()
        and bool(re.fullmatch(r"\d+(?:\.\d+)?", bare))
        and contains(page_text, bare)
    )


def numbers(text: str) -> set[str]:
    """Every number, as a canonical string: '1,000' → '1000', '1.00' → '1', '05' → '5'."""
    found = set()
    for raw in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        found.add(value.lstrip("0") or "0")
    return found


def sentences(text: str) -> int:
    total = 0
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip()
        if line:
            line = re.sub(r"\bapprox\.(?=\s)", "approx", line, flags=re.IGNORECASE)
            total += sum(1 for s in _SEGMENTER.segment(line) if s.strip())
    return total


# --- results --------------------------------------------------------------------------------


@dataclass
class Grades:
    checks: dict[str, bool | None] = field(default_factory=dict)  # None = not applicable
    notes: dict[str, str] = field(default_factory=dict)

    def set(self, name: str, ok: bool | None, note: str = "") -> None:
        self.checks[name] = ok
        if note and ok is False:
            self.notes[name] = note

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.checks.items() if v is False]


# --- graders --------------------------------------------------------------------------------


def grade(
    case: Case,
    response: ChatResponse,
    *,
    cited_doc_id: str | None,
    cited_chunk: Chunk | None,
    doc_as_of: str | None,
    llm_calls: int,
    logged_text: str,
) -> Grades:
    g = Grades()
    g.set("type_correct", response.type in case.expected_type, f"got {response.type}")

    # Value: only answer cases that carry expected values, and only when the answer was given.
    if case.has_value and "answer" in case.expected_type:
        if response.type != "answer":
            g.set("value_correct", None if not case.answerable else False, f"type {response.type}")
        else:
            any_ok = not case.accept or any(contains(response.text, a) for a in case.accept)
            all_ok = all(contains(response.text, a) for a in case.accept_all)
            g.set("value_correct", any_ok and all_ok, f"expected {case.accept or case.accept_all}")

    # Grounding: every number in the answer is in the cited chunk, its as-of date or the question.
    if response.type == "answer":
        if cited_chunk is None:
            g.set("grounded", False, "no cited chunk recorded")
        else:
            allowed = numbers(cited_chunk.text) | numbers(case.question)
            for part in (doc_as_of or "", cited_chunk.doc_as_of or ""):
                allowed |= numbers(part.replace("-", " "))
            extra = sorted(numbers(response.text) - allowed)
            g.set("grounded", not extra, f"not in source: {extra}")

    # Citation correctness: the right document, or the right fixed link.
    if response.citation is not None and (case.expected_citation or case.expected_citation_url):
        if case.expected_citation_url:
            ok = response.citation.url == case.expected_citation_url
            g.set("citation_correct", ok, f"got {response.citation.url}")
        elif response.type in case.expected_type and response.type != "refusal":
            ok = cited_doc_id in (case.expected_citation or [])
            g.set("citation_correct", ok, f"got {cited_doc_id}")

    # Format: ≤ 3 sentences, one link (none for clarify/greeting), footer, no links in the text.
    problems = []
    if sentences(response.text) > 3:
        problems.append("more than 3 sentences")
    if _URLISH.search(response.text):
        problems.append("link in the answer text")
    if response.type in NO_CITATION_TYPES:
        if response.citation is not None or response.last_updated is not None:
            problems.append("clarify/greeting must have no link and no footer")
    else:
        if response.citation is None:
            problems.append("missing citation")
        if response.last_updated is None or not _FOOTER.match(footer(response.last_updated)):
            problems.append("missing or malformed footer")
    g.set("format_ok", not problems, "; ".join(problems))

    # Advice phrases (the LLM judge is applied by the runner on model-written answers).
    hits = [p for p in case.must_not_contain if p.lower() in response.text.lower()]
    g.set("no_advice_phrases", not hits, f"contains {hits}")

    # PII: no LLM call, and the made-up value is nowhere in the logs.
    if case.group == "pii":
        leaked = bool(case.pii_value) and case.pii_value in logged_text
        g.set(
            "pii_not_forwarded",
            llm_calls == 0 and not leaked,
            f"llm calls {llm_calls}, in logs {leaked}",
        )

    # Not covered: never a corpus scheme's facts.
    if case.group == "not_covered":
        g.set("not_misrouted", response.type != "answer", "answered with corpus facts")

    # Advisory and false refusals.
    if case.group == "advisory" or (case.expected_type == ["refusal"]):
        g.set("refused", response.type == "refusal", f"got {response.type}")
    if case.answerable:
        g.set(
            "not_falsely_refused",
            response.type not in ("refusal", "performance_redirect", "out_of_scope"),
        )
    return g


# --- citation validity (live) ---------------------------------------------------------------


class CitationChecker:
    """URL on the allowlist, HTTP 200 now, and the expected value in the freshly fetched page."""

    def __init__(self, client):
        self.client = client
        self._cache: dict[str, tuple[int | None, str, str]] = {}

    def _fetch(self, url: str) -> tuple[int | None, str, str]:
        if url not in self._cache:
            try:
                r = self.client.get(url, follow_redirects=True, timeout=30)
                text = _page_text(r.content, r.headers.get("content-type", ""))
                self._cache[url] = (r.status_code, text, "")
            except Exception as exc:  # unreachable: BLOCKED, not a model failure
                self._cache[url] = (None, "", type(exc).__name__)
        return self._cache[url]

    def check(self, case: Case, response: ChatResponse) -> tuple[bool | None, str]:
        """(ok, note). ok is None when the source can't be reached (BLOCKED)."""
        if response.citation is None:
            return None, ""
        url = response.citation.url
        if urlparse(url).hostname not in ALLOWED_HOSTS:
            return False, f"host not allowed: {url}"
        status, text, error = self._fetch(url)
        if status is None:
            return None, f"BLOCKED: {error}"
        if status != 200:
            return False, f"HTTP {status}"
        if response.type == "answer" and case.has_value:
            wanted = case.accept_all or case.accept
            present = [a for a in wanted if on_page(text, a)]
            ok = (
                bool(present)
                if case.accept and not case.accept_all
                else len(present) == len(wanted)
            )
            return ok, "" if ok else f"none of {wanted} in the fetched page"
        return True, ""


def _page_text(content: bytes, content_type: str) -> str:
    if content[:4] == b"%PDF" or "pdf" in content_type:
        import pymupdf

        with pymupdf.open(stream=io.BytesIO(content), filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)
    from bs4 import BeautifulSoup

    return BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
