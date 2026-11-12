"""Map question words to fact-card fields (used by the scheme resolver and the retriever boost)."""

import re
import unicodedata

# Order matters only for display; a question can mention several fields.
FIELD_PATTERNS: dict[str, re.Pattern] = {
    "expense_ratio": re.compile(
        r"\b(expense ratio|ter|total expenses?|expense|fees?|charges?|annual cost)\b"
    ),
    "exit_load": re.compile(
        r"\b(exit load|exit fee|exit charge|redemption (?:charge|fee|load)"
        r"|load on (?:exit|redemption)"
        r"|penalty (?:on|for) (?:redemption|withdrawal|exit))\b"
    ),
    "min_sip": re.compile(
        r"\b(min(?:imum)?\.? sip|sip (?:amount|minimum)|smallest sip|lowest sip"
        r"|start (?:a )?sip with"
        r"|minimum (?:monthly )?(?:sip )?instal?ments?)\b"
        r"|\bminimum\b.{0,30}\bsip\b|\bsip\b.{0,30}\bminimum\b"
    ),
    "min_lumpsum": re.compile(
        r"\b(lump ?sum|one[- ]time (?:investment|payment|purchase)"
        r"|minimum (?:application|investment|purchase|amount)|min(?:imum)?\.? investment)\b"
    ),
    "lock_in": re.compile(r"\b(lock[\s-]?in|locked in|lock period|lockin)\b"),
    "riskometer": re.compile(
        r"\b(risk[\s-]?o[\s-]?meter|risk (?:level|category|grade|rating|profile|label)|how risky"
        r"|risky|risk)\b"
    ),
    "benchmark": re.compile(
        r"\b(benchmark|benchmarked|which index|tracks? (?:which|what) index)\b"
    ),
}

GENERAL_TOPIC = re.compile(
    r"\b(statements?|capital gains?|cas\b|consolidated account|account statement|tax statement"
    r"|download .*statement)\b"
)


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def detect_fields(text: str) -> list[str]:
    norm = _norm(text)
    found = [name for name, pattern in FIELD_PATTERNS.items() if pattern.search(norm)]
    # "minimum investment through SIP" is the SIP minimum, not the lump-sum one.
    if (
        "min_sip" in found
        and "min_lumpsum" in found
        and not re.search(r"lump ?sum|one[- ]time", norm)
    ):
        found.remove("min_lumpsum")
    return found


def is_general_topic(text: str) -> bool:
    """Questions answered from the statement pages, not from a scheme's documents."""
    return bool(GENERAL_TOPIC.search(_norm(text)))
