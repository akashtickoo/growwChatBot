"""Application settings, read from environment variables and an optional .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["low", "medium", "high"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (Groq)
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_fallback_model: str | None = None
    llm_reasoning_effort: ReasoningEffort = "low"
    extraction_reasoning_effort: ReasoningEffort = "medium"
    llm_max_tokens: int = Field(default=2048, gt=0)
    judge_model: str = "qwen/qwen3.8-27b"

    # App
    app_env: Literal["development", "production"] = "development"
    index_dir: Path = Path("data/index")
    retrieval_top_k: int = Field(default=5, ge=1)
    min_similarity: float = Field(default=0.45, ge=0.0, le=1.0)
    request_timeout_s: float = Field(default=20.0, gt=0)
    rate_limit: str = "20/minute"
    display_timezone: str = "Asia/Kolkata"
    stale_source_days: int = Field(default=14, ge=1)
    debug_log_queries: bool = False

    @field_validator("groq_api_key", "groq_fallback_model", mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        # An empty line in .env (e.g. "GROQ_FALLBACK_MODEL=") means "not set".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("display_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _no_query_logging_in_production(self) -> "Settings":
        # EC-PII-18: question text must never be logged in production.
        if self.app_env == "production" and self.debug_log_queries:
            raise ValueError("DEBUG_LOG_QUERIES must be false when APP_ENV=production")
        return self

    @property
    def llm_configured(self) -> bool:
        return self.groq_api_key is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
