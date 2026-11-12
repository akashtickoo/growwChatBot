"""Where ingestion reads and writes files. Tests pass their own Paths pointing at a temp folder."""

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    data: Path  # data/ (gitignored apart from .gitkeep)
    index: Path  # data/index/, one folder per index version plus the `current` pointer

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def processed(self) -> Path:
        return self.data / "processed"

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    @property
    def registry(self) -> Path:
        return self.data / "registry.sqlite"

    @property
    def lock(self) -> Path:
        return self.data / "ingest.lock"

    @classmethod
    def under(cls, data: Path) -> "Paths":
        return cls(data=data, index=data / "index")

    @classmethod
    def default(cls) -> "Paths":
        from app.settings import get_settings

        index_dir = get_settings().index_dir
        if not index_dir.is_absolute():
            index_dir = REPO_ROOT / index_dir
        return cls(data=REPO_ROOT / "data", index=index_dir)
