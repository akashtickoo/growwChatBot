"""Fetching: allowlist, redirects, retries, raw copies (fake network)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ingestion.fetch import Fetched, FetchError, fetch, load_manual, save_raw

ALLOWED = ["www.hdfcfund.com", "files.hdfcfund.com"]
URL = "https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")


def test_fetch_returns_content():
    fetched = fetch(_client(_ok), URL, ALLOWED, sleep=lambda s: None)
    assert fetched.content == b"<html>ok</html>" and fetched.final_url == URL


def test_off_allowlist_url_is_never_requested():
    requested = []

    def handler(request):
        requested.append(request.url)
        return _ok(request)

    with pytest.raises(FetchError, match="allowlist"):
        fetch(_client(handler), "https://groww.in/mutual-funds/x", ALLOWED)
    assert requested == []


def test_plain_http_is_rejected():
    with pytest.raises(FetchError, match="HTTPS"):
        fetch(_client(_ok), "http://www.hdfcfund.com/x", ALLOWED)


def test_redirect_off_the_allowlist_is_rejected():
    def handler(request):
        if request.url.host == "www.hdfcfund.com":
            return httpx.Response(302, headers={"location": "https://evil.example.com/page"})
        return _ok(request)

    with pytest.raises(FetchError, match="redirected off the allowlist"):
        fetch(_client(handler), URL, ALLOWED, sleep=lambda s: None)


def test_server_errors_are_retried_with_backoff():
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else _ok(request)

    fetched = fetch(_client(handler), URL, ALLOWED, sleep=waits.append)
    assert fetched.content == b"<html>ok</html>"
    assert len(calls) == 3 and waits == [1.0, 2.0]


def test_retry_after_is_respected_and_capped():
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "500"})
        return _ok(request)

    fetch(_client(handler), URL, ALLOWED, sleep=waits.append)
    assert waits == [30.0]


def test_client_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    with pytest.raises(FetchError, match="HTTP 404"):
        fetch(_client(handler), URL, ALLOWED, sleep=lambda s: None)
    assert len(calls) == 1


def test_gives_up_after_the_last_retry():
    with pytest.raises(FetchError, match="HTTP 502"):
        fetch(_client(lambda r: httpx.Response(502)), URL, ALLOWED, retries=2, sleep=lambda s: None)


def _fetched(minutes: int) -> Fetched:
    when = datetime(2026, 11, 11, 9, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return Fetched(URL, URL, "text/html", f"copy {minutes}".encode(), when)


def test_save_raw_keeps_only_the_newest_copies(tmp_path):
    for minutes in range(5):
        save_raw(tmp_path, "doc", _fetched(minutes), "html", keep=3)
    names = sorted(p.name for p in (tmp_path / "doc").iterdir())
    assert names == ["20261111T090200Z.html", "20261111T090300Z.html", "20261111T090400Z.html"]


def test_load_manual_reads_the_newest_copy(tmp_path):
    save_raw(tmp_path, "doc", _fetched(0), "html")
    save_raw(tmp_path, "doc", _fetched(7), "html")
    loaded = load_manual(tmp_path, "doc", URL, "html")
    assert loaded.content == b"copy 7"
    assert loaded.fetched_at == datetime(2026, 11, 11, 9, 7, tzinfo=UTC)


def test_load_manual_without_a_file_fails(tmp_path):
    with pytest.raises(FetchError, match="manual document"):
        load_manual(tmp_path, "doc", URL, "pdf")
