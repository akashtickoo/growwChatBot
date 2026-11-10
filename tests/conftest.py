from collections.abc import Iterator

import pytest

from app.settings import Settings, get_settings

SETTINGS_ENV_VARS = [name.upper() for name in Settings.model_fields]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Remove every settings variable from the environment so tests see only what they set."""
    for var in SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
