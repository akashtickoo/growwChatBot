"""FastAPI entry point. Run with: uvicorn app.main:app"""

from fastapi import FastAPI

from app.settings import get_settings


def create_app() -> FastAPI:
    # Load settings first so invalid configuration stops startup
    # (e.g. DEBUG_LOG_QUERIES on in production).
    get_settings()

    app = FastAPI(title="HDFC Mutual Fund FAQ Assistant", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        # The index loader arrives in Phase 4B.
        return {"index_loaded": False}

    return app


app = create_app()
