from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Account, Assignment, AuditLog, Conversation, Message
from app.db.session import transaction
from app.services.dispatcher import PINNED_OPERATOR_UNAVAILABLE, dispatch_batch, dispatch_one
from tests.helpers import (
    CLIENT_TEXT,
    Factory,
    audit_actions,
    dispatch_all,
    get,
    new_message,
    rows,
    set_account,
)


@pytest.fixture
async def main_id(make_account) -> int:
    return (await make_account(kind="main")).id


@pytest.fixture
async def operators(make_account) -> list[int]:
    return [(await make_account()).id for _ in range(3)]


async def _operator_of(factory: Factory, message_id: int) -> int | None:
    return (await get(factory, Message, message_id)).operator_account_id


async def test_round_robin_over_three_operators(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    messages = [await new_message(session_factory, main_id, client) for client in range(1, 10)]

    assert await dispatch_all(session_factory) == messages
    assert [await _operator_of(session_factory, m) for m in messages] == operators * 3
    assignments = await rows(session_factory, Assignment)
    assert [(a.message_id, a.reason, a.actor) for a in assignments] == [
        (m, "auto_round_robin", "dispatcher") for m in messages
    ]
    for conversation in await rows(session_factory, Conversation):
        assert conversation.operator_account_id is not None


async def test_conversation_is_pinned_to_its_first_operator(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    a1 = await new_message(session_factory, main_id, client=1)
    b1 = await new_message(session_factory, main_id, client=2)
    a2 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    a3 = await new_message(session_factory, main_id, client=1)
    c1 = await new_message(session_factory, main_id, client=3)
    await dispatch_all(session_factory)

    first, second, third = operators
    owners = {m: await _operator_of(session_factory, m) for m in (a1, a2, a3, b1, c1)}
    assert owners == {a1: first, a2: first, a3: first, b1: second, c1: third}
    reasons = {a.message_id: a.reason for a in await rows(session_factory, Assignment)}
    assert reasons == {
        a1: "auto_round_robin",
        b1: "auto_round_robin",
        a2: "pinned",
        a3: "pinned",
        c1: "auto_round_robin",
    }


async def test_unavailable_pinned_operator_keeps_messages_pending(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    first = operators[0]
    a1 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    await set_account(session_factory, first, on_shift=False)
    held = [await new_message(session_factory, main_id, client=1) for _ in range(3)]
    b1 = await new_message(session_factory, main_id, client=2)

    # A candidate window smaller than the held backlog must not starve client 2.
    assert await dispatch_batch(session_factory, 100, candidate_limit=1) == [b1]
    assert await dispatch_batch(session_factory, 100, candidate_limit=1) == []
    assert await _operator_of(session_factory, b1) != first
    for message_id in held:
        message = await get(session_factory, Message, message_id)
        assert (message.status, message.operator_account_id, message.review_reason) == (
            "pending",
            None,
            PINNED_OPERATOR_UNAVAILABLE,
        )
        # Audited once, however many passes saw it.
        assert await audit_actions(session_factory, "message", message_id) == [
            "message_ingested",
            "message_held",
        ]
    conversation = (await rows(session_factory, Conversation, Conversation.id == 1))[0]
    assert conversation.operator_account_id == first  # no automatic reassignment

    await set_account(session_factory, first, on_shift=True)
    assert sorted(await dispatch_all(session_factory)) == held
    for message_id in [a1, *held]:
        message = await get(session_factory, Message, message_id)
        assert (message.status, message.operator_account_id, message.review_reason) == (
            "assigned",
            first,
            None,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"on_shift": False},
        {"state": "flood_wait", "flood_wait_until": datetime.now(UTC) + timedelta(hours=1)},
        {"flood_wait_until": datetime.now(UTC) + timedelta(hours=1)},
        {"state": "restricted"},
        {"state": "paused"},
        {"state": "disconnected"},
        {"max_active": 0},
        {"kind": "main"},
        {"kind": "bot"},
    ],
    ids=lambda o: "-".join(f"{k}={v}" for k, v in o.items())[:40],
)
async def test_ineligible_accounts_get_nothing(
    session_factory: Factory, make_account, main_id: int, overrides: dict
) -> None:
    blocked = (await make_account(**overrides)).id
    eligible = (await make_account()).id
    messages = [await new_message(session_factory, main_id, client) for client in range(1, 4)]

    assert await dispatch_all(session_factory) == messages
    assert {await _operator_of(session_factory, m) for m in messages} == {eligible}
    assert blocked != eligible


async def test_expired_flood_wait_does_not_block(
    session_factory: Factory, make_account, main_id: int
) -> None:
    account = await make_account(flood_wait_until=datetime.now(UTC) - timedelta(seconds=1))
    message_id = await new_message(session_factory, main_id, client=1)

    assert await dispatch_all(session_factory) == [message_id]
    assert await _operator_of(session_factory, message_id) == account.id


async def test_max_active_limits_load(session_factory: Factory, make_account, main_id: int) -> None:
    limited = (await make_account(max_active=1)).id
    unlimited = (await make_account()).id
    m1, m2, m3 = [await new_message(session_factory, main_id, c) for c in (1, 2, 3)]
    pinned_extra = await new_message(session_factory, main_id, client=1)

    await dispatch_all(session_factory)

    assert await _operator_of(session_factory, m1) == limited
    assert await _operator_of(session_factory, m2) == unlimited
    assert await _operator_of(session_factory, m3) == unlimited  # limited is full
    # A full pinned operator is "not eligible": the client's next message waits for it.
    extra = await get(session_factory, Message, pinned_extra)
    assert (extra.status, extra.review_reason) == ("pending", PINNED_OPERATOR_UNAVAILABLE)


async def test_no_eligible_operator_leaves_message_pending(
    session_factory: Factory, make_account, main_id: int
) -> None:
    await make_account(on_shift=False)
    message_id = await new_message(session_factory, main_id, client=1)

    async with transaction(session_factory) as s:
        assert await dispatch_one(s) is None
    assert await dispatch_all(session_factory) == []
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == ("pending", None)


async def test_locked_conversation_is_skipped_not_waited_for(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    locked_msg = await new_message(session_factory, main_id, client=1)
    free_msg = await new_message(session_factory, main_id, client=2)
    locked_conv = (await get(session_factory, Message, locked_msg)).conversation_id

    async with transaction(session_factory) as holder:
        await holder.execute(
            select(Conversation.id).where(Conversation.id == locked_conv).with_for_update()
        )
        async with transaction(session_factory) as s:
            assert await dispatch_one(s) == free_msg
        async with transaction(session_factory) as s:
            assert await dispatch_one(s) is None  # only locked work left
        assert await dispatch_batch(session_factory, 10, max_busy_retries=2) == []

    assert await dispatch_all(session_factory) == [locked_msg]


async def test_locked_operator_is_skipped(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    message_id = await new_message(session_factory, main_id, client=1)
    first, second, _ = operators

    async with transaction(session_factory) as holder:
        await holder.execute(select(Account.id).where(Account.id == first).with_for_update())
        assert await dispatch_batch(session_factory, 10) == [message_id]

    assert await _operator_of(session_factory, message_id) == second


async def test_assignment_is_audited_without_client_text(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    message_id = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)

    audit = await rows(session_factory, AuditLog, AuditLog.action == "message_assigned")
    assert [(a.entity_id, a.actor) for a in audit] == [(message_id, "dispatcher")]
    assert audit[0].details == {
        "from": "pending",
        "to": "assigned",
        "conversation_id": 1,
        "operator_account_id": operators[0],
        "reason": "auto_round_robin",
    }
    assert CLIENT_TEXT not in str([a.details for a in await rows(session_factory, AuditLog)])
