from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import SecretStr
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

from app.config import get_settings


def async_url(url: str | SecretStr | URL) -> URL:
    """Parse a PostgreSQL URL and force the asyncpg driver."""
    raw = url.get_secret_value() if isinstance(url, SecretStr) else url
    parsed = make_url(raw)
    if parsed.get_backend_name() not in ("postgresql", "postgres"):
        raise ValueError("only PostgreSQL URLs are supported")
    return parsed.set(drivername="postgresql+asyncpg")


def create_engine(url: str | SecretStr | URL | None = None, **kwargs: Any) -> AsyncEngine:
    kwargs.setdefault("pool_pre_ping", True)
    # Keep bound parameters (client text, reply bodies) out of exception messages and logs.
    kwargs.setdefault("hide_parameters", True)
    return _create_async_engine(async_url(url or get_settings().DATABASE_URL), **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One session, one transaction: commit on success, roll back on error."""
    async with factory() as session, session.begin():
        yield session
