"""Page content (title, disclaimer, welcome, example questions) from config/ui.yaml.

Shared by the Streamlit UI and GET /api/meta, so neither hard-codes user-facing text.
"""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from app.responses.composer import short_name
from ingestion.manifest import load_schemes

UI_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ui.yaml"


class SchemeInfo(BaseModel):
    id: str
    name: str
    short_name: str


class Meta(BaseModel):
    title: str
    disclaimer: str
    welcome: str
    examples: list[str]
    pii_hint: str
    max_message_chars: int
    schemes: list[SchemeInfo]


@lru_cache(maxsize=1)
def load_meta(path: Path = UI_CONFIG_PATH) -> Meta:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["welcome"] = " ".join(data["welcome"].split())
    schemes = [
        SchemeInfo(id=s.id, name=s.name, short_name=short_name(s)) for s in load_schemes().schemes
    ]
    return Meta(**data, schemes=schemes)
