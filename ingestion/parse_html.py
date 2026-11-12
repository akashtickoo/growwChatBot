"""Parse HDFC MF HTML pages into sections (architecture §5.2).

Scheme pages are parsed allowlist-first: only the labeled fact boxes and a few factual sections
are kept. Everything else is dropped, including returns, NAV and benchmark performance,
"Ideal for", "Why invest", "Who can consider", holdings and "Product Suitability"
(EC-PERF-07, EC-INT-06, EC-RET-06). Phase 1 found that trafilatura picks the wrong part of
some scheme pages, so these pages are read with BeautifulSoup.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ingestion.normalize import normalize_text

HEADING_TAGS = ("h1", "h2", "h3", "h4")
FOOTER_MARKERS = ("OUR VISION", "Did you find this interesting?")
JUNK_LINES = {"Read More", "INVEST NOW", "Yes", "No", "OR", "Graph", "Table", "Click here"}

# Lines never kept anywhere, as a second line of defense after section filtering.
BANNED_LINE = re.compile(
    r"since inception|value of investment|ideal for|suitable for|consult their financial advis"
    r"|benchmark returns|scheme returns|past performance",
    re.IGNORECASE,
)

# Scheme-page sections kept (matched against the heading).
ALLOWED_SCHEME_SECTIONS = (
    re.compile(r"^What is HDFC\b", re.IGNORECASE),
    re.compile(r"^Investment (Strategy|Objective)\b", re.IGNORECASE),
)
FUND_MANAGERS = re.compile(r"^Fund Managers?$", re.IGNORECASE)
# Headings that end the Fund Managers list or the Exit Load section. Any other heading under
# Fund Managers is a manager's name (some pages omit "Mr."); any other h3 under Exit Load is
# exit-load text (some pages put the bullet points in h3 tags).
SECTION_AFTER_MANAGERS = re.compile(
    r"Portfolio|Holdings|Downloads|Exit Load|Product|FAQs|Benchmark|NAV|Performance",
    re.IGNORECASE,
)
SECTION_AFTER_EXIT_LOAD = re.compile(r"^(Product|Benchmark|FAQs)", re.IGNORECASE)
MENTIONS_RETURNS = re.compile(r"\breturns?\b|\bperform|\bCAGR\b", re.IGNORECASE)

# FAQ questions that ask for opinions, suitability or performance are dropped.
DENIED_FAQ = re.compile(
    r"\b(risky|risk|ideal|better|best|suitable|suit|beginners?|role|why|should|consider"
    r"|horizon|returns?|perform\w*|differ\w*|compare\w*|vs)\b",
    re.IGNORECASE,
)

BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "attention required",
    "verify you are human",
    "request blocked",
    "enable javascript and cookies",
)


@dataclass
class Block:
    kind: str  # "H" heading or "T" text
    tag: str | None
    text: str


def page_blocks(html: str) -> list[Block]:
    """The page as an ordered list of headings and text strings."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    blocks: list[Block] = []
    for node in soup.descendants:
        if isinstance(node, Tag) and node.name in HEADING_TAGS:
            text = node.get_text(" ", strip=True)
            if text:
                blocks.append(Block("H", node.name, " ".join(text.split())))
        elif isinstance(node, NavigableString) and not isinstance(node, Comment):
            if node.find_parent(HEADING_TAGS) is not None:
                continue
            text = " ".join(node.split())
            if text:
                blocks.append(Block("T", None, text))
    return blocks


def visible_text(html: str) -> str:
    return "\n".join(b.text for b in page_blocks(html))


def blocked_markers(text: str) -> list[str]:
    low = text.lower()
    return [m for m in BLOCK_MARKERS if m in low]


def _body(blocks: list[Block]) -> list[Block]:
    """From the first h1 up to the site footer."""
    start = next((i for i, b in enumerate(blocks) if b.kind == "H" and b.tag == "h1"), 0)
    body = blocks[start:]
    for i, b in enumerate(body):
        if b.text in FOOTER_MARKERS:
            return body[:i]
    return body


def _clean_lines(lines: list[str]) -> list[str]:
    kept = []
    for line in lines:
        line = line.lstrip("●•▪-– ").strip()
        if not line or line in JUNK_LINES or BANNED_LINE.search(line):
            continue
        kept.append(line)
    return kept


def _split_sections(body: list[Block]) -> list[tuple[str, str | None, list[str]]]:
    """(heading, heading tag, text lines) for each heading in the body."""
    sections: list[tuple[str, str | None, list[str]]] = []
    current: tuple[str, str | None, list[str]] | None = None
    for block in body:
        if block.kind == "H":
            current = (block.text, block.tag, [])
            sections.append(current)
        elif current is not None:
            current[2].append(block.text)
    return sections


# --- Scheme pages ------------------------------------------------------------------------


def _region(lines: list[str], start_label: str, end_label: str) -> list[str]:
    try:
        start = lines.index(start_label)
    except ValueError:
        return lines
    end = next((i for i in range(start, len(lines)) if lines[i] == end_label), len(lines))
    return lines[start:end]


def _value_after(lines: list[str], label: str, skip_long: bool = False) -> str | None:
    """The value line after a box label, skipping the box's explanation text if asked."""
    for i, line in enumerate(lines):
        if line != label:
            continue
        j = i + 1
        while skip_long and j < len(lines) and (len(lines[j]) > 60 or lines[j] == label):
            j += 1
        return lines[j] if j < len(lines) else None
    return None


def extract_scheme_facts(body_lines: list[str]) -> dict[str, str]:
    """Read the labeled fact boxes (between "Returns" and "INVEST NOW") on a scheme page."""
    box = _region(body_lines, "Returns", "INVEST NOW")
    facts: dict[str, str] = {}

    scheme_type = next(
        (line for line in body_lines if re.match(r"^An open[ -]ended", line, re.IGNORECASE)),
        None,
    )
    if scheme_type:
        facts["scheme_type"] = scheme_type.rstrip(".")

    for key, label, skip in [
        ("riskometer", "Riskometer", False),
        ("min_sip", "Min SIP", False),
        ("entry_load", "Entry Load", True),
        ("lock_in", "Lock in", True),
        ("inception_date", "Inception Date", False),
    ]:
        value = _value_after(box, label, skip_long=skip)
        if value:
            facts[key] = value

    if "TER" in box:
        start = box.index("TER")
        ter = next((x for x in box[start : start + 10] if re.fullmatch(r"\d+(\.\d+)?", x)), None)
        if ter:
            facts["expense_ratio"] = ter

    for i, line in enumerate(box):
        if line.startswith("AUM"):
            window = box[i : i + 6]
            as_on = next((m for x in window if (m := re.search(r"\d{2}/\d{2}/\d{4}", x))), None)
            value = next((x for x in window if x.startswith("₹")), None)
            if value:
                facts["aum"] = value
                if as_on:
                    facts["aum_as_on"] = as_on.group(0)
            break

    benchmark = _value_after(box, "Benchmark", skip_long=True)
    if benchmark and benchmark.endswith("..."):
        i = box.index(benchmark)
        benchmark = box[i + 1] if i + 1 < len(box) else benchmark
    if benchmark:
        facts["benchmark"] = benchmark
    return facts


def _key_facts_text(facts: dict[str, str]) -> str:
    lines = []
    if "scheme_type" in facts:
        lines.append(f"Scheme type: {facts['scheme_type']}.")
    if "riskometer" in facts:
        lines.append(f"Riskometer: {facts['riskometer']}.")
    if "min_sip" in facts:
        lines.append(f"Minimum SIP amount: {facts['min_sip']}.")
    if "expense_ratio" in facts:
        lines.append(
            f"Total expense ratio (TER): {facts['expense_ratio']}%, including additional expenses"
            " and goods and service tax on management fees, if any."
        )
    if "lock_in" in facts:
        lines.append(f"Lock-in period: {facts['lock_in']}.")
    if "entry_load" in facts:
        lines.append(f"Entry load: {facts['entry_load']}.")
    if "aum" in facts:
        as_on = f" as on {facts['aum_as_on']}" if "aum_as_on" in facts else ""
        lines.append(f"Assets under management (AUM){as_on}: {facts['aum']}")
    if "benchmark" in facts:
        lines.append(f"Benchmark: {facts['benchmark']}.")
    if "inception_date" in facts:
        lines.append(f"Inception date: {facts['inception_date']}.")
    return "\n".join(lines)


@dataclass
class SchemePage:
    title: str
    facts: dict[str, str]
    sections: list[tuple[str, str]]  # (heading, text)
    has_exit_load: bool


def parse_scheme_page(html: str) -> SchemePage:
    body = _body(page_blocks(html))
    title = next((b.text for b in body if b.kind == "H" and b.tag == "h1"), "")
    body_lines = [b.text for b in body if b.kind == "T"]
    facts = extract_scheme_facts(body_lines)

    sections: list[tuple[str, str]] = []
    key_facts = _key_facts_text(facts)
    if key_facts:
        sections.append(("Key facts", key_facts))

    in_faq = False
    managers: list[str] | None = None  # collecting "Mr. X – title" lines under Fund Managers
    exit_load: list[str] | None = None  # collecting Exit Load text, including h3 bullet points
    has_exit_load = False

    def flush_managers() -> None:
        if managers:
            sections.append(("Fund Managers", "\n".join(managers)))

    def flush_exit_load() -> None:
        nonlocal has_exit_load
        text = _clean_lines(exit_load or [])
        if text:
            has_exit_load = True
            sections.append(("Exit Load", "\n".join(text)))

    for heading, tag, lines in _split_sections(body):
        if managers is not None:
            if not SECTION_AFTER_MANAGERS.search(heading):
                managers.append(" – ".join([heading, *_clean_lines(lines)]))
                continue
            flush_managers()
            managers = None

        if exit_load is not None:
            if tag in ("h3", "h4") and not SECTION_AFTER_EXIT_LOAD.match(heading):
                exit_load.extend([heading, *lines])
                continue
            flush_exit_load()
            exit_load = None

        if heading.lower() == "exit load":
            exit_load = list(lines)
            continue

        if heading == "FAQs":
            in_faq = True
            continue
        if in_faq:
            answer = _clean_lines(lines)
            keep = tag in ("h3", "h4") and not DENIED_FAQ.search(heading)
            if keep and answer and not any(MENTIONS_RETURNS.search(a) for a in answer):
                sections.append((f"FAQ: {heading}", "\n".join(answer)))
            continue

        if FUND_MANAGERS.match(heading):
            managers = []
            continue
        if not any(p.match(heading) for p in ALLOWED_SCHEME_SECTIONS):
            continue
        text = _clean_lines(lines)
        if text:
            sections.append((heading, "\n".join(text)))
    flush_managers()
    if exit_load is not None:
        flush_exit_load()

    return SchemePage(title=title, facts=facts, sections=sections, has_exit_load=has_exit_load)


# --- Other pages (statement pages, factsheet index) --------------------------------------

_LAST_UPDATED = re.compile(r"Last Updated On:\s*(\d{1,2} \w{3} \d{4})", re.IGNORECASE)


@dataclass
class GenericPage:
    title: str
    sections: list[tuple[str, str]]
    last_updated: date | None


def parse_generic_page(html: str) -> GenericPage:
    body = _body(page_blocks(html))
    title = next((b.text for b in body if b.kind == "H" and b.tag == "h1"), "")
    last_updated = None
    for b in body:
        if b.kind == "T" and (m := _LAST_UPDATED.search(b.text)):
            try:
                last_updated = datetime.strptime(m.group(1), "%d %b %Y").date()
            except ValueError:
                pass
            break

    sections: list[tuple[str, str]] = []
    intro: list[str] = []
    for block in body:
        if block.kind == "H":
            break
        intro.append(block.text)
    for heading, _tag, lines in _split_sections(body):
        text = _clean_lines([x for x in lines if not _LAST_UPDATED.search(x)])
        if text:
            sections.append((heading, "\n".join(text)))
    if intro and _clean_lines(intro):
        sections.insert(0, (title or "Introduction", "\n".join(_clean_lines(intro))))
    return GenericPage(title=title, sections=sections, last_updated=last_updated)


def normalize_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(normalize_text(h), normalize_text(t)) for h, t in sections if normalize_text(t)]
