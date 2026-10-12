import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL

from app.db.base import Base
from app.db.session import create_engine
from tests.conftest import alembic_config, temporary_database

EXPECTED_TABLES = {
    "accounts",
    "clients",
    "conversations",
    "messages",
    "assignments",
    "templates",
    "settings",
    "outbox",
    "audit_log",
    "drafts",
}
TABLES_0001 = EXPECTED_TABLES - {"drafts"}
COLUMNS_0002 = {
    "clients": {"external_key"},
    "messages": {"text_hash", "link", "reaction"},
    "accounts": {"daily_send_limit", "last_sent_at", "session_env"},
    "outbox": {"random_id"},
}


async def _inspect(url: URL) -> dict:
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:

            def collect(sync_conn) -> dict:
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names()) - {"alembic_version"}
                return {
                    "tables": tables,
                    "columns": {t: {c["name"] for c in insp.get_columns(t)} for t in tables},
                    "diff": compare_metadata(MigrationContext.configure(sync_conn), Base.metadata),
                }

            info = await conn.run_sync(collect)
            info["checks"] = set(
                await conn.scalars(text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))
            )
            index_def = text("SELECT indexdef FROM pg_indexes WHERE indexname = :name")
            info["live_index"] = await conn.scalar(index_def, {"name": "uq_outbox_live_message"})
            info["pending_draft_index"] = await conn.scalar(
                index_def, {"name": "uq_drafts_pending_message"}
            )
            return info
    finally:
        await engine.dispose()


def test_upgrade_downgrade_upgrade() -> None:
    with temporary_database("dispatcher_migrations") as url:
        cfg = alembic_config(url)

        command.upgrade(cfg, "head")
        info = asyncio.run(_inspect(url))
        assert info["tables"] == EXPECTED_TABLES
        assert info["diff"] == [], "models and migrations disagree"
        model_checks = {
            c.name
            for t in Base.metadata.tables.values()
            for c in t.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        assert model_checks <= info["checks"]
        assert "UNIQUE INDEX" in info["live_index"]
        for status in ("queued", "sending", "sent", "uncertain"):
            assert f"'{status}'" in info["live_index"]
        assert "'failed'" not in info["live_index"]
        assert "UNIQUE INDEX" in info["pending_draft_index"]
        assert "'pending'" in info["pending_draft_index"]

        command.downgrade(cfg, "0001_initial")
        info = asyncio.run(_inspect(url))
        assert info["tables"] == TABLES_0001
        for table, columns in COLUMNS_0002.items():
            assert not columns & info["columns"][table], table

        command.upgrade(cfg, "head")
        info = asyncio.run(_inspect(url))
        assert info["diff"] == []
        for table, columns in COLUMNS_0002.items():
            assert columns <= info["columns"][table], table

        command.downgrade(cfg, "base")
        assert asyncio.run(_inspect(url))["tables"] == set()

        command.upgrade(cfg, "head")
        assert asyncio.run(_inspect(url))["tables"] == EXPECTED_TABLES


async def _execute(url: URL, *statements: str) -> None:
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


def test_downgrade_refuses_to_drop_leads_with_unknown_authors() -> None:
    with temporary_database("dispatcher_migrations") as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        asyncio.run(_execute(url, "INSERT INTO clients (external_key) VALUES ('msg:-100/7')"))

        with pytest.raises(RuntimeError, match="external_key"):
            command.downgrade(cfg, "0001_initial")
        assert "drafts" in asyncio.run(_inspect(url))["tables"]  # nothing was changed

        asyncio.run(_execute(url, "DELETE FROM clients"))
        command.downgrade(cfg, "0001_initial")
        assert asyncio.run(_inspect(url))["tables"] == TABLES_0001
