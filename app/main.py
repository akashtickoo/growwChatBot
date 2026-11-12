"""FastAPI entry point (optional JSON API). Run with: uvicorn app.main:app

The deployed UI is Streamlit (streamlit_app.py). This app serves the same pipeline as JSON:
POST /api/chat, GET /api/meta and GET /healthz.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import build_router
from app.pipeline import Pipeline, ServiceUnavailable, build_pipeline
from app.retrieval.index_loader import IndexUnavailable, load_index
from app.settings import get_settings
from ingestion.embed import Embedder
from ingestion.paths import Paths

log = logging.getLogger(__name__)

# JSON only: nothing may be loaded, framed or run from these responses.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _field_name(loc: tuple) -> str:
    # ("body", "message") → "message"; malformed JSON gives ("body", <position>) → "body"
    parts = [str(p) for p in loc if p != "body" and not isinstance(p, int)]
    return ".".join(parts) or "body"


def create_app(
    index_dir: Path | None = None,
    embedder: Embedder | None = None,
    pipeline: Pipeline | None = None,
) -> FastAPI:
    """`pipeline` replaces the production wiring (tests). Otherwise the index and the Groq client
    are loaded at startup."""
    # Load settings first so invalid configuration stops startup
    # (e.g. DEBUG_LOG_QUERIES on in production).
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if pipeline is not None:
            app.state.pipeline = pipeline
            yield
            return
        # The index is loaded once; a newer build is picked up on restart (EC-RET-08).
        index = None
        try:
            index = load_index(index_dir or Paths.default().index, embedder)
            index.embed_query("warm up")  # load the embedding model now, not on the first question
        except IndexUnavailable as exc:
            # Template replies still work; factual questions get a 503 (EC-RET-07).
            log.warning("index not loaded: %s", exc)
            index = None
        app.state.pipeline = build_pipeline(settings, index)
        yield

    app = FastAPI(
        title="HDFC Mutual Fund FAQ Assistant",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.pipeline = pipeline

    # Rate limit per client address. Behind a proxy, run uvicorn with --proxy-headers and
    # --forwarded-allow-ips set to the host's proxy only, so a forged X-Forwarded-For is
    # ignored (EC-API-05).
    limiter = Limiter(key_func=get_remote_address, headers_enabled=True, storage_uri="memory://")
    app.state.limiter = limiter

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field names only. FastAPI's default repeats the input, which could hold a PAN
        # (EC-PII-15, EC-API-01).
        fields = sorted({_field_name(e.get("loc", ())) for e in exc.errors()})
        return JSONResponse({"error": "invalid_request", "fields": fields}, status_code=422)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        response = JSONResponse({"error": "rate_limited"}, status_code=429)
        return request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)

    @app.exception_handler(ServiceUnavailable)
    async def unavailable(request: Request, exc: ServiceUnavailable) -> JSONResponse:
        return JSONResponse(
            {"error": "service_unavailable", "reason": exc.reason},
            status_code=503,
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": "http_error"}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error: %s", type(exc).__name__)  # type only, never content (§10)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.include_router(build_router(settings, limiter))

    @app.get("/healthz")
    def healthz() -> dict:
        current = app.state.pipeline
        index = current.index if current is not None else None
        body: dict = {"index_loaded": index is not None}
        if index is not None:
            body.update(
                index_version=index.version,
                index_built_at=index.built_at,
                chunk_count=len(index.chunks),
            )
        if current is not None:
            cutoff = datetime.now(UTC) - timedelta(days=settings.stale_source_days)
            body["llm_configured"] = current.llm is not None
            body["stale_sources"] = current.composer.documents.stale(cutoff)
        return body

    # No CORS middleware: same origin only (EC-API-06).
    return app


app = create_app()
