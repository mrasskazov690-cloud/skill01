"""PostgreSQL schema. Statuses are text + CHECK; values come from app.domain.states."""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.states import (
    LIVE_SEND_STATUSES,
    AccountKind,
    AccountState,
    AssignmentReason,
    ConversationStatus,
    MessageSource,
    MessageStatus,
    OutboxKind,
    SendStatus,
)

BigId = sa.BigInteger
EMPTY_JSON = sa.text("'{}'::jsonb")


def _sql_list(values: Iterable[StrEnum]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def check_in(column: str, values: type[StrEnum]) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({_sql_list(values)})", name=column)


def _pk() -> Mapped[int]:
    return mapped_column(BigId, sa.Identity(), primary_key=True)


def _now() -> Any:
    return mapped_column(server_default=sa.func.now())


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Account(Timestamps, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        check_in("kind", AccountKind),
        check_in("state", AccountState),
        sa.CheckConstraint("max_active IS NULL OR max_active >= 0", name="max_active_non_negative"),
    )

    id: Mapped[int] = _pk()
    tg_user_id: Mapped[int | None] = mapped_column(BigId, unique=True)
    kind: Mapped[str] = mapped_column(sa.Text)
    display_name: Mapped[str] = mapped_column(sa.Text)
    state: Mapped[str] = mapped_column(sa.Text, server_default=AccountState.ACTIVE.value)
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())
    on_shift: Mapped[bool] = mapped_column(server_default=sa.true())
    max_active: Mapped[int | None] = mapped_column(sa.Integer)  # NULL = no limit
    flood_wait_until: Mapped[datetime | None]
    last_assigned_at: Mapped[datetime | None]
    diagnostics: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)


class Client(Timestamps, Base):
    __tablename__ = "clients"
    __table_args__ = (
        sa.CheckConstraint("tg_user_id IS NOT NULL OR username IS NOT NULL", name="has_identity"),
        sa.CheckConstraint("username = lower(username)", name="username_lowercase"),
    )

    id: Mapped[int] = _pk()
    tg_user_id: Mapped[int | None] = mapped_column(BigId, unique=True)
    username: Mapped[str | None] = mapped_column(sa.Text, unique=True)
    display_name: Mapped[str | None] = mapped_column(sa.Text)


class Conversation(Timestamps, Base):
    __tablename__ = "conversations"
    __table_args__ = (check_in("status", ConversationStatus),)

    id: Mapped[int] = _pk()
    # UNIQUE: one conversation per client is what pins a client to one operator.
    client_id: Mapped[int] = mapped_column(sa.ForeignKey("clients.id"), unique=True)
    source_chat_id: Mapped[int | None] = mapped_column(BigId)
    operator_account_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("accounts.id"), index=True
    )
    status: Mapped[str] = mapped_column(sa.Text, server_default=ConversationStatus.OPEN.value)
    last_activity_at: Mapped[datetime] = _now()


class Template(Timestamps, Base):
    __tablename__ = "templates"
    __table_args__ = (sa.CheckConstraint("version >= 1", name="version_positive"),)

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(sa.Text, unique=True)
    intent: Mapped[str] = mapped_column(sa.Text)
    language: Mapped[str] = mapped_column(sa.Text, server_default="ru")
    body: Mapped[str] = mapped_column(sa.Text)
    conditions: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)
    allowed_fields: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), server_default=sa.text("'{}'::text[]")
    )
    active: Mapped[bool] = mapped_column(server_default=sa.true())
    version: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("1"))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Idempotency key: a re-delivered Telegram update maps to the same row.
        sa.UniqueConstraint("source_account_id", "tg_chat_id", "tg_message_id"),
        sa.Index("ix_messages_status_received_at", "status", "received_at"),
        check_in("source", MessageSource),
        check_in("status", MessageStatus),
    )

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(sa.Text)
    source_account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"))
    tg_chat_id: Mapped[int] = mapped_column(BigId)
    tg_message_id: Mapped[int] = mapped_column(BigId)
    conversation_id: Mapped[int] = mapped_column(sa.ForeignKey("conversations.id"), index=True)
    text: Mapped[str] = mapped_column(sa.Text)
    meta: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)
    status: Mapped[str] = mapped_column(sa.Text, server_default=MessageStatus.PENDING.value)
    operator_account_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("accounts.id"), index=True
    )
    template_id: Mapped[int | None] = mapped_column(sa.ForeignKey("templates.id"))
    template_version: Mapped[int | None] = mapped_column(sa.Integer)
    review_reason: Mapped[str | None] = mapped_column(sa.Text)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text)
    lease_expires_at: Mapped[datetime | None]
    received_at: Mapped[datetime] = _now()
    assigned_at: Mapped[datetime | None]
    answered_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (check_in("reason", AssignmentReason),)

    id: Mapped[int] = _pk()
    conversation_id: Mapped[int] = mapped_column(sa.ForeignKey("conversations.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(sa.ForeignKey("messages.id"), index=True)
    from_account_id: Mapped[int | None] = mapped_column(sa.ForeignKey("accounts.id"))
    to_account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"))
    reason: Mapped[str] = mapped_column(sa.Text)
    actor: Mapped[str] = mapped_column(sa.Text)
    at: Mapped[datetime] = _now()


class AppSetting(Base):
    """Non-secret routing/processing settings (table `settings`)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )
    updated_by: Mapped[str] = mapped_column(sa.Text)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        check_in("kind", OutboxKind),
        check_in("status", SendStatus),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # At most one live send per message; a new attempt needs the old row failed.
        sa.Index(
            "uq_outbox_live_message",
            "message_id",
            unique=True,
            postgresql_where=sa.text(
                f"status IN ({_sql_list(s for s in SendStatus if s in LIVE_SEND_STATUSES)})"
            ),
        ),
    )

    id: Mapped[int] = _pk()
    message_id: Mapped[int] = mapped_column(sa.ForeignKey("messages.id"))
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"))
    idempotency_key: Mapped[str] = mapped_column(sa.Text, unique=True)
    kind: Mapped[str] = mapped_column(sa.Text)
    template_id: Mapped[int | None] = mapped_column(sa.ForeignKey("templates.id"))
    template_version: Mapped[int | None] = mapped_column(sa.Integer)
    body: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.Text, server_default=SendStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    not_before: Mapped[datetime | None]
    tg_sent_message_id: Mapped[int | None] = mapped_column(BigId)
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now()
    )
    sent_at: Mapped[datetime | None]


class AuditLog(Base):
    """Event history. `details` holds IDs, statuses and error codes only — never client text."""

    __tablename__ = "audit_log"
    __table_args__ = (sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = _pk()
    at: Mapped[datetime] = _now()
    actor: Mapped[str] = mapped_column(sa.Text)
    action: Mapped[str] = mapped_column(sa.Text)
    entity_type: Mapped[str] = mapped_column(sa.Text)
    entity_id: Mapped[int | None] = mapped_column(BigId)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON)
