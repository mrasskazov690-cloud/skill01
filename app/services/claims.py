"""Leases on messages in work: claim (assigned -> processing), release, recovery."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import Row, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import DateTime

from app.config import get_settings
from app.db.models import Message, Outbox
from app.domain.errors import NotFound, NotOwner, SendInFlight, WrongState
from app.domain.states import LIVE_SEND_STATUSES, MessageStatus, SendStatus
from app.services.transitions import change_message_status, change_send_status

SEND_IN_FLIGHT_ON_RECOVERY = "send_in_flight_on_recovery"
_RECOVERY_ACTOR = "system:recover_stale"
_RELEASED = {"claimed_by": None, "lease_expires_at": None}


async def claim_message(
    session: AsyncSession,
    message_id: int,
    account_id: int,
    worker: str,
    lease_seconds: int | None = None,
) -> datetime:
    """Take an assigned message into work for its operator; returns the lease expiry."""
    lease = lease_seconds or get_settings().DISPATCH_LEASE_SECONDS
    message = (
        await session.execute(
            select(Message.status, Message.operator_account_id)
            .where(Message.id == message_id)
            .with_for_update()
        )
    ).one_or_none()
    if message is None:
        raise NotFound(f"message {message_id}")
    if message.operator_account_id != account_id:
        raise NotOwner(f"message {message_id} is not assigned to account {account_id}")
    await change_message_status(
        session,
        message_id,
        message.status,
        MessageStatus.PROCESSING,
        actor=f"worker:{worker}",
        action="message_claimed",
        values={"claimed_by": worker, "lease_expires_at": func.now() + timedelta(seconds=lease)},
        details={"account_id": account_id, "lease_seconds": lease},
    )
    expires = await session.execute(
        select(Message.lease_expires_at).where(Message.id == message_id)
    )
    return expires.scalar_one()  # type: ignore[return-value]  # set just above


async def release(session: AsyncSession, message_id: int, worker: str) -> None:
    """Give a claimed message back (processing -> assigned) without answering it."""
    message = (
        await session.execute(
            select(Message.status, Message.claimed_by)
            .where(Message.id == message_id)
            .with_for_update()
        )
    ).one_or_none()
    if message is None:
        raise NotFound(f"message {message_id}")
    if message.status != MessageStatus.PROCESSING:
        raise WrongState(f"message {message_id} is {message.status}, not processing")
    if message.claimed_by != worker:
        raise NotOwner(f"message {message_id} is claimed by another worker")
    if await _live_sends(session, message_id):
        raise SendInFlight(f"message {message_id} has a live send")
    await change_message_status(
        session,
        message_id,
        MessageStatus.PROCESSING,
        MessageStatus.ASSIGNED,
        actor=f"worker:{worker}",
        action="message_released",
        values=_RELEASED,
    )


async def recover_stale(session: AsyncSession, now: datetime | None = None) -> int:
    """Return processing messages with an expired lease to their operator's queue.

    A message whose reply may already be on its way (outbox sending/uncertain) goes to
    awaiting_review instead: putting it back in work could produce a second reply.
    A reply still queued was never sent, so it is cancelled with the lease.
    """
    cutoff = literal(now, DateTime(timezone=True)) if now is not None else func.now()
    stale = (
        await session.scalars(
            select(Message.id)
            .where(
                Message.status == MessageStatus.PROCESSING.value,
                Message.lease_expires_at < cutoff,
            )
            .order_by(Message.id)
            # Rows locked right now are being worked on (e.g. a send is being recorded).
            .with_for_update(skip_locked=True)
        )
    ).all()
    for message_id in stale:
        sends = await _live_sends(session, message_id)
        in_flight = [s for s in sends if s.status != SendStatus.QUEUED]
        if in_flight:
            await change_message_status(
                session,
                message_id,
                MessageStatus.PROCESSING,
                MessageStatus.AWAITING_REVIEW,
                actor=_RECOVERY_ACTOR,
                action="message_recovered",
                values={**_RELEASED, "review_reason": SEND_IN_FLIGHT_ON_RECOVERY},
                details={"outbox_ids": [s.id for s in in_flight]},
            )
            continue
        for send in sends:
            await change_send_status(
                session,
                send.id,
                SendStatus.QUEUED,
                SendStatus.FAILED,
                actor=_RECOVERY_ACTOR,
                action="send_cancelled",
                values={"error": "lease_expired"},
                details={"message_id": message_id},
            )
        await change_message_status(
            session,
            message_id,
            MessageStatus.PROCESSING,
            MessageStatus.ASSIGNED,
            actor=_RECOVERY_ACTOR,
            action="message_recovered",
            values=_RELEASED,
        )
    return len(stale)


async def _live_sends(session: AsyncSession, message_id: int) -> Sequence[Row[int, str]]:
    # Lock order: the message is already locked by the caller; outbox comes after it.
    return (
        await session.execute(
            select(Outbox.id, Outbox.status)
            .where(
                Outbox.message_id == message_id,
                Outbox.status.in_([s.value for s in LIVE_SEND_STATUSES]),
            )
            .with_for_update()
        )
    ).all()
