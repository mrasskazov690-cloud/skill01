from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog
from app.logging import redact


async def write_audit(
    session: AsyncSession,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append an audit record in the caller's transaction.

    `details` must hold IDs, statuses and error codes only; keys that look like
    client text or secrets (text, body, token, ...) are dropped as a safety net.
    """
    await session.execute(
        insert(AuditLog).values(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=redact(dict(details or {})),
        )
    )
