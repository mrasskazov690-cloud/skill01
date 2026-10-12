"""Structured JSON logging with redaction of client text and secrets."""

import logging
import sys
from collections.abc import Mapping
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

# A key is sensitive if it equals one of these words or contains one as a
# `_`/`-`-separated part (e.g. `message_text`, `bot_token`, `session_string`).
SENSITIVE_WORDS = frozenset({"text", "body", "password", "token", "secret", "api_hash", "session"})
SENSITIVE_KEYS = frozenset({"api_hash", "database_url", "dsn"})


def is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return not SENSITIVE_WORDS.isdisjoint(lowered.replace("-", "_").split("_"))


def redact(value: Any) -> Any:
    """Return a copy of `value` with sensitive keys removed at any nesting depth."""
    if isinstance(value, Mapping):
        return {k: redact(v) for k, v in value.items() if not is_sensitive_key(k)}
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    return value


def redact_processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    return {k: redact(v) for k, v in event_dict.items() if not is_sensitive_key(k)}


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.WriteLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None, **initial: Any) -> Any:
    if name is not None:
        initial["logger"] = name
    return structlog.get_logger().bind(**initial)
