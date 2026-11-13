"""Case schema for the golden and held-out sets (eval.md §11.1)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_set.yaml"
HOLDOUT = HERE / "holdout_set.yaml"

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
Group = Literal[
    "fact",
    "paraphrase",
    "process",
    "advisory",
    "looks_advisory",
    "performance",
    "pii",
    "not_covered",
    "clarify",
    "edge",
]


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    group: Group
    question: str
    expected_type: list[ResponseType]
    expected_scheme: str | None = None
    expected_field: str | None = None
    accept: list[str] = []  # any one, after normalization, counts as correct
    accept_all: list[str] = []  # every one must appear (several facts in one answer)
    must_not_contain: list[str] = []
    expected_citation: list[str] | None = None  # acceptable registry doc ids
    expected_citation_url: str | None = None  # fixed education link
    pii_value: str | None = None  # the made-up personal value inside a PII case
    edge_case_ref: str | None = None
    label_source: str
    verified_on: str
    volatile: bool = False

    @field_validator("expected_type", mode="before")
    @classmethod
    def _as_list(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value

    @property
    def answerable(self) -> bool:
        """Cases where only an answer is right (value accuracy and false refusals count)."""
        return self.expected_type == ["answer"]

    @property
    def has_value(self) -> bool:
        return bool(self.accept or self.accept_all)

    def redacted_question(self) -> str:
        """Reports never contain the made-up PII value either (eval.md §11.2)."""
        if self.pii_value:
            return self.question.replace(self.pii_value, "[PII]")
        return self.question


def load(path: Path) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Case.model_validate(c) for c in data["cases"]]
