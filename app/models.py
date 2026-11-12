"""Response models shared by the pipeline, the API and the CLI (architecture §7, §9)."""

from datetime import date
from typing import Literal

from pydantic import BaseModel

ResponseType = Literal[
    "answer",
    "refusal",
    "performance_redirect",
    "pii_blocked",
    "out_of_scope",
    "not_found",
    "clarify",
    "greeting",
]

# Types that carry no facts, so no link and no footer (EC-CIT-02).
NO_CITATION_TYPES: frozenset[str] = frozenset({"clarify", "greeting"})


class Citation(BaseModel):
    url: str
    title: str


class ChatResponse(BaseModel):
    type: ResponseType
    text: str  # the answer text only; the UI adds the Source line and the footer
    citation: Citation | None
    last_updated: date | None  # Asia/Kolkata date of the cited source's last fetch
    request_id: str


def footer(last_updated: date) -> str:
    """The footer line (EC-CIT-09): "Last updated from sources: 10 Nov 2026"."""
    return f"Last updated from sources: {last_updated.day} {last_updated:%b %Y}"
