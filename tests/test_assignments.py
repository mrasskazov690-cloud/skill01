import pytest
from sqlalchemy import update

from app.db.models import Assignment, AuditLog, Conversation, Message, Outbox
from app.db.session import transaction
from app.domain.errors import AccountNotAssignable, ConversationBusy, NotFound
from app.domain.states import InvalidTransition
from app.services.assignments import assign_manual, reassign_conversation
from app.services.dispatcher import PINNED_OPERATOR_UNAVAILABLE
from app.services.outbox import execute_send
from app.telegram.base import SendTimeout
from app.telegram.fake import FakeTelegramAccount
from tests.helpers import (
    Factory,
    claim,
    dispatch_all,
    get,
    new_message,
    queued_reply,
    rows,
    set_account,
)

ACTOR = "admin:olga"


@pytest.fixture
async def main_id(make_account) -> int:
    return (await make_account(kind="main")).id


@pytest.fixture
async def operators(make_account) -> list[int]:
    return [(await make_account()).id for _ in range(3)]


async def _snapshot(factory: Factory) -> list[tuple]:
    return [(m.id, m.status, m.operator_account_id) for m in await rows(factory, Message)] + [
        (c.id, c.operator_account_id) for c in await rows(factory, Conversation)
    ]


async def test_reassign_moves_pin_and_assigned_messages(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    first, _, third = operators
    m1 = await new_message(session_factory, main_id, client=1)
    m2 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    await set_account(session_factory, first, on_shift=False)
    held = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)

    async with transaction(session_factory) as s:
        moved = await reassign_conversation(s, 1, third, ACTOR)

    assert moved == [m1, m2]
    for message_id in (m1, m2):
        message = await get(session_factory, Message, message_id)
        assert (message.status, message.operator_account_id) == ("assigned", third)
    pending = await get(session_factory, Message, held)
    assert (pending.status, pending.review_reason) == ("pending", None)
    assert (await get(session_factory, Conversation, 1)).operator_account_id == third
    history = [
        (a.message_id, a.from_account_id, a.to_account_id, a.reason, a.actor)
        for a in await rows(session_factory, Assignment, Assignment.actor == ACTOR)
    ]
    assert history == [
        (None, first, third, "manual_reassign", ACTOR),
        (m1, first, third, "manual_reassign", ACTOR),
        (m2, first, third, "manual_reassign", ACTOR),
    ]
    # The dispatcher follows the new pin; it never moves the conversation by itself.
    assert await dispatch_all(session_factory) == [held]
    assert (await get(session_factory, Message, held)).operator_account_id == third


async def test_reassign_to_current_operator_is_a_no_op(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    before = await _snapshot(session_factory)

    async with transaction(session_factory) as s:
        assert await reassign_conversation(s, 1, operators[0], ACTOR) == []

    assert await _snapshot(session_factory) == before


async def test_assign_manual_pins_a_new_conversation(
    session_factory: Factory, main_id: int, make_account
) -> None:
    off_shift = (await make_account(on_shift=False)).id  # allowed for a manual decision
    message_id = await new_message(session_factory, main_id, client=1)

    async with transaction(session_factory) as s:
        assert await assign_manual(s, message_id, off_shift, ACTOR) == [message_id]

    message = await get(session_factory, Message, message_id)
    assert (message.status, message.operator_account_id) == ("assigned", off_shift)
    assert message.assigned_at is not None
    assert (await get(session_factory, Conversation, 1)).operator_account_id == off_shift
    reasons = [(a.message_id, a.reason) for a in await rows(session_factory, Assignment)]
    assert reasons == [(None, "manual_assign"), (message_id, "manual_assign")]
    actions = [a.action for a in await rows(session_factory, AuditLog, AuditLog.actor == ACTOR)]
    assert actions == ["conversation_pinned", "message_assigned"]


async def test_assign_manual_of_held_message_moves_the_conversation(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    first, second, _ = operators
    m1 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    await set_account(session_factory, first, enabled=True, on_shift=False)
    h1 = await new_message(session_factory, main_id, client=1)
    h2 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    assert (await get(session_factory, Message, h2)).review_reason == PINNED_OPERATOR_UNAVAILABLE

    async with transaction(session_factory) as s:
        assert await assign_manual(s, h1, second, ACTOR) == [m1, h1]

    owners = {m.id: (m.status, m.operator_account_id) for m in await rows(session_factory, Message)}
    assert owners == {m1: ("assigned", second), h1: ("assigned", second), h2: ("pending", None)}
    assert await dispatch_all(session_factory) == [h2]
    assert (await get(session_factory, Message, h2)).operator_account_id == second


async def test_processing_message_blocks_reassignment(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    m1 = await new_message(session_factory, main_id, client=1)
    m2 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    await claim(session_factory, m1)
    before = await _snapshot(session_factory)

    with pytest.raises(ConversationBusy):
        async with transaction(session_factory) as s:
            await reassign_conversation(s, 1, operators[1], ACTOR)
    with pytest.raises(ConversationBusy):
        async with transaction(session_factory) as s:
            await assign_manual(s, m2, operators[1], ACTOR)
    with pytest.raises(ConversationBusy):  # same operator, but the target itself is in work
        async with transaction(session_factory) as s:
            await assign_manual(s, m1, operators[0], ACTOR)

    assert await _snapshot(session_factory) == before


async def test_live_send_blocks_reassignment(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    message_id = await new_message(session_factory, main_id, client=1, username="client")
    await dispatch_all(session_factory)
    account_id = await claim(session_factory, message_id)
    outbox = await queued_reply(session_factory, message_id, account_id)
    await execute_send(session_factory, outbox.id, FakeTelegramAccount(account_id, [SendTimeout]))
    assert (await get(session_factory, Message, message_id)).status == "awaiting_review"

    with pytest.raises(ConversationBusy):
        async with transaction(session_factory) as s:
            await reassign_conversation(s, 1, operators[1], ACTOR)
    with pytest.raises(ConversationBusy):
        async with transaction(session_factory) as s:
            await assign_manual(s, message_id, operators[1], ACTOR)
    assert (await get(session_factory, Outbox, outbox.id)).status == "uncertain"


@pytest.mark.parametrize("overrides", [{"kind": "main"}, {"kind": "bot"}, {"enabled": False}])
async def test_target_must_be_an_enabled_operator(
    session_factory: Factory, main_id: int, make_account, overrides: dict
) -> None:
    target = (await make_account(**overrides)).id
    message_id = await new_message(session_factory, main_id, client=1)

    with pytest.raises(AccountNotAssignable):
        async with transaction(session_factory) as s:
            await assign_manual(s, message_id, target, ACTOR)
    with pytest.raises(AccountNotAssignable):
        async with transaction(session_factory) as s:
            await reassign_conversation(s, 1, target, ACTOR)
    assert (await get(session_factory, Message, message_id)).status == "pending"


async def test_unknown_ids_and_terminal_messages(
    session_factory: Factory, main_id: int, operators: list[int]
) -> None:
    message_id = await new_message(session_factory, main_id, client=1)
    with pytest.raises(NotFound):
        async with transaction(session_factory) as s:
            await assign_manual(s, 999, operators[0], ACTOR)
    with pytest.raises(NotFound):
        async with transaction(session_factory) as s:
            await reassign_conversation(s, 999, operators[0], ACTOR)
    with pytest.raises(NotFound):
        async with transaction(session_factory) as s:
            await assign_manual(s, message_id, 999, ACTOR)

    async with transaction(session_factory) as s:  # test setup: a closed message
        await s.execute(update(Message).values(status="cancelled"))
    with pytest.raises(InvalidTransition):
        async with transaction(session_factory) as s:
            await assign_manual(s, message_id, operators[0], ACTOR)
    assert (await get(session_factory, Conversation, 1)).operator_account_id is None
