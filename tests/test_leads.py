"""Lead duplicates: never write to a client twice; same text matters only without an author."""

import pytest
from sqlalchemy import update

from app.db.models import Client, Conversation, Message, Outbox
from app.db.session import transaction
from app.services.ingest import ClientRef, ingest_message
from app.services.leads import (
    DUPLICATE_CLIENT,
    DUPLICATE_TEXT,
    classify_duplicate,
    normalize_text,
    text_hash,
)
from app.telegram.links import LeadLink
from tests.helpers import Factory, get, processing_message, queued_reply

LEAD = "Сдаётся 2-к квартира, Ёлки!  Звоните..."
LINK = LeadLink(kind="message", chat_username="flats", message_id=77, url="https://t.me/flats/77")


@pytest.fixture
async def main_id(make_account) -> int:
    return (await make_account(kind="main")).id


async def _lead(
    factory: Factory,
    main_id: int,
    tg_message_id: int,
    client: ClientRef,
    text: str = LEAD,
    link: LeadLink | None = None,
) -> Message:
    async with transaction(factory) as s:
        result = await ingest_message(
            s,
            source="work_chat",
            source_account_id=main_id,
            tg_chat_id=-100,
            tg_message_id=tg_message_id,
            client=client,
            text=text,
            link=link,
        )
    return await get(factory, Message, result.message_id)


async def _client_id(factory: Factory, message: Message) -> int:
    return (await get(factory, Conversation, message.conversation_id)).client_id


async def _classify(factory: Factory, message: Message) -> str | None:
    client_id = await _client_id(factory, message)
    async with factory() as s:
        return await classify_duplicate(s, client_id, message.text_hash, message_id=message.id)


def _unknown(tg_message_id: int) -> ClientRef:
    return ClientRef(external_key=f"msg:-100/{tg_message_id}")


async def _set_status(factory: Factory, message_id: int, status: str) -> None:
    async with transaction(factory) as s:
        await s.execute(update(Message).where(Message.id == message_id).values(status=status))


def test_normalize_text() -> None:
    assert normalize_text(LEAD) == "сдается 2 к квартира елки звоните"
    assert normalize_text("  ЁЖ_ик!!\n 42 ") == "еж ик 42"
    assert text_hash(LEAD) == text_hash("сдается 2 к   квартира, елки звоните")
    assert text_hash(LEAD) != text_hash("сдается 3 к квартира елки звоните")
    assert len(text_hash(LEAD) or "") == 64
    assert text_hash(" ...!? ") is None  # nothing to compare


async def test_ingest_stores_hash_link_and_external_client(
    session_factory: Factory, main_id: int
) -> None:
    first = await _lead(session_factory, main_id, 1, _unknown(1), link=LINK)
    again = await _lead(session_factory, main_id, 2, _unknown(1))

    assert first.text_hash == text_hash(LEAD)
    assert first.link == {
        "kind": "message",
        "chat_username": "flats",
        "chat_internal_id": None,
        "message_id": 77,
        "username": None,
        "user_id": None,
        "url": "https://t.me/flats/77",
    }
    client = await get(session_factory, Client, await _client_id(session_factory, first))
    assert (client.tg_user_id, client.username, client.external_key) == (None, None, "msg:-100/1")
    assert again.conversation_id == first.conversation_id  # same key, same client


async def test_never_again_after_a_sent_reply_from_any_account(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    await make_account()
    old, account = await processing_message(session_factory, main_id, client=5, username="anna")
    outbox = await queued_reply(session_factory, old, account)
    async with transaction(session_factory) as s:
        await s.execute(update(Outbox).where(Outbox.id == outbox.id).values(status="sent"))
    await _set_status(session_factory, old, "answered")

    new = await _lead(session_factory, main_id, 50, ClientRef(tg_user_id=5), text="другой текст")
    assert await _classify(session_factory, new) == DUPLICATE_CLIENT


async def test_failed_send_alone_is_not_never_again(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    old, account = await processing_message(session_factory, main_id, client=5)
    outbox = await queued_reply(session_factory, old, account)
    async with transaction(session_factory) as s:
        await s.execute(update(Outbox).where(Outbox.id == outbox.id).values(status="failed"))
    await _set_status(session_factory, old, "cancelled")

    new = await _lead(session_factory, main_id, 50, ClientRef(tg_user_id=5))
    assert await _classify(session_factory, new) is None


@pytest.mark.parametrize(
    ("other_status", "expected"),
    [
        ("assigned", DUPLICATE_CLIENT),
        ("processing", DUPLICATE_CLIENT),
        ("awaiting_review", DUPLICATE_CLIENT),
        ("pending", None),
        ("cancelled", None),
    ],
)
async def test_another_lead_of_the_client_in_work(
    session_factory: Factory, main_id: int, other_status: str, expected: str | None
) -> None:
    other = await _lead(session_factory, main_id, 1, ClientRef(tg_user_id=9), text="первая")
    await _set_status(session_factory, other.id, other_status)
    new = await _lead(session_factory, main_id, 2, ClientRef(tg_user_id=9), text="вторая")

    assert await _classify(session_factory, new) == expected


async def test_the_lead_itself_does_not_make_it_a_duplicate(
    session_factory: Factory, main_id: int
) -> None:
    lead = await _lead(session_factory, main_id, 1, ClientRef(tg_user_id=9))
    await _set_status(session_factory, lead.id, "assigned")
    assert await _classify(session_factory, lead) is None


async def test_same_text_is_a_duplicate_only_when_the_author_is_unknown(
    session_factory: Factory, main_id: int
) -> None:
    known = await _lead(session_factory, main_id, 1, ClientRef(tg_user_id=1))
    unknown = await _lead(session_factory, main_id, 2, _unknown(2), text=LEAD.upper() + " !")
    assert await _classify(session_factory, unknown) == DUPLICATE_TEXT

    async with session_factory() as s:  # no client at all: the author is unknown too
        assert await classify_duplicate(s, None, unknown.text_hash) == DUPLICATE_TEXT
    # A known author is never a text duplicate, even of an unknown author's lead.
    assert await _classify(session_factory, known) is None


async def test_different_authors_with_the_same_text_are_different_clients(
    session_factory: Factory, main_id: int
) -> None:
    first = await _lead(session_factory, main_id, 1, ClientRef(tg_user_id=1))
    second = await _lead(session_factory, main_id, 2, ClientRef(tg_user_id=2))
    third = await _lead(session_factory, main_id, 3, ClientRef(username="vera"))

    assert len({first.conversation_id, second.conversation_id, third.conversation_id}) == 3
    for lead in (first, second, third):
        assert await _classify(session_factory, lead) is None


async def test_cancelled_same_text_does_not_count(session_factory: Factory, main_id: int) -> None:
    old = await _lead(session_factory, main_id, 1, _unknown(1))
    await _set_status(session_factory, old.id, "cancelled")
    new = await _lead(session_factory, main_id, 2, _unknown(2))

    assert await _classify(session_factory, new) is None
    assert await _classify(session_factory, old) == DUPLICATE_TEXT  # new one is not cancelled


async def test_unknown_author_with_different_text_is_not_a_duplicate(
    session_factory: Factory, main_id: int
) -> None:
    await _lead(session_factory, main_id, 1, _unknown(1))
    new = await _lead(session_factory, main_id, 2, _unknown(2), text="совсем другая заявка")
    assert await _classify(session_factory, new) is None
