from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

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


def test_validation_error_does_not_echo_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TEST_DATABASE_URL=postgresql://dispatcher:{PASSWORD}@db.example/test\n"
        "TG_API_HASH=hash-value-1\n"
        "DISPATCH_MAX_SEND_ATTEMPTS=-7\n"
    )

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=env_file)  # type: ignore[call-arg]

    rendered = f"{exc.value}\n{exc.value!r}"
    for leaked in (PASSWORD, "hash-value-1", "-7"):
        assert leaked not in rendered
    assert "DATABASE_URL" in rendered and "DISPATCH_MAX_SEND_ATTEMPTS" in rendered


def test_non_postgres_url_rejected() -> None:
    with pytest.raises(ValueError):
        async_url("sqlite:///x.db")


def test_send_limits_and_tail_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ACCOUNT_DAILY_LIMIT", "REACTION_PRIVACY", "DRAFT_TTL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings(DATABASE_URL=URL)
    assert settings.ACCOUNT_DAILY_LIMIT == 25  # 50 a day on two accounts
    assert settings.REACTION_PRIVACY == "🚫"
    assert settings.DRAFT_TTL_HOURS == 24


def test_draft_provider_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DRAFT_PROVIDER", "ANTHROPIC_API_KEY", "PROFILE_PATH"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings(DATABASE_URL=URL)
    assert settings.DRAFT_PROVIDER == "template"
    assert settings.ANTHROPIC_API_KEY is None
    assert settings.PROFILE_PATH == "profile.local.yaml"


def test_draft_provider_is_llm_or_template() -> None:
    assert _settings(DATABASE_URL=URL, DRAFT_PROVIDER="llm").DRAFT_PROVIDER == "llm"
    with pytest.raises(ValidationError):
        _settings(DATABASE_URL=URL, DRAFT_PROVIDER="gpt")
