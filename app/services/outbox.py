"""Outbox: at most one live send per message, state committed before every API call.

Outbox rows only change while their message row is locked (lock order: conversation
-> message -> outbox -> account), so a message and its live send never disagree.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, Row, func, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import Account, Client, Conversation, Message, Outbox
from app.db.session import transaction
from app.domain.errors import IdempotencyConflict, NotFound, NotOwner, SendInFlight, WrongState
from app.domain.states import (
    LIVE_SEND_STATUSES,
    AccountState,
    MessageStatus,
    OutboxKind,
    SendStatus,
)
from app.logging import get_logger
from app.services.audit import write_audit
from app.services.transitions import (
    change_message_status,
    change_message_status_if_allowed,
    change_send_status,
)
from app.telegram.base import (
    FloodWait,
    PeerFlood,
    PeerRef,
    PermanentSendError,
    SentMessage,
    TelegramAccount,
)

ACTOR = "outbox"
SEND_UNCERTAIN = "send_uncertain"
ACCOUNT_RESTRICTED = "account_restricted"
FLOOD_WAIT_LIMIT = "flood_wait_limit"
# Accounts in these states send nothing; their queued rows wait for a human.
_PAUSED_STATES = frozenset(
    {AccountState.RESTRICTED, AccountState.PAUSED, AccountState.DISCONNECTED}
)
# Strong references to send-result recordings: the event loop keeps only weak ones, and
# a recording outlives an execute_send that is cancelled while waiting for it.
_recordings: set[asyncio.Task[SendStatus]] = set()


def default_idempotency_key(message_id: int) -> str:
    return f"msg:{message_id}:reply"


async def enqueue_reply(
    session: AsyncSession,
    *,
    message_id: int,
    account_id: int,
    kind: OutboxKind | str,
    body: str,
    template_id: int | None = None,
    template_version: int | None = None,
    idempotency_key: str | None = None,
) -> Outbox:
    """Queue the reply to a message that `account_id` has in processing.

    The same key returns the existing row (whatever its status); a second live send
    for the message is refused with SendInFlight.
    """
    kind = OutboxKind(kind)
    key = idempotency_key or default_idempotency_key(message_id)
    message = await _lock_message(session, message_id)
    existing = await session.scalar(
        select(Outbox)
        .where(Outbox.idempotency_key == key)
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if existing.message_id != message_id:
            raise IdempotencyConflict(f"key is used by message {existing.message_id}")
        return existing
    if message.operator_account_id != account_id:
        raise NotOwner(f"message {message_id} is not assigned to account {account_id}")
    live = await session.scalar(
        select(Outbox.id).where(
            Outbox.message_id == message_id,
            Outbox.status.in_([s.value for s in LIVE_SEND_STATUSES]),
        )
    )
    if live is not None:
        raise SendInFlight(f"message {message_id} already has live outbox row {live}")
    if message.status != MessageStatus.PROCESSING:
        raise WrongState(f"message {message_id} is {message.status}, not processing")
    # uq_outbox_live_message remains the database-level guarantee behind the check above.
    outbox_id = await session.scalar(
        pg_insert(Outbox)
        .values(
            message_id=message_id,
            account_id=account_id,
            idempotency_key=key,
            kind=kind.value,
            template_id=template_id,
            template_version=template_version,
            body=body,
            status=SendStatus.QUEUED.value,
        )
        .on_conflict_do_nothing()
        .returning(Outbox.id)
    )
    if outbox_id is None:
        # This message's rows are written under its lock, so only another message's
        # concurrent insert of the same key can conflict here.
        raise IdempotencyConflict("idempotency key was taken concurrently by another message")
    await write_audit(
        session,
        f"account:{account_id}",
        "reply_enqueued",
        "outbox",
        outbox_id,
        {
            "message_id": message_id,
            "kind": kind.value,
            "template_id": template_id,
            "template_version": template_version,
        },
    )
    return await session.get_one(Outbox, outbox_id)


@dataclass(frozen=True, slots=True)
class _SendJob:
    outbox_id: int
    message_id: int
    account_id: int
    attempt: int
    peer: PeerRef
    body: str


async def execute_send(
    session_factory: async_sessionmaker[AsyncSession],
    outbox_id: int,
    adapter: TelegramAccount,
    *,
    now: datetime | None = None,
    max_attempts: int | None = None,
) -> SendStatus | None:
    """Send one queued row through `adapter`, which must be the row's own account.

    Returns the row's new status, or None if nothing was attempted: the row is not
    queued, is not due yet (`not_before`, account flood wait) or its account is paused.
    `now` overrides the database clock for due checks (tests, replays).
    Cancelled during the API call, it records the row as 'uncertain' and re-raises.
    """
    async with transaction(session_factory) as session:
        job = await _begin_send(session, outbox_id, adapter.account_id, now)
    if not isinstance(job, _SendJob):
        return job
    # 'sending' is committed and no transaction is open during the call: if the process
    # dies now, the startup sweep finds the row in 'sending' and never re-sends it blindly.
    outcome: SentMessage | BaseException
    try:
        outcome = await adapter.send_text(job.peer, job.body)
    except (Exception, asyncio.CancelledError) as exc:
        outcome = exc
    # The result is written in its own task, so cancelling this one (again) cannot
    # interrupt it: a cancelled call becomes 'uncertain' now, not at the next start.
    recording = asyncio.create_task(
        _record_result(
            session_factory,
            job,
            outcome,
            now,
            max_attempts or get_settings().DISPATCH_MAX_SEND_ATTEMPTS,
        )
    )
    _recordings.add(recording)
    recording.add_done_callback(_recordings.discard)
    status = await asyncio.shield(recording)
    if isinstance(outcome, asyncio.CancelledError):
        raise outcome
    return status


async def _record_result(
    session_factory: async_sessionmaker[AsyncSession],
    job: _SendJob,
    outcome: SentMessage | BaseException,
    now: datetime | None,
    max_attempts: int,
) -> SendStatus:
    async with transaction(session_factory) as session:
        return await _finish_send(session, job, outcome, now, max_attempts)


async def _begin_send(
    session: AsyncSession, outbox_id: int, account_id: int, now: datetime | None
) -> _SendJob | SendStatus | None:
    message_id = await session.scalar(select(Outbox.message_id).where(Outbox.id == outbox_id))
    if message_id is None:
        raise NotFound(f"outbox {outbox_id}")
    message = await _lock_message(session, message_id)
    row = (
        await session.execute(
            select(
                Outbox.status, Outbox.account_id, Outbox.not_before, Outbox.attempts, Outbox.body
            )
            .where(Outbox.id == outbox_id)
            .with_for_update()
        )
    ).one()
    if row.account_id != account_id:
        # Replies never move to another account (PEER_FLOOD included).
        raise ValueError(f"outbox {outbox_id} belongs to account {row.account_id}")
    if row.status != SendStatus.QUEUED:
        return None
    now = now or await _db_now(session)
    if row.not_before is not None and row.not_before > now:
        return None
    account = (
        await session.execute(
            select(Account.state, Account.flood_wait_until).where(Account.id == account_id)
        )
    ).one()
    if account.state in _PAUSED_STATES:
        return None
    if account.flood_wait_until is not None and account.flood_wait_until > now:
        await session.execute(
            update(Outbox)
            .where(Outbox.id == outbox_id)
            .values(not_before=account.flood_wait_until)
            .execution_options(synchronize_session=False)
        )
        await _extend_lease(session, message_id, account.flood_wait_until)
        return None
    if message.status != MessageStatus.PROCESSING or message.operator_account_id != account_id:
        await change_send_status(
            session,
            outbox_id,
            SendStatus.QUEUED,
            SendStatus.FAILED,
            actor=ACTOR,
            action="send_cancelled",
            values={"error": "message_not_processing"},
            details={"message_id": message_id, "message_status": message.status},
        )
        return SendStatus.FAILED
    attempt = row.attempts + 1
    await change_send_status(
        session,
        outbox_id,
        SendStatus.QUEUED,
        SendStatus.SENDING,
        actor=ACTOR,
        action="send_started",
        values={"attempts": attempt},
        details={"message_id": message_id, "account_id": account_id, "attempt": attempt},
    )
    return _SendJob(
        outbox_id=outbox_id,
        message_id=message_id,
        account_id=account_id,
        attempt=attempt,
        peer=await _peer(session, message.conversation_id),
        body=row.body,
    )


async def _finish_send(
    session: AsyncSession,
    job: _SendJob,
    outcome: SentMessage | BaseException,
    now: datetime | None,
    max_attempts: int,
) -> SendStatus:
    message = await _lock_message(session, job.message_id)
    status = await _lock_outbox_status(session, job.outbox_id)
    now = now or await _db_now(session)
    await _apply_account_effect(session, job.account_id, outcome, now)
    code = None if isinstance(outcome, SentMessage) else error_code(outcome)
    if code is not None and not isinstance(outcome, FloodWait | PeerFlood | PermanentSendError):
        get_logger(__name__).warning("send_result_uncertain", outbox_id=job.outbox_id, error=code)

    if status != SendStatus.SENDING:
        # The startup sweep already marked the row uncertain; only a confirmed delivery
        # adds information, everything else stays with the human reviewing it.
        if isinstance(outcome, SentMessage) and status == SendStatus.UNCERTAIN:
            await _mark_sent(
                session, job.outbox_id, job.message_id, status, message.status, outcome, ACTOR
            )
            return SendStatus.SENT
        await write_audit(
            session,
            ACTOR,
            "send_result_ignored",
            "outbox",
            job.outbox_id,
            {"status": status.value, "result": code or "sent"},
        )
        return status

    async def to_message(target: MessageStatus, reason: str | None) -> None:
        await change_message_status_if_allowed(
            session,
            job.message_id,
            message.status,
            target,
            actor=ACTOR,
            action="message_send_result",
            values={"review_reason": reason, "claimed_by": None, "lease_expires_at": None},
            details={"outbox_id": job.outbox_id, "result": code},
        )

    async def to_outbox(target: SendStatus, **values: object) -> None:
        await change_send_status(
            session,
            job.outbox_id,
            SendStatus.SENDING,
            target,
            actor=ACTOR,
            action="send_result",
            values={"error": code, **values},
            details={"message_id": job.message_id, "attempt": job.attempt, "result": code},
        )

    match outcome:
        case SentMessage():
            await _mark_sent(
                session, job.outbox_id, job.message_id, status, message.status, outcome, ACTOR
            )
            return SendStatus.SENT
        case FloodWait() if job.attempt < max_attempts:
            not_before = now + timedelta(seconds=max(outcome.seconds, 0))
            await to_outbox(SendStatus.QUEUED, not_before=not_before)
            await _extend_lease(session, job.message_id, not_before)
            return SendStatus.QUEUED
        case FloodWait():
            await to_outbox(SendStatus.FAILED)
            await to_message(MessageStatus.AWAITING_REVIEW, FLOOD_WAIT_LIMIT)
            return SendStatus.FAILED
        case PeerFlood():
            # The account is paused (see _apply_account_effect); nothing is re-routed.
            await to_outbox(SendStatus.FAILED)
            await to_message(MessageStatus.AWAITING_REVIEW, ACCOUNT_RESTRICTED)
            return SendStatus.FAILED
        case PermanentSendError():
            await to_outbox(SendStatus.FAILED)
            await to_message(MessageStatus.FAILED, code)
            return SendStatus.FAILED
        case _:
            # SendTimeout, a cancelled call or anything unexpected: delivery is unknown.
            # Never retried automatically; a human checks the chat (resolve_uncertain()).
            await to_outbox(SendStatus.UNCERTAIN)
            await to_message(MessageStatus.AWAITING_REVIEW, SEND_UNCERTAIN)
            return SendStatus.UNCERTAIN


async def resolve_uncertain(
    session: AsyncSession,
    outbox_id: int,
    outcome: Literal["sent", "not_sent"],
    actor: str,
) -> None:
    """Record an operator's check of an uncertain send.

    `sent`: the reply is in the chat, the message is answered. `not_sent`: the row
    fails and the message returns to its operator; a new send needs a new key.
    """
    if outcome not in ("sent", "not_sent"):
        raise ValueError("outcome must be 'sent' or 'not_sent'")
    message_id = await session.scalar(select(Outbox.message_id).where(Outbox.id == outbox_id))
    if message_id is None:
        raise NotFound(f"outbox {outbox_id}")
    message = await _lock_message(session, message_id)
    status = await _lock_outbox_status(session, outbox_id)
    if status != SendStatus.UNCERTAIN:
        raise WrongState(f"outbox {outbox_id} is {status}, not uncertain")
    if outcome == "sent":
        await _mark_sent(
            session,
            outbox_id,
            message_id,
            SendStatus.UNCERTAIN,
            message.status,
            None,
            actor,
            strict=True,
        )
        return
    await change_send_status(
        session,
        outbox_id,
        SendStatus.UNCERTAIN,
        SendStatus.FAILED,
        actor=actor,
        action="send_resolved",
        values={"error": "not_sent"},
        details={"message_id": message_id, "outcome": outcome},
    )
    await change_message_status(
        session,
        message_id,
        message.status,
        MessageStatus.ASSIGNED,
        actor=actor,
        action="message_send_resolved",
        values={"review_reason": None, "claimed_by": None, "lease_expires_at": None},
        details={"outbox_id": outbox_id, "outcome": outcome},
    )


async def recover_interrupted_sends(session: AsyncSession) -> int:
    """Startup sweep: 'sending' means the process died between the two transactions of
    execute_send, so the reply may have been delivered. Such rows become 'uncertain'
    for manual review. Run before any send worker starts."""
    rows = (
        await session.execute(
            select(Outbox.id, Outbox.message_id, Message.conversation_id)
            .join(Message, Message.id == Outbox.message_id)
            .where(Outbox.status == SendStatus.SENDING.value)
            .order_by(Message.conversation_id, Outbox.message_id)
        )
    ).all()
    recovered = 0
    for outbox_id, message_id, conversation_id in rows:
        # Lock order conversation -> message -> outbox, as in reassignment, which locks
        # a conversation's messages in id order: otherwise the two can deadlock.
        await session.execute(
            select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
        )
        message = await _lock_message(session, message_id)
        status = await _lock_outbox_status(session, outbox_id)
        if status != SendStatus.SENDING:
            continue
        await change_send_status(
            session,
            outbox_id,
            SendStatus.SENDING,
            SendStatus.UNCERTAIN,
            actor="system:startup",
            action="send_interrupted",
            values={"error": "interrupted"},
            details={"message_id": message_id},
        )
        await change_message_status_if_allowed(
            session,
            message_id,
            message.status,
            MessageStatus.AWAITING_REVIEW,
            actor="system:startup",
            action="message_send_result",
            values={"review_reason": SEND_UNCERTAIN, "claimed_by": None, "lease_expires_at": None},
            details={"outbox_id": outbox_id, "result": "interrupted"},
        )
        recovered += 1
    return recovered


async def expire_flood_waits(session: AsyncSession, now: datetime | None = None) -> int:
    """Return accounts whose flood wait is over to 'active' (eligible for dispatch again)."""
    now = now or await _db_now(session)
    expired = await session.scalars(
        update(Account)
        .where(Account.state == AccountState.FLOOD_WAIT.value, Account.flood_wait_until <= now)
        .values(state=AccountState.ACTIVE.value, flood_wait_until=None)
        .returning(Account.id)
        .execution_options(synchronize_session=False)
    )
    ids = expired.all()
    for account_id in ids:
        await _audit_account(session, account_id, AccountState.ACTIVE, "flood_wait_expired")
    return len(ids)


def error_code(exc: BaseException) -> str:
    """Stable snake_case code for an adapter error (class name only, never its message)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()


async def _mark_sent(
    session: AsyncSession,
    outbox_id: int,
    message_id: int,
    current: SendStatus,
    message_status: str,
    sent: SentMessage | None,
    actor: str,
    *,
    strict: bool = False,
) -> None:
    await change_send_status(
        session,
        outbox_id,
        current,
        SendStatus.SENT,
        actor=actor,
        action="send_result" if actor == ACTOR else "send_resolved",
        values={
            "tg_sent_message_id": sent.tg_message_id if sent else None,
            "sent_at": func.now(),
            "error": None,
        },
        details={"message_id": message_id, "result": "sent"},
    )
    template = (
        await session.execute(
            select(Outbox.template_id, Outbox.template_version).where(Outbox.id == outbox_id)
        )
    ).one()
    change = change_message_status if strict else change_message_status_if_allowed
    await change(
        session,
        message_id,
        message_status,
        MessageStatus.ANSWERED,
        actor=actor,
        action="message_answered",
        values={
            "answered_at": func.now(),
            "template_id": template.template_id,
            "template_version": template.template_version,
            "review_reason": None,
            "claimed_by": None,
            "lease_expires_at": None,
        },
        details={"outbox_id": outbox_id},
    )


async def _extend_lease(session: AsyncSession, message_id: int, retry_at: datetime) -> None:
    """Keep the claim alive until a deferred send is due, so recover_stale does not
    cancel a reply that is only waiting out a flood limit."""
    lease_end = retry_at + timedelta(seconds=get_settings().DISPATCH_LEASE_SECONDS)
    await session.execute(
        update(Message)
        .where(Message.id == message_id, Message.status == MessageStatus.PROCESSING)
        .values(lease_expires_at=func.greatest(Message.lease_expires_at, lease_end))
        .execution_options(synchronize_session=False)
    )


async def _apply_account_effect(
    session: AsyncSession, account_id: int, outcome: SentMessage | BaseException, now: datetime
) -> None:
    """Account state follows what the API said, whatever happened to the row meanwhile."""
    if isinstance(outcome, FloodWait):
        until = now + timedelta(seconds=max(outcome.seconds, 0))
        await _set_account_state(
            session,
            account_id,
            AccountState.FLOOD_WAIT,
            reason="flood_wait",
            from_states=(AccountState.ACTIVE, AccountState.FLOOD_WAIT),
            flood_wait_until=func.greatest(func.coalesce(Account.flood_wait_until, until), until),
        )
    elif isinstance(outcome, PeerFlood):
        await _set_account_state(
            session,
            account_id,
            AccountState.RESTRICTED,
            reason="peer_flood",
            from_states=tuple(s for s in AccountState if s is not AccountState.RESTRICTED),
        )
    elif isinstance(outcome, SentMessage):
        await _set_account_state(
            session,
            account_id,
            AccountState.ACTIVE,
            reason="flood_wait_expired",
            from_states=(AccountState.FLOOD_WAIT,),
            extra_where=Account.flood_wait_until <= now,
            flood_wait_until=None,
        )


async def _set_account_state(
    session: AsyncSession,
    account_id: int,
    state: AccountState,
    *,
    reason: str,
    from_states: tuple[AccountState, ...],
    extra_where: ColumnElement[bool] | None = None,
    **values: object,
) -> None:
    changed = await session.scalar(
        update(Account)
        .where(
            Account.id == account_id,
            Account.state.in_([s.value for s in from_states]),
            true() if extra_where is None else extra_where,
        )
        .values(state=state.value, **values)
        .returning(Account.id)
        .execution_options(synchronize_session=False)
    )
    if changed is not None:
        await _audit_account(session, account_id, state, reason)


async def _audit_account(
    session: AsyncSession, account_id: int, state: AccountState, reason: str
) -> None:
    await write_audit(
        session,
        ACTOR,
        "account_state_changed",
        "account",
        account_id,
        {"state": state.value, "reason": reason},
    )


async def _lock_message(session: AsyncSession, message_id: int) -> Row[str, int | None, int]:
    message = (
        await session.execute(
            select(Message.status, Message.operator_account_id, Message.conversation_id)
            .where(Message.id == message_id)
            .with_for_update()
        )
    ).one_or_none()
    if message is None:
        raise NotFound(f"message {message_id}")
    return message


async def _lock_outbox_status(session: AsyncSession, outbox_id: int) -> SendStatus:
    status = await session.execute(
        select(Outbox.status).where(Outbox.id == outbox_id).with_for_update()
    )
    return SendStatus(status.scalar_one())


async def _peer(session: AsyncSession, conversation_id: int) -> PeerRef:
    client = (
        await session.execute(
            select(Client.tg_user_id, Client.username)
            .join(Conversation, Conversation.client_id == Client.id)
            .where(Conversation.id == conversation_id)
        )
    ).one()
    # Never a bare username: it may belong to someone else by now. The adapter checks
    # that whatever it resolves is this tg_user_id.
    return PeerRef(tg_user_id=client.tg_user_id, username=client.username)


async def _db_now(session: AsyncSession) -> datetime:
    return (await session.execute(select(func.now()))).scalar_one()
