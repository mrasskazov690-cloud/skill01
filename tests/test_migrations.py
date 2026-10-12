import asyncio

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
}


async def _inspect(url: URL) -> dict:
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:

            def collect(sync_conn) -> dict:
                insp = inspect(sync_conn)
                return {
                    "tables": set(insp.get_table_names()) - {"alembic_version"},
                    "diff": compare_metadata(MigrationContext.configure(sync_conn), Base.metadata),
                }

            info = await conn.run_sync(collect)
            info["checks"] = set(
                await conn.scalars(text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))
            )
            info["live_index"] = await conn.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_outbox_live_message'")
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

        command.downgrade(cfg, "base")
        assert asyncio.run(_inspect(url))["tables"] == set()

        command.upgrade(cfg, "head")
        assert asyncio.run(_inspect(url))["tables"] == EXPECTED_TABLES
