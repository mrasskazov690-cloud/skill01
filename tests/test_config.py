from typing import Any

import pytest

from app.config import Settings
from app.db.session import async_url

PASSWORD = "s3cr3t-Pa55"
URL = f"postgresql+asyncpg://dispatcher:{PASSWORD}@db.example:5432/dispatcher"


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_settings_never_render_the_database_password() -> None:
    settings = _settings(DATABASE_URL=URL)
    for rendered in (
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        repr(settings.model_dump()),
    ):
        assert PASSWORD not in rendered
    assert settings.DATABASE_URL.get_secret_value() == URL


def test_engine_url_masks_password() -> None:
    url = async_url(_settings(DATABASE_URL=URL).DATABASE_URL)
    assert PASSWORD not in str(url) and PASSWORD not in repr(url)
    assert url.password == PASSWORD


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("DISPATCH_MAX_SEND_ATTEMPTS", "")  # empty as in .env.example -> default
    settings = _settings()
    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.DISPATCH_MAX_SEND_ATTEMPTS == 3


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError):
        _settings()


def test_non_postgres_url_rejected() -> None:
    with pytest.raises(ValueError):
        async_url("sqlite:///x.db")
