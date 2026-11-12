"""Data passed between the ingestion steps: parsed documents, sections and chunks."""

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Section:
    heading: str
    text: str
    is_table: bool = False
    page: int | None = None


@dataclass
class ParsedDocument:
    doc_id: str
    scheme_id: str | None
    doc_type: str
    title: str
    url: str
    sections: list[Section]
    doc_as_of: date | None = None
    facts: dict[str, str] = field(default_factory=dict)  # labeled values found on scheme pages

    def full_text(self) -> str:
        return "\n\n".join(f"{s.heading}\n{s.text}" for s in self.sections)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    scheme_id: str | None
    doc_type: str
    section: str
    text: str  # includes the context header line
    source_url: str
    doc_as_of: str | None = None  # ISO date
    page: int | None = None
    is_table: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Chunk":
        return cls(**data)
