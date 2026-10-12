"""leads, sender alternation and drafts

clients.external_key (lead with an unknown author), messages.text_hash/link/reaction,
accounts.daily_send_limit/last_sent_at/session_env, outbox.random_id, table drafts.

Revision ID: 0002_leads_and_drafts
Revises: 0001_initial
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_leads_and_drafts"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTITY_OLD = "tg_user_id IS NOT NULL OR username IS NOT NULL"
_IDENTITY_NEW = "tg_user_id IS NOT NULL OR username IS NOT NULL OR external_key IS NOT NULL"


def upgrade() -> None:
    op.add_column("clients", sa.Column("external_key", sa.Text(), nullable=True))
    op.create_unique_constraint(op.f("uq_clients_external_key"), "clients", ["external_key"])
    op.drop_constraint(op.f("ck_clients_has_identity"), "clients", type_="check")
    op.create_check_constraint(op.f("ck_clients_has_identity"), "clients", _IDENTITY_NEW)

    op.add_column("messages", sa.Column("text_hash", sa.Text(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("link", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("messages", sa.Column("reaction", sa.Text(), nullable=True))
    op.create_index(op.f("ix_messages_text_hash"), "messages", ["text_hash"], unique=False)

    op.add_column("accounts", sa.Column("daily_send_limit", sa.Integer(), nullable=True))
    op.add_column("accounts", sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("accounts", sa.Column("session_env", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_accounts_daily_send_limit_non_negative"),
        "accounts",
        "daily_send_limit IS NULL OR daily_send_limit >= 0",
    )

    op.add_column("outbox", sa.Column("random_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(op.f("uq_outbox_random_id"), "outbox", ["random_id"])

    op.create_table(
        "drafts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "issues",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "source IN ('template', 'llm', 'manual')", name=op.f("ck_drafts_source")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name=op.f("ck_drafts_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_drafts_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], name=op.f("fk_drafts_message_id_messages")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drafts")),
    )
    # At most one undecided draft per message.
    op.create_index(
        "uq_drafts_pending_message",
        "drafts",
        ["message_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Clients known only by external_key cannot satisfy the old identity check. Their
    # leads are real data: refuse instead of deleting them silently.
    orphans = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM clients WHERE tg_user_id IS NULL AND username IS NULL")
    )
    if orphans:
        raise RuntimeError(
            f"{orphans} clients are known only by external_key; resolve or delete them first"
        )

    op.drop_table("drafts")

    op.drop_constraint(op.f("uq_outbox_random_id"), "outbox", type_="unique")
    op.drop_column("outbox", "random_id")

    op.drop_constraint(op.f("ck_accounts_daily_send_limit_non_negative"), "accounts", type_="check")
    for column in ("session_env", "last_sent_at", "daily_send_limit"):
        op.drop_column("accounts", column)

    op.drop_index(op.f("ix_messages_text_hash"), table_name="messages")
    for column in ("reaction", "link", "text_hash"):
        op.drop_column("messages", column)

    op.drop_constraint(op.f("ck_clients_has_identity"), "clients", type_="check")
    op.create_check_constraint(op.f("ck_clients_has_identity"), "clients", _IDENTITY_OLD)
    op.drop_constraint(op.f("uq_clients_external_key"), "clients", type_="unique")
    op.drop_column("clients", "external_key")
