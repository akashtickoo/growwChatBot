"""Text normalization (architecture §5.3)."""

import re
import unicodedata

# "Rs.100", "Rs 100", "INR 100", "Re. 1" -> "₹100", "₹1". Only when a number follows.
_CURRENCY = re.compile(r"\b(?:Rs\.?|INR|Re\.)\s*(?=\d)")
_RUPEE_SPACE = re.compile(r"₹\s+(?=\d)")
# A word broken across lines at a hyphen: keep the hyphen ("switch-\nin" -> "switch-in").
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")
_SPACES = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\s*\n\s*")


def normalize_text(text: str) -> str:
    """NFKC, one currency style, joined line-break hyphens, collapsed whitespace.

    Single line breaks are kept (they separate list items); runs of blank lines are collapsed.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _CURRENCY.sub("₹", text)
    text = _RUPEE_SPACE.sub("₹", text)
    text = _LINE_BREAK_HYPHEN.sub(r"\1-\2", text)
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    return text.strip()


def squash(text: str) -> str:
    """Lowercase letters and digits only, for loose name comparisons."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", text).lower())
