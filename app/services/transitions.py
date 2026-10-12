"""The only way services change message and outbox statuses: validate, update, audit.

Callers must already hold the row lock; the `status = current` guard turns a
violated assumption into an error instead of a silent lost update.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Outbox
from app.domain.states import (
    MessageStatus,
    SendStatus,
    can_transition,
    ensure_send_transition,
    ensure_transition,
)
from app.services.audit import write_audit


async def change_message_status(
    session: AsyncSession,
    message_id: int,
    current: MessageStatus | str,
    target: MessageStatus | str,
    *,
    actor: str,
    action: str,
    values: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> MessageStatus:
    new = ensure_transition(current, target)
    await _apply(session, Message, message_id, str(current), new, values)
    await write_audit(
        session,
        actor,
        action,
        "message",
        message_id,
        {"from": str(current), "to": new.value, **(details or {})},
    )
    return new


async def change_message_status_if_allowed(
    session: AsyncSession,
    message_id: int,
    current: MessageStatus | str,
    target: MessageStatus | str,
    *,
    actor: str,
    action: str,
    values: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> bool:
    """For send results: the outbox outcome must be recorded even if the message
    already moved on (e.g. recovery put it under review); then it is left as is."""
    if not can_transition(current, target):
        await write_audit(
            session,
            actor,
            "message_transition_skipped",
            "message",
            message_id,
            {"from": str(current), "to": str(target), "for": action},
        )
        return False
    await change_message_status(
        session,
        message_id,
        current,
        target,
        actor=actor,
        action=action,
        values=values,
        details=details,
    )
    return True


async def change_send_status(
    session: AsyncSession,
    outbox_id: int,
    current: SendStatus | str,
    target: SendStatus | str,
    *,
    actor: str,
    action: str,
    values: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> SendStatus:
    new = ensure_send_transition(current, target)
    await _apply(session, Outbox, outbox_id, str(current), new, values)
    await write_audit(
        session,
        actor,
        action,
        "outbox",
        outbox_id,
        {"from": str(current), "to": new.value, **(details or {})},
    )
    return new


async def _apply(
    session: AsyncSession,
    model: type[Message] | type[Outbox],
    row_id: int,
    current: str,
    target: MessageStatus | SendStatus,
    values: Mapping[str, Any] | None,
) -> None:
    result = await session.execute(
        update(model)
        .where(model.id == row_id, model.status == current)
        .values(status=target.value, **(values or {}))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RuntimeError(f"{model.__tablename__} {row_id} changed under a held lock")
