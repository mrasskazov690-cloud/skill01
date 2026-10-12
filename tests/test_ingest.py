import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AuditLog, Client, Conversation, Message
from app.db.session import transaction
from app.services.ingest import ClientRef, IngestResult, ingest_message

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def main_account_id(make_account) -> int:
    return (await make_account(kind="main")).id


async def _ingest(factory: Factory, account_id: int, **overrides: Any) -> IngestResult:
    params: dict[str, Any] = {
        "source": "direct",
        "source_account_id": account_id,
        "tg_chat_id": 500,
        "tg_message_id": 1,
        "client": ClientRef(tg_user_id=500, username="Client500"),
        "text": "Сколько стоит?",
        "meta": {"link": "https://t.me/example"},
        "source_chat_id": 500,
    } | overrides
    async with transaction(factory) as s:
        return await ingest_message(s, **params)


async def _count(factory: Factory, model: type) -> int:
    async with factory() as s:
        return (await s.scalar(select(func.count()).select_from(model))) or 0


async def _clients(factory: Factory) -> list[tuple[int | None, str | None]]:
    async with factory() as s:
        rows = await s.execute(select(Client.tg_user_id, Client.username).order_by(Client.id))
        return [(r.tg_user_id, r.username) for r in rows]


async def test_sequential_duplicate_returns_same_id(
    session_factory: Factory, main_account_id: int
) -> None:
    first = await _ingest(session_factory, main_account_id)
    second = await _ingest(session_factory, main_account_id, text="другой текст повторной доставки")

    assert first.created and not second.created
    assert first.message_id == second.message_id
    assert await _count(session_factory, Message) == 1
    async with session_factory() as s:
        message = await s.get_one(Message, first.message_id)
        assert (message.status, message.text, message.meta) == (
            "pending",
            "Сколько стоит?",
            {"link": "https://t.me/example"},
        )
        audit = (await s.scalars(select(AuditLog))).all()
    assert [(a.action, a.entity_type, a.entity_id) for a in audit] == [
        ("message_ingested", "message", first.message_id)
    ]
    assert "text" not in audit[0].details


async def test_concurrent_identical_ingests_create_one_row(
    session_factory: Factory, main_account_id: int
) -> None:
    results = await asyncio.gather(*(_ingest(session_factory, main_account_id) for _ in range(20)))

    assert len({r.message_id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    for model in (Message, Client, Conversation, AuditLog):
        assert await _count(session_factory, model) == 1


async def test_concurrent_messages_of_new_client_share_one_conversation(
    session_factory: Factory, main_account_id: int
) -> None:
    results = await asyncio.gather(
        *(_ingest(session_factory, main_account_id, tg_message_id=i) for i in range(20))
    )

    assert all(r.created for r in results)
    assert len({r.message_id for r in results}) == 20
    assert await _count(session_factory, Client) == 1
    assert await _count(session_factory, Conversation) == 1
    async with session_factory() as s:
        conversation_ids = set(await s.scalars(select(Message.conversation_id)))
    assert len(conversation_ids) == 1


async def test_idempotency_key_includes_account_and_chat(
    session_factory: Factory, main_account_id: int, make_account
) -> None:
    other_account_id = (await make_account(kind="operator")).id
    ids = {
        (await _ingest(session_factory, main_account_id)).message_id,
        (await _ingest(session_factory, main_account_id, tg_chat_id=501)).message_id,
        (await _ingest(session_factory, other_account_id)).message_id,
    }
    assert len(ids) == 3


async def test_different_clients_get_different_conversations(
    session_factory: Factory, main_account_id: int
) -> None:
    a = await _ingest(
        session_factory, main_account_id, client=ClientRef(tg_user_id=1), tg_message_id=1
    )
    b = await _ingest(
        session_factory, main_account_id, client=ClientRef(tg_user_id=2), tg_message_id=2
    )
    c = await _ingest(
        session_factory, main_account_id, client=ClientRef(username="third"), tg_message_id=3
    )

    async with session_factory() as s:
        conversations = {
            m.id: m.conversation_id
            for m in await s.scalars(
                select(Message).where(Message.id.in_([a.message_id, b.message_id, c.message_id]))
            )
        }
    assert len(set(conversations.values())) == 3
    assert await _count(session_factory, Client) == 3


async def test_username_is_lowercased_and_matched_case_insensitively(
    session_factory: Factory, main_account_id: int
) -> None:
    await _ingest(
        session_factory, main_account_id, client=ClientRef(username=" @Some_User "), tg_message_id=1
    )
    await _ingest(
        session_factory, main_account_id, client=ClientRef(username="SOME_user"), tg_message_id=2
    )

    assert await _clients(session_factory) == [(None, "some_user")]
    assert await _count(session_factory, Conversation) == 1


async def test_same_tg_user_id_is_same_client_even_if_username_changed(
    session_factory: Factory, main_account_id: int
) -> None:
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=7, username="old"),
        tg_message_id=1,
    )
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=7, username="new"),
        tg_message_id=2,
    )
    await _ingest(session_factory, main_account_id, client=ClientRef(tg_user_id=7), tg_message_id=3)

    # The current username replaces the stored one; a message without one keeps it.
    assert await _clients(session_factory) == [(7, "new")]
    assert await _count(session_factory, Conversation) == 1


async def test_released_username_moves_to_its_new_owner(
    session_factory: Factory, main_account_id: int
) -> None:
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=55, username="dave"),
        tg_message_id=1,
    )
    # Telegram user 55 gave up "dave" and user 77 took it.
    for message_id in (2, 3):
        await _ingest(
            session_factory,
            main_account_id,
            client=ClientRef(tg_user_id=77, username="@Dave"),
            tg_message_id=message_id,
        )

    assert await _clients(session_factory) == [(55, None), (77, "dave")]
    assert await _count(session_factory, Conversation) == 2


async def test_username_only_holder_keeps_its_username(
    session_factory: Factory, main_account_id: int
) -> None:
    await _ingest(
        session_factory, main_account_id, client=ClientRef(username="erin"), tg_message_id=1
    )
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=66, username="frank"),
        tg_message_id=2,
    )
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=66, username="erin"),
        tg_message_id=3,
    )

    # It may be the same person; not merged here, and nothing is taken from it.
    assert await _clients(session_factory) == [(None, "erin"), (66, "frank")]
    assert await _count(session_factory, Message) == 3


async def test_username_only_client_gets_tg_user_id_attached(
    session_factory: Factory, main_account_id: int
) -> None:
    # First seen through a work-chat link (username only), then writes directly.
    await _ingest(
        session_factory,
        main_account_id,
        source="work_chat",
        client=ClientRef(username="bob"),
        tg_message_id=1,
    )
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=22, username="@Bob"),
        tg_message_id=2,
    )
    await _ingest(
        session_factory, main_account_id, client=ClientRef(tg_user_id=22), tg_message_id=3
    )

    assert await _clients(session_factory) == [(22, "bob")]
    assert await _count(session_factory, Conversation) == 1


async def test_username_owned_by_another_tg_user_is_not_merged(
    session_factory: Factory, main_account_id: int
) -> None:
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=33, username="carol"),
        tg_message_id=1,
    )
    await _ingest(
        session_factory,
        main_account_id,
        client=ClientRef(tg_user_id=44, username="carol"),
        tg_message_id=2,
    )

    assert await _clients(session_factory) == [(33, "carol"), (44, None)]
    assert await _count(session_factory, Conversation) == 2


async def test_concurrent_username_and_tg_user_id_converge(
    session_factory: Factory, main_account_id: int
) -> None:
    refs = [
        ClientRef(username="dave"),
        ClientRef(tg_user_id=55, username="Dave"),
        ClientRef(tg_user_id=55),
    ] * 7
    await asyncio.gather(
        *(
            _ingest(session_factory, main_account_id, client=ref, tg_message_id=i)
            for i, ref in enumerate(refs)
        )
    )
    clients = set(await _clients(session_factory))
    # Either the username row got the id attached, or an id-only row was created first
    # (a message without username cannot be linked to a username-only client).
    assert clients in ({(55, "dave")}, {(55, None), (None, "dave")})
    assert await _count(session_factory, Message) == len(refs)


async def test_last_activity_moves_only_for_new_messages(
    session_factory: Factory, main_account_id: int
) -> None:
    async def last_activity() -> Any:
        async with session_factory() as s:
            return await s.scalar(select(Conversation.last_activity_at))

    await _ingest(session_factory, main_account_id, tg_message_id=1)
    t1 = await last_activity()
    await _ingest(session_factory, main_account_id, tg_message_id=1)
    assert await last_activity() == t1
    await _ingest(session_factory, main_account_id, tg_message_id=2)
    assert await last_activity() > t1


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"client": ClientRef()}, ValueError),
        ({"client": ClientRef(username=" @ ")}, ValueError),
        ({"source": "email"}, ValueError),
    ],
)
async def test_invalid_input_is_rejected(
    session_factory: Factory, main_account_id: int, overrides: dict, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await _ingest(session_factory, main_account_id, **overrides)
    assert await _count(session_factory, Message) == 0
