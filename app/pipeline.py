"""Question → response (architecture §6.1).

Order: PII guard → intent rules → scheme resolver → retrieval → LLM → validator → composer.
Template replies (PII, refusals, redirects, scope, clarify, greeting) need neither the index
nor the LLM. Factual questions need both; without them `ServiceUnavailable` is raised and the
API answers 503 (EC-RET-07, EC-LLM-12).

Each request writes one JSON log line with ids, types and timings, never the question text
(§10). `DEBUG_LOG_QUERIES` adds the text in development only, and never for a PII message.
"""

import json
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.guards.intent import classify_intent
from app.guards.output_validator import validate
from app.guards.pii import detect_pii
from app.llm.client import AnswerClient
from app.llm.groq_json import LLMBadOutput, LLMError
from app.llm.prompts import build_retry_message, build_user_message
from app.models import ChatResponse
from app.responses.composer import Composer
from app.retrieval.hybrid import RetrievalResult, retrieve
from app.retrieval.index_loader import LoadedIndex
from app.retrieval.scheme_resolver import resolve
from app.settings import Settings

log = logging.getLogger("app.requests")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏ -‮⁠﻿]")


class ServiceUnavailable(RuntimeError):
    """The index or the LLM is needed but not available. The API maps this to 503."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # index_unavailable | llm_not_configured | llm_unavailable


@dataclass
class Trace:
    """What happened to one request. Logged (without question text) and shown by the CLI."""

    request_id: str
    type: str = ""
    intent: str = ""
    intent_rule: str = ""
    pii_kinds: list[str] = field(default_factory=list)
    resolver: str = ""
    scheme_id: str | None = None
    fields: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)
    best_similarity: float | None = None
    llm_status: str = ""
    llm_attempts: int = 0
    validator: list[list[str]] = field(default_factory=list)  # failed checks per attempt
    model: str = ""
    usage: list[dict] = field(default_factory=list)
    rate_limit_wait_s: float = 0.0
    citation_url: str = ""
    error: str = ""
    latency_ms: float = 0.0


def clean(message: str) -> str:
    """NFKC and no control or zero-width characters, before any check (EC-IN-08)."""
    return " ".join(_CONTROL.sub("", unicodedata.normalize("NFKC", message)).split())


class Pipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        composer: Composer,
        index: LoadedIndex | None,
        llm: AnswerClient | None,
    ):
        self.settings = settings
        self.composer = composer
        self.index = index
        self.llm = llm
        self.scheme_names = {sid: s.name for sid, s in composer.schemes.items()}

    # --- public --------------------------------------------------------------------------

    def answer(self, message: str) -> ChatResponse:
        return self.run(message)[0]

    def run(self, message: str) -> tuple[ChatResponse, Trace]:
        trace = Trace(request_id=uuid.uuid4().hex)
        start = time.perf_counter()
        text = clean(message)
        try:
            response = self._respond(text, trace)
        except ServiceUnavailable as exc:
            trace.type, trace.error = "error", exc.reason
            self._log(trace, start, text)
            raise
        response.request_id = trace.request_id
        trace.type = response.type
        trace.citation_url = response.citation.url if response.citation else ""
        self._log(trace, start, text)
        return response, trace

    def answer_from_chunks(
        self, question: str, scheme_id: str | None, chunks: list
    ) -> tuple[ChatResponse, Trace]:
        """LLM step, validator and composer on given chunks, skipping guards and retrieval.

        Used by the generation eval (eval.md §9.2), which holds retrieval fixed.
        """
        from app.retrieval.hybrid import ScoredChunk

        if self.llm is None:
            raise ServiceUnavailable("llm_not_configured")
        trace = Trace(request_id=uuid.uuid4().hex, scheme_id=scheme_id)
        found = RetrievalResult(
            chunks=[ScoredChunk(c, 1.0, None) for c in chunks], best_similarity=1.0, relevant=True
        )
        trace.chunk_ids = found.chunk_ids
        start = time.perf_counter()
        response = self._llm_answer(clean(question), scheme_id, found, trace)
        response.request_id = trace.request_id
        trace.type = response.type
        trace.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return response, trace

    # --- stages --------------------------------------------------------------------------

    def _respond(self, text: str, trace: Trace) -> ChatResponse:
        c = self.composer
        if kinds := detect_pii(text):
            trace.pii_kinds = kinds  # kinds only; the message goes no further (EC-PII-10)
            return c.pii_blocked()

        intent = classify_intent(text)
        trace.intent, trace.intent_rule = intent.intent, intent.rule
        if intent.intent == "greeting":
            return c.greeting()
        if intent.intent == "out_of_scope":
            if intent.rule == "other_amc":  # name the 7 covered schemes (EC-SCH-07)
                return c.not_covered("other_amc", None)
            return c.out_of_scope()

        resolution = resolve(text)
        trace.resolver, trace.fields = resolution.kind, resolution.fields
        trace.scheme_id = resolution.scheme_id or resolution.closest_scheme_id
        if intent.intent == "advisory":
            # D5: a fact mixed into an advisory question is refused, with an invitation to ask
            # for the fact on its own.
            return c.refusal(mixed=bool(resolution.fields))
        if resolution.kind == "not_covered":  # before performance: never link another fund
            return c.not_covered(resolution.reason, resolution.closest_scheme_id)
        if intent.intent == "performance":
            return c.performance(resolution.scheme_id, nav=intent.rule == "nav")
        if resolution.kind in ("several", "needs_scheme"):
            return c.clarify(several=resolution.kind == "several")

        scheme_id = resolution.scheme_id  # None for a general question (statement pages)
        if self.index is None:
            raise ServiceUnavailable("index_unavailable")
        found = retrieve(
            self.index,
            text,
            scheme_id=scheme_id,
            fields=resolution.fields,
            top_k=self.settings.retrieval_top_k,
            min_similarity=self.settings.min_similarity,
        )
        trace.chunk_ids = found.chunk_ids
        trace.best_similarity = round(found.best_similarity, 4)
        if not found.relevant or not found.chunks:
            return c.not_found(scheme_id)  # no LLM call (EC-RET-01)
        if self.llm is None:
            raise ServiceUnavailable("llm_not_configured")
        return self._llm_answer(text, scheme_id, found, trace)

    def _llm_answer(
        self, question: str, scheme_id: str | None, found: RetrievalResult, trace: Trace
    ) -> ChatResponse:
        c = self.composer
        chunks = [s.chunk for s in found.chunks]
        turns = [
            {
                "role": "user",
                "content": build_user_message(question, chunks, self.scheme_names),
            }
        ]
        for attempt in range(2):  # the first try and one retry (§6.7)
            trace.llm_attempts = attempt + 1
            try:
                result = self.llm.complete(turns)
            except LLMBadOutput as exc:  # cut off, empty or malformed (EC-LLM-04/05)
                trace.validator.append(["bad_output"])
                turns = [turns[0], {"role": "user", "content": _bad_output_note(exc)}]
                continue
            except LLMError as exc:  # unavailable, rate-limited out, API error
                trace.error = type(exc).__name__
                raise ServiceUnavailable("llm_unavailable") from None
            trace.model = result.model
            trace.rate_limit_wait_s += result.waited_s
            if result.usage:
                trace.usage.append(result.usage)
            reply = result.answer
            trace.llm_status = reply.status

            if reply.status == "advisory":
                return c.refusal()
            if reply.status == "out_of_scope":
                return c.out_of_scope()
            if reply.status == "insufficient_context":
                return c.not_found(scheme_id)

            cited = next((ch for ch in chunks if ch.chunk_id == reply.source_id), None)
            doc = self.composer.documents.get(cited.doc_id) if cited else None
            check = validate(
                reply,
                chunks,
                question=question,
                extra_dates=[doc.doc_as_of] if doc else [],
            )
            trace.validator.append(check.checks)
            if check.ok:
                response = c.answer(reply.answer, cited)
                return response if response is not None else c.not_found(scheme_id)
            turns = [
                turns[0],
                {"role": "assistant", "content": result.raw},
                {"role": "user", "content": build_retry_message(check.messages)},
            ]
        return c.not_found(scheme_id)

    # --- logging -------------------------------------------------------------------------

    def _log(self, trace: Trace, start: float, text: str) -> None:
        trace.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        record = asdict(trace)
        record.pop("usage")
        record["tokens"] = sum(u.get("total_tokens", 0) for u in trace.usage) or None
        if (
            self.settings.debug_log_queries
            and self.settings.app_env != "production"
            and not trace.pii_kinds
        ):
            record["question"] = text  # local debugging only (EC-PII-18)
        log.info(json.dumps(record, ensure_ascii=False))


def build_pipeline(
    settings: Settings, index: LoadedIndex | None, registry_path: Path | None = None
) -> Pipeline:
    """The production wiring: registry documents, templates and the Groq answer client."""
    from app.llm.client import make_answer_client
    from app.responses.documents import RegistryDocuments
    from ingestion.paths import Paths

    composer = Composer(
        RegistryDocuments(registry_path or Paths.default().registry),
        timezone=settings.display_timezone,
    )
    return Pipeline(
        settings=settings, composer=composer, index=index, llm=make_answer_client(settings)
    )


def _bad_output_note(exc: Exception) -> str:
    return (
        f"Your previous reply could not be used ({exc}). Reply again with a short answer of at "
        "most 3 sentences in the required JSON format."
    )
