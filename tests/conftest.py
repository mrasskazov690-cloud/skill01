"""Database fixtures.

Each pytest session creates a fresh PostgreSQL database (unique name) on the server
from TEST_DATABASE_URL, runs `alembic upgrade head` on it and drops it at the end.
Tables are truncated before every test that uses the `engine` fixture.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import NullPool, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import models  # noqa: F401  (registers tables)
from app.db.base import Base
from app.db.models import Account
from app.db.session import async_url, create_engine, create_sessionmaker, transaction
from tests.factories import create_account

ROOT = Path(__file__).resolve().parents[1]
ALL_TABLES = ", ".join(table.name for table in Base.metadata.sorted_tables)


class _TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_ignore_empty=True, extra="ignore")
    TEST_DATABASE_URL: SecretStr | None = None


def server_url() -> URL:
    url = _TestEnv().TEST_DATABASE_URL
    if url is None:
        pytest.fail("TEST_DATABASE_URL is not set (see .env.example and README)", pytrace=False)
    return async_url(url)


async def _run_autocommit(url: URL, sql: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@contextmanager
def temporary_database(prefix: str = "dispatcher_pytest") -> Iterator[URL]:
    """Create an empty database next to TEST_DATABASE_URL's one; drop it afterwards."""
    server = server_url()
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    asyncio.run(_run_autocommit(server, f'CREATE DATABASE "{name}"'))
    try:
        yield server.set(database=name)
    finally:
        _drop_database(server, name)


def _drop_database(server: URL, name: str, attempts: int = 5) -> None:
    # FORCE cannot terminate a backend the role does not own (an autovacuum worker that
    # just started on the busy new database): that refusal is transient, so retry.
    for attempt in range(1, attempts + 1):
        try:
            asyncio.run(_run_autocommit(server, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            return
        except ProgrammingError:
            if attempt == attempts:
                raise
            time.sleep(0.2 * attempt)


def alembic_config(url: URL) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[URL]:
    # Sync fixture on purpose: Alembic's env.py runs its own event loop.
    with temporary_database() as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
async def engine(database_url: URL) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(database_url, pool_size=30, max_overflow=10)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def make_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[Account]]:
    """Create and commit an account: `await make_account(kind="main", max_active=2)`."""

    async def _make(**overrides: Any) -> Account:
        async with transaction(session_factory) as s:
            return await create_account(s, **overrides)

    return _make


@pytest.fixture
def settings() -> Settings:
    """The cached settings services read; change fields with monkeypatch.setattr."""
    return get_settings()


@pytest.fixture
def oldest_first(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Core FIFO dispatch order (the default is newest_first, for leads)."""
    monkeypatch.setattr(settings, "DISPATCH_ORDER", "oldest_first")
