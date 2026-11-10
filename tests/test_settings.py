from pathlib import Path

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.settings import Settings


def test_defaults_with_only_api_key(clean_env):
    """EV-0-05: every default applies when only the API key is set."""
    clean_env.setenv("GROQ_API_KEY", "test-key")
    s = Settings(_env_file=None)

    assert s.llm_configured
    assert s.groq_model == "openai/gpt-oss-120b"
    assert s.groq_fallback_model is None
    assert s.llm_reasoning_effort == "low"
    assert s.extraction_reasoning_effort == "medium"
    assert s.llm_max_tokens == 2048
    assert s.judge_model == "qwen/qwen3.8-27b"
    assert s.app_env == "development"
    assert s.index_dir == Path("data/index")
    assert s.retrieval_top_k == 5
    assert s.min_similarity == 0.45
    assert s.request_timeout_s == 20
    assert s.rate_limit == "20/minute"
    assert s.display_timezone == "Asia/Kolkata"
    assert s.stale_source_days == 14
    assert s.debug_log_queries is False


def test_starts_without_api_key(clean_env):
    s = Settings(_env_file=None)
    assert not s.llm_configured


def test_empty_values_mean_unset(clean_env):
    clean_env.setenv("GROQ_API_KEY", "")
    clean_env.setenv("GROQ_FALLBACK_MODEL", "  ")
    s = Settings(_env_file=None)
    assert s.groq_api_key is None
    assert s.groq_fallback_model is None


def test_api_key_never_printed(clean_env):
    """EC-SEC-05: the key must not appear when settings are printed or dumped."""
    clean_env.setenv("GROQ_API_KEY", "secret-value-123")
    s = Settings(_env_file=None)
    assert "secret-value-123" not in repr(s)
    assert "secret-value-123" not in str(s.model_dump())
    assert s.groq_api_key.get_secret_value() == "secret-value-123"


def test_query_logging_blocked_in_production(clean_env):
    """EV-0-06 / EC-PII-18."""
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DEBUG_LOG_QUERIES", "true")
    with pytest.raises(ValidationError, match="DEBUG_LOG_QUERIES"):
        Settings(_env_file=None)


def test_app_refuses_to_start_with_query_logging_in_production(clean_env):
    """EV-0-06: the app itself fails at startup, not only the settings object."""
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DEBUG_LOG_QUERIES", "true")
    with pytest.raises(ValidationError):
        create_app()


def test_query_logging_allowed_in_development(clean_env):
    clean_env.setenv("DEBUG_LOG_QUERIES", "true")
    assert Settings(_env_file=None).debug_log_queries is True


def test_unknown_timezone_rejected(clean_env):
    clean_env.setenv("DISPLAY_TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValidationError, match="timezone"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("LLM_REASONING_EFFORT", "extreme"),
        ("APP_ENV", "staging"),
        ("MIN_SIMILARITY", "1.5"),
        ("RETRIEVAL_TOP_K", "0"),
    ],
)
def test_invalid_values_rejected(clean_env, var, value):
    clean_env.setenv(var, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
