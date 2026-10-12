"""The worker: reads leads from the work chat and sends replies, one at a time.

Two loops under one TaskGroup. The intake loop reads WORK_CHAT_ID through the reader
account every WORK_CHAT_POLL_SECONDS. The send loop wakes every SEND_INTERVAL_SECONDS
and makes at most one send per tick; it runs only while this process holds the
sender advisory lock, so there is one active sender in the whole system.
Nothing here logs or audits client text, reply bodies or sessions.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy import literal as sa_literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import Account, AppSetting, Client, Conversation, Message, Outbox
from app.db.session import transaction
from app.domain.states import (
    LIVE_SEND_STATUSES,
    MessageSource,
    MessageStatus,
    OutboxKind,
    SendStatus,
)
from app.logging import get_logger
from app.services.approvals import (
    create_draft,
    expire_stale_approvals,
    is_auto_mode,
    is_paused,
)
from app.services.audit import write_audit
from app.services.claims import claim_message
from app.services.dispatcher import CAN_SEND, dispatch_one
from app.services.guard import check_draft
from app.services.ingest import ClientRef, ingest_message
from app.services.leads import classify_duplicate
from app.services.outbox import (
    cancel_queued_send,
    enqueue_reply,
    error_code,
    execute_send,
    expire_flood_waits,
    record_flood_wait,
    recover_interrupted_sends,
)
from app.services.transitions import change_message_status
from app.telegram.base import (
    AuthorUnavailable,
    FloodWait,
    PrivacyRestricted,
    ReactionInvalid,
    TelegramAccount,
    TelegramError,
    WorkChatMessage,
)
from app.telegram.links import pick_lead_link
from app.worker.clock import Clock, SystemClock
from app.worker.drafts import (
    DraftProvider,
    DraftUnavailable,
    NoDraft,
    Reply,
    TemplateDraftProvider,
)
from app.worker.lock import SenderLock

if TYPE_CHECKING:
    from app.drafts.profile import ServiceProfile

HEART = "❤️"
BLOCKED = "🚫"
# Used for a privacy refusal when the work chat does not allow REACTION_PRIVACY.
PRIVACY_FALLBACK = "👎"
HANDLED_MANUALLY = "handled_manually"
NO_LINK = "no_link"
NO_TEMPLATE = "no_template"
DIALOG_CHECK_FAILED = "dialog_check_failed"
DUPLICATE_CLIENT = "duplicate_client"
CURSOR_KEY = "work_chat_cursor:{chat_id}"
# FloodWait while resolving one author: wait and retry this often, then the poll stops
# (the cursor stays before the lead, the next poll starts with it again).
RESOLVE_ATTEMPTS = 5
# A work-chat message whose storing fails this many polls in a row is stored without
# its author for a person (or, if even that fails, skipped and audited): one bad
# message must not stop intake for good. A database outage fails the fallback too,
# so nothing is skipped then.
INTAKE_ATTEMPTS = 3
INTAKE_FAILED = "intake_failed"
# Leads that end without a send (duplicates, no template) do not end the tick; this
# bounds how many of them one tick goes through.
LEADS_PER_TICK = 20
_RELEASED = {"claimed_by": None, "lease_expires_at": None}
_PRIVACY_CODE = error_code(PrivacyRestricted())  # outbox.error of a privacy refusal
# messages.meta key for what the draft provider made of a lead (country, need...).
DRAFT_INFO_KEY = "draft_info"
# The lead was not drafted because it no longer needs a reply (taken, duplicate): the
# locked step below decides what happens to it.
_SKIP = object()

TickKind = Literal["locked_out", "paused", "idle", "sent", "draft", "deferred"]


class TickResult(NamedTuple):
    kind: TickKind
    message_id: int | None = None
    account_id: int | None = None
    status: SendStatus | None = None  # outcome of the send, for kind='sent'


@dataclass(frozen=True, slots=True)
class _Lead:
    message_id: int
    account_id: int
    client_id: int
    tg_user_id: int | None
    text_hash: str | None


class Worker:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        adapters: Mapping[int, TelegramAccount],
        *,
        clock: Clock | None = None,
        drafts: DraftProvider | None = None,
        profile: "ServiceProfile | None" = None,
        name: str = "sender",
    ) -> None:
        self.factory = session_factory
        self.adapters = dict(adapters)
        self.clock = clock or SystemClock()
        self.drafts = drafts or TemplateDraftProvider()
        self.profile = profile  # the guard checks disclosure and quotes against it
        self.name = name
        self.lock = SenderLock(engine)
        self._warned_locked_out = False
        self._intake_failures: dict[tuple[int, int], int] = {}
        self._log = get_logger(__name__, worker=name)

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.run_intake())
                tasks.create_task(self.run_sender())
        finally:
            await self.close()

    async def close(self) -> None:
        await self.lock.release()

    # --- loops -----------------------------------------------------------------------

    async def run_intake(self, max_polls: int | None = None) -> None:
        polls = 0
        while max_polls is None or polls < max_polls:
            try:
                await self.poll_work_chat()
            except Exception as exc:
                # Error code and place only: an exception's text (or a traceback that
                # renders it) may carry the client's message.
                self._log.error("intake_poll_failed", error=error_code(exc), where=_origin(exc))
            polls += 1
            await self.clock.sleep(get_settings().WORK_CHAT_POLL_SECONDS)

    async def run_sender(self, max_ticks: int | None = None) -> None:
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            try:
                await self.send_tick()
            except Exception:
                self._log.exception("send_tick_failed")
            ticks += 1
            # One tick per interval and at most one send per tick: one send per interval.
            await self.clock.sleep(get_settings().SEND_INTERVAL_SECONDS)

    # --- intake ----------------------------------------------------------------------

    async def poll_work_chat(self) -> int:
        """Read new work-chat messages oldest first; returns how many were stored."""
        settings = get_settings()
        chat_id, reader_id = settings.WORK_CHAT_ID, settings.READER_ACCOUNT_ID
        if chat_id is None or reader_id is None:
            raise RuntimeError("WORK_CHAT_ID and READER_ACCOUNT_ID must be set")
        reader = self.adapters[reader_id]
        cursor = await self._cursor(chat_id)
        first_run = cursor is None
        limit = settings.WORK_CHAT_BACKFILL if first_run else max(settings.WORK_CHAT_BACKFILL, 100)
        try:
            batch = await reader.fetch_work_chat(chat_id, min_id=cursor or 0, limit=max(limit, 1))
        except FloodWait as exc:
            await self.clock.sleep(exc.seconds)
            return 0
        if first_run and settings.WORK_CHAT_BACKFILL == 0:
            if batch:  # no backfill: start after the newest message
                async with transaction(self.factory) as s:
                    await self._save_cursor(s, chat_id, max(m.message_id for m in batch))
            return 0
        stored = 0
        # Oldest first, one transaction each: received_at follows the chat's order, so
        # newest_first dispatch takes the newest lead.
        for message in sorted(batch, key=lambda m: m.message_id):
            taken = await self._take(reader, message)
            if taken is None:
                break
            stored += taken
        return stored

    async def _take(self, reader: TelegramAccount, message: WorkChatMessage) -> bool | None:
        """Store one work-chat message. True: stored; False: skipped; None: retry later."""
        key = (message.chat_id, message.message_id)
        try:
            taken = await self._store(reader, message)
        except Exception as exc:
            failures = self._intake_failures.get(key, 0) + 1
            self._intake_failures[key] = failures
            self._log.warning(
                "intake_message_failed",
                tg_message_id=message.message_id,
                attempt=failures,
                error=error_code(exc),
            )
            if failures < INTAKE_ATTEMPTS:
                return None
            taken = await self._store_unresolved(reader, message, error_code(exc))
        self._intake_failures.pop(key, None)
        return taken

    async def _store(self, reader: TelegramAccount, message: WorkChatMessage) -> bool | None:
        if message.reactions & handled_reactions():
            async with transaction(self.factory) as s:
                await self._save_cursor(s, message.chat_id, message.message_id)
            return False
        external = ClientRef(external_key=f"msg:{message.chat_id}/{message.message_id}")
        link = pick_lead_link(message.links)
        client, reason = external, NO_LINK
        if link is not None:
            for _ in range(RESOLVE_ATTEMPTS):
                try:
                    author = await reader.resolve_author(link)
                except FloodWait as exc:
                    await self.clock.sleep(exc.seconds)
                    continue
                except Exception as exc:  # SourceUnavailable, AuthorUnavailable, anything odd
                    client, reason = external, error_code(exc)
                    break
                if author.is_bot:
                    client, reason = external, error_code(AuthorUnavailable())
                else:
                    client = ClientRef(
                        tg_user_id=author.tg_user_id,
                        username=author.username,
                        display_name=author.display_name,
                        username_known=author.username_known,
                    )
                    reason = None
                break
            else:
                self._log.warning("resolve_author_flood", tg_message_id=message.message_id)
                return None
        async with transaction(self.factory) as s:
            result = await ingest_message(
                s,
                source=MessageSource.WORK_CHAT,
                source_account_id=reader.account_id,
                tg_chat_id=message.chat_id,
                tg_message_id=message.message_id,
                client=client,
                text=message.text,
                meta={"forwarded": message.forwarded},
                link=link,
            )
            if result.created:
                await self._classify(s, result.message_id, reason)
            await self._save_cursor(s, message.chat_id, message.message_id)
        return result.created

    async def _store_unresolved(
        self, reader: TelegramAccount, message: WorkChatMessage, error: str
    ) -> bool:
        """Last resort for a message that keeps failing: without author and link, for a
        person; if even that cannot be stored, skip it (audited) and go on."""
        try:
            async with transaction(self.factory) as s:
                result = await ingest_message(
                    s,
                    source=MessageSource.WORK_CHAT,
                    source_account_id=reader.account_id,
                    tg_chat_id=message.chat_id,
                    tg_message_id=message.message_id,
                    client=ClientRef(external_key=f"msg:{message.chat_id}/{message.message_id}"),
                    text=message.text,
                    meta={"forwarded": message.forwarded, "intake_error": error},
                )
                if result.created:
                    await self._classify(s, result.message_id, INTAKE_FAILED)
                await self._save_cursor(s, message.chat_id, message.message_id)
            return result.created
        except Exception as exc:
            self._log.error(
                "intake_message_skipped", tg_message_id=message.message_id, error=error_code(exc)
            )
            async with transaction(self.factory) as s:
                await write_audit(
                    s,
                    "worker:intake",
                    "work_chat_message_skipped",
                    "work_chat_message",
                    message.message_id,
                    {"chat_id": message.chat_id, "error": error, "store_error": error_code(exc)},
                )
                await self._save_cursor(s, message.chat_id, message.message_id)
            return False

    async def _classify(self, session: AsyncSession, message_id: int, reason: str | None) -> None:
        row = (
            await session.execute(
                select(Conversation.client_id, Message.text_hash)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.id == message_id)
            )
        ).one()
        duplicate = await classify_duplicate(
            session, row.client_id, row.text_hash, message_id=message_id
        )
        if duplicate is not None:
            target, reason = MessageStatus.CANCELLED, duplicate
        elif reason is not None:
            target = MessageStatus.AWAITING_REVIEW  # unknown author: a person decides
        else:
            return
        await change_message_status(
            session,
            message_id,
            MessageStatus.PENDING,
            target,
            actor="worker:intake",
            action="lead_classified",
            values={"review_reason": reason},
            details={"reason": reason},
        )

    async def _cursor(self, chat_id: int) -> int | None:
        async with self.factory() as s:
            value = await s.scalar(
                select(AppSetting.value).where(AppSetting.key == CURSOR_KEY.format(chat_id=chat_id))
            )
        return value if isinstance(value, int) else None

    async def _save_cursor(self, session: AsyncSession, chat_id: int, message_id: int) -> None:
        stmt = pg_insert(AppSetting).values(
            key=CURSOR_KEY.format(chat_id=chat_id), value=message_id, updated_by="worker:intake"
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": stmt.excluded.value, "updated_at": func.now()},
            )
        )

    # --- sending ---------------------------------------------------------------------

    async def send_tick(self) -> TickResult:
        """At most one send: an approved/queued reply first, otherwise the next lead."""
        if not await self._hold_lock():
            return TickResult("locked_out")
        async with transaction(self.factory) as s:
            if await is_paused(s):
                return TickResult("paused")
            await expire_flood_waits(s)
            await expire_stale_approvals(s, actor=f"worker:{self.name}")
        queued = await self._send_queued()
        if queued is not None:
            return queued
        for _ in range(LEADS_PER_TICK):
            lead = await self._next_lead()
            if lead is None:
                return TickResult("idle")
            result = await self._work_on(lead)
            if result is not None:
                return result
        return TickResult("idle")

    async def _hold_lock(self) -> bool:
        was_held = self.lock.held
        if not await self.lock.acquire():
            if not self._warned_locked_out:
                self._log.warning("sender_lock_taken", detail="another worker is sending")
                self._warned_locked_out = True
            return False
        if not was_held:
            self._warned_locked_out = False
            # Startup sweep, only once we are the sender: another worker's 'sending'
            # row is a live call, not an interrupted one.
            async with transaction(self.factory) as s:
                recovered = await recover_interrupted_sends(s)
            self._log.info("sender_lock_acquired", interrupted_sends=recovered)
        return True

    async def _send_queued(self) -> TickResult | None:
        """Approved drafts (and replies queued before a crash or a flood wait)."""
        async with self.factory() as s:
            rows = (
                await s.execute(
                    select(Outbox.id, Outbox.account_id)
                    .join(Account, Account.id == Outbox.account_id)
                    .where(
                        Outbox.status == SendStatus.QUEUED.value,
                        Outbox.account_id.in_(self.adapters),
                        or_(Outbox.not_before.is_(None), Outbox.not_before <= func.now()),
                        # Approved replies count against the daily limit when they are
                        # sent; those of an account at its limit wait for the next day.
                        CAN_SEND,
                    )
                    .order_by(Outbox.created_at, Outbox.id)
                    .limit(10)
                )
            ).all()
        for outbox_id, account_id in rows:
            check = await self._recheck_lead(outbox_id, self.adapters[account_id])
            if check == "handled":
                continue
            if check == "deferred":
                return TickResult("deferred", None, account_id)
            status = await execute_send(self.factory, outbox_id, self.adapters[account_id])
            if status is not None:
                message_id = await self._after_send(outbox_id, status, self.adapters[account_id])
                return TickResult("sent", message_id, account_id, status)
        return None

    async def _next_lead(self) -> _Lead | None:
        """A lead left assigned to one of our accounts (a crash between steps, a
        deferred dialog check), else the next one from the dispatcher."""
        live = exists().where(
            Outbox.message_id == Message.id,
            Outbox.status.in_([s.value for s in LIVE_SEND_STATUSES]),
        )
        async with transaction(self.factory) as s:
            # Only for an account that may send now: one disabled, off shift, flood
            # limited or at its daily limit keeps its leads until it can again (or a
            # person moves them), and dispatch goes on with the other accounts.
            message_id = await s.scalar(
                select(Message.id)
                .join(Account, Account.id == Message.operator_account_id)
                .where(
                    Message.status == MessageStatus.ASSIGNED.value,
                    Message.operator_account_id.in_(self.adapters),
                    ~live,
                    CAN_SEND,
                )
                .order_by(Message.received_at.desc(), Message.id.desc())
                .limit(1)
            )
            if message_id is None:
                message_id = await dispatch_one(s)
            if message_id is None:
                return None
            row = (
                await s.execute(
                    select(
                        Message.operator_account_id,
                        Message.text_hash,
                        Conversation.client_id,
                        Client.tg_user_id,
                    )
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .join(Client, Client.id == Conversation.client_id)
                    .where(Message.id == message_id)
                )
            ).one()
        if row.operator_account_id not in self.adapters:
            # The message stays assigned for a person; this account has no session here.
            self._log.warning(
                "no_adapter_for_account", message_id=message_id, account_id=row.operator_account_id
            )
            return None
        return _Lead(
            message_id, row.operator_account_id, row.client_id, row.tg_user_id, row.text_hash
        )

    async def _work_on(self, lead: _Lead) -> TickResult | None:
        """Decide and (maybe) send; None when the lead ended without a send or draft."""
        adapter = self.adapters[lead.account_id]
        has_dialog = False
        if lead.tg_user_id is not None:
            try:
                has_dialog = await adapter.has_dialog_with(lead.tg_user_id)
            except FloodWait as exc:
                # The lead stays assigned; the account sits out the wait (see _next_lead).
                async with transaction(self.factory) as s:
                    await record_flood_wait(s, lead.account_id, exc.seconds)
                return TickResult("deferred", lead.message_id, lead.account_id)
            except TelegramError as exc:
                async with transaction(self.factory) as s:
                    await self._close_lead(
                        s, lead, MessageStatus.AWAITING_REVIEW, DIALOG_CHECK_FAILED, error_code(exc)
                    )
                return None

        # The draft is made before the locks below are taken: a Claude call takes
        # seconds, and nothing should wait on this lead's rows meanwhile.
        reply: Reply | NoDraft | object | None = _SKIP
        if not has_dialog:
            try:
                reply = await self._make_draft(lead)
            except DraftUnavailable as exc:
                # Rate limit, server error, network: the lead stays assigned and is
                # tried again on the next tick.
                self._log.warning("draft_unavailable", message_id=lead.message_id, error=exc.code)
                return TickResult("deferred", lead.message_id, lead.account_id)

        outbox_id: int | None = None
        async with transaction(self.factory) as s:
            # Lock order: conversation -> message (claim, draft and outbox follow).
            await s.execute(
                select(Conversation.id)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(Message.id == lead.message_id)
                .with_for_update(of=Conversation)
            )
            current = (
                await s.execute(
                    select(Message.status, Message.operator_account_id)
                    .where(Message.id == lead.message_id)
                    .with_for_update()
                )
            ).one()
            if current.status != MessageStatus.ASSIGNED or current.operator_account_id != (
                lead.account_id
            ):
                return None
            # Never write twice: an existing private dialog of this account, or (any
            # account) a sent reply / another lead of the client in work.
            duplicate = (
                DUPLICATE_CLIENT
                if has_dialog
                else await classify_duplicate(
                    s, lead.client_id, lead.text_hash, message_id=lead.message_id
                )
            )
            if duplicate is not None:
                await self._close_lead(s, lead, MessageStatus.CANCELLED, duplicate)
                return None
            if reply is _SKIP:
                # It looked taken or a duplicate a moment ago and no longer does: the
                # next tick drafts it.
                return None
            await claim_message(s, lead.message_id, lead.account_id, self.name)
            if isinstance(reply, NoDraft):
                await self._save_draft_info(s, lead.message_id, reply.info)
                await change_message_status(
                    s,
                    lead.message_id,
                    MessageStatus.PROCESSING,
                    MessageStatus.AWAITING_REVIEW,
                    actor=f"worker:{self.name}",
                    action="lead_not_drafted",
                    values={"review_reason": reply.reason, **_RELEASED},
                    details={"reason": reply.reason, "error": reply.error},
                )
                return None
            if not isinstance(reply, Reply):
                await change_message_status(
                    s,
                    lead.message_id,
                    MessageStatus.PROCESSING,
                    MessageStatus.AWAITING_REVIEW,
                    actor=f"worker:{self.name}",
                    action="lead_needs_template",
                    values={"review_reason": NO_TEMPLATE, **_RELEASED},
                )
                return None
            await self._save_draft_info(s, lead.message_id, reply.info)
            issues = check_draft(reply.body, reply.source, self.profile)
            if issues or not await is_auto_mode(s):
                await create_draft(
                    s,
                    message_id=lead.message_id,
                    account_id=lead.account_id,
                    body=reply.body,
                    source=reply.source,
                    issues=issues,
                )
                return TickResult("draft", lead.message_id, lead.account_id)
            attempts = await s.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.message_id == lead.message_id)
            )
            outbox = await enqueue_reply(
                s,
                message_id=lead.message_id,
                account_id=lead.account_id,
                kind=OutboxKind.TEMPLATE if reply.template_id is not None else OutboxKind.MANUAL,
                body=reply.body,
                template_id=reply.template_id,
                template_version=reply.template_version,
                # Earlier failed rows of this message keep their keys.
                idempotency_key=f"auto:{lead.message_id}:{attempts}",
            )
            outbox_id = outbox.id
        check = await self._recheck_lead(outbox_id, adapter)
        if check == "handled":
            return None
        if check == "deferred":  # the reply stays queued for the next tick
            return TickResult("deferred", lead.message_id, lead.account_id)
        status = await execute_send(self.factory, outbox_id, adapter)
        if status is None:
            return TickResult("deferred", lead.message_id, lead.account_id)
        await self._after_send(outbox_id, status, adapter)
        return TickResult("sent", lead.message_id, lead.account_id, status)

    async def _make_draft(self, lead: _Lead) -> Reply | NoDraft | object | None:
        """The provider's answer for a lead that still needs one, else _SKIP."""
        async with transaction(self.factory) as s:
            current = (
                await s.execute(
                    select(Message.status, Message.operator_account_id).where(
                        Message.id == lead.message_id
                    )
                )
            ).one()
            if current.status != MessageStatus.ASSIGNED or (
                current.operator_account_id != lead.account_id
            ):
                return _SKIP
            # Checked again under the locks; here it only saves a Claude call.
            if await classify_duplicate(
                s, lead.client_id, lead.text_hash, message_id=lead.message_id
            ):
                return _SKIP
            return await self.drafts.draft(s, lead.message_id)

    async def _save_draft_info(
        self, session: AsyncSession, message_id: int, info: Mapping[str, Any]
    ) -> None:
        if not info:
            return
        await session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(
                meta=Message.meta.op("||")(sa_literal({DRAFT_INFO_KEY: dict(info)}, type_=JSONB))
            )
        )

    async def _close_lead(
        self,
        session: AsyncSession,
        lead: _Lead,
        target: MessageStatus,
        reason: str,
        error: str | None = None,
    ) -> None:
        current = await session.scalar(
            select(Message.status).where(Message.id == lead.message_id).with_for_update()
        )
        if current != MessageStatus.ASSIGNED:
            return
        await change_message_status(
            session,
            lead.message_id,
            MessageStatus.ASSIGNED,
            target,
            actor=f"worker:{self.name}",
            action="lead_closed",
            values={"review_reason": reason},
            details={"reason": reason, "account_id": lead.account_id, "error": error},
        )

    async def _recheck_lead(
        self, outbox_id: int, adapter: TelegramAccount
    ) -> Literal["send", "handled", "deferred"]:
        """Re-read the lead's reactions right before its reply goes out: if people have
        handled it by hand meanwhile (❤️/🚫), the reply is withdrawn and the lead is
        cancelled('handled_manually'). A flood wait on the read defers the send; any
        other read failure (e.g. the lead was deleted) is audited and the send goes on."""
        async with self.factory() as s:
            row = (
                await s.execute(
                    select(
                        Outbox.message_id, Message.source, Message.tg_chat_id, Message.tg_message_id
                    )
                    .join(Message, Message.id == Outbox.message_id)
                    .where(Outbox.id == outbox_id)
                )
            ).one()
        if row.source != MessageSource.WORK_CHAT:
            return "send"
        # The reader account reads the work chat; the sender reacts there, so it can too.
        reader = self.adapters.get(get_settings().READER_ACCOUNT_ID or 0, adapter)
        try:
            reactions = await reader.fetch_reactions(row.tg_chat_id, row.tg_message_id)
        except FloodWait:
            return "deferred"
        except Exception as exc:
            self._log.warning(
                "reaction_check_failed", message_id=row.message_id, error=error_code(exc)
            )
            async with transaction(self.factory) as s:
                await write_audit(
                    s,
                    f"worker:{self.name}",
                    "reaction_check_failed",
                    "message",
                    row.message_id,
                    {"outbox_id": outbox_id, "error": error_code(exc)},
                )
            return "send"
        if not reactions & handled_reactions():
            return "send"
        async with transaction(self.factory) as s:
            await cancel_queued_send(
                s, outbox_id, reason=HANDLED_MANUALLY, actor=f"worker:{self.name}"
            )
        return "handled"

    async def _after_send(
        self, outbox_id: int, status: SendStatus, adapter: TelegramAccount
    ) -> int:
        """❤️ on the lead after a send, REACTION_PRIVACY (🚫) when the user's privacy
        blocks us (👎 if the chat does not allow it), else none. A failed reaction never
        changes the send's outcome; it is audited."""
        async with self.factory() as s:
            row = (
                await s.execute(
                    select(
                        Outbox.message_id,
                        Outbox.error,
                        Message.source,
                        Message.tg_chat_id,
                        Message.tg_message_id,
                    )
                    .join(Message, Message.id == Outbox.message_id)
                    .where(Outbox.id == outbox_id)
                )
            ).one()
        if status == SendStatus.SENT:
            emoji = HEART
        elif status == SendStatus.FAILED and row.error == _PRIVACY_CODE:
            emoji = get_settings().REACTION_PRIVACY
        else:
            return row.message_id
        if row.source != MessageSource.WORK_CHAT:
            return row.message_id
        details = {"emoji": emoji, "account_id": adapter.account_id, "outbox_id": outbox_id}
        try:
            try:
                await adapter.react(row.tg_chat_id, row.tg_message_id, emoji)
            except ReactionInvalid:
                if emoji in (HEART, PRIVACY_FALLBACK):
                    raise
                self._log.warning(
                    "reaction_invalid",
                    message_id=row.message_id,
                    emoji=emoji,
                    fallback=PRIVACY_FALLBACK,
                    detail="the work chat does not allow REACTION_PRIVACY",
                )
                emoji = PRIVACY_FALLBACK
                details["emoji"] = emoji
                await adapter.react(row.tg_chat_id, row.tg_message_id, emoji)
        except Exception as exc:
            self._log.warning("reaction_failed", message_id=row.message_id, error=error_code(exc))
            async with transaction(self.factory) as s:
                await write_audit(
                    s,
                    f"worker:{self.name}",
                    "reaction_failed",
                    "message",
                    row.message_id,
                    {**details, "error": error_code(exc)},
                )
            return row.message_id
        async with transaction(self.factory) as s:
            await s.execute(
                update(Message)
                .where(Message.id == row.message_id)
                .values(reaction=emoji)
                .execution_options(synchronize_session=False)
            )
            await write_audit(
                s, f"worker:{self.name}", "reaction_set", "message", row.message_id, details
            )
        return row.message_id


def handled_reactions() -> frozenset[str]:
    """Reactions that mark a lead as handled by people (or by us): such a lead is never
    taken at intake, and its queued reply is withdrawn before sending."""
    return frozenset({HEART, BLOCKED, get_settings().REACTION_PRIVACY, PRIVACY_FALLBACK})


def _origin(exc: BaseException) -> str | None:
    """Where an exception was raised (module:line), without its text."""
    tb = exc.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{tb.tb_frame.f_globals.get('__name__', '?')}:{tb.tb_lineno}"
