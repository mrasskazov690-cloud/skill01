"""Settings loaded from the environment (and `.env` in the working directory)."""

import os
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DispatchOrder = Literal["newest_first", "oldest_first"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `KEY=` lines from .env.example fall back to defaults
        extra="ignore",
        # A failed field must not echo its input: for a missing one that is the whole
        # dict of values read from the environment and .env, secrets included.
        hide_input_in_errors=True,
    )

    # SecretStr keeps the URL (with its password) out of repr/str/model_dump_json.
    DATABASE_URL: SecretStr
    LOG_LEVEL: LogLevel = "INFO"
    DISPATCH_MAX_SEND_ATTEMPTS: int = Field(default=3, ge=1)
    DISPATCH_LEASE_SECONDS: int = Field(default=300, ge=1)
    DISPATCH_CANDIDATE_LIMIT: int = Field(default=10, ge=1)
    DISPATCH_ORDER: DispatchOrder = "newest_first"

    # Telegram (MTProto). Sessions are read from TG_SESSION_<NAME>, named per account
    # in accounts.session_env; they are not settings fields.
    TG_API_ID: int | None = None
    TG_API_HASH: SecretStr | None = None
    WORK_CHAT_ID: int | None = None
    READER_ACCOUNT_ID: int | None = None
    WORK_CHAT_POLL_SECONDS: int = Field(default=15, ge=1)
    WORK_CHAT_BACKFILL: int = Field(default=200, ge=0)

    # Sending: one send per interval system-wide, a daily cap per account (counted in
    # TIMEZONE), the first CONFIRM_FIRST_N replies only after the owner approves them.
    SEND_INTERVAL_SECONDS: int = Field(default=120, ge=1)
    ACCOUNT_DAILY_LIMIT: int = Field(default=20, ge=0)
    CONFIRM_FIRST_N: int = Field(default=20, ge=0)
    TIMEZONE: str = "Europe/Moscow"
    SEND_RETRY_UNCERTAIN_WITH_RANDOM_ID: bool = False

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("TIMEZONE")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown IANA time zone") from exc
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment


def read_env(name: str) -> str | None:
    """A variable that is not a settings field (TG_SESSION_<NAME>), looked up where
    Settings looks: the environment first, then `.env`. The file is read, not exported."""
    value = os.environ.get(name)
    if value:
        return value
    env_file = Settings.model_config.get("env_file")
    if not isinstance(env_file, str) or not os.path.isfile(env_file):
        return None
    return dotenv_values(env_file, encoding="utf-8").get(name) or None
