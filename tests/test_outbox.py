import asyncio
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import select, text, update

from app.db.models import Account, AuditLog, Message, Outbox
from app.db.session import transaction
from app.domain.errors import (
    ConversationBusy,
    IdempotencyConflict,
    NotOwner,
    SendInFlight,
    WrongState,
)
from app.logging import configure_logging
from app.services.assignments import reassign_conversation
from app.services.claims import recover_stale
from app.services.outbox import (
    enqueue_reply,
    execute_send,
    expire_flood_waits,
    recover_interrupted_sends,
    resolve_uncertain,
)
from app.telegram.base import (
    FloodWait,
    PeerFlood,
    PeerInvalid,
    PeerRef,
    PermanentSendError,
    SendTimeout,
    SentMessage,
)
from app.telegram.fake import FakeTelegramAccount
from tests.helpers import (
    CLIENT_TEXT,
    REPLY_BODY,
    Factory,
    claim,
    count_rows,
    dispatch_all,
    get,
    idle_in_transaction,
    new_message,
    processing_message,
    queued_reply,
    rows,
)


class Crash(BaseException):
    """Stands in for the process dying in the middle of the API call."""


@pytest.fixture
async def main_id(make_account) -> int:
    return (await make_account(kind="main")).id


@pytest.fixture
async def work(session_factory: Factory, main_id: int, make_account) -> tuple[int, int]:
    """(message_id, operator_id): a client's message claimed by its operator."""
    await make_account()
    return await processing_message(session_factory, main_id)


async def _send(factory: Factory, outbox_id: int, adapter: FakeTelegramAccount, **kw):  # type: ignore[no-untyped-def]
    return await execute_send(factory, outbox_id, adapter, **kw)


async def _until(predicate, what: str) -> None:  # type: ignore[no-untyped-def]
    for _ in range(500):
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def test_successful_send(session_factory: Factory, work: tuple[int, int]) -> None:
    message_id, operator_id = work
    queued = await queued_reply(session_factory, message_id, operator_id)
    assert (queued.status, queued.idempotency_key, queued.attempts) == (
        "queued",
        f"msg:{message_id}:reply",
        0,
    )
    adapter = FakeTelegramAccount(operator_id, [SentMessage(777)])

    assert await _send(session_factory, queued.id, adapter) == "sent"

    assert adapter.sent == [(PeerRef(tg_user_id=1, username="client"), REPLY_BODY)]
    row = await get(session_factory, Outbox, queued.id)
    assert (row.status, row.tg_sent_message_id, row.attempts, row.error) == ("sent", 777, 1, None)
    assert row.sent_at is not None
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.claimed_by, message.lease_expires_at) == (
        "answered",
        None,
        None,
    )
    assert message.answered_at is not None
    assert await _send(session_factory, queued.id, adapter) is None  # never twice
    assert len(adapter.calls) == 1


async def test_sending_is_committed_before_the_api_call(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    seen: dict[str, object] = {}

    class Probe(FakeTelegramAccount):
        async def send_text(self, peer, text):  # type: ignore[no-untyped-def]
            row = await get(session_factory, Outbox, outbox_id)
            seen["row"] = (row.status, row.attempts)
            seen["open_transactions"] = await idle_in_transaction(session_factory)
            return await super().send_text(peer, text)

    assert await _send(session_factory, outbox_id, Probe(operator_id)) == "sent"
    assert seen == {"row": ("sending", 1), "open_transactions": 0}


async def test_timeout_is_uncertain_and_never_retried(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [SendTimeout])

    assert await _send(session_factory, outbox_id, adapter) == "uncertain"

    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.error, row.attempts) == ("uncertain", "send_timeout", 1)
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == ("awaiting_review", "send_uncertain")
    for _ in range(3):
        assert await _send(session_factory, outbox_id, adapter) is None
    async with transaction(session_factory) as s:
        assert await recover_stale(s) == 0
    with pytest.raises(SendInFlight):  # no second reply while the first may be delivered
        async with transaction(session_factory) as s:
            await enqueue_reply(
                s,
                message_id=message_id,
                account_id=operator_id,
                kind="manual",
                body="ещё раз",
                idempotency_key="retry",
            )
    assert len(adapter.calls) == 1
    assert await count_rows(session_factory, Outbox) == 1


async def test_unexpected_adapter_error_is_treated_as_uncertain(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id

    adapter = FakeTelegramAccount(operator_id, [ConnectionResetError("x")])
    assert await _send(session_factory, outbox_id, adapter) == "uncertain"
    assert (await get(session_factory, Outbox, outbox_id)).error == "connection_reset_error"


async def test_resolve_uncertain_as_sent(session_factory: Factory, work: tuple[int, int]) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    await _send(session_factory, outbox_id, FakeTelegramAccount(operator_id, [SendTimeout]))

    async with transaction(session_factory) as s:
        await resolve_uncertain(s, outbox_id, "sent", "admin:olga")

    assert (await get(session_factory, Outbox, outbox_id)).status == "sent"
    assert (await get(session_factory, Message, message_id)).status == "answered"
    with pytest.raises(WrongState):
        async with transaction(session_factory) as s:
            await resolve_uncertain(s, outbox_id, "not_sent", "admin:olga")


async def test_resolve_uncertain_as_not_sent_allows_a_new_send(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    first = (await queued_reply(session_factory, message_id, operator_id)).id
    await _send(session_factory, first, FakeTelegramAccount(operator_id, [SendTimeout]))

    async with transaction(session_factory) as s:
        await resolve_uncertain(s, first, "not_sent", "admin:olga")

    row = await get(session_factory, Outbox, first)
    assert (row.status, row.error) == ("failed", "not_sent")
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.operator_account_id) == ("assigned", operator_id)
    await claim(session_factory, message_id)
    # The old key still names the old row; a new attempt needs a new key.
    assert (await queued_reply(session_factory, message_id, operator_id)).id == first
    second = (await queued_reply(session_factory, message_id, operator_id, idempotency_key="r2")).id
    assert second != first
    assert await _send(session_factory, second, FakeTelegramAccount(operator_id)) == "sent"
    assert (await get(session_factory, Message, message_id)).status == "answered"


async def test_flood_wait_requeues_and_pauses_the_account(
    session_factory: Factory, work: tuple[int, int], main_id: int, make_account
) -> None:
    message_id, operator_id = work
    spare = (await make_account()).id
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [FloodWait(30)])

    assert await _send(session_factory, outbox_id, adapter) == "queued"

    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.attempts, row.error) == ("queued", 1, "flood_wait")
    account = await get(session_factory, Account, operator_id)
    assert (account.state, account.flood_wait_until) == ("flood_wait", row.not_before)
    assert row.not_before is not None
    message = await get(session_factory, Message, message_id)
    assert message.status == "processing"
    assert message.lease_expires_at is not None
    assert message.lease_expires_at >= row.not_before + timedelta(seconds=300)

    assert await _send(session_factory, outbox_id, adapter) is None  # not due yet
    assert len(adapter.calls) == 1
    other = await new_message(session_factory, main_id, client=2)
    assert await dispatch_all(session_factory) == [other]
    assert (await get(session_factory, Message, other)).operator_account_id == spare

    due = row.not_before + timedelta(seconds=1)
    assert await _send(session_factory, outbox_id, adapter, now=due) == "sent"
    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.attempts) == ("sent", 2)
    account = await get(session_factory, Account, operator_id)
    assert (account.state, account.flood_wait_until) == ("active", None)


async def test_flood_wait_holds_the_accounts_other_replies(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    first, operator_id = await processing_message(session_factory, main_id, client=1)
    second, same_operator = await processing_message(session_factory, main_id, client=2)
    assert same_operator == operator_id
    row_a = (await queued_reply(session_factory, first, operator_id)).id
    row_b = (await queued_reply(session_factory, second, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [FloodWait(60)])

    await _send(session_factory, row_a, adapter)
    assert await _send(session_factory, row_b, adapter) is None

    assert len(adapter.calls) == 1
    waiting = await get(session_factory, Outbox, row_b)
    assert (waiting.status, waiting.attempts) == ("queued", 0)
    assert waiting.not_before == (await get(session_factory, Account, operator_id)).flood_wait_until


async def test_flood_wait_attempt_limit_goes_to_review(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [FloodWait(5)] * 5)
    results = []
    for _ in range(4):
        row = await get(session_factory, Outbox, outbox_id)
        now = row.not_before + timedelta(seconds=1) if row.not_before else None
        results.append(await _send(session_factory, outbox_id, adapter, now=now))

    assert results == ["queued", "queued", "failed", None]  # DISPATCH_MAX_SEND_ATTEMPTS=3
    assert len(adapter.calls) == 3
    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.attempts, row.error) == ("failed", 3, "flood_wait")
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == ("awaiting_review", "flood_wait_limit")


async def test_peer_flood_restricts_the_account_and_never_reroutes(
    session_factory: Factory, work: tuple[int, int], main_id: int, make_account
) -> None:
    message_id, operator_id = work
    other_operator = (await make_account()).id
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [PeerFlood])
    other_adapter = FakeTelegramAccount(other_operator)

    with pytest.raises(ValueError, match="belongs to account"):
        await _send(session_factory, outbox_id, other_adapter)
    assert await _send(session_factory, outbox_id, adapter) == "failed"

    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.error, row.account_id) == ("failed", "peer_flood", operator_id)
    assert (await get(session_factory, Account, operator_id)).state == "restricted"
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason, message.operator_account_id) == (
        "awaiting_review",
        "account_restricted",
        operator_id,
    )
    assert await count_rows(session_factory, Outbox) == 1
    assert other_adapter.calls == []
    # The restricted account receives no new conversations and sends nothing more.
    later = await new_message(session_factory, main_id, client=2)
    await dispatch_all(session_factory)
    assert (await get(session_factory, Message, later)).operator_account_id == other_operator
    async with transaction(session_factory) as s:
        await s.execute(
            update(Account).where(Account.id == operator_id).values(state="active")
        )  # test setup: a second message of the same account, then restricted again
    second, _ = await processing_message(session_factory, main_id, client=1)
    queued = (await queued_reply(session_factory, second, operator_id)).id
    async with transaction(session_factory) as s:
        await s.execute(update(Account).where(Account.id == operator_id).values(state="restricted"))
    assert await _send(session_factory, queued, adapter) is None
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [(PeerInvalid, "peer_invalid"), (PermanentSendError, "permanent_send_error")],
)
async def test_permanent_error_fails_send_and_message(
    session_factory: Factory, work: tuple[int, int], error: type[Exception], code: str
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id

    assert await _send(session_factory, outbox_id, FakeTelegramAccount(operator_id, [error])) == (
        "failed"
    )

    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.error) == ("failed", code)
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == ("failed", code)
    assert (await get(session_factory, Account, operator_id)).state == "active"


async def test_parallel_enqueue_creates_one_live_send(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work

    async def enqueue(key: str | None) -> int | Exception:
        try:
            async with transaction(session_factory) as s:
                row = await enqueue_reply(
                    s,
                    message_id=message_id,
                    account_id=operator_id,
                    kind="manual",
                    body=REPLY_BODY,
                    idempotency_key=key,
                )
                return row.id
        except SendInFlight as exc:
            return exc

    distinct = await asyncio.gather(*(enqueue(f"k{i}") for i in range(10)))
    same = await asyncio.gather(*(enqueue(None) for _ in range(10)))

    created = [r for r in distinct if isinstance(r, int)]
    assert len(created) == 1
    assert all(isinstance(r, SendInFlight) for r in distinct if not isinstance(r, int))
    assert all(isinstance(r, SendInFlight) for r in same)  # default key is new, row is live
    assert await count_rows(session_factory, Outbox) == 1
    assert await count_rows(session_factory, AuditLog, AuditLog.action == "reply_enqueued") == 1


async def test_same_key_returns_the_existing_row(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    concurrent = await asyncio.gather(
        *(queued_reply(session_factory, message_id, operator_id) for _ in range(10))
    )
    later = await queued_reply(session_factory, message_id, operator_id)

    assert {row.id for row in concurrent} == {later.id}
    assert await count_rows(session_factory, Outbox) == 1


async def test_enqueue_preconditions(
    session_factory: Factory, work: tuple[int, int], main_id: int, make_account
) -> None:
    message_id, operator_id = work
    other = (await make_account()).id
    assigned_only = await new_message(session_factory, main_id, client=2)
    await dispatch_all(session_factory)
    assert (await get(session_factory, Message, assigned_only)).operator_account_id == other
    await queued_reply(session_factory, message_id, operator_id, idempotency_key="shared")

    with pytest.raises(NotOwner):
        await queued_reply(session_factory, message_id, other, idempotency_key="x")
    with pytest.raises(WrongState):
        await queued_reply(session_factory, assigned_only, other)
    await claim(session_factory, assigned_only)
    with pytest.raises(IdempotencyConflict):
        await queued_reply(session_factory, assigned_only, other, idempotency_key="shared")


async def test_sending_row_found_at_startup_becomes_uncertain(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [Crash()])

    with pytest.raises(Crash):
        await _send(session_factory, outbox_id, adapter)
    assert (await get(session_factory, Outbox, outbox_id)).status == "sending"

    async with transaction(session_factory) as s:
        assert await recover_interrupted_sends(s) == 1

    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.error) == ("uncertain", "interrupted")
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == ("awaiting_review", "send_uncertain")
    assert await _send(session_factory, outbox_id, adapter) is None
    assert len(adapter.calls) == 1
    async with transaction(session_factory) as s:
        assert await recover_interrupted_sends(s) == 0


async def test_delivery_confirmed_after_the_sweep_is_recorded(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id

    class SlowAdapter(FakeTelegramAccount):
        async def send_text(self, peer, text):  # type: ignore[no-untyped-def]
            async with transaction(session_factory) as s:  # another process restarted
                await recover_interrupted_sends(s)
            return await super().send_text(peer, text)

    assert await _send(session_factory, outbox_id, SlowAdapter(operator_id)) == "sent"
    assert (await get(session_factory, Message, message_id)).status == "answered"


async def test_queued_reply_of_a_message_no_longer_in_work_is_not_sent(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    async with transaction(session_factory) as s:  # test setup: state left by an old bug
        await s.execute(update(Message).values(status="assigned"))
    adapter = FakeTelegramAccount(operator_id)

    assert await _send(session_factory, outbox_id, adapter) == "failed"
    assert adapter.calls == []
    assert (await get(session_factory, Outbox, outbox_id)).error == "message_not_processing"


async def test_peer_is_the_user_id_without_username(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    message_id, operator_id = await processing_message(
        session_factory, main_id, client=4242, username=None
    )
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id)

    await _send(session_factory, outbox_id, adapter)
    assert adapter.sent == [(PeerRef(tg_user_id=4242), REPLY_BODY)]


async def test_reply_peer_has_the_authors_id_and_current_username(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    message_id, operator_id = await processing_message(
        session_factory, main_id, client=31, username="old_name"
    )
    await new_message(session_factory, main_id, client=31, username="new_name")  # renamed
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id)

    assert await _send(session_factory, outbox_id, adapter) == "sent"
    # Never a bare "@old_name", which may belong to someone else by now.
    assert adapter.sent == [(PeerRef(tg_user_id=31, username="new_name"), REPLY_BODY)]


async def test_cancelled_send_becomes_uncertain_at_once(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, delay=3600)  # the request hangs

    async def called() -> bool:
        return bool(adapter.calls)

    sending = asyncio.create_task(_send(session_factory, outbox_id, adapter))
    await _until(called, "the API call")
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending

    row = await get(session_factory, Outbox, outbox_id)
    assert (row.status, row.error, row.attempts) == ("uncertain", "cancelled_error", 1)
    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason) == ("awaiting_review", "send_uncertain")
    async with transaction(session_factory) as s:
        assert await recover_interrupted_sends(s) == 0  # nothing left for the next start
    assert await _send(session_factory, outbox_id, adapter) is None
    assert len(adapter.calls) == 1


async def test_send_result_is_recorded_when_cancelled_meanwhile(
    session_factory: Factory, work: tuple[int, int]
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    tasks: list[asyncio.Task] = []

    class CancelledOnReturn(FakeTelegramAccount):
        async def send_text(self, peer, text):  # type: ignore[no-untyped-def]
            sent = await super().send_text(peer, text)
            # Lands while the result is being written.
            asyncio.get_running_loop().call_soon(tasks[0].cancel)
            return sent

    tasks.append(
        asyncio.create_task(_send(session_factory, outbox_id, CancelledOnReturn(operator_id)))
    )
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]

    async def recorded() -> bool:
        return (await get(session_factory, Outbox, outbox_id)).status != "sending"

    await _until(recorded, "the send result")
    assert (await get(session_factory, Outbox, outbox_id)).status == "sent"
    assert (await get(session_factory, Message, message_id)).status == "answered"


async def test_account_flood_wait_extends_the_lease_of_deferred_replies(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    first, operator_id = await processing_message(session_factory, main_id, client=1)
    second, _ = await processing_message(session_factory, main_id, client=2)
    row_a = (await queued_reply(session_factory, first, operator_id)).id
    row_b = (await queued_reply(session_factory, second, operator_id)).id
    adapter = FakeTelegramAccount(operator_id, [FloodWait(3600)])
    assert await _send(session_factory, row_a, adapter) == "queued"

    assert await _send(session_factory, row_b, adapter) is None  # the account waits

    until = (await get(session_factory, Account, operator_id)).flood_wait_until
    assert until is not None
    lease = (await get(session_factory, Message, second)).lease_expires_at
    assert lease is not None and lease >= until + timedelta(seconds=300)
    async with transaction(session_factory) as s:  # long after the claim's own lease
        assert await recover_stale(s, datetime.now(UTC) + timedelta(seconds=1000)) == 0
    assert (await get(session_factory, Outbox, row_b)).status == "queued"
    assert (await get(session_factory, Message, second)).status == "processing"


async def _lock_waiters(factory: Factory) -> int:
    async with factory() as s:
        return (
            await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock'"
                )
            )
        ) or 0


async def test_startup_sweep_and_reassignment_do_not_deadlock(
    session_factory: Factory, main_id: int, make_account
) -> None:
    await make_account()
    target = (await make_account()).id
    m1 = await new_message(session_factory, main_id, client=1)
    m2 = await new_message(session_factory, main_id, client=1)
    await dispatch_all(session_factory)
    operator_id = await claim(session_factory, m1)
    await claim(session_factory, m2)
    for message_id in (m2, m1):  # outbox ids in the reverse order of message ids
        outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
        with pytest.raises(Crash):
            await _send(session_factory, outbox_id, FakeTelegramAccount(operator_id, [Crash()]))
    conversation_id = (await get(session_factory, Message, m1)).conversation_id

    async def reassign() -> None:
        async with transaction(session_factory) as s:
            await reassign_conversation(s, conversation_id, target, "admin:olga")

    async def sweep() -> int:
        async with transaction(session_factory) as s:
            return await recover_interrupted_sends(s)

    async def waiting(n: int):  # type: ignore[no-untyped-def]
        async def check() -> bool:
            return await _lock_waiters(session_factory) >= n

        await _until(check, f"{n} lock waiters")

    # Deterministic interleaving: the reassignment holds the conversation and queues for
    # m1 first; the sweep starts while it waits.
    async with transaction(session_factory) as blocker:
        await blocker.execute(select(Message.id).where(Message.id == m1).with_for_update())
        reassigning = asyncio.create_task(reassign())
        await waiting(1)
        sweeping = asyncio.create_task(sweep())
        await waiting(2)
    results = await asyncio.wait_for(
        asyncio.gather(reassigning, sweeping, return_exceptions=True), timeout=30
    )

    assert isinstance(results[0], ConversationBusy)  # its messages are in work
    assert results[1] == 2
    assert {r.status for r in await rows(session_factory, Outbox)} == {"uncertain"}


async def test_expire_flood_waits(session_factory: Factory, make_account) -> None:
    waiting = await make_account(state="flood_wait")
    async with transaction(session_factory) as s:
        await s.execute(
            update(Account)
            .where(Account.id == waiting.id)
            .values(flood_wait_until=Account.created_at + timedelta(seconds=60))
        )
    account = await get(session_factory, Account, waiting.id)
    assert account.flood_wait_until is not None

    async with transaction(session_factory) as s:
        assert await expire_flood_waits(s, account.flood_wait_until - timedelta(seconds=1)) == 0
    async with transaction(session_factory) as s:
        assert await expire_flood_waits(s, account.flood_wait_until) == 1
    account = await get(session_factory, Account, waiting.id)
    assert (account.state, account.flood_wait_until) == ("active", None)


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", buf)
    yield buf
    structlog.reset_defaults()


async def test_client_text_and_reply_never_reach_logs_or_audit(
    session_factory: Factory, work: tuple[int, int], log_output: io.StringIO
) -> None:
    message_id, operator_id = work
    outbox_id = (await queued_reply(session_factory, message_id, operator_id)).id
    await _send(session_factory, outbox_id, FakeTelegramAccount(operator_id, [SendTimeout]))
    async with transaction(session_factory) as s:
        await resolve_uncertain(s, outbox_id, "sent", "admin:olga")

    records = [json.loads(line) for line in log_output.getvalue().splitlines()]
    assert [r["event"] for r in records] == ["send_result_uncertain"]
    audit = json.dumps(
        [a.details for a in await rows(session_factory, AuditLog)], ensure_ascii=False
    )
    for secret in (CLIENT_TEXT, REPLY_BODY):
        assert secret not in log_output.getvalue()
        assert secret not in audit
