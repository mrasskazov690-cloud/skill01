"""Lead duplicates: never write to a client twice; same text counts only for unknown authors."""

import hashlib
import re
from typing import Literal

from sqlalchemy import exists, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Conversation, Message, Outbox
from app.domain.states import MessageStatus, SendStatus

DuplicateReason = Literal["duplicate_client", "duplicate_text"]
DUPLICATE_CLIENT: DuplicateReason = "duplicate_client"
DUPLICATE_TEXT: DuplicateReason = "duplicate_text"

# Another lead of the same client in these statuses is already being answered.
_IN_WORK = (MessageStatus.ASSIGNED, MessageStatus.PROCESSING, MessageStatus.AWAITING_REVIEW)
_NOT_ALNUM = re.compile(r"[\W_]+")


def normalize_text(text: str) -> str:
    """Lower case, yo -> ye (\u0451 -> \u0435), letters and digits only, single spaces."""
    return _NOT_ALNUM.sub(" ", text.lower().replace("\u0451", "\u0435")).strip()


def text_hash(text: str) -> str | None:
    """sha256 of the normalized text; None when nothing is left to compare."""
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else None


async def classify_duplicate(
    session: AsyncSession,
    client_id: int | None,
    text_hash: str | None,
    *,
    message_id: int | None = None,
) -> DuplicateReason | None:
    """Why the lead `message_id` must not be answered, or None.

    duplicate_client: the client already got a reply (a sent outbox row, from any
    account) or has another lead in work. duplicate_text: only for a lead whose
    author is unknown (no client, or a client known by external_key alone), when
    another non-cancelled message has the same text hash. Leads of different known
    authors are never duplicates of each other, whatever their text.
    """
    other = Message.id != message_id if message_id is not None else true()
    if client_id is not None:
        client_messages = (
            select(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.client_id == client_id)
        )
        answered = exists().where(
            Outbox.message_id.in_(client_messages), Outbox.status == SendStatus.SENT.value
        )
        in_work = exists(
            client_messages.where(other, Message.status.in_([s.value for s in _IN_WORK]))
        )
        if await session.scalar(select(answered | in_work)):
            return DUPLICATE_CLIENT
        known = await session.scalar(
            select((Client.tg_user_id.is_not(None)) | (Client.username.is_not(None))).where(
                Client.id == client_id
            )
        )
        if known:
            return None
    if text_hash is None:
        return None
    same_text = exists().where(
        Message.text_hash == text_hash,
        other,
        Message.status != MessageStatus.CANCELLED.value,
    )
    return DUPLICATE_TEXT if await session.scalar(select(same_text)) else None
