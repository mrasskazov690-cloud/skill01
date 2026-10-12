"""Concurrent dispatchers on a real PostgreSQL. A failure here is a defect, not a flake."""

import asyncio
from collections import Counter, defaultdict

from sqlalchemy import func, select
from sqlalchemy.engine import URL

from app.db.models import Assignment, AuditLog, Conversation, Message
from app.db.session import create_engine, create_sessionmaker, transaction
from app.services.dispatcher import dispatch_batch
from tests.helpers import Factory, add_message, rows

WORKERS = 20


async def _seed(factory: Factory, main_id: int, messages: int, clients: int) -> list[int]:
    async with transaction(factory) as s:
        # Clients interleaved, so many conversations compete inside every candidate window.
        return [await add_message(s, main_id, 1 + i % clients) for i in range(messages)]


async def _run_workers(database_url: URL, limit: int = 10_000) -> list[list[int]]:
    """WORKERS dispatch_batch loops, each on its own engine with exactly one connection."""
    engines = [create_engine(database_url, pool_size=1, max_overflow=0) for _ in range(WORKERS)]
    start = asyncio.Event()

    async def worker(factory: Factory) -> list[int]:
        await start.wait()
        return await dispatch_batch(factory, limit)

    try:
        tasks = [asyncio.create_task(worker(create_sessionmaker(e))) for e in engines]
        await asyncio.sleep(0)
        start.set()
        return await asyncio.gather(*tasks)
    finally:
        await asyncio.gather(*(e.dispose() for e in engines))


async def test_parallel_dispatchers_assign_every_message_exactly_once(
    database_url: URL, session_factory: Factory, make_account
) -> None:
    main_id = (await make_account(kind="main")).id
    operators = {(await make_account()).id for _ in range(3)}
    message_ids = await _seed(session_factory, main_id, messages=200, clients=50)

    results = await _run_workers(database_url)

    dispatched = [m for batch in results for m in batch]
    assert sorted(dispatched) == sorted(message_ids)  # each exactly once, none lost
    assert sum(1 for batch in results if batch) > 1  # the work really was shared

    messages = await rows(session_factory, Message)
    assert {m.status for m in messages} == {"assigned"}
    conversations = {c.id: c.operator_account_id for c in await rows(session_factory, Conversation)}
    assert len(conversations) == 50
    operators_per_conversation: dict[int, set[int | None]] = defaultdict(set)
    for m in messages:
        operators_per_conversation[m.conversation_id].add(m.operator_account_id)
    for conversation_id, owners in operators_per_conversation.items():
        assert owners == {conversations[conversation_id]}, conversation_id
    assert set(conversations.values()) == operators

    assignments = await rows(session_factory, Assignment)
    assert len(assignments) == 200
    assert len({a.message_id for a in assignments}) == 200
    assert Counter(a.reason for a in assignments) == {"auto_round_robin": 50, "pinned": 150}
    by_message = {m.id: m.operator_account_id for m in messages}
    assert all(
        a.message_id is not None and a.to_account_id == by_message[a.message_id]
        for a in assignments
    )
    async with session_factory() as s:
        audited = await s.scalar(
            select(func.count(func.distinct(AuditLog.entity_id))).where(
                AuditLog.action == "message_assigned"
            )
        )
    assert audited == 200

    # Round-robin: 50 new conversations over 3 operators is 17/17/16 when serial. Under
    # SKIP LOCKED an operator whose row is in use loses that turn to the next one, so a
    # small spread is expected (stress runs: almost always 1, at most 3); never starvation.
    load = Counter(conversations.values())
    assert max(load.values()) - min(load.values()) <= 4, load


async def test_parallel_dispatchers_keep_a_new_conversation_on_one_operator(
    database_url: URL, session_factory: Factory, make_account
) -> None:
    main_id = (await make_account(kind="main")).id
    for _ in range(3):
        await make_account()
    await _seed(session_factory, main_id, messages=40, clients=1)

    results = await _run_workers(database_url)

    assert sum(len(batch) for batch in results) == 40
    messages = await rows(session_factory, Message)
    assert len({m.operator_account_id for m in messages}) == 1
    reasons = Counter(a.reason for a in await rows(session_factory, Assignment))
    assert reasons == {"auto_round_robin": 1, "pinned": 39}


async def test_parallel_dispatchers_respect_max_active(
    database_url: URL, session_factory: Factory, make_account
) -> None:
    main_id = (await make_account(kind="main")).id
    operators = {(await make_account(max_active=5)).id for _ in range(3)}
    await _seed(session_factory, main_id, messages=200, clients=200)

    results = await _run_workers(database_url)

    assert sum(len(batch) for batch in results) == 15
    load = Counter(
        m.operator_account_id
        for m in await rows(session_factory, Message, Message.status == "assigned")
    )
    assert load == dict.fromkeys(operators, 5)
