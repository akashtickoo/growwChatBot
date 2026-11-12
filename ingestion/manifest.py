"""Load and validate the scheme registry, the source manifest and the education links in config/."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SCHEMES_PATH = CONFIG_DIR / "schemes.yaml"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"
EDUCATION_LINKS_PATH = CONFIG_DIR / "education_links.yaml"

DocType = Literal["SCHEME_PAGE", "SERVICE_PAGE", "FACTSHEET_INDEX"]
LinkedDocType = Literal["SID", "KIM", "FUND_FACTS"]


def host_allowed(url: str, allowed_domains: list[str]) -> bool:
    """Exact host match only. Lookalike hosts and unlisted subdomains are rejected."""
    host = (urlsplit(url).hostname or "").lower()
    return host in {d.lower() for d in allowed_domains}


def _check_https_and_host(item_id: str, url: str, allowed_domains: list[str]) -> None:
    if urlsplit(url).scheme != "https":
        raise ValueError(f"{item_id}: only https URLs are allowed")
    if not host_allowed(url, allowed_domains):
        raise ValueError(f"{item_id}: host is not in allowed_domains")


# --- Scheme registry ---------------------------------------------------------------------


class Scheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    category: str
    groww_ref: HttpUrl  # identifier only; never fetched, indexed or cited (D1)
    source_doc_id: str
    aliases: list[str] = Field(min_length=1)


class SchemeRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amc: str
    plan: str
    schemes: list[Scheme]

    @model_validator(mode="after")
    def _unique_ids(self) -> "SchemeRegistry":
        ids = [s.id for s in self.schemes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scheme id in schemes.yaml")
        return self


# --- Source manifest ---------------------------------------------------------------------


class LinkedDocumentSpec(BaseModel):
    """A PDF linked from a scheme page, found by the start of its link text."""

    model_config = ConfigDict(extra="forbid")

    doc_type: LinkedDocType
    link_text_prefix: str = Field(min_length=1)
    index: bool = True  # False: fetched and registered (for links and checks) but not searched


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scheme_id: str | None
    doc_type: DocType
    url: HttpUrl
    format: Literal["html"]
    refresh: Literal["weekly", "monthly"]
    fetch: Literal["auto", "manual"] = "auto"
    index: bool = True  # False: fetched and registered (for links and checks) but not searched
    linked_documents: list[LinkedDocumentSpec] = []

    @model_validator(mode="after")
    def _scheme_rules(self) -> "ManifestEntry":
        if self.doc_type == "SCHEME_PAGE" and not self.scheme_id:
            raise ValueError(f"{self.id}: a scheme page needs a scheme_id")
        if self.doc_type != "SCHEME_PAGE" and self.scheme_id is not None:
            raise ValueError(f"{self.id}: only scheme pages belong to a scheme")
        if self.linked_documents and self.doc_type != "SCHEME_PAGE":
            raise ValueError(f"{self.id}: only scheme pages can list linked documents")
        return self


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_domains: list[str]
    documents: list[ManifestEntry]

    @model_validator(mode="after")
    def _check_entries(self) -> "SourceManifest":
        ids = [d.id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate document id in sources.yaml")
        for doc in self.documents:
            _check_https_and_host(doc.id, str(doc.url), self.allowed_domains)
        return self


def linked_doc_id(scheme_id: str, doc_type: str) -> str:
    """ID for a scheme's linked PDF: ('hdfc-mid-cap', 'FUND_FACTS') -> 'hdfc-mid-cap-fund-facts'."""
    return f"{scheme_id}-{doc_type.lower().replace('_', '-')}"


# --- Education links ---------------------------------------------------------------------


class EducationLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    url: HttpUrl
    used_in: list[str] = Field(min_length=1)


class EducationLinks(BaseModel):
    """Fixed links used in reply templates. Linked only, never fetched into the index."""

    model_config = ConfigDict(extra="forbid")

    allowed_domains: list[str]
    links: dict[str, EducationLink]

    @model_validator(mode="after")
    def _check_links(self) -> "EducationLinks":
        for name, link in self.links.items():
            _check_https_and_host(name, str(link.url), self.allowed_domains)
        return self


# --- Loaders -----------------------------------------------------------------------------


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_schemes(path: Path = SCHEMES_PATH) -> SchemeRegistry:
    return SchemeRegistry.model_validate(_load_yaml(path))


def load_sources(path: Path = SOURCES_PATH) -> SourceManifest:
    return SourceManifest.model_validate(_load_yaml(path))


def load_education_links(path: Path = EDUCATION_LINKS_PATH) -> EducationLinks:
    return EducationLinks.model_validate(_load_yaml(path))
