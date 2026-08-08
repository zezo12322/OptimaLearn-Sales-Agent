"""The model endpoint must be fully configured before the service will boot.

Two shapes are legal and one is not, and the illegal one is the dangerous case:
a half-set endpoint would let the service start, pass its health check, and fail
on the first prospect's message instead.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

# conftest sets the classic Azure vars for the rest of the suite, and
# pydantic-settings reads the environment for anything not passed explicitly.
# Without clearing them, every case below would silently inherit a complete
# configuration and pass while proving nothing — which is exactly what the first
# run of this file did.
AZURE_ENV = (
    "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION",
)


@pytest.fixture(autouse=True)
def _clear_azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AZURE_ENV:
        monkeypatch.delenv(name, raising=False)

BASE = {
    "internal_api_key": "k",
    "database_url": "postgresql+asyncpg://u:p@h/db",
    "sync_database_url": "postgresql+psycopg2://u:p@h/db",
    "redis_url": "redis://h:6379/0",
    "azure_openai_api_key": "key",
}


def test_v1_base_url_alone_is_enough() -> None:
    settings = Settings(
        **BASE,
        azure_openai_base_url="https://res.openai.azure.com/openai/v1",
        _env_file=None,
    )
    assert settings.azure_openai_base_url.endswith("/openai/v1")
    # No api-version to pin is the whole point of this shape.
    assert settings.azure_openai_api_version is None


def test_classic_endpoint_plus_version_is_enough() -> None:
    settings = Settings(
        **BASE,
        azure_openai_endpoint="https://res.openai.azure.com/",
        azure_openai_api_version="2024-10-21",
        _env_file=None,
    )
    assert settings.azure_openai_base_url is None


@pytest.mark.parametrize(
    "extra",
    [
        {},
        # The classic shape half-set. Each of these on its own is unusable, and
        # each is an easy thing to leave behind when switching between shapes.
        {"azure_openai_endpoint": "https://res.openai.azure.com/"},
        {"azure_openai_api_version": "2024-10-21"},
    ],
)
def test_incomplete_configuration_refuses_to_boot(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="Azure OpenAI is not configured"):
        Settings(**BASE, **extra, _env_file=None)
