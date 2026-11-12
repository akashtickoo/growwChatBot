"""Streamlit UI (architecture §8). Entry point: streamlit_app.py.

The page calls the pipeline in-process, so the guards, the citation rules and the logging are
exactly those of the API. Notes:
- Answer text is shown as escaped Markdown: nothing from a model or a source can become a link,
  HTML or a formula (EC-UI-05). The only link is the citation, built here from the registry URL,
  and only when it is http(s) (EC-UI-06).
- Chat history lives in the browser session's memory on the server and is cleared on reload
  (EC-UI-08). A message the PII guard blocks is never stored or shown back; a placeholder is.
- Two rate limits run before each question: per session (RATE_LIMIT) and for the whole process
  (GLOBAL_RATE_LIMIT).
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date

import streamlit as st

from app.bootstrap import Startup, start
from app.guards.pii import contains_pii
from app.meta import Meta, load_meta
from app.models import ChatResponse, footer
from app.pipeline import ServiceUnavailable, clean
from app.ratelimit import SlidingWindowLimiter
from app.settings import get_settings

# Tests replace this to inject a pipeline (see tests/test_streamlit_ui.py).
startup_factory = None

PII_PLACEHOLDER = "(Message hidden: it looked like it contained personal details.)"
DUPLICATE_WINDOW_S = 3.0  # a second click on the same example within this time is ignored
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>$~])")
_STYLE = """
<style>
/* Keep the disclaimer visible at every scroll position (EC-UI-10). */
.st-key-disclaimer {
  position: sticky; top: 3.5rem; z-index: 99;
  background: var(--background-color, #fff); padding-top: .25rem;
}
.st-key-disclaimer [data-testid="stAlert"] { margin-bottom: 0; }
</style>
"""


def md_escape(text: str) -> str:
    """Show text literally in st.markdown: no links, emphasis, HTML or LaTeX."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _safe_url(url: str) -> str | None:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


# --- shared, process-wide resources -------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _startup() -> Startup:
    _configure_logging()
    return start(get_settings())


@st.cache_resource(show_spinner=False)
def _global_limiter() -> SlidingWindowLimiter:
    return SlidingWindowLimiter(get_settings().global_rate_limit)


@st.cache_resource(show_spinner=False)
def _executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="pipeline")


def _configure_logging() -> None:
    # One JSON line per request, without question text (§10), on stdout for the host's logs.
    requests_log = logging.getLogger("app.requests")
    if not requests_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        requests_log.addHandler(handler)
        requests_log.setLevel(logging.INFO)
        requests_log.propagate = False


# --- messages -----------------------------------------------------------------------------


def _assistant_message(response: ChatResponse) -> dict:
    return {
        "role": "assistant",
        "type": response.type,
        "text": response.text,
        "citation": response.citation.model_dump() if response.citation else None,
        "last_updated": response.last_updated.isoformat() if response.last_updated else None,
    }


def _notice(text: str) -> dict:
    return {"role": "assistant", "type": "notice", "text": text, "citation": None}


def render_message(message: dict) -> None:
    avatar = "🙂" if message["role"] == "user" else "📄"
    with st.chat_message(message["role"], avatar=avatar):
        if message.get("type") == "notice":
            st.info(message["text"])
            return
        st.markdown(md_escape(message["text"]))
        citation = message.get("citation")
        if citation:
            url = _safe_url(citation["url"])
            title = md_escape(citation["title"] or citation["url"])
            st.markdown(f"Source: [{title}]({url})" if url else f"Source: {title}")
        if message.get("last_updated"):
            st.caption(footer(date.fromisoformat(message["last_updated"])))


def _ask(question: str, startup: Startup, meta: Meta) -> None:
    """Run one question and append the exchange to the session history."""
    settings = get_settings()
    messages: list[dict] = st.session_state.messages
    text = question.strip()
    if not text:
        return
    if len(text) > meta.max_message_chars:
        messages.append(_notice(f"Please keep questions to {meta.max_message_chars} characters."))
        return

    last = st.session_state.get("last_question")
    if last and last[0] == text and time.monotonic() - last[1] < DUPLICATE_WINDOW_S:
        return  # double click on an example (EC-UI-01/02)
    st.session_state.last_question = (text, time.monotonic())

    session_limiter = st.session_state.setdefault(
        "limiter", SlidingWindowLimiter(settings.rate_limit)
    )
    wait = session_limiter.try_acquire() or _global_limiter().try_acquire()
    if wait:
        messages.append(
            _notice(f"You're sending questions quickly. Please wait about {int(wait) + 1} seconds.")
        )
        return

    # The PII guard runs again in the pipeline; this only decides what the history shows.
    shown = PII_PLACEHOLDER if contains_pii(clean(text)) else text
    messages.append({"role": "user", "type": "question", "text": shown, "citation": None})
    render_message(messages[-1])

    with st.chat_message("assistant", avatar="📄"), st.spinner("Looking that up…"):
        future = _executor().submit(startup.pipeline.answer, text)
        try:
            response = future.result(timeout=settings.request_timeout_s)
        except FutureTimeout:
            messages.append(_notice("That took too long. Please try again in a moment."))
            return
        except ServiceUnavailable:
            messages.append(
                _notice("The assistant is temporarily unavailable. Please try again in a moment.")
            )
            return
    messages.append(_assistant_message(response))


# --- page ---------------------------------------------------------------------------------


def _sidebar(meta: Meta, startup: Startup) -> None:
    with st.sidebar:
        st.markdown(f"**{md_escape(meta.disclaimer)}**")
        st.markdown(
            "Answers come only from official HDFC Mutual Fund pages and documents, each with one "
            "source link. Covered schemes (Direct Growth plans):"
        )
        st.markdown("\n".join(f"- {md_escape(s.short_name)}" for s in meta.schemes))
        st.caption(meta.pii_hint)
        index = startup.pipeline.index
        if index is not None:
            st.caption(f"Index {index.version}, {len(index.chunks)} passages.")
        if st.button("Clear conversation", key="clear"):
            st.session_state.messages = []
            st.session_state.pop("last_question", None)
            st.rerun()


def main() -> None:
    meta = load_meta()
    st.set_page_config(page_title=meta.title, page_icon="📄", layout="centered", menu_items={})
    st.markdown(_STYLE, unsafe_allow_html=True)  # static CSS only, no user or model text

    with st.container(key="disclaimer"):
        st.warning(meta.disclaimer, icon="⚠️")
    st.title(meta.title)

    if startup_factory is not None:
        startup = startup_factory()
    else:
        with st.spinner(
            "Preparing the index from the official HDFC MF sources. "
            "The first start after a deploy can take a few minutes…"
        ):
            startup = _startup()
    if startup.pipeline.index is None:
        st.info(
            "Factual answers are unavailable right now because the search index isn't loaded. "
            "Other replies still work."
        )
    if startup.pipeline.llm is None:
        st.info("Factual answers are unavailable right now: the Groq API key isn't configured.")
    _sidebar(meta, startup)

    st.session_state.setdefault("messages", [])
    with st.chat_message("assistant", avatar="📄"):
        st.markdown(md_escape(meta.welcome))
        for i, example in enumerate(meta.examples):
            if st.button(example, key=f"example_{i}"):
                st.session_state.queued = example

    for message in st.session_state.messages:
        render_message(message)

    st.caption(meta.pii_hint)
    typed = st.chat_input("Type your question…", max_chars=meta.max_message_chars)
    question = typed or st.session_state.pop("queued", None)
    if question:
        _ask(question, startup, meta)
        st.rerun()
