"""Manual assignment: the only way a pinned conversation moves to another operator."""

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Assignment, Conversation, Message, Outbox
from app.domain.errors import AccountNotAssignable, ConversationBusy, NotFound
from app.domain.states import (
    LIVE_SEND_STATUSES,
    TERMINAL_STATUSES,
    AccountKind,
    AssignmentReason,
    InvalidTransition,
    MessageStatus,
)
from app.services.audit import write_audit
from app.services.dispatcher import PINNED_OPERATOR_UNAVAILABLE
from app.services.transitions import change_message_status


async def assign_manual(
    session: AsyncSession, message_id: int, account_id: int, actor: str
) -> list[int]:
    """Assign one message (pending, assigned or awaiting_review) to `account_id`.

    The conversation is pinned to that account too, and its other `assigned`
    messages follow. Returns the ids of the messages that changed operator.
    """
    conversation_id = await session.scalar(
        select(Message.conversation_id).where(Message.id == message_id)
    )
    if conversation_id is None:
        raise NotFound(f"message {message_id}")
    return await _move(session, conversation_id, account_id, actor, message_id)


async def reassign_conversation(
    session: AsyncSession, conversation_id: int, to_account_id: int, actor: str
) -> list[int]:
    """Pin the conversation to `to_account_id` and move its `assigned` messages.

    Pending messages stay pending and reach the new operator through the dispatcher.
    """
    return await _move(session, conversation_id, to_account_id, actor, None)


async def _move(
    session: AsyncSession,
    conversation_id: int,
    account_id: int,
    actor: str,
    target_id: int | None,
) -> list[int]:
    await _ensure_assignable(session, account_id)
    # Lock order: conversation -> messages. Outbox rows only change under their
    # message's lock, so the live-send check below is stable while we hold them.
    conversation = (
        await session.execute(
            select(Conversation.operator_account_id)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).one_or_none()
    if conversation is None:
        raise NotFound(f"conversation {conversation_id}")
    previous = conversation.operator_account_id
    messages = {
        m.id: m
        for m in (
            await session.execute(
                select(Message.id, Message.status, Message.operator_account_id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.status.not_in([s.value for s in TERMINAL_STATUSES]),
                )
                .order_by(Message.id)
                .with_for_update()
            )
        ).all()
    }
    target = messages.get(target_id) if target_id is not None else None
    if target_id is not None and target is None:  # answered or cancelled
        status = await session.scalar(select(Message.status).where(Message.id == target_id))
        raise InvalidTransition(status, MessageStatus.ASSIGNED)

    pin_changes = previous != account_id
    live = set(
        await session.scalars(
            select(Outbox.message_id).where(
                Outbox.message_id.in_(list(messages)),
                Outbox.status.in_([s.value for s in LIVE_SEND_STATUSES]),
            )
        )
    )
    # A message in processing or with a live send keeps its operator, so the
    # conversation cannot move away from it without splitting it across operators.
    affected = messages.values() if pin_changes else [target] if target else []
    busy = sorted(m.id for m in affected if m.status == MessageStatus.PROCESSING or m.id in live)
    if busy:
        raise ConversationBusy(f"conversation {conversation_id}: messages {busy} are in work")

    if pin_changes:
        pin_reason = AssignmentReason.MANUAL_ASSIGN if target else AssignmentReason.MANUAL_REASSIGN
        await _pin(session, conversation_id, previous, account_id, pin_reason, actor)

    moved: list[int] = []
    for message in messages.values():
        if message is target:
            if message.status == MessageStatus.ASSIGNED and message.operator_account_id == account_id:
                continue
            reason = AssignmentReason.MANUAL_ASSIGN
        elif pin_changes and message.status == MessageStatus.ASSIGNED:
            reason = AssignmentReason.MANUAL_REASSIGN
        else:
            continue
        await _assign(session, conversation_id, message, account_id, reason, actor)
        moved.append(message.id)
    return moved


async def _ensure_assignable(session: AsyncSession, account_id: int) -> None:
    account = (
        await session.execute(
            select(Account.kind, Account.enabled).where(Account.id == account_id)
        )
    ).one_or_none()
    if account is None:
        raise NotFound(f"account {account_id}")
    # on_shift and load limits are not checked: a human decided this explicitly.
    if account.kind != AccountKind.OPERATOR or not account.enabled:
        raise AccountNotAssignable(f"account {account_id} is not an enabled operator")


async def _pin(
    session: AsyncSession,
    conversation_id: int,
    previous: int | None,
    account_id: int,
    reason: AssignmentReason,
    actor: str,
) -> None:
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(operator_account_id=account_id)
    )
    # Held messages waited for the previous operator; let the dispatcher re-evaluate them.
    await session.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == MessageStatus.PENDING.value,
            Message.review_reason == PINNED_OPERATOR_UNAVAILABLE,
        )
        .values(review_reason=None)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        insert(Assignment).values(
            conversation_id=conversation_id,
            from_account_id=previous,
            to_account_id=account_id,
            reason=reason.value,
            actor=actor,
        )
    )
    await write_audit(
        session,
        actor,
        "conversation_pinned",
        "conversation",
        conversation_id,
        {"from_account_id": previous, "to_account_id": account_id, "reason": reason.value},
    )


async def _assign(
    session: AsyncSession,
    conversation_id: int,
    message: Row[int, str, int | None],
    account_id: int,
    reason: AssignmentReason,
    actor: str,
) -> None:
    await change_message_status(
        session,
        message.id,
        message.status,
        MessageStatus.ASSIGNED,
        actor=actor,
        action="message_assigned",
        values={
            "operator_account_id": account_id,
            "assigned_at": func.now(),
            "review_reason": None,
            "claimed_by": None,
            "lease_expires_at": None,
        },
        details={
            "conversation_id": conversation_id,
            "from_account_id": message.operator_account_id,
            "operator_account_id": account_id,
            "reason": reason.value,
        },
    )
    await session.execute(
        insert(Assignment).values(
            conversation_id=conversation_id,
            message_id=message.id,
            from_account_id=message.operator_account_id,
            to_account_id=account_id,
            reason=reason.value,
            actor=actor,
        )
    )
