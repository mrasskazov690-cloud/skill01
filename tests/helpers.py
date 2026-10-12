"""Multi-transaction helpers for service tests (each step commits, like real workers)."""

from itertools import count
from typing import Any, TypeVar

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, AuditLog, Message, Outbox
from app.db.session import transaction
from app.services.claims import claim_message
from app.services.dispatcher import dispatch_batch
from app.services.ingest import ClientRef, ingest_message
from app.services.outbox import enqueue_reply

Factory = async_sessionmaker[AsyncSession]
CLIENT_TEXT = "Здравствуйте, нужна квартира на Ленина 5, тел +7 900 000-00-00"
REPLY_BODY = "Добрый день! Отправляю подборку, ответный текст оператора"

T = TypeVar("T")
_tg_message_ids = count(1)


async def add_message(
    session: AsyncSession, source_account_id: int, client: int, username: str | None = None
) -> int:
    """Ingest one new message from client `client` (its tg_user_id) in the caller's transaction."""
    result = await ingest_message(
        session,
        source="direct",
        source_account_id=source_account_id,
        tg_chat_id=client,
        tg_message_id=next(_tg_message_ids),
        client=ClientRef(tg_user_id=client, username=username),
        text=CLIENT_TEXT,
    )
    return result.message_id


async def new_message(
    factory: Factory, source_account_id: int, client: int, username: str | None = None
) -> int:
    async with transaction(factory) as s:
        return await add_message(s, source_account_id, client, username)


async def dispatch_all(factory: Factory) -> list[int]:
    return await dispatch_batch(factory, 10_000)


async def claim(factory: Factory, message_id: int, worker: str = "w1", lease: int = 300) -> int:
    """Claim the message for its operator; returns the operator's account id."""
    message = await get(factory, Message, message_id)
    assert message.operator_account_id is not None
    async with transaction(factory) as s:
        await claim_message(s, message_id, message.operator_account_id, worker, lease)
    return message.operator_account_id


async def processing_message(
    factory: Factory, source_account_id: int, client: int = 1, username: str | None = "client"
) -> tuple[int, int]:
    """A new message dispatched and claimed: (message_id, operator account id)."""
    message_id = await new_message(factory, source_account_id, client, username)
    assert message_id in await dispatch_all(factory)
    return message_id, await claim(factory, message_id)


async def queued_reply(factory: Factory, message_id: int, account_id: int, **kw: Any) -> Outbox:
    async with transaction(factory) as s:
        return await enqueue_reply(
            s, message_id=message_id, account_id=account_id, kind="manual", body=REPLY_BODY, **kw
        )


async def get(factory: Factory, model: type[T], key: Any) -> T:
    async with factory() as s:
        return await s.get_one(model, key)


async def rows(factory: Factory, model: type[T], *where: Any) -> list[T]:
    async with factory() as s:
        order = getattr(model, "id", None)
        stmt = select(model).where(*where)
        if order is not None:
            stmt = stmt.order_by(order)
        return list((await s.scalars(stmt)).all())


async def count_rows(factory: Factory, model: type, *where: Any) -> int:
    async with factory() as s:
        return (await s.scalar(select(func.count()).select_from(model).where(*where))) or 0


async def audit_actions(factory: Factory, entity_type: str, entity_id: int) -> list[str]:
    return [
        a.action
        for a in await rows(
            factory, AuditLog, AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id
        )
    ]


async def set_account(factory: Factory, account_id: int, **values: Any) -> None:
    async with transaction(factory) as s:
        account = await s.get_one(Account, account_id)
        for key, value in values.items():
            setattr(account, key, value)


async def idle_in_transaction(factory: Factory) -> int:
    """Other connections to this database sitting inside an open transaction."""
    async with factory() as s:
        return (
            await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
            )
        ) or 0
