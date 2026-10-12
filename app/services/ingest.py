"""Idempotent intake of incoming Telegram messages."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, NamedTuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Conversation, Message
from app.domain.errors import DomainError
from app.domain.states import MessageSource, MessageStatus
from app.services.audit import write_audit
from app.services.leads import text_hash
from app.telegram.links import LeadLink

# Concurrent first-time inserts converge after one retry; running out means a bug.
_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ClientRef:
    """How the client is known: Telegram user id and/or @username. A lead whose author
    could not be determined gets `external_key` (`msg:<chat>/<message_id>`) instead.
    `username_known=True` says the author's profile was read, so `username=None` means
    the user has no username now (a stored one is stale); by default None is "unknown"."""

    tg_user_id: int | None = None
    username: str | None = None
    display_name: str | None = None
    external_key: str | None = None
    username_known: bool = False

    @property
    def normalized_username(self) -> str | None:
        name = (self.username or "").strip().removeprefix("@").lower()
        return name or None


class IngestResult(NamedTuple):
    message_id: int
    created: bool


class IngestConflict(DomainError):
    """An upsert did not converge; indicates a schema or logic bug."""


async def ingest_message(
    session: AsyncSession,
    *,
    source: MessageSource | str,
    source_account_id: int,
    tg_chat_id: int,
    tg_message_id: int,
    client: ClientRef,
    text: str,
    meta: Mapping[str, Any] | None = None,
    source_chat_id: int | None = None,
    link: LeadLink | None = None,
) -> IngestResult:
    """Store a message once per (source_account_id, tg_chat_id, tg_message_id).

    Runs in the caller's transaction and does not commit. Safe under concurrent
    calls: every insert is `ON CONFLICT DO NOTHING` followed by a re-read.
    """
    source = MessageSource(source)
    key = (
        Message.source_account_id == source_account_id,
        Message.tg_chat_id == tg_chat_id,
        Message.tg_message_id == tg_message_id,
    )
    # Fast path for redelivered updates; the ON CONFLICT below covers concurrent ones.
    existing = await session.scalar(select(Message.id).where(*key))
    if existing is not None:
        return IngestResult(existing, created=False)

    client_id = await _resolve_client(session, client)
    conversation_id = await _lock_conversation(session, client_id, source_chat_id)

    message_id = await session.scalar(
        insert(Message)
        .values(
            source=source.value,
            source_account_id=source_account_id,
            tg_chat_id=tg_chat_id,
            tg_message_id=tg_message_id,
            conversation_id=conversation_id,
            text=text,
            text_hash=text_hash(text),
            link=asdict(link) if link is not None else None,
            meta=dict(meta or {}),
            status=MessageStatus.PENDING.value,
        )
        .on_conflict_do_nothing(index_elements=["source_account_id", "tg_chat_id", "tg_message_id"])
        .returning(Message.id)
    )
    if message_id is None:
        existing = await session.scalar(select(Message.id).where(*key))
        if existing is None:
            raise IngestConflict("message key conflicted but no row is visible")
        return IngestResult(existing, created=False)

    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(last_activity_at=func.now())
    )
    await write_audit(
        session,
        f"account:{source_account_id}",
        "message_ingested",
        "message",
        message_id,
        {"source": source.value, "conversation_id": conversation_id},
    )
    return IngestResult(message_id, created=True)


async def _lock_conversation(
    session: AsyncSession, client_id: int, source_chat_id: int | None
) -> int:
    # Lock order is conversation -> message -> account everywhere: the conversation
    # row is locked here, before the message insert, like the dispatcher does.
    for _ in range(_MAX_ATTEMPTS):
        conversation_id = await session.scalar(
            insert(Conversation)
            .values(client_id=client_id, source_chat_id=source_chat_id)
            .on_conflict_do_nothing(index_elements=["client_id"])
            .returning(Conversation.id)
        )
        if conversation_id is None:
            conversation_id = await session.scalar(
                select(Conversation.id).where(Conversation.client_id == client_id).with_for_update()
            )
        if conversation_id is not None:
            return conversation_id
    raise IngestConflict("conversation upsert did not converge")


async def _resolve_client(session: AsyncSession, ref: ClientRef) -> int:
    """Find or create the client. tg_user_id wins over username; a client first seen
    by username only gets its tg_user_id attached when it later shows up with both.
    A client found by tg_user_id gets the username it shows up with now, or loses its
    stored one if it is known to have none."""
    username = ref.normalized_username
    if ref.tg_user_id is None and username is None:
        if ref.external_key is None:
            raise ValueError("client needs tg_user_id, username or external_key")
        return await _resolve_external_client(session, ref.external_key)

    for _ in range(_MAX_ATTEMPTS):
        if ref.tg_user_id is not None:
            found = (
                await session.execute(
                    select(Client.id, Client.username).where(Client.tg_user_id == ref.tg_user_id)
                )
            ).one_or_none()
            if found is not None:
                if username is not None and found.username != username:
                    await _refresh_username(session, found.id, username)
                elif username is None and ref.username_known and found.username is not None:
                    # The author has no username any more: the stored one was released
                    # and may belong to someone else soon.
                    await session.execute(
                        update(Client).where(Client.id == found.id).values(username=None)
                    )
                return found.id

        insert_username = username
        if username is not None:
            row = (
                await session.execute(
                    select(Client.id, Client.tg_user_id).where(Client.username == username)
                )
            ).one_or_none()
            if row is not None:
                if ref.tg_user_id is None:
                    return row.id
                if row.tg_user_id is None:
                    if await _attach_tg_user_id(session, row.id, ref.tg_user_id):
                        return row.id
                    continue  # lost a race for this row or this tg_user_id; re-read
                # The username now belongs to another Telegram user; do not merge.
                insert_username = None

        created = await session.scalar(
            insert(Client)
            .values(
                tg_user_id=ref.tg_user_id, username=insert_username, display_name=ref.display_name
            )
            .on_conflict_do_nothing()
            .returning(Client.id)
        )
        if created is not None:
            return created
    raise IngestConflict("client upsert did not converge")


async def _resolve_external_client(session: AsyncSession, external_key: str) -> int:
    """A lead with an unknown author: its own client, found again by the same key."""
    for _ in range(_MAX_ATTEMPTS):
        client_id = await session.scalar(
            insert(Client)
            .values(external_key=external_key)
            .on_conflict_do_nothing(index_elements=["external_key"])
            .returning(Client.id)
        )
        if client_id is None:
            client_id = await session.scalar(
                select(Client.id).where(Client.external_key == external_key)
            )
        if client_id is not None:
            return client_id
    raise IngestConflict("client upsert did not converge")


async def _refresh_username(session: AsyncSession, client_id: int, username: str) -> None:
    """Store the username a client (matched by tg_user_id) has now. Another client with
    a tg_user_id that still holds it has a stale copy (Telegram released the name), so
    that copy is cleared. A username-only holder may be this same person and is left
    alone; the stored name then stays as it was, as it does if the name is taken
    concurrently. Outbox never relies on the stored name alone (see PeerRef)."""
    try:
        async with session.begin_nested():
            # Both rows locked in id order, so crossing refreshes cannot deadlock.
            rows = (
                await session.execute(
                    select(Client.id, Client.tg_user_id)
                    .where(or_(Client.id == client_id, Client.username == username))
                    .order_by(Client.id)
                    .with_for_update()
                )
            ).all()
            holder = next((r for r in rows if r.id != client_id), None)
            if holder is not None:
                if holder.tg_user_id is None:
                    return
                await session.execute(
                    update(Client).where(Client.id == holder.id).values(username=None)
                )
            await session.execute(
                update(Client).where(Client.id == client_id).values(username=username)
            )
    except IntegrityError:  # the name was taken concurrently; keep the stored one
        return


async def _attach_tg_user_id(session: AsyncSession, client_id: int, tg_user_id: int) -> bool:
    try:
        async with session.begin_nested():
            attached = await session.scalar(
                update(Client)
                .where(Client.id == client_id, Client.tg_user_id.is_(None))
                .values(tg_user_id=tg_user_id)
                .returning(Client.id)
            )
    except IntegrityError:  # tg_user_id was taken by a concurrently created client
        return False
    return attached is not None
