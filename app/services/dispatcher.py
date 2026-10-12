"""Assignment of pending messages: pinned operator first, otherwise round-robin.

Lock order everywhere: conversation -> message -> account. Conversations and
accounts are taken with SKIP LOCKED, so concurrent dispatchers never wait on
each other; a skipped row counts as "busy" and is retried by dispatch_batch.
An account is locked only when something about it can race: its round-robin
position (last_sent_at, last_assigned_at) or its max_active load. It is locked FOR NO KEY UPDATE:
rows that merely reference the account (assignments, messages, outbox) hold a FK
KEY SHARE lock on it, which must not make the operator look busy.
"""

import asyncio
from typing import NamedTuple

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    bindparam,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import Account, Assignment, Conversation, Message, Outbox
from app.db.session import transaction
from app.domain.states import (
    ACTIVE_STATUSES,
    AccountKind,
    AccountState,
    AssignmentReason,
    MessageStatus,
    SendStatus,
)
from app.services.audit import write_audit
from app.services.transitions import change_message_status

ACTOR = "dispatcher"
PINNED_OPERATOR_UNAVAILABLE = "pinned_operator_unavailable"


_active_count = (
    select(func.count())
    .select_from(Message)
    .where(
        Message.operator_account_id == Account.id,
        Message.status.in_([s.value for s in ACTIVE_STATUSES]),
    )
    .correlate(Account)
    .scalar_subquery()
)
# Values read from the settings at execution time, so the statements below stay constant.
_TIMEZONE = bindparam("timezone", callable_=lambda: get_settings().TIMEZONE)
_DAILY_LIMIT = bindparam(
    "account_daily_limit", callable_=lambda: get_settings().ACCOUNT_DAILY_LIMIT
)
# Midnight of the current day in TIMEZONE, as timestamptz.
_DAY_START = func.timezone(_TIMEZONE, func.date_trunc("day", func.timezone(_TIMEZONE, func.now())))
_sent_today = (
    select(func.count())
    .select_from(Outbox)
    .where(
        Outbox.account_id == Account.id,
        Outbox.status == SendStatus.SENT.value,
        Outbox.sent_at >= _DAY_START,
    )
    .correlate(Account)
    .scalar_subquery()
)
# Public names for reports (the owner's /status): replies sent today, the daily limit.
SENT_TODAY = _sent_today
DAILY_LIMIT: ColumnElement[int] = func.coalesce(Account.daily_send_limit, _DAILY_LIMIT)
# SQL condition on Account: may send right now (switched on, on shift, not limited by
# Telegram, under its daily limit). Checked again at send time, because a reply may be
# queued or left assigned long after its assignment (approved drafts, resumed leads).
# Statements are immutable, so they are built once here instead of on every pass.
CAN_SEND: ColumnElement[bool] = and_(
    Account.enabled.is_(True),
    Account.on_shift.is_(True),
    Account.state == AccountState.ACTIVE.value,
    or_(Account.flood_wait_until.is_(None), Account.flood_wait_until <= func.now()),
    _sent_today < DAILY_LIMIT,
)
# SQL condition on Account: may receive new assignments right now.
ELIGIBLE: ColumnElement[bool] = and_(
    Account.kind == AccountKind.OPERATOR.value,
    CAN_SEND,
    or_(Account.max_active.is_(None), _active_count < Account.max_active),
)


def _candidates(newest_first: bool) -> Select[tuple[int, int]]:
    order = (
        (Message.received_at.desc(), Message.id.desc())
        if newest_first
        else (Message.received_at, Message.id)
    )
    pending = (
        select(Message.id, Message.conversation_id, Message.received_at)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.status == MessageStatus.PENDING.value,
            # Messages already held for an unavailable pinned operator are skipped until
            # that operator is eligible again: otherwise they would hog the candidate
            # window (starving newer messages) and add an audit row on every pass.
            or_(
                Message.review_reason.is_distinct_from(PINNED_OPERATOR_UNAVAILABLE),
                Conversation.operator_account_id.is_(None),
                exists().where(Account.id == Conversation.operator_account_id, ELIGIBLE),
            ),
        )
        # One candidate per conversation, the first in dispatch order: a conversation
        # that is locked or has a long backlog takes one slot of the window, not all.
        .ext(distinct_on(Message.conversation_id))
        .order_by(Message.conversation_id, *order)
        .subquery()
    )
    outer = (
        (pending.c.received_at.desc(), pending.c.id.desc())
        if newest_first
        else (pending.c.received_at, pending.c.id)
    )
    return select(pending.c.id, pending.c.conversation_id).order_by(*outer)


_CANDIDATES = {"newest_first": _candidates(True), "oldest_first": _candidates(False)}

# Senders alternate: the account that sent least recently goes first. Among accounts
# with the same last_sent_at (none sent yet) the least recently assigned one does.
_ROUND_ROBIN_ORDER = (
    select(Account.id, Account.last_sent_at, Account.last_assigned_at)
    .where(ELIGIBLE)
    .order_by(
        Account.last_sent_at.asc().nulls_first(),
        Account.last_assigned_at.asc().nulls_first(),
        Account.id,
    )
)


async def is_eligible(session: AsyncSession, account_id: int) -> bool:
    return bool(await session.scalar(select(exists().where(Account.id == account_id, ELIGIBLE))))


class _Pass(NamedTuple):
    message_id: int | None
    busy: bool = False  # dispatchable work was skipped: another transaction held its locks
    held: bool = False  # messages were newly held for an unavailable pinned operator


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
        elif result.held:
            continue  # held messages leave the candidate window; newer ones may be dispatchable
        elif result.busy and busy_streak < max_busy_retries:
            busy_streak += 1
            await asyncio.sleep(0.002 * busy_streak)
        else:
            break
    return assigned


async def _dispatch_pass(session: AsyncSession, candidate_limit: int | None) -> _Pass:
    limit = candidate_limit or get_settings().DISPATCH_CANDIDATE_LIMIT
    candidates_stmt = _CANDIDATES[get_settings().DISPATCH_ORDER]
    candidates = (await session.execute(candidates_stmt.limit(limit))).all()
    busy = held = operators_busy = False
    for message_id, conversation_id in candidates:
        conversation = (
            await session.execute(
                select(Conversation.operator_account_id)
                .where(Conversation.id == conversation_id)
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if conversation is None:
            busy = True
            continue
        status = await session.scalar(
            select(Message.status).where(Message.id == message_id).with_for_update()
        )
        if status != MessageStatus.PENDING:
            continue

        pinned = conversation.operator_account_id
        if pinned is not None:
            locked = await _reserve_pinned_operator(session, pinned)
            if locked is None:
                busy = True
            elif locked:
                await _assign(session, message_id, conversation_id, pinned, AssignmentReason.PINNED)
                return _Pass(message_id)
            else:
                # Owner's rule: no automatic reassignment; a human moves the conversation.
                held |= await _hold_for_pinned_operator(session, conversation_id, pinned)
            continue

        if operators_busy:  # all eligible operators were locked a moment ago; next pass
            busy = True
            continue
        account_id, operators_busy = await _lock_round_robin(session)
        if account_id is not None:
            await _assign(
                session, message_id, conversation_id, account_id, AssignmentReason.AUTO_ROUND_ROBIN
            )
            return _Pass(message_id)
        if not operators_busy:
            return _Pass(None, held=held)  # no eligible operator at all; messages stay pending
        busy = True
    return _Pass(None, busy, held)


async def _reserve_pinned_operator(session: AsyncSession, account_id: int) -> bool | None:
    """True: assign to it; False: not eligible; None: locked by another transaction."""
    account = (
        await session.execute(
            select(Account.max_active, ELIGIBLE.label("eligible")).where(Account.id == account_id)
        )
    ).one()
    if not account.eligible:
        return False
    if account.max_active is None:
        # No load limit to protect and the round-robin position does not change, so no
        # lock: holding it would make concurrent round-robin picks skip this operator.
        return True
    return await _lock_operator(session, account_id)


async def _lock_operator(session: AsyncSession, account_id: int) -> bool | None:
    """Lock the account (SKIP LOCKED) and re-check eligibility under the lock.

    None: locked by another transaction; False: not eligible (lock released).
    """
    savepoint = await session.begin_nested()
    locked = await session.scalar(
        select(Account.id)
        .where(Account.id == account_id)
        .with_for_update(skip_locked=True, key_share=True)  # FOR NO KEY UPDATE
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
    """Pick and lock the eligible operator whose turn it is (see _ROUND_ROBIN_ORDER).

    Returns (account_id, False) on success, (None, True) when eligible operators exist
    but all are locked by other transactions, (None, False) when none is eligible.
    """
    # Not one `ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED`: under READ COMMITTED that
    # sorts by the statement snapshot and may lock a row whose position was just moved
    # by a concurrent commit, giving one operator two turns in a row.
    # Instead walk the order and lock a row only if its position is still the one seen.
    order = (await session.execute(_ROUND_ROBIN_ORDER)).all()
    busy = False
    for account_id, seen_sent, seen_assigned in order:
        savepoint = await session.begin_nested()
        locked = await session.scalar(
            select(Account.id)
            .where(
                Account.id == account_id,
                Account.last_sent_at.is_not_distinct_from(seen_sent),
                Account.last_assigned_at.is_not_distinct_from(seen_assigned),
            )
            .with_for_update(skip_locked=True, key_share=True)  # FOR NO KEY UPDATE
        )
        # Fresh statement under the lock: sees assignments committed meanwhile (max_active).
        if locked is not None and await is_eligible(session, account_id):
            await savepoint.commit()
            return account_id, False
        await savepoint.rollback()
        busy |= locked is None  # in use by another dispatcher, or just took a turn
    return None, busy


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
        values={
            "operator_account_id": account_id,
            "assigned_at": func.now(),
            "review_reason": None,
        },
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
) -> bool:
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
    message_ids = held.all()
    for message_id in message_ids:
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
    return bool(message_ids)
