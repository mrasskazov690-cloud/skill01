"""Assignment of pending messages: pinned operator first, otherwise round-robin.

Lock order everywhere: conversation -> message -> account. Conversations and
accounts are taken with SKIP LOCKED, so concurrent dispatchers never wait on
each other; a skipped row counts as "busy" and is retried by dispatch_batch.
"""

import asyncio
from typing import NamedTuple

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    exists,
    func,
    insert,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import Account, Assignment, Conversation, Message
from app.db.session import transaction
from app.domain.states import (
    ACTIVE_STATUSES,
    AccountKind,
    AccountState,
    AssignmentReason,
    MessageStatus,
)
from app.services.audit import write_audit
from app.services.transitions import change_message_status

ACTOR = "dispatcher"
PINNED_OPERATOR_UNAVAILABLE = "pinned_operator_unavailable"


def eligible_clause() -> ColumnElement[bool]:
    """SQL condition on Account: may receive new assignments right now."""
    active_count = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.operator_account_id == Account.id,
            Message.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
        .correlate(Account)
        .scalar_subquery()
    )
    return and_(
        Account.kind == AccountKind.OPERATOR.value,
        Account.enabled.is_(True),
        Account.on_shift.is_(True),
        Account.state == AccountState.ACTIVE.value,
        or_(Account.flood_wait_until.is_(None), Account.flood_wait_until <= func.now()),
        or_(Account.max_active.is_(None), active_count < Account.max_active),
    )


async def is_eligible(session: AsyncSession, account_id: int) -> bool:
    return bool(
        await session.scalar(select(exists().where(Account.id == account_id, eligible_clause())))
    )


class _Pass(NamedTuple):
    message_id: int | None
    busy: bool  # dispatchable work was skipped because another transaction held its locks


async def dispatch_one(session: AsyncSession, *, candidate_limit: int | None = None) -> int | None:
    """Assign one pending message in the caller's transaction; None if nothing was assigned."""
    return (await _dispatch_pass(session, candidate_limit)).message_id


async def dispatch_batch(
    session_factory: async_sessionmaker[AsyncSession],
    limit: int,
    *,
    candidate_limit: int | None = None,
    max_busy_retries: int = 50,
) -> list[int]:
    """Run dispatch_one in separate transactions; return the assigned message ids.

    Stops at `limit`, when nothing is pending or no operator is eligible. A pass that
    found only locked work is retried with a short backoff: the lock holders are
    other short transactions, so the work is either done by them or free soon.
    """
    assigned: list[int] = []
    busy_streak = 0
    while len(assigned) < limit:
        async with transaction(session_factory) as session:
            result = await _dispatch_pass(session, candidate_limit)
        if result.message_id is not None:
            assigned.append(result.message_id)
            busy_streak = 0
        elif result.busy and busy_streak < max_busy_retries:
            busy_streak += 1
            await asyncio.sleep(0.002 * busy_streak)
        else:
            break
    return assigned


def _candidates(limit: int) -> Select[int, int]:
    pinned_operator_eligible = exists().where(
        Account.id == Conversation.operator_account_id, eligible_clause()
    )
    return (
        select(Message.id, Message.conversation_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.status == MessageStatus.PENDING.value,
            # Messages already held for an unavailable pinned operator are skipped until
            # that operator is eligible again: otherwise they would hog the candidate
            # window (starving newer messages) and add an audit row on every pass.
            or_(
                Message.review_reason.is_distinct_from(PINNED_OPERATOR_UNAVAILABLE),
                Conversation.operator_account_id.is_(None),
                pinned_operator_eligible,
            ),
        )
        .order_by(Message.received_at, Message.id)
        .limit(limit)
    )


async def _dispatch_pass(session: AsyncSession, candidate_limit: int | None) -> _Pass:
    limit = candidate_limit or get_settings().DISPATCH_CANDIDATE_LIMIT
    candidates = (await session.execute(_candidates(limit))).all()
    busy = False
    skipped: set[int] = set()
    for message_id, conversation_id in candidates:
        if conversation_id in skipped:
            continue
        conversation = (
            await session.execute(
                select(Conversation.operator_account_id)
                .where(Conversation.id == conversation_id)
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if conversation is None:
            busy = True
            skipped.add(conversation_id)
            continue
        status = await session.scalar(
            select(Message.status).where(Message.id == message_id).with_for_update()
        )
        if status != MessageStatus.PENDING:
            continue

        pinned = conversation.operator_account_id
        if pinned is not None:
            locked = await _lock_operator(session, pinned)
            if locked is None:
                busy = True
            elif locked:
                await _assign(session, message_id, conversation_id, pinned, AssignmentReason.PINNED)
                return _Pass(message_id, busy)
            else:
                # Owner's rule: no automatic reassignment; a human moves the conversation.
                await _hold_for_pinned_operator(session, conversation_id, pinned)
            skipped.add(conversation_id)
            continue

        account_id, operators_busy = await _lock_round_robin(session)
        if account_id is not None:
            await _assign(
                session, message_id, conversation_id, account_id, AssignmentReason.AUTO_ROUND_ROBIN
            )
            return _Pass(message_id, busy)
        if not operators_busy:
            return _Pass(None, False)  # no eligible operator at all; messages stay pending
        busy = True
        skipped.add(conversation_id)
    return _Pass(None, busy)


async def _lock_operator(session: AsyncSession, account_id: int) -> bool | None:
    """Lock the account (SKIP LOCKED) and re-check eligibility under the lock.

    None: locked by another transaction; False: not eligible (lock released).
    """
    savepoint = await session.begin_nested()
    locked = await session.scalar(
        select(Account.id).where(Account.id == account_id).with_for_update(skip_locked=True)
    )
    # A fresh statement sees assignments committed after the locking SELECT's snapshot,
    # so max_active cannot be overshot by a concurrent dispatcher.
    if locked is not None and await is_eligible(session, account_id):
        await savepoint.commit()
        return True
    # Releasing the lock (savepoint rollback) before looking at further candidates keeps
    # this transaction from holding an account while it waits on a message row lock.
    await savepoint.rollback()
    return None if locked is None else False


async def _lock_round_robin(session: AsyncSession) -> tuple[int | None, bool]:
    """Pick and lock the least recently assigned eligible operator.

    Returns (account_id, False) on success, (None, True) when eligible operators exist
    but all are locked by other transactions, (None, False) when none is eligible.
    """
    excluded: list[int] = []
    while True:
        savepoint = await session.begin_nested()
        account_id = await session.scalar(
            select(Account.id)
            .where(eligible_clause(), Account.id.not_in(excluded) if excluded else true())
            .order_by(Account.last_assigned_at.asc().nulls_first(), Account.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if account_id is not None and await is_eligible(session, account_id):
            await savepoint.commit()
            return account_id, False
        await savepoint.rollback()
        if account_id is None:
            remaining = exists().where(
                eligible_clause(), Account.id.not_in(excluded) if excluded else true()
            )
            return None, bool(await session.scalar(select(remaining)))
        excluded.append(account_id)  # reached max_active since the locking SELECT's snapshot


async def _assign(
    session: AsyncSession,
    message_id: int,
    conversation_id: int,
    account_id: int,
    reason: AssignmentReason,
) -> None:
    if reason is AssignmentReason.AUTO_ROUND_ROBIN:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(operator_account_id=account_id)
        )
        # clock_timestamp(), not now(): round-robin order must follow assignment time,
        # not the start time of whichever concurrent transaction committed first.
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(last_assigned_at=func.clock_timestamp())
        )
    await change_message_status(
        session,
        message_id,
        MessageStatus.PENDING,
        MessageStatus.ASSIGNED,
        actor=ACTOR,
        action="message_assigned",
        values={"operator_account_id": account_id, "assigned_at": func.now(), "review_reason": None},
        details={
            "conversation_id": conversation_id,
            "operator_account_id": account_id,
            "reason": reason.value,
        },
    )
    await session.execute(
        insert(Assignment).values(
            conversation_id=conversation_id,
            message_id=message_id,
            to_account_id=account_id,
            reason=reason.value,
            actor=ACTOR,
        )
    )


async def _hold_for_pinned_operator(
    session: AsyncSession, conversation_id: int, account_id: int
) -> None:
    """Mark the conversation's pending messages (status stays pending), auditing each once."""
    held = await session.scalars(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == MessageStatus.PENDING.value,
            Message.review_reason.is_distinct_from(PINNED_OPERATOR_UNAVAILABLE),
        )
        .values(review_reason=PINNED_OPERATOR_UNAVAILABLE)
        .returning(Message.id)
        .execution_options(synchronize_session=False)
    )
    for message_id in held.all():
        await write_audit(
            session,
            ACTOR,
            "message_held",
            "message",
            message_id,
            {
                "review_reason": PINNED_OPERATOR_UNAVAILABLE,
                "conversation_id": conversation_id,
                "operator_account_id": account_id,
            },
        )
