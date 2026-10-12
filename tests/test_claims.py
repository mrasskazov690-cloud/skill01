from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import AuditLog, Message, Outbox
from app.db.session import transaction
from app.domain.errors import NotFound, NotOwner, SendInFlight, WrongState
from app.domain.states import InvalidTransition
from app.services.claims import (
    SEND_IN_FLIGHT_ON_RECOVERY,
    claim_message,
    recover_stale,
    release,
)
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
)


@pytest.fixture
async def assigned(session_factory: Factory, make_account) -> tuple[int, int]:
    """(message_id, operator_id) of a dispatched, not yet claimed message."""
    main_id = (await make_account(kind="main")).id
    operator_id = (await make_account()).id
    message_id = await new_message(session_factory, main_id, client=1, username="client")
    await dispatch_all(session_factory)
    return message_id, operator_id


async def _recover(factory: Factory, after: timedelta = timedelta(seconds=301)) -> int:
    """recover_stale as seen `after` from now; helpers claim with a 300 s lease."""
    async with transaction(factory) as s:
        return await recover_stale(s, datetime.now(UTC) + after)


async def test_claim_moves_to_processing_with_lease(
    session_factory: Factory, assigned: tuple[int, int]
) -> None:
    message_id, operator_id = assigned
    async with transaction(session_factory) as s:
        expires = await claim_message(s, message_id, operator_id, "bot-1", lease_seconds=120)

    message = await get(session_factory, Message, message_id)
    assert (message.status, message.claimed_by, message.lease_expires_at) == (
        "processing",
        "bot-1",
        expires,
    )
    assert timedelta(seconds=119) < expires - message.updated_at <= timedelta(seconds=121)
    audit = (await rows(session_factory, AuditLog, AuditLog.action == "message_claimed"))[0]
    assert (audit.actor, audit.entity_id) == ("worker:bot-1", message_id)
    assert audit.details["to"] == "processing"


async def test_claim_requires_own_assigned_message(
    session_factory: Factory, assigned: tuple[int, int], make_account
) -> None:
    message_id, operator_id = assigned
    other = (await make_account()).id

    with pytest.raises(NotOwner):
        async with transaction(session_factory) as s:
            await claim_message(s, message_id, other, "bot-1")
    with pytest.raises(NotFound):
        async with transaction(session_factory) as s:
            await claim_message(s, 999, operator_id, "bot-1")
    await claim(session_factory, message_id)
    with pytest.raises(InvalidTransition):  # already processing
        async with transaction(session_factory) as s:
            await claim_message(s, message_id, operator_id, "bot-2")
    assert (await get(session_factory, Message, message_id)).claimed_by == "w1"


async def test_release_returns_to_assigned(
    session_factory: Factory, assigned: tuple[int, int]
) -> None:
    message_id, _ = assigned
    with pytest.raises(WrongState):
        async with transaction(session_factory) as s:
            await release(s, message_id, "w1")
    await claim(session_factory, message_id, worker="w1")
    with pytest.raises(NotOwner):
        async with transaction(session_factory) as s:
            await release(s, message_id, "w2")

    async with transaction(session_factory) as s:
        await release(s, message_id, "w1")

    message = await get(session_factory, Message, message_id)
    assert (message.status, message.claimed_by, message.lease_expires_at) == (
        "assigned",
        None,
        None,
    )


async def test_release_refuses_with_live_send(
    session_factory: Factory, assigned: tuple[int, int]
) -> None:
    message_id, operator_id = assigned
    await claim(session_factory, message_id)
    await queued_reply(session_factory, message_id, operator_id)

    with pytest.raises(SendInFlight):
        async with transaction(session_factory) as s:
            await release(s, message_id, "w1")


async def test_recover_stale_returns_expired_claims(
    session_factory: Factory, assigned: tuple[int, int]
) -> None:
    message_id, _ = assigned
    await claim(session_factory, message_id)

    assert await _recover(session_factory, after=timedelta(seconds=299)) == 0
    assert (await get(session_factory, Message, message_id)).status == "processing"
    assert await _recover(session_factory) == 1

    message = await get(session_factory, Message, message_id)
    assert (message.status, message.claimed_by, message.lease_expires_at) == (
        "assigned",
        None,
        None,
    )
    assert await _recover(session_factory) == 0


async def test_recover_stale_with_send_in_flight_goes_to_review(
    session_factory: Factory, assigned: tuple[int, int]
) -> None:
    message_id, operator_id = assigned
    await claim(session_factory, message_id)
    outbox = await queued_reply(session_factory, message_id, operator_id)
    probe: dict[str, int] = {}

    class RecoveringAdapter(FakeTelegramAccount):
        async def send_text(self, peer, text):  # type: ignore[no-untyped-def]
            # The lease runs out while the API call is in flight.
            probe["recovered"] = await _recover(session_factory)
            raise SendTimeout

    await execute_send(session_factory, outbox.id, RecoveringAdapter(operator_id))

    assert probe == {"recovered": 1}
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == (
        "awaiting_review",
        SEND_IN_FLIGHT_ON_RECOVERY,
    )
    assert (await get(session_factory, Outbox, outbox.id)).status == "uncertain"


async def test_recover_stale_cancels_a_reply_that_was_never_sent(
    session_factory: Factory, assigned: tuple[int, int]
) -> None:
    message_id, operator_id = assigned
    await claim(session_factory, message_id)
    outbox = await queued_reply(session_factory, message_id, operator_id)

    assert await _recover(session_factory) == 1

    assert (await get(session_factory, Message, message_id)).status == "assigned"
    row = await get(session_factory, Outbox, outbox.id)
    assert (row.status, row.error, row.attempts) == ("failed", "lease_expired", 0)
