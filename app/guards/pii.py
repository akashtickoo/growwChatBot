"""PII guard (architecture §6.2). Runs first on every message.

`detect_pii` returns only the KINDS of personal data found, never the matched text, so nothing
sensitive is passed on or logged. A message with any match is dropped: not logged, not sent to
retrieval or the LLM (EC-PII-10).
"""

import re
import unicodedata

# --- Verhoeff checksum (Aadhaar numbers carry one) ---------------------------------------

_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_valid(number: str) -> bool:
    c = 0
    for i, digit in enumerate(reversed(number)):
        c = _D[c][_P[i % 8][int(digit)]]
    return c == 0


def verhoeff_check_digit(number: str) -> str:
    """The digit to append so that `number + digit` is Verhoeff-valid (used by tests)."""
    c = 0
    for i, digit in enumerate(reversed(number)):
        c = _D[c][_P[(i + 1) % 8][int(digit)]]
    return str(_INV[c])


# --- patterns ----------------------------------------------------------------------------

# PAN: 5 letters, 4 digits, 1 letter. The 4th letter is the holder type, which keeps ordinary
# words out ("funds 2025 a" has 'd' there).
_PAN = re.compile(r"\b[a-z]{3}[abcfghljpt][a-z]\s?\d{4}\s?[a-z]\b", re.IGNORECASE)
_AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
_AADHAAR_MASKED = re.compile(r"\b[x*]{4}[\s-]?[x*]{4}[\s-]?\d{4}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_OTP = re.compile(
    r"\b(?:otp|one[\s-]?time\s+(?:password|pin|code)|verification\s+code|\bpin)\b"
    r"\D{0,15}\d{4,8}(?!\d)",
    re.IGNORECASE,
)
_FOLIO = re.compile(r"\bfolio\b\D{0,15}\d[\d/ -]{4,}", re.IGNORECASE)
_ACCOUNT = re.compile(
    r"\b(?:a/c|acct|account|demat)(?:\s+(?:no|number))?\.?\D{0,10}\d{6,}", re.IGNORECASE
)
_LONG_DIGITS = re.compile(r"(?<![\d.,])\d{9,18}(?![\d.,])")
# Amounts are not account numbers (EC-PII-13).
_AMOUNT_BEFORE = re.compile(r"(?:₹|rs\.?|inr|re\.?)\s*$", re.IGNORECASE)
_AMOUNT_AFTER = re.compile(
    r"^\s*(?:/-|rupees?|rs\b|inr\b|lakhs?|lacs?|crores?|cr\b|%)", re.IGNORECASE
)


def _is_amount(text: str, start: int, end: int) -> bool:
    return bool(_AMOUNT_BEFORE.search(text[max(0, start - 6) : start])) or bool(
        _AMOUNT_AFTER.match(text[end : end + 10])
    )


def detect_pii(text: str) -> list[str]:
    """Kinds of personal data in `text`: pan, aadhaar, phone, email, otp, account. Empty if none."""
    text = unicodedata.normalize("NFKC", text)
    kinds: set[str] = set()
    taken: list[tuple[int, int]] = []  # digit spans already classified

    def free(match: re.Match) -> bool:
        start, end = match.span()
        return not _is_amount(text, start, end) and all(end <= a or start >= b for a, b in taken)

    if _PAN.search(text):
        kinds.add("pan")
    if _AADHAAR_MASKED.search(text):
        kinds.add("aadhaar")
    if _EMAIL.search(text):
        kinds.add("email")
    if _OTP.search(text):
        kinds.add("otp")
    if _FOLIO.search(text) or _ACCOUNT.search(text):
        kinds.add("account")
    for match in _PHONE.finditer(text):
        if free(match):
            kinds.add("phone")
            taken.append(match.span())
    for match in _AADHAAR.finditer(text):
        if free(match):
            digits = re.sub(r"\D", "", match.group())
            # A 12-digit number that fails the checksum still counts as an account number.
            kinds.add("aadhaar" if verhoeff_valid(digits) else "account")
            taken.append(match.span())
    for match in _LONG_DIGITS.finditer(text):
        if free(match):
            kinds.add("account")
    return sorted(kinds)


def contains_pii(text: str) -> bool:
    return bool(detect_pii(text))
