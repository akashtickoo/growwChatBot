"""Resolve which scheme a question is about (architecture §6.4).

Order:
1. Not-covered checks, BEFORE any fuzzy matching (EC-SCH-05/06/07/09): other AMCs, HDFC schemes
   outside the corpus, Regular plan / IDCW option, the plain Gold/Silver ETF.
2. Exact alias match (aliases all contain "hdfc").
3. Fuzzy match of "hdfc …" phrases against aliases (rapidfuzz ratio ≥ 85 on word windows).
4. Category words without "hdfc" ("ELSS", "small cap", "silver fund") → the HDFC scheme
   (architecture §2.1: name it in the answer, EC-SCH-08).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from rapidfuzz import fuzz

from app.retrieval.fields import detect_fields, is_general_topic
from ingestion.manifest import Scheme, load_schemes

Kind = Literal["one", "several", "needs_scheme", "general", "not_covered"]
FUZZY_THRESHOLD = 85

OTHER_AMCS = (
    "sbi",
    "icici",
    "prudential",
    "axis",
    "kotak",
    "nippon",
    "reliance",
    "aditya birla",
    "absl",
    "birla sun life",
    "uti",
    "mirae",
    "dsp",
    "tata",
    "parag parikh",
    "ppfas",
    "quant",
    "motilal",
    "franklin",
    "templeton",
    "invesco",
    "edelweiss",
    "canara robeco",
    "sundaram",
    "bandhan",
    "idfc",
    "hsbc",
    "pgim",
    "baroda bnp",
    "bank of india",
    "lic mf",
    "lic mutual",
    "mahindra",
    "union mutual",
    "navi",
    "groww mutual",
    "groww amc",
    "whiteoak",
    "white oak",
    "jm financial",
    "360 one",
    "bajaj finserv",
    "samco",
    "zerodha",
    "helios",
    "old bridge",
    "iti mutual",
    "taurus",
    "shriram",
    "quantum",
    "trust mf",
    "nj mutual",
    "unifi",
)
_OTHER_AMC = re.compile(r"\b(" + "|".join(re.escape(a) for a in OTHER_AMCS) + r")\b")

# HDFC MF schemes that exist but aren't in the corpus. These names are never a corpus scheme:
_OTHER_HDFC_ALWAYS = re.compile(
    r"\b(flexi cap|balanced advantage|large (?:and|&) mid cap|large mid cap|hybrid equity)\b"
)
# These words only mean another scheme next to "hdfc", and only when no corpus scheme is named
# ("HDFC Mid Cap ... NIFTY Midcap 150" is a benchmark question, not the HDFC Nifty fund).
OTHER_HDFC_SCHEMES = (
    "hybrid debt",
    "focused",
    "value fund",
    "dividend yield",
    "index fund",
    "nifty",
    "sensex",
    "liquid",
    "overnight",
    "ultra short",
    "short term",
    "corporate bond",
    "banking and psu",
    "banking & psu",
    "gilt",
    "credit risk",
    "dynamic debt",
    "medium term",
    "long duration",
    "money market",
    "floating rate",
    "arbitrage",
    "equity savings",
    "children",
    "retirement",
    "multi asset",
    "infrastructure",
    "defence",
    "manufacturing",
    "pharma",
    "healthcare",
    "technology",
    "banking and financial",
    "business cycle",
    "transportation",
    "housing",
    "mnc",
    "developed world",
    "us equity",
    "asset allocator",
    "income fund",
    "low duration",
    "capital builder",
    "tax advantage",
    "non cyclical",
    "innovation",
    "momentum",
    "quality",
    "low volatility",
)
_OTHER_HDFC = re.compile(r"\b(" + "|".join(re.escape(s) for s in OTHER_HDFC_SCHEMES) + r")\b")
_REGULAR = re.compile(r"\bregular\b(?!\s+(?:income|sip|basis|intervals?|investments?))")
_IDCW = re.compile(r"\b(idcw|dividend (?:option|plan|payout|reinvestment)|payout option)\b")
_ETF = re.compile(r"\b(gold|silver) (?:etf|exchange traded fund)\b")
_FOF = re.compile(r"\b(fof|fund of funds?)\b")

CATEGORY_WORDS = {
    "hdfc-large-cap": r"\b(large cap|top 100)\b",
    "hdfc-mid-cap": r"\b(mid cap|mid cap opportunities)\b",
    "hdfc-small-cap": r"\bsmall cap\b",
    "hdfc-multi-cap": r"\bmulti cap\b",
    "hdfc-elss-tax-saver": r"\b(elss|tax saver|tax saving|tax saving fund|80c fund)\b",
    "hdfc-gold-etf-fof": r"\bgold\b",
    "hdfc-silver-etf-fof": r"\bsilver\b",
}


@dataclass
class Resolution:
    kind: Kind
    scheme_ids: list[str] = field(default_factory=list)
    reason: str = ""  # not_covered: other_amc | other_hdfc_scheme | regular_plan | idcw | etf
    closest_scheme_id: str | None = None  # not_covered: the corpus scheme they probably meant
    fields: list[str] = field(default_factory=list)

    @property
    def scheme_id(self) -> str | None:
        return self.scheme_ids[0] if self.kind == "one" else None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"(small|mid|large|multi|flexi)\s*-?\s*cap", r"\1 cap", text)
    text = text.replace("taxsaver", "tax saver").replace("&", " & ")
    text = re.sub(r"[^\w&%/ ]+", " ", text)
    return " ".join(text.split())


@lru_cache(maxsize=1)
def _schemes() -> tuple[Scheme, ...]:
    return tuple(load_schemes().schemes)


def _aliases(scheme: Scheme) -> list[str]:
    names = [scheme.name.split(" – ")[0], *scheme.aliases]
    return sorted({normalize(a) for a in names}, key=len, reverse=True)


def scheme_names(scheme_id: str) -> list[str]:
    """Normalized names and aliases of a corpus scheme, longest first (used by retrieval)."""
    scheme = next((s for s in _schemes() if s.id == scheme_id), None)
    return _aliases(scheme) if scheme else []


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _fuzzy_matches(text: str) -> set[str]:
    """Schemes whose alias closely matches a word window of the question (typos)."""
    words = text.split()
    found = set()
    for scheme in _schemes():
        for alias in _aliases(scheme):
            n = len(alias.split())
            for i in range(len(words) - n + 1):
                window = " ".join(words[i : i + n])
                if window.startswith("hdfc") and fuzz.ratio(window, alias) >= FUZZY_THRESHOLD:
                    found.add(scheme.id)
    return found


def _category(text: str) -> set[str]:
    return {sid for sid, pattern in CATEGORY_WORDS.items() if re.search(pattern, text)}


def resolve(question: str) -> Resolution:
    text = normalize(question)
    fields = detect_fields(question)
    has_hdfc = _contains(text, "hdfc")

    def not_covered(reason: str, closest: str | None = None) -> Resolution:
        return Resolution("not_covered", reason=reason, closest_scheme_id=closest, fields=fields)

    # 1. Not covered (checked before any matching).
    if _OTHER_AMC.search(text):
        return not_covered("other_amc")
    named = {s.id for s in _schemes() if any(_contains(text, a) for a in _aliases(s))}
    if _OTHER_HDFC_ALWAYS.search(text) or (has_hdfc and not named and _OTHER_HDFC.search(text)):
        return not_covered("other_hdfc_scheme")
    categories = _category(text)
    closest = next(iter(categories)) if len(categories) == 1 else None
    if (etf := _ETF.search(text)) and not _FOF.search(text):
        return not_covered("etf", f"hdfc-{etf.group(1)}-etf-fof")
    if _REGULAR.search(text):
        return not_covered("regular_plan", closest)
    if _IDCW.search(text):
        return not_covered("idcw", closest)

    # 2–4. Find the schemes mentioned.
    matched = set(named)
    if not matched and has_hdfc:
        matched = _fuzzy_matches(text)
    if not matched:
        matched = categories
    elif re.search(r"\b(and|or)\b|,|&", text):
        matched |= categories  # "HDFC Mid Cap and Small Cap" names two schemes (EC-SCH-10)

    ordered = [s.id for s in _schemes() if s.id in matched]
    if len(ordered) == 1:
        return Resolution("one", ordered, fields=fields)
    if len(ordered) > 1:
        return Resolution("several", ordered, fields=fields)
    if is_general_topic(question) or not fields:
        return Resolution("general", fields=fields)
    return Resolution("needs_scheme", fields=fields)
