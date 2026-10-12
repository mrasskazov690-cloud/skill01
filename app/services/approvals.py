"""Owner approval of replies: drafts, confirm-first-N, auto mode and pause.

The first CONFIRM_FIRST_N replies are sent only after the owner approves their
drafts; auto mode is then switched on only by an explicit command. Audit records
carry ids, statuses and issue codes, never the draft body or the client's text.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import Row, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AppSetting, Draft, Message, Outbox
from app.domain.errors import AutoModeLocked, NotFound, NotOwner, SendInFlight, WrongState
from app.domain.states import (
    LIVE_SEND_STATUSES,
    DraftSource,
    DraftStatus,
    MessageStatus,
    OutboxKind,
    SendStatus,
)
from app.services.audit import write_audit
from app.services.claims import claim_message
from app.services.guard import check_draft
from app.services.outbox import PAUSED_SETTING, enqueue_reply
from app.services.transitions import (
    change_message_status,
    change_message_status_if_allowed,
    change_send_status,
)

if TYPE_CHECKING:
    from app.drafts.profile import ServiceProfile

NEEDS_APPROVAL = "needs_approval"
REJECTED_BY_OWNER = "rejected_by_owner"
AUTO_MODE = "auto_mode"
PAUSED = PAUSED_SETTING
STALE_APPROVAL = "stale_approval"
_RELEASED = {"claimed_by": None, "lease_expires_at": None}


async def create_draft(
    session: AsyncSession,
    *,
    message_id: int,
    account_id: int,
    body: str,
    source: DraftSource | str,
    issues: Sequence[str] | None = None,
) -> Draft:
    """Park a claimed message (processing) for the owner: awaiting_review('needs_approval')
    with a pending draft. `issues` defaults to the guard's findings for the body."""
    source = DraftSource(source)
    message = await _lock_message(session, message_id)
    if message.operator_account_id != account_id:
        raise NotOwner(f"message {message_id} is not assigned to account {account_id}")
    if message.status != MessageStatus.PROCESSING:
        raise WrongState(f"message {message_id} is {message.status}, not processing")
    live = await session.scalar(
        select(Outbox.id).where(
            Outbox.message_id == message_id,
            Outbox.status.in_([s.value for s in LIVE_SEND_STATUSES]),
        )
    )
    if live is not None:
        raise SendInFlight(f"message {message_id} already has live outbox row {live}")
    found = list(check_draft(body, source) if issues is None else issues)
    draft = Draft(
        message_id=message_id,
        account_id=account_id,
        body=body,
        source=source.value,
        status=DraftStatus.PENDING.value,
        issues=found,
    )
    session.add(draft)
    await session.flush()
    await change_message_status(
        session,
        message_id,
        MessageStatus.PROCESSING,
        MessageStatus.AWAITING_REVIEW,
        actor=f"account:{account_id}",
        action="message_needs_approval",
        values={**_RELEASED, "review_reason": NEEDS_APPROVAL},
        details={"draft_id": draft.id},
    )
    await write_audit(
        session,
        f"account:{account_id}",
        "draft_created",
        "draft",
        draft.id,
        {"message_id": message_id, "source": source.value, "issues": found},
    )
    return draft


async def list_pending(session: AsyncSession) -> Sequence[Draft]:
    return (
        await session.scalars(
            select(Draft)
            .where(Draft.status == DraftStatus.PENDING.value)
            .order_by(Draft.created_at, Draft.id)
        )
    ).all()


async def approve(
    session: AsyncSession, message_id: int, actor: str, edited_body: str | None = None
) -> Outbox:
    """Approve the message's pending draft (optionally edited) and queue it for sending
    from the draft's account; the message goes back into work (processing)."""
    message = await _lock_message(session, message_id)
    draft = await _lock_pending_draft(session, message_id)
    if message.status != MessageStatus.AWAITING_REVIEW:
        raise WrongState(f"message {message_id} is {message.status}, not awaiting_review")
    if message.operator_account_id != draft.account_id:
        raise NotOwner(f"message {message_id} is no longer assigned to the draft's account")
    body = draft.body if edited_body is None else edited_body
    await _decide(session, draft, DraftStatus.APPROVED, actor, body=body)
    await change_message_status(
        session,
        message_id,
        MessageStatus.AWAITING_REVIEW,
        MessageStatus.ASSIGNED,
        actor=actor,
        action="message_approved",
        values={"review_reason": None},
        details={"draft_id": draft.id},
    )
    await claim_message(session, message_id, draft.account_id, actor)
    template = draft.source == DraftSource.TEMPLATE and edited_body is None
    outbox = await enqueue_reply(
        session,
        message_id=message_id,
        account_id=draft.account_id,
        kind=OutboxKind.TEMPLATE if template else OutboxKind.MANUAL,
        body=body,
        # One key per draft: an earlier failed send of this message keeps its own key.
        idempotency_key=f"draft:{draft.id}",
    )
    await write_audit(
        session,
        actor,
        "draft_approved",
        "draft",
        draft.id,
        {"message_id": message_id, "outbox_id": outbox.id, "edited": edited_body is not None},
    )
    return outbox


async def reject(session: AsyncSession, message_id: int, actor: str) -> None:
    """Reject the pending draft; the lead is cancelled without a reply or reaction."""
    message = await _lock_message(session, message_id)
    draft = await _lock_pending_draft(session, message_id)
    if message.status != MessageStatus.AWAITING_REVIEW:
        raise WrongState(f"message {message_id} is {message.status}, not awaiting_review")
    await _decide(session, draft, DraftStatus.REJECTED, actor)
    await change_message_status(
        session,
        message_id,
        MessageStatus.AWAITING_REVIEW,
        MessageStatus.CANCELLED,
        actor=actor,
        action="message_rejected",
        values={**_RELEASED, "review_reason": REJECTED_BY_OWNER},
        details={"draft_id": draft.id},
    )
    await write_audit(
        session, actor, "draft_rejected", "draft", draft.id, {"message_id": message_id}
    )


class DraftDecision(NamedTuple):
    """What a decision by draft id did: 'approved', 'rejected', 'edited', 'not_found',
    or 'already_<status>' when the draft was decided before (a repeated button press)."""

    outcome: str
    message_id: int | None = None
    outbox_id: int | None = None
    issues: tuple[str, ...] = ()


async def approve_draft(session: AsyncSession, draft_id: int, actor: str) -> DraftDecision:
    """approve() for exactly this draft. A draft decided before is left as it is, so
    pressing the button again changes nothing."""
    draft = await _lock_draft(session, draft_id)
    if draft is None:
        return DraftDecision("not_found")
    if draft.status != DraftStatus.PENDING:
        return DraftDecision(f"already_{draft.status}", draft.message_id)
    outbox = await approve(session, draft.message_id, actor)
    return DraftDecision("approved", draft.message_id, outbox.id)


async def reject_draft(session: AsyncSession, draft_id: int, actor: str) -> DraftDecision:
    """reject() for exactly this draft; a draft decided before is left as it is."""
    draft = await _lock_draft(session, draft_id)
    if draft is None:
        return DraftDecision("not_found")
    if draft.status != DraftStatus.PENDING:
        return DraftDecision(f"already_{draft.status}", draft.message_id)
    await reject(session, draft.message_id, actor)
    return DraftDecision("rejected", draft.message_id)


async def edit_draft(
    session: AsyncSession,
    draft_id: int,
    body: str,
    actor: str,
    profile: "ServiceProfile | None" = None,
) -> DraftDecision:
    """Replace the text of a pending draft with the owner's own. The new text is a
    manual draft: the guard checks it (with the profile) and its issues are stored
    for the card; it is still sent only when the owner approves it."""
    draft = await _lock_draft(session, draft_id)
    if draft is None:
        return DraftDecision("not_found")
    if draft.status != DraftStatus.PENDING:
        return DraftDecision(f"already_{draft.status}", draft.message_id)
    found = check_draft(body, DraftSource.MANUAL, profile)
    await session.execute(
        update(Draft)
        .where(Draft.id == draft.id)
        .values(body=body, source=DraftSource.MANUAL.value, issues=found)
        .execution_options(synchronize_session=False)
    )
    await write_audit(
        session,
        actor,
        "draft_edited",
        "draft",
        draft.id,
        {"message_id": draft.message_id, "source_before": draft.source, "issues": found},
    )
    return DraftDecision("edited", draft.message_id, issues=tuple(found))


async def expire_stale_approvals(session: AsyncSession, actor: str = "system:approvals") -> int:
    """Approved replies still queued DRAFT_TTL_HOURS after approval (daily limit, flood
    waits, a switched-off account) are not sent that late: the queued row fails, the
    message goes back to the owner as awaiting_review('stale_approval') with a new
    pending draft of the same text, to approve again or reject. Returns how many."""
    ttl = timedelta(hours=get_settings().DRAFT_TTL_HOURS)
    stale = (
        await session.execute(
            select(Outbox.id, Outbox.message_id, Draft.id.label("draft_id"))
            .join(
                Draft,
                and_(
                    Draft.message_id == Outbox.message_id,
                    # approve() queues each draft under the key draft:<id>.
                    Outbox.idempotency_key == func.concat("draft:", Draft.id),
                ),
            )
            .where(
                Outbox.status == SendStatus.QUEUED.value,
                Draft.status == DraftStatus.APPROVED.value,
                func.coalesce(Draft.decided_at, Draft.created_at) < func.now() - ttl,
            )
            .order_by(Outbox.message_id)
        )
    ).all()
    expired = 0
    for outbox_id, message_id, draft_id in stale:
        # Lock order: message -> outbox; re-checked under the locks.
        message = await _lock_message(session, message_id)
        row = (
            await session.execute(
                select(Outbox.status, Outbox.kind, Outbox.body, Outbox.account_id)
                .where(Outbox.id == outbox_id)
                .with_for_update()
            )
        ).one()
        if row.status != SendStatus.QUEUED:
            continue
        draft = await session.get_one(Draft, draft_id)
        await change_send_status(
            session,
            outbox_id,
            SendStatus.QUEUED,
            SendStatus.FAILED,
            actor=actor,
            action="send_cancelled",
            values={"error": STALE_APPROVAL},
            details={"message_id": message_id, "draft_id": draft.id, "reason": STALE_APPROVAL},
        )
        if not await change_message_status_if_allowed(
            session,
            message_id,
            message.status,
            MessageStatus.AWAITING_REVIEW,
            actor=actor,
            action="message_needs_approval",
            values={**_RELEASED, "review_reason": STALE_APPROVAL},
            details={"draft_id": draft.id, "reason": STALE_APPROVAL},
        ):
            continue
        # An edited template went out as a manual reply; offer it again as such.
        source = (
            DraftSource.MANUAL
            if draft.source == DraftSource.TEMPLATE and row.kind != OutboxKind.TEMPLATE
            else DraftSource(draft.source)
        )
        again = Draft(
            message_id=message_id,
            account_id=row.account_id,
            body=row.body,
            source=source.value,
            status=DraftStatus.PENDING.value,
            issues=list(draft.issues),
        )
        session.add(again)
        await session.flush()
        await write_audit(
            session,
            actor,
            "approval_expired",
            "draft",
            draft.id,
            {"message_id": message_id, "outbox_id": outbox_id, "new_draft_id": again.id},
        )
        expired += 1
    return expired


async def approved_count(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Draft).where(Draft.status == DraftStatus.APPROVED.value)
    )
    return count or 0


async def enable_auto_mode(session: AsyncSession, actor: str) -> None:
    """Only an explicit command switches auto mode on, and only after CONFIRM_FIRST_N
    approved drafts; reaching N by itself changes nothing."""
    required = get_settings().CONFIRM_FIRST_N
    approved = await approved_count(session)
    if approved < required:
        raise AutoModeLocked(f"{approved} of {required} replies approved so far")
    await _set_flag(session, AUTO_MODE, True, actor)


async def disable_auto_mode(session: AsyncSession, actor: str) -> None:
    await _set_flag(session, AUTO_MODE, False, actor)


async def is_auto_mode(session: AsyncSession) -> bool:
    return await _get_flag(session, AUTO_MODE)


async def set_paused(session: AsyncSession, paused: bool, actor: str) -> None:
    await _set_flag(session, PAUSED, paused, actor)


async def is_paused(session: AsyncSession) -> bool:
    return await _get_flag(session, PAUSED)


async def _get_flag(session: AsyncSession, key: str) -> bool:
    value: Any = await session.scalar(select(AppSetting.value).where(AppSetting.key == key))
    return value is True


async def _set_flag(session: AsyncSession, key: str, value: bool, actor: str) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value, updated_by=actor)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": stmt.excluded.value, "updated_by": actor, "updated_at": func.now()},
        )
    )
    await write_audit(session, actor, "setting_changed", "setting", None, {key: value})


async def _decide(
    session: AsyncSession, draft: Draft, status: DraftStatus, actor: str, **values: object
) -> None:
    await session.execute(
        update(Draft)
        .where(Draft.id == draft.id)
        .values(status=status.value, decided_at=func.now(), decided_by=actor, **values)
        .execution_options(synchronize_session=False)
    )


async def _lock_message(session: AsyncSession, message_id: int) -> Row[str, int | None]:
    message = (
        await session.execute(
            select(Message.status, Message.operator_account_id)
            .where(Message.id == message_id)
            .with_for_update()
        )
    ).one_or_none()
    if message is None:
        raise NotFound(f"message {message_id}")
    return message


async def _lock_pending_draft(session: AsyncSession, message_id: int) -> Draft:
    # Lock order: the message is already locked; its draft comes after it.
    draft = await session.scalar(
        select(Draft)
        .where(Draft.message_id == message_id, Draft.status == DraftStatus.PENDING.value)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if draft is None:
        raise WrongState(f"message {message_id} has no pending draft")
    return draft


async def _lock_draft(session: AsyncSession, draft_id: int) -> Draft | None:
    """The draft, locked after its message (lock order: message -> draft)."""
    message_id = await session.scalar(select(Draft.message_id).where(Draft.id == draft_id))
    if message_id is None:
        return None
    await _lock_message(session, message_id)
    return await session.scalar(
        select(Draft)
        .where(Draft.id == draft_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
