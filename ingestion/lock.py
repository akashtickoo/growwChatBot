"""A lock file so two ingestion runs can't overlap (EC-ING-08)."""

import os
from datetime import UTC, datetime
from pathlib import Path


class LockHeld(Exception):
    pass


class IngestLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> "IngestLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockHeld(
                f"Another ingestion run is in progress ({self.path} exists). "
                "If no run is active, delete that file and try again."
            ) from None
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n")
        return self

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)
