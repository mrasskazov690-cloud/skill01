"""Settings loaded from the environment (and `.env` in the working directory)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


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

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
