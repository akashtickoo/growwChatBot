"""Output validator (architecture §6.7). Every `answered` reply must pass every check.

Returns the failed checks, each with a short code (logged) and a message (sent back to the model
in the one retry). Statuses other than `answered` aren't checked here: the pipeline maps them to
templates.
"""

import re
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.llm.schemas import LLMAnswer
from ingestion.models import Chunk

with warnings.catch_warnings():  # pysbd's language modules have invalid escape sequences
    warnings.simplefilter("ignore", SyntaxWarning)
    import pysbd

MAX_SENTENCES = 3
_SEGMENTER = pysbd.Segmenter(language="en", clean=False)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LINK = re.compile(r"https?://|\bwww\.|\[[^\]]*\]\([^)]*\)", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•▪●]|\d+[.)])\s+")
# Strongly advisory phrases only, so legitimate text ("for better liquidity") passes (EC-FMT-04).
_ADVICE = re.compile(
    r"\b(you should (?:invest|buy|consider|choose|switch|redeem|sell|hold)"
    r"|(?:i|we) (?:would )?(?:recommend|suggest|advise)"
    r"|good (?:choice|investment|option|time to invest)|better than|best fund"
    r"|worth investing|ideal for you|suitable for you|safe investment)\b",
    re.IGNORECASE,
)
_RETURNS = re.compile(
    r"\b(?:cagr|xirr|annuali[sz]ed returns?|gave\s+(?:a\s+)?returns?)\b"
    r"|\breturns? of\s*(?:₹|rs\.?\s*)?\d"
    r"|\breturned\s+\d"
    r"|\d+(?:\.\d+)?\s*%\s*(?:annual\w*\s+)?returns?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Failure:
    check: str  # source | empty | length | language | link | grounding | advice | returns
    message: str


@dataclass
class Validation:
    failures: list[Failure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def checks(self) -> list[str]:
        return [f.check for f in self.failures]

    @property
    def messages(self) -> list[str]:
        return [f.message for f in self.failures]


def count_sentences(text: str) -> int:
    """Sentences in the answer text. Each line or bullet counts at least once (EC-FMT-02)."""
    count = 0
    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line:
            continue
        line = re.sub(r"\bapprox\.(?=\s)", "approx", line, flags=re.IGNORECASE)
        count += len([s for s in _SEGMENTER.segment(line) if s.strip()])
    return count


def _canonical(number: str) -> str:
    """'1,000' → '1000', '1.00' → '1', '0.750' → '0.75'."""
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return number
    return format(value.normalize(), "f")


def numbers_in(text: str) -> set[str]:
    return {_canonical(n) for n in _NUMBER.findall(text)}


def _date_parts(iso_date: str | None) -> set[str]:
    """'2025-11-21' → {'2025', '11', '21'} so "21 Nov 2025" or "21/11/2025" are grounded."""
    if not iso_date:
        return set()
    return {_canonical(p) for p in iso_date.split("-") if p.isdigit()}


def _english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    non_latin = sum(1 for c in letters if not ("a" <= c.lower() <= "z"))
    return non_latin / len(letters) <= 0.1


def validate(
    reply: LLMAnswer,
    chunks: Sequence[Chunk],
    *,
    question: str = "",
    extra_dates: Iterable[str | None] = (),
) -> Validation:
    """Check an `answered` reply against the chunks the model was given.

    Grounding accepts numbers from the cited chunk's text, its `doc_as_of` (EC-FLD-12) and any
    `extra_dates` (the cited document's as-of date from the registry), plus numbers the user
    wrote in the question ("after 6 months").
    """
    result = Validation()
    if reply.status != "answered":
        return result
    fail = result.failures.append

    text = reply.answer.strip()
    if not text:
        fail(Failure("empty", "status is answered but the answer text is empty"))
        return result

    by_id = {c.chunk_id: c for c in chunks}
    cited = by_id.get(reply.source_id)
    if cited is None:
        fail(Failure("source", "source_id must be the id of one of the given sources"))

    if (n := count_sentences(text)) > MAX_SENTENCES:
        fail(Failure("length", f"the answer has {n} sentences; the limit is {MAX_SENTENCES}"))
    if not _english(text):
        fail(Failure("language", "the answer must be in English"))
    if _LINK.search(text):
        fail(Failure("link", "the answer must not contain URLs or links"))
    if _ADVICE.search(text):
        fail(Failure("advice", "the answer must not give advice or recommendations"))
    if _RETURNS.search(text):
        fail(Failure("returns", "the answer must not contain return or performance figures"))

    if cited is not None:
        allowed = numbers_in(cited.text) | numbers_in(question) | _date_parts(cited.doc_as_of)
        for extra in extra_dates:
            allowed |= _date_parts(extra)
        missing = sorted(numbers_in(text) - allowed)
        if missing:
            fail(
                Failure(
                    "grounding",
                    "these numbers are not in the cited source: "
                    + ", ".join(missing)
                    + ". Copy numbers exactly as written in the source",
                )
            )
    return result
