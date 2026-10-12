"""Database-level guarantees that services rely on."""

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, Client, Draft, Message, Outbox
from app.db.session import transaction
from app.services.ingest import ClientRef, ingest_message

Factory = async_sessionmaker[AsyncSession]


async def _message(factory: Factory, make_account) -> tuple[int, int]:
    account = await make_account(kind="operator")
    async with transaction(factory) as s:
        result = await ingest_message(
            s,
            source="direct",
            source_account_id=account.id,
            tg_chat_id=1,
            tg_message_id=1,
            client=ClientRef(tg_user_id=1),
            text="hi",
        )
    return result.message_id, account.id


async def _expect_integrity_error(factory: Factory, stmt) -> None:
    with pytest.raises(IntegrityError):
        async with transaction(factory) as s:
            await s.execute(stmt)


async def test_one_live_outbox_row_per_message(session_factory: Factory, make_account) -> None:
    message_id, account_id = await _message(session_factory, make_account)

    def row(key: str, status: str):
        return insert(Outbox).values(
            message_id=message_id,
            account_id=account_id,
            idempotency_key=key,
            kind="manual",
            body="reply",
            status=status,
        )

    async with transaction(session_factory) as s:
        await s.execute(row("k-failed-1", "failed"))
        await s.execute(row("k-failed-2", "failed"))
        await s.execute(row("k-live", "queued"))
    for status in ("queued", "sending", "sent", "uncertain"):
        await _expect_integrity_error(session_factory, row(f"k-{status}", status))
    await _expect_integrity_error(
        session_factory, row("k-live", "failed")
    )  # idempotency_key UNIQUE

    async with transaction(session_factory) as s:
        await s.execute(
            update(Outbox).where(Outbox.idempotency_key == "k-live").values(status="failed")
        )
        await s.execute(row("k-retry", "queued"))


@pytest.mark.parametrize(
    "values",
    [
        {"tg_user_id": None, "username": None},
        {"tg_user_id": None, "username": None, "external_key": None},
        {"username": "UpperCase"},
    ],
    ids=["no-identity", "no-identity-nor-key", "uppercase-username"],
)
async def test_client_checks(session_factory: Factory, values: dict) -> None:
    await _expect_integrity_error(session_factory, insert(Client).values(**values))


async def test_status_checks_reject_unknown_values(session_factory: Factory, make_account) -> None:
    message_id, _ = await _message(session_factory, make_account)
    await _expect_integrity_error(
        session_factory, update(Message).where(Message.id == message_id).values(status="done")
    )
    await _expect_integrity_error(
        session_factory, insert(Account).values(kind="robot", display_name="x")
    )


async def test_client_known_by_external_key_only(session_factory: Factory) -> None:
    async with transaction(session_factory) as s:
        await s.execute(insert(Client).values(external_key="msg:-100/1"))
    await _expect_integrity_error(session_factory, insert(Client).values(external_key="msg:-100/1"))


async def test_daily_send_limit_is_not_negative(session_factory: Factory) -> None:
    await _expect_integrity_error(
        session_factory,
        insert(Account).values(kind="operator", display_name="x", daily_send_limit=-1),
    )


async def test_one_pending_draft_per_message(session_factory: Factory, make_account) -> None:
    message_id, account_id = await _message(session_factory, make_account)

    def draft(status: str, **values: str):
        return insert(Draft).values(
            message_id=message_id,
            account_id=account_id,
            body="draft",
            status=status,
            **({"source": "llm"} | values),
        )

    async with transaction(session_factory) as s:
        await s.execute(draft("rejected"))
        await s.execute(draft("approved"))
        await s.execute(draft("pending"))
    await _expect_integrity_error(session_factory, draft("pending"))
    await _expect_integrity_error(session_factory, draft("done"))
    await _expect_integrity_error(session_factory, draft("rejected", source="human"))
