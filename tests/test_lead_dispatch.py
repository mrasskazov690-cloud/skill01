"""Lead dispatch: newest first, daily send limit per account, senders alternate."""

from datetime import UTC, datetime, timedelta
from itertools import count
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, update

from app.config import Settings
from app.db.models import Account, Message, Outbox
from app.db.session import transaction
from app.services.claims import claim_message
from app.services.dispatcher import dispatch_one
from app.services.ingest import ClientRef, ingest_message
from app.services.outbox import enqueue_reply, execute_send
from app.telegram.base import PeerInvalid
from app.telegram.fake import FakeTelegramAccount
from app.telegram.links import LeadLink
from tests.helpers import (
    Factory,
    add_message,
    get,
    new_message,
    processing_message,
    queued_reply,
)

_history_clients = count(10_000)


@pytest.fixture
async def main_id(make_account) -> int:
    return (await make_account(kind="main")).id


async def _dispatch(factory: Factory) -> int | None:
    async with transaction(factory) as s:
        return await dispatch_one(s)


async def _operator_of(factory: Factory, message_id: int) -> int | None:
    return (await get(factory, Message, message_id)).operator_account_id


async def _sent_history(
    factory: Factory, main_id: int, account_id: int, sent_at: list[datetime]
) -> None:
    """Answered leads with a sent reply from `account_id` at the given times."""
    for when in sent_at:
        message_id = await new_message(factory, main_id, client=next(_history_clients))
        async with transaction(factory) as s:
            await s.execute(
                update(Message).where(Message.id == message_id).values(status="answered")
            )
            await s.execute(
                insert(Outbox).values(
                    message_id=message_id,
                    account_id=account_id,
                    idempotency_key=f"history:{message_id}",
                    kind="manual",
                    body="reply",
                    status="sent",
                    sent_at=when,
                )
            )


def _day_start(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)


async def test_newest_lead_is_dispatched_first(
    session_factory: Factory, main_id: int, make_account, settings: Settings
) -> None:
    assert settings.DISPATCH_ORDER == "newest_first"  # the default
    await make_account()
    leads = [await new_message(session_factory, main_id, client) for client in range(1, 5)]

    assert [await _dispatch(session_factory) for _ in leads] == leads[::-1]


async def test_newest_first_breaks_ties_by_id(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    async with transaction(session_factory) as s:  # one transaction: equal received_at
        leads = [await add_message(s, main_id, client) for client in (1, 2, 3)]

    assert [await _dispatch(session_factory) for _ in leads] == leads[::-1]


async def test_oldest_first_is_still_available(
    session_factory: Factory, main_id: int, make_account, oldest_first: None
) -> None:
    await make_account()
    leads = [await new_message(session_factory, main_id, client) for client in range(1, 4)]
    assert [await _dispatch(session_factory) for _ in leads] == leads


async def test_daily_limit_takes_the_account_out_of_rotation(
    session_factory: Factory, main_id: int, make_account
) -> None:
    limited = (await make_account(daily_send_limit=2)).id
    other = (await make_account()).id
    now = datetime.now(UTC)
    await _sent_history(session_factory, main_id, limited, [now - timedelta(seconds=2)])

    first = await new_message(session_factory, main_id, client=1)
    assert await _dispatch(session_factory) == first
    assert await _operator_of(session_factory, first) == limited  # 1 of 2 sent today

    await _sent_history(session_factory, main_id, limited, [now - timedelta(seconds=1)])
    leads = [await new_message(session_factory, main_id, client) for client in (2, 3, 4)]
    for _ in leads:
        await _dispatch(session_factory)
    assert {await _operator_of(session_factory, m) for m in leads} == {other}


async def test_global_daily_limit_and_zero(
    session_factory: Factory, main_id: int, make_account, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_DAILY_LIMIT", 1)
    used = (await make_account()).id
    blocked = (await make_account(daily_send_limit=0)).id  # explicit 0 beats the default
    fresh = (await make_account()).id
    await _sent_history(session_factory, main_id, used, [datetime.now(UTC)])

    leads = [await new_message(session_factory, main_id, client) for client in (1, 2)]
    for _ in leads:
        await _dispatch(session_factory)
    assert {await _operator_of(session_factory, m) for m in leads} == {fresh}
    assert blocked != fresh

    monkeypatch.setattr(settings, "ACCOUNT_DAILY_LIMIT", 2)
    lead = await new_message(session_factory, main_id, client=3)
    assert await _dispatch(session_factory) == lead
    assert await _operator_of(session_factory, lead) == used


async def test_day_starts_at_midnight_in_the_configured_time_zone(
    session_factory: Factory, main_id: int, make_account, monkeypatch, settings: Settings
) -> None:
    assert settings.TIMEZONE == "Europe/Moscow"  # the default
    # Midnight comes at different instants in the two zones; a send one minute before
    # the later midnight is "today" in the other zone and "yesterday" in this one.
    earlier, later = sorted(("Europe/Moscow", "Asia/Tokyo"), key=_day_start)
    account = (await make_account(daily_send_limit=1)).id
    sent_at = _day_start(later) - timedelta(minutes=1)
    await _sent_history(session_factory, main_id, account, [sent_at])
    lead = await new_message(session_factory, main_id, client=1)

    monkeypatch.setattr(settings, "TIMEZONE", earlier)
    assert await _dispatch(session_factory) is None  # the limit is used up today

    monkeypatch.setattr(settings, "TIMEZONE", later)
    assert await _dispatch(session_factory) == lead


async def test_only_sent_replies_count_toward_the_limit(
    session_factory: Factory, main_id: int, make_account
) -> None:
    account = (await make_account(daily_send_limit=1)).id
    await _sent_history(session_factory, main_id, account, [datetime.now(UTC)])
    async with transaction(session_factory) as s:
        await s.execute(update(Outbox).values(status="failed"))

    lead = await new_message(session_factory, main_id, client=1)
    assert await _dispatch(session_factory) == lead


async def _tick(factory: Factory, main_id: int, client: int, adapters: dict[int, object]) -> int:
    """One sender tick: a new lead is dispatched, claimed, queued and sent."""
    await new_message(factory, main_id, client)
    async with transaction(factory) as s:
        message_id = await dispatch_one(s)
        assert message_id is not None
        account_id = (await s.get_one(Message, message_id)).operator_account_id
        assert account_id is not None
        await claim_message(s, message_id, account_id, "sender")
        outbox = await enqueue_reply(
            s, message_id=message_id, account_id=account_id, kind="template", body="Здравствуйте!"
        )
    assert await execute_send(factory, outbox.id, adapters[account_id]) == "sent"  # type: ignore[arg-type]
    return account_id


async def test_senders_alternate_a_b_a_b(
    session_factory: Factory, main_id: int, make_account
) -> None:
    a, b = (await make_account()).id, (await make_account()).id
    adapters = {a: FakeTelegramAccount(a), b: FakeTelegramAccount(b)}

    senders = [await _tick(session_factory, main_id, client, adapters) for client in range(1, 7)]

    assert senders == [a, b, a, b, a, b]
    first, second = await get(session_factory, Account, a), await get(session_factory, Account, b)
    assert first.last_sent_at is not None and second.last_sent_at is not None
    assert second.last_sent_at > first.last_sent_at  # b sent last


async def test_turn_follows_last_sent_at_not_last_assignment(
    session_factory: Factory, main_id: int, make_account
) -> None:
    now = datetime.now(UTC)
    sent_recently = (
        await make_account(last_sent_at=now - timedelta(minutes=1), last_assigned_at=now)
    ).id
    sent_long_ago = (
        await make_account(
            last_sent_at=now - timedelta(hours=1), last_assigned_at=now + timedelta(seconds=1)
        )
    ).id
    lead = await new_message(session_factory, main_id, client=1)

    assert await _dispatch(session_factory) == lead
    assert await _operator_of(session_factory, lead) == sent_long_ago
    assert sent_recently != sent_long_ago


async def test_last_sent_at_is_set_only_by_a_successful_send(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    message_id, account_id = await processing_message(session_factory, main_id, client=1)
    outbox = await queued_reply(session_factory, message_id, account_id)
    failing = FakeTelegramAccount(account_id, [PeerInvalid()])

    assert await execute_send(session_factory, outbox.id, failing) == "failed"
    assert (await get(session_factory, Account, account_id)).last_sent_at is None

    message_id, account_id = await processing_message(session_factory, main_id, client=2)
    outbox = await queued_reply(session_factory, message_id, account_id)
    assert await execute_send(session_factory, outbox.id, FakeTelegramAccount(account_id)) == "sent"
    assert (await get(session_factory, Account, account_id)).last_sent_at is not None


async def test_outbox_gets_a_random_id_and_the_peer_its_source_link(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    link = LeadLink(kind="message", chat_username="flats", message_id=7, url="t.me/flats/7")
    async with transaction(session_factory) as s:
        result = await ingest_message(
            s,
            source="work_chat",
            source_account_id=main_id,
            tg_chat_id=-100,
            tg_message_id=1,
            client=ClientRef(tg_user_id=42, username="anna"),
            text="заявка",
            link=link,
        )
    assert await _dispatch(session_factory) == result.message_id
    async with transaction(session_factory) as s:
        account_id = (await s.get_one(Message, result.message_id)).operator_account_id
        assert account_id is not None
        await claim_message(s, result.message_id, account_id, "w")
    first = await queued_reply(session_factory, result.message_id, account_id)
    assert first.random_id is not None and 0 < first.random_id < 2**63

    adapter = FakeTelegramAccount(account_id)
    assert await execute_send(session_factory, first.id, adapter) == "sent"
    peer, _ = adapter.sent[0]
    assert (peer.tg_user_id, peer.username, peer.source) == (42, "anna", link)
