"""Fetch sources over HTTPS (architecture §5.1) and keep raw copies under data/raw/<doc_id>/."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ingestion.manifest import host_allowed

USER_AGENT = "faq-assistant-ingest/0.1"
TIMEOUT_S = 20.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 30.0
RAW_STAMP = "%Y%m%dT%H%M%SZ"


class FetchError(Exception):
    pass


@dataclass
class Fetched:
    url: str
    final_url: str
    content_type: str
    content: bytes
    fetched_at: datetime


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
        follow_redirects=True,
        transport=httpx.HTTPTransport(retries=2),  # connection-level retries
    )


def _check_url(url: str, allowed_domains: list[str]) -> None:
    if urlsplit(url).scheme != "https":
        raise FetchError(f"not HTTPS: {url}")
    if not host_allowed(url, allowed_domains):
        raise FetchError(f"host not on the allowlist: {urlsplit(url).hostname}")


def fetch(
    client: httpx.Client,
    url: str,
    allowed_domains: list[str],
    *,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Fetched:
    """GET one URL. Every redirect hop must stay on the allowlist. Retries 429 and 5xx."""
    _check_url(url, allowed_domains)
    last_error = "no attempt made"
    for attempt in range(retries + 1):
        wait = float(2**attempt)
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            last_error = f"request failed: {type(exc).__name__}"
        else:
            for hop in [*response.history, response]:
                try:
                    _check_url(str(hop.url), allowed_domains)
                except FetchError as exc:
                    raise FetchError(f"redirected off the allowlist ({exc})") from None
            if response.status_code == 200:
                return Fetched(
                    url=url,
                    final_url=str(response.url),
                    content_type=response.headers.get("content-type", ""),
                    content=response.content,
                    fetched_at=datetime.now(UTC),
                )
            if response.status_code not in RETRY_STATUSES:
                raise FetchError(f"HTTP {response.status_code}")
            last_error = f"HTTP {response.status_code}"
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                wait = min(float(retry_after), MAX_RETRY_AFTER_S)
        if attempt < retries:
            sleep(wait)
    raise FetchError(last_error)


def save_raw(raw_dir: Path, doc_id: str, fetched: Fetched, ext: str, keep: int = 3) -> Path:
    """Save the raw bytes and keep only the newest `keep` copies for this document."""
    folder = raw_dir / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{fetched.fetched_at.strftime(RAW_STAMP)}.{ext}"
    path.write_bytes(fetched.content)
    copies = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix == f".{ext}")
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    return path


def load_manual(raw_dir: Path, doc_id: str, url: str, ext: str) -> Fetched:
    """Read the newest hand-saved copy of a `fetch: manual` document."""
    folder = raw_dir / doc_id
    copies = sorted(folder.glob(f"*.{ext}")) if folder.exists() else []
    if not copies:
        raise FetchError(f"manual document: no saved .{ext} file in {folder}")
    newest = copies[-1]
    try:
        saved_at = datetime.strptime(newest.stem, RAW_STAMP).replace(tzinfo=UTC)
    except ValueError:
        saved_at = datetime.fromtimestamp(newest.stat().st_mtime, UTC)
    content_type = "application/pdf" if ext == "pdf" else "text/html"
    return Fetched(url, url, content_type, newest.read_bytes(), saved_at)
