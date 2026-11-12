"""Get a servable pipeline, building the index first when the environment has none.

A fresh deployment (Streamlit Community Cloud, a new clone) has no data/: page content and the
index are never committed. On first start this:
1. imports the reviewed fact cards from config/reviewed_fact_cards.yaml into an empty registry,
2. fetches the official HDFC MF sources and builds the index (python -m ingestion.run),
3. loads the index and wires the pipeline.
Later starts in the same environment reuse the index. A failed build leaves the app running with
template replies only, and factual questions get "unavailable" (EC-RET-07).
"""

import logging
from dataclasses import dataclass

from app.pipeline import Pipeline, build_pipeline
from app.retrieval.index_loader import IndexUnavailable, load_index
from app.settings import Settings
from ingestion.build_index import read_current
from ingestion.card_export import import_cards
from ingestion.embed import SentenceTransformerEmbedder
from ingestion.paths import Paths

log = logging.getLogger(__name__)


@dataclass
class Startup:
    pipeline: Pipeline
    built_index: bool  # this start fetched the sources and built a new index
    message: str  # why the index is missing, if it is


def build_index_from_sources(paths: Paths, embedder: SentenceTransformerEmbedder) -> str:
    """Fetch the official sources and build the index. Returns the run's index summary."""
    from ingestion.fetch import make_client
    from ingestion.lock import IngestLock
    from ingestion.registry import Registry
    from ingestion.run import Ingestor

    with Registry(paths.registry) as registry:
        added = import_cards(registry)
    log.info("imported %d reviewed fact cards", added)
    with IngestLock(paths.lock), make_client() as client:
        report = Ingestor(paths, client=client, embedder=embedder).run()
    failed = [r.doc_id for r in report.failed]
    if failed:
        log.warning("%d documents failed to fetch: %s", len(failed), ", ".join(failed))
    return report.index


def start(settings: Settings, paths: Paths | None = None) -> Startup:
    paths = paths or Paths.default()
    embedder = SentenceTransformerEmbedder()
    built, message = False, ""
    if read_current(paths.index) is None:
        try:
            message = build_index_from_sources(paths, embedder)
            built = True
        except Exception as exc:  # network, lock, parse: keep serving templates
            message = f"index build failed: {type(exc).__name__}"
            log.error(message)
    index = None
    try:
        index = load_index(paths.index, embedder)
        index.embed_query("warm up")  # load the embedding model now, not on the first question
    except IndexUnavailable as exc:
        message = message or str(exc)
        index = None
    return Startup(build_pipeline(settings, index, paths.registry), built, message)
