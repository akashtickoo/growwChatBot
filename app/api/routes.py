"""JSON API (architecture §7): POST /api/chat, GET /api/meta. /healthz lives in app/main.py.

The deployed UI is the Streamlit app (streamlit_app.py), which calls the pipeline in-process.
This API serves the same pipeline for local integration and scripted checks.

Error mapping:
- 422 invalid input, field names only, never the input (EC-PII-15, EC-API-01) — app/main.py
- 429 rate limit with Retry-After (EC-API-04)
- 503 index or LLM unavailable, or the request took longer than REQUEST_TIMEOUT_S (EC-API-08)
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, StringConstraints
from slowapi import Limiter

from app.meta import Meta, load_meta
from app.models import ChatResponse
from app.pipeline import ServiceUnavailable
from app.settings import Settings

MAX_MESSAGE_CHARS = 500


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Trimmed first, so whitespace-only is empty (EC-IN-02). Length is in Unicode code points
    # (EC-IN-04), the same count Streamlit's chat input uses.
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)
    ]


def build_router(settings: Settings, limiter: Limiter) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/chat", response_model=ChatResponse)
    @limiter.limit(settings.rate_limit)
    async def chat(request: Request, response: Response, body: ChatRequest) -> ChatResponse:
        pipeline = request.app.state.pipeline
        try:
            return await asyncio.wait_for(
                run_in_threadpool(pipeline.answer, body.message),
                timeout=settings.request_timeout_s,
            )
        except TimeoutError:
            raise ServiceUnavailable("timeout") from None

    @router.get("/meta", response_model=Meta)
    def meta() -> Meta:
        return load_meta()

    return router
