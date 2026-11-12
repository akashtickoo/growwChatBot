"""End-to-end ingestion for one scheme against a fake HDFC MF site (no network, fake embedder)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fixture_builders import fund_facts_pdf, kim_pdf, scheme_page_html, sid_pdf
from test_build_index import fake_embedder

from ingestion.build_index import read_build_info, read_chunks, read_current
from ingestion.paths import Paths
from ingestion.registry import Registry
from ingestion.run import Ingestor

PAGE_ID = "hdfc-mid-cap-page"
PAGE_URL = "https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct"
BLOCK_PAGE = "<html><body><h1>Access Denied</h1><p>Complete the captcha.</p></body></html>"


class FakeSite:
    """Serves the fixture page and PDFs. Tests change `page` to simulate site changes."""

    def __init__(self):
        self.page = scheme_page_html()
        self.pdfs = {"/SID/": sid_pdf(), "/KIM/": kim_pdf(), "Fund%20Facts": fund_facts_pdf()}
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == PAGE_URL:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=self.page)
        for key, pdf in self.pdfs.items():
            if key in url:
                return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)
        return httpx.Response(404)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(minutes=1)
        return self.t


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def run(tmp_path, site):
    paths = Paths.under(tmp_path / "data")
    clock = Clock()

    def _run(**kwargs):
        client = httpx.Client(transport=httpx.MockTransport(site), follow_redirects=True)
        ingestor = Ingestor(
            paths, client=client, embedder=fake_embedder, sleep=lambda s: None, now=clock, **kwargs
        )
        report = ingestor.run(only=PAGE_ID)
        ingestor.registry.close()
        return report

    _run.paths = paths
    return _run


def _statuses(report) -> dict[str, str]:
    return {r.doc_id: r.status for r in report.results}


def test_first_run_fetches_everything_and_builds_the_index(run):
    report = run()

    assert _statuses(report) == {
        "hdfc-mid-cap-page": "fetched",
        "hdfc-mid-cap-sid": "fetched",
        "hdfc-mid-cap-kim": "fetched",
        "hdfc-mid-cap-fund-facts": "fetched",
    }
    assert report.index.startswith("built ")

    current = read_current(run.paths.index)
    doc_ids = {c.doc_id for c in read_chunks(current)}
    assert doc_ids == {"hdfc-mid-cap-page", "hdfc-mid-cap-kim"}  # SID and Fund Facts not indexed
    assert set(read_build_info(current)["documents"]) == doc_ids


def test_registry_records_sources_for_citations(run):
    run()
    with Registry(run.paths.registry) as registry:
        page = registry.get_document("hdfc-mid-cap-page")
        kim = registry.get_document("hdfc-mid-cap-kim")
        facts = registry.get_document("hdfc-mid-cap-fund-facts")

    assert page["url"] == PAGE_URL and page["fetched_at"] and page["indexed"] == 1
    assert kim["title"] == "KIM - HDFC Mid Cap Fund" and kim["doc_as_of"] == "2025-11-21"
    assert kim["url"].startswith("https://files.hdfcfund.com/") and kim["content_hash"]
    assert facts["indexed"] == 0 and facts["last_status"] == "fetched"


def test_second_run_skips_unchanged_documents(run):
    first = run()
    with Registry(run.paths.registry) as registry:
        first_fetch = registry.get_document("hdfc-mid-cap-page")["fetched_at"]

    second = run()

    assert set(_statuses(second).values()) == {"unchanged"}
    assert second.index == f"unchanged ({read_current(run.paths.index).name})"
    assert first.index.split(":")[0] == f"built {read_current(run.paths.index).name}"
    with Registry(run.paths.registry) as registry:
        assert registry.get_document("hdfc-mid-cap-page")["fetched_at"] > first_fetch


def test_changed_fact_reprocesses_and_rebuilds(run, site):
    run()
    site.page = site.page.replace("<p>0.74</p>", "<p>0.81</p>")

    report = run()

    assert _statuses(report)["hdfc-mid-cap-page"] == "fetched"
    assert _statuses(report)["hdfc-mid-cap-kim"] == "unchanged"
    assert report.index.startswith("built ")
    texts = [c.text for c in read_chunks(read_current(run.paths.index))]
    assert any("TER): 0.81%" in t for t in texts)


def test_block_page_fails_and_keeps_the_last_good_version(run, site):
    run()
    version = read_current(run.paths.index).name
    site.page = BLOCK_PAGE

    report = run()

    statuses = _statuses(report)
    assert statuses["hdfc-mid-cap-page"] == "failed"
    assert {statuses[d] for d in statuses if d != "hdfc-mid-cap-page"} == {"skipped"}
    assert "block page" in report.failed[0].detail
    assert read_current(run.paths.index).name == version
    with Registry(run.paths.registry) as registry:
        page = registry.get_document("hdfc-mid-cap-page")
    assert page["last_status"] == "failed" and page["content_hash"]  # last good kept


def test_missing_pdf_link_fails_only_that_document(run, site):
    site.page = site.page.replace(">KIM - HDFC Mid Cap Fund<", ">Brochure<")

    report = run()

    statuses = _statuses(report)
    assert statuses["hdfc-mid-cap-kim"] == "failed"
    assert statuses["hdfc-mid-cap-page"] == "fetched" and statuses["hdfc-mid-cap-sid"] == "fetched"


def test_run_report_lists_every_document(run):
    report = run()
    text = report.report_path.read_text(encoding="utf-8")
    for result in report.results:
        assert f"| {result.doc_id} |" in text
    assert "Index: built" in text


def test_processed_chunks_hold_no_banned_text(run):
    """EV-2-07 on the full pipeline output."""
    run()
    banned = ["since inception", "value of investment", "ideal for", "suitable for", "18.50%"]
    for chunk in read_chunks(read_current(run.paths.index)):
        low = chunk.text.lower()
        assert not any(b in low for b in banned), chunk.chunk_id


def test_processed_store_is_json(run):
    run()
    data = json.loads((run.paths.processed / "hdfc-mid-cap-page.json").read_text("utf-8"))
    assert data["doc_id"] == "hdfc-mid-cap-page" and data["chunks"]
