"""Streamlit UI (app/ui.py) run headless with Streamlit's AppTest.

The real pipeline runs with a fake LLM over the synthetic fixture index, so no API calls and no
model download.
"""

from pathlib import Path

import pytest
from fake_index import CHUNKS, NOW, bow_embedder
from streamlit.testing.v1 import AppTest
from test_pipeline import GOOD, FakeCompleter, _docs

import app.ui as ui
from app.bootstrap import Startup
from app.llm.client import AnswerClient
from app.pipeline import Pipeline
from app.responses.composer import Composer
from app.retrieval.index_loader import load_index
from app.settings import Settings, get_settings
from ingestion.build_index import build_index

APP = str(Path(__file__).resolve().parents[1] / "streamlit_app.py")
PAN = "ABCPE1234F"


@pytest.fixture
def run_app(tmp_path, clean_env, monkeypatch):
    build_index(tmp_path, CHUNKS, bow_embedder, doc_hashes={"x": "h"}, now=NOW)
    index = load_index(tmp_path, bow_embedder)

    def make(*replies, rate_limit="20/minute", with_index=True, with_llm=True):
        clean_env.setenv("RATE_LIMIT", rate_limit)
        get_settings.cache_clear()
        fake = FakeCompleter(*replies)
        pipeline = Pipeline(
            settings=Settings(_env_file=None, min_similarity=0.3),
            composer=Composer(_docs()),
            index=index if with_index else None,
            llm=AnswerClient(fake, reasoning_effort="low", max_tokens=2048) if with_llm else None,
        )
        monkeypatch.setattr(ui, "startup_factory", lambda: Startup(pipeline, False, ""))
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        return at, fake

    yield make
    get_settings.cache_clear()


def _texts(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [w.value for w in at.warning] + [i.value for i in at.info]
    return "\n".join(str(p) for p in parts)


def test_page_shows_disclaimer_welcome_and_three_examples(run_app):
    """EV-5-04."""
    at, _ = run_app()
    assert not at.exception
    assert at.warning[0].value == "Facts-only. No investment advice."
    assert at.title[0].value == "HDFC Mutual Fund FAQ Assistant"
    examples = [b for b in at.button if b.key and b.key.startswith("example_")]
    assert len(examples) == 3
    assert "expense ratio" in examples[0].label
    assert len(at.chat_input) == 1


def test_typed_question_gets_a_cited_answer_with_the_footer(run_app):
    at, fake = run_app(GOOD)
    at.chat_input[0].set_value("What is the exit load of HDFC Mid Cap?").run()
    text = _texts(at)
    assert "exit load of HDFC Mid Cap Fund is 1 percent" in text.replace("\\", "")
    assert "Source: [" in text and "](https://www.hdfcfund.com/" in text
    assert "Last updated from sources: 11 Nov 2026" in text
    assert len(fake.requests) == 1


def test_example_button_sends_its_question(run_app):
    at, _ = run_app()
    at.button(key="example_2").click().run()  # capital gains statement: no LLM reply queued
    roles = [m.name for m in at.chat_message]
    assert roles.count("user") == 1 and roles[-1] == "assistant"


def test_pii_message_is_blocked_and_never_shown_back(run_app):
    """EC-UI-07: the PAN is not stored in the history or echoed."""
    at, fake = run_app(GOOD)
    at.chat_input[0].set_value(f"My PAN is {PAN}, what is the exit load of HDFC Mid Cap?").run()
    text = _texts(at)
    assert PAN not in text
    assert ui.md_escape(ui.PII_PLACEHOLDER) in text
    assert "Please don" in text and fake.requests == []
    assert all(PAN not in m["text"] for m in at.session_state.messages)


def test_session_rate_limit_shows_a_wait_message(run_app):
    at, _ = run_app(rate_limit="1/minute")
    at.chat_input[0].set_value("hi").run()
    at.chat_input[0].set_value("hello there").run()
    assert "sending questions quickly" in _texts(at)


def test_without_index_templates_still_work(run_app):
    """EC-RET-07."""
    at, _ = run_app(with_index=False)
    assert "index isn't loaded" in _texts(at)
    at.chat_input[0].set_value("Should I invest in HDFC Mid Cap?").run()
    assert "can't advise" in _texts(at).replace("\\", "")


def test_factual_question_without_llm_shows_unavailable(run_app):
    at, _ = run_app(with_llm=False)
    at.chat_input[0].set_value("What is the exit load of HDFC Mid Cap?").run()
    assert "temporarily unavailable" in _texts(at)


def test_clear_conversation_empties_the_history(run_app):
    at, _ = run_app()
    at.chat_input[0].set_value("hi").run()
    assert at.session_state.messages
    at.button(key="clear").click().run()
    assert at.session_state.messages == []


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("<b>bold</b>", "\\<b\\>bold\\</b\\>"),
        ("[link](https://x.y)", "\\[link\\]\\(https://x\\.y\\)"),
        ("costs $5 and $6", "costs \\$5 and \\$6"),
        ("0.74% *TER*", "0\\.74% \\*TER\\*"),
    ],
)
def test_markdown_is_escaped(raw, shown):
    """EC-UI-05: HTML, links, LaTeX and emphasis in answer text are shown literally."""
    assert ui.md_escape(raw) == shown


def test_only_http_citations_become_links():
    """EC-UI-06."""
    assert ui._safe_url("javascript:alert(1)") is None
    assert ui._safe_url("https://x.y/a (b).pdf") == "https://x.y/a%20%28b%29.pdf"
