"""Intent rules (architecture §6.3): decide the obvious cases without the LLM.

Priority when several match: advisory > performance > out_of_scope > greeting > factual.
Rules lean toward caution: a false refusal costs little, answering an advisory question is a
compliance failure. The LLM's `advisory` status is the second line of defense.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.retrieval.scheme_resolver import _OTHER_AMC

Intent = Literal["advisory", "performance", "out_of_scope", "greeting", "factual"]

# Factual phrasings that contain advisory or performance words (EC-INT-02/03, EC-PERF-02/09).
# They are removed before the advisory/performance rules run.
EXCEPTIONS = re.compile(
    r"\b(should i pay|do i (?:have|need) to pay|will i (?:have to )?pay|is there an?"
    r"|best way to|(?:return|redeem|redemption of) (?:my )?units|portfolio of"
    r"|should i (?:submit|use|fill|download))\b"
)

ADVISORY = {
    "should_i": r"\b(should|shall|must) (?:i|we)\b",
    "is_it_good": r"\bis (?:it|this|that|the (?:fund|scheme)|hdfc [\w ]{0,30}?) "
    r"(?:a )?(?:good|bad|wise|right|worth|safe|suitable|ideal)\b",
    "better_best": r"\b(better|best|worst|top performing|top fund|top rated|outperform\w*)\b",
    "recommend": r"\b(recommend\w*|suggest\w*|advi[cs]e\w*|advisor|adviser|tips?|opinion)\b",
    "worth": r"\b(worth (?:it|investing|buying)|good (?:time|choice|option|investment|buy))\b",
    "safe": r"\b(safe|safer|safest|guaranteed?)\b",
    "which_fund": r"\bwhich (?:fund|scheme|one|plan|option|of these)\b",
    "portfolio": r"\b(my portfolio|portfolio allocation|asset allocation for me|diversify)\b",
    "suitability": r"\b(suitab\w*|suit (?:me|my)|right for me|good for me|ideal for)\b",
    "ratings": r"\b(rating|ratings|rated|star|stars|crisil rank|morningstar|value research)\b",
    "prediction": r"\b(will (?:it|the nav|nav|this|the fund|the scheme|hdfc [\w ]{0,30}?) "
    r"(?:go|rise|fall|grow|increase|decrease|double|crash|give|beat)|predict\w*|forecast\w*"
    r"|outlook|future (?:returns?|performance|growth))\b",
    "choose": r"\b(sip or lump ?sum|lump ?sum or sip|direct or regular|regular or direct"
    r"|growth or idcw|idcw or growth|invest or not|buy or sell|hold or sell|sell or hold)\b",
    "where_invest": r"\b(where (?:should|can|do) i (?:invest|put|park)|how much should i invest"
    r"|what should i (?:buy|invest)|invest my (?:money|savings|bonus|salary))\b",
    "compare": r"\b(compare\w*|comparison|vs|versus|instead of|or hdfc)\b",
    "roleplay": r"\b(pretend|act as|you are my|be my) .*(advis\w+|planner|expert)\b",
    "jailbreak": r"\b(ignore (?:your|all|the|previous)|disregard (?:your|the|previous))\b",
    "why_moved": r"\bwhy (?:did|has|is) .*\b(fall|fell|drop|dropped|rise|rose|down|up|crash)\b",
    "tax_calc": r"\bhow much tax (?:will|can|would) i save\b",
}

PERFORMANCE = {
    "returns": r"\breturns?\b.{0,40}\b(\d|%|year|yr|month|annual\w*|cagr|since|past|last|so far"
    r"|ytd|historical|average|expected)\b"
    r"|\b(\d|year|yr|month|annual\w*|past|last|historical|expected|average)\b.{0,25}\breturns?\b"
    r"|\b(gave|give|giving|generated|delivered|earned) (?:a |any )?returns?\b",
    "metrics": r"\b(cagr|xirr|cumulative return|absolute return|alpha|sharpe|performance"
    r"|performed|perform)\b",
    "earnings": r"\bhow much (?:will|would|can|could) (?:i|my money|it) "
    r"(?:earn|get|make|grow|become)"
    r"|\bhow did (?:the|this|hdfc) [\w ]{0,40}?(?:fund|scheme)? ?do\b|\bgrowth of\b",
    "nav": r"\b(nav|net asset value|unit price|price today|current price)\b",
}

OUT_OF_SCOPE = {
    "url": r"(https?://|www\.)",
    "other_products": r"\b(stocks?|shares?|equity shares|ipo|crypto\w*|bitcoin|fixed deposit|fd"
    r"|insurance|ulip|loan|credit card|gold coins?|real estate|bonds? of|nps|ppf|epf)\b",
    "app_account": r"\b(groww (?:app|account|login|order|wallet|support)|log ?in|login|password"
    r"|my account|my order|my sip (?:failed|status)|kyc status|app (?:not working|crash\w*))\b",
    "tax_filing": r"\b(file (?:my )?(?:itr|taxes|tax return)|itr|income tax return)\b",
    "feedback": r"\b(your answer is wrong|you are wrong|that is wrong|useless|bad bot"
    r"|not helpful)\b",
    "abuse": r"\b(stupid|idiot|dumb|shut up|damn|hell)\b",
    "chit_chat": r"\b(weather|joke|who are you|your name|how are you|cricket|movie)\b",
}

GREETING_WORDS = {
    "hi",
    "hii",
    "hiii",
    "hello",
    "hey",
    "heya",
    "namaste",
    "hola",
    "good",
    "morning",
    "afternoon",
    "evening",
    "thanks",
    "thank",
    "you",
    "thx",
    "ty",
    "ok",
    "okay",
    "bye",
    "goodbye",
    "see",
    "later",
    "great",
    "cool",
    "there",
    "team",
    "bot",
    "a",
    "lot",
    "so",
    "much",
}


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    rule: str  # name of the rule that fired (never the matched text)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(text.split())


def _first(rules: dict[str, str], text: str) -> str | None:
    return next((name for name, pattern in rules.items() if re.search(pattern, text)), None)


def classify_intent(message: str) -> IntentResult:
    text = _norm(message)
    if not re.search(r"[a-z0-9]", text):
        return IntentResult("out_of_scope", "no_words")  # emoji or symbols only (EC-IN-05)

    stripped = EXCEPTIONS.sub(" ", text)
    if rule := _first(ADVISORY, stripped):
        return IntentResult("advisory", rule)
    if rule := _first(PERFORMANCE, stripped):
        return IntentResult("performance", rule)
    if _OTHER_AMC.search(text):
        return IntentResult("out_of_scope", "other_amc")
    if rule := _first(OUT_OF_SCOPE, text):
        return IntentResult("out_of_scope", rule)

    words = re.findall(r"[a-z]+", text)
    if words and len(words) <= 6 and all(w in GREETING_WORDS for w in words):
        return IntentResult("greeting", "greeting")  # the WHOLE message (EC-INT-16)
    return IntentResult("factual", "default")
