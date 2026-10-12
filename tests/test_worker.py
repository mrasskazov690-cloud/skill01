"""The worker with fake Telegram accounts and a manual clock: intake, the single
sender, alternation, limits, approvals, guard, pause, reactions, never-again."""

# ruff: noqa: RUF001  (Russian reply texts by design)

import io
import json
import struct
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count, pairwise
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.db.models import (
    Account,
    AppSetting,
    AuditLog,
    Client,
    Conversation,
    Draft,
    Message,
    Outbox,
    Template,
)
from app.db.session import transaction
from app.domain.errors import AutoModeLocked
from app.domain.states import DraftSource
from app.drafts.llm import ClaudeDraftProvider
from app.drafts.profile import load_profile
from app.logging import configure_logging
from app.services.approvals import approve, enable_auto_mode, set_paused
from app.services.outbox import resolve_uncertain
from app.telegram.base import (
    FloodWait,
    PeerInvalid,
    PeerRef,
    PrivacyRestricted,
    ReactionInvalid,
    ResolvedAuthor,
    SendRefused,
    SendTimeout,
    SentMessage,
    SourceUnavailable,
    UserRestricted,
    WorkChatMessage,
)
from app.telegram.fake import FakeTelegramAccount
from app.telegram.links import LeadLink
from app.worker import __main__ as entry
from app.worker.clock import ManualClock
from app.worker.drafts import Reply
from app.worker.worker import CURSOR_KEY, INTAKE_ATTEMPTS, Worker
from tests.helpers import Factory, get, processing_message, queued_reply, rows
from tests.test_drafts_llm import FIXTURE as LLM_FIXTURE
from tests.test_drafts_llm import GOOD as GOOD_DRAFT
from tests.test_drafts_llm import FakeClient
from tests.test_drafts_llm import response as llm_response
from tests.test_drafts_llm import result as llm_result
from tests.test_drafts_llm import status_error as llm_status_error

LLM_PROFILE = load_profile(LLM_FIXTURE)

CHAT = -100_777
TEMPLATE = "Здравствуйте! Подскажите, пожалуйста, что именно вас интересует?"
OWNER = "owner:olga"


class TimedFake(FakeTelegramAccount):
    """Records the (manual) clock time of every send attempt."""

    def __init__(self, account_id: int, clock: ManualClock) -> None:
        super().__init__(account_id)
        self.clock = clock
        self.sent_at: list[datetime] = []

    async def send_text(
        self, peer: PeerRef, text: str, *, random_id: int | None = None
    ) -> SentMessage:
        self.sent_at.append(self.clock.now())
        return await super().send_text(peer, text, random_id=random_id)


@dataclass
class Env:
    engine: AsyncEngine
    factory: Factory
    clock: ManualClock
    reader: FakeTelegramAccount
    ops: list[TimedFake]
    workers: list[Worker] = field(default_factory=list)
    tg_ids: Iterator[int] = field(default_factory=lambda: count(1))

    def worker(self, **kw: object) -> Worker:
        adapters = {self.reader.account_id: self.reader} | {f.account_id: f for f in self.ops}
        w = Worker(self.engine, self.factory, adapters, clock=self.clock, **kw)  # type: ignore[arg-type]
        self.workers.append(w)
        return w

    def post(
        self,
        user: int | None = None,
        *,
        text: str | None = None,
        reactions: set[str] | frozenset[str] = frozenset(),
        author: ResolvedAuthor | BaseException | None = None,
        linked: bool = True,
    ) -> int:
        """A lead in the work chat behind a hidden link to the author's message."""
        message_id = next(self.tg_ids)
        links = []
        if linked:
            link = LeadLink(
                kind="message",
                chat_username="flats_src",
                message_id=1000 + message_id,
                url=f"https://t.me/flats_src/{1000 + message_id}",
            )
            links.append(link)
            if author is None and user is not None:
                author = ResolvedAuthor(user, f"user{user}", f"User {user}")
            if author is not None:
                self.reader.set_author(link, author)
        self.reader.add_work_message(
            WorkChatMessage(
                chat_id=CHAT,
                message_id=message_id,
                date=datetime.now(UTC),
                text=text or f"Ищу квартиру, заявка {message_id}",
                links=links,
                reactions=set(reactions),
            )
        )
        return message_id

    def senders(self) -> list[int]:
        """Accounts of successful sends, in time order."""
        sends = [(t, f.account_id) for f in self.ops for t in f.sent_at[: len(f.sent)]]
        return [account for _, account in sorted(sends)]


@pytest.fixture
async def env(
    engine: AsyncEngine, session_factory: Factory, make_account, monkeypatch, settings: Settings
) -> AsyncIterator[Env]:
    reader = await make_account(kind="main")
    a, b = await make_account(), await make_account()
    monkeypatch.setattr(settings, "WORK_CHAT_ID", CHAT)
    monkeypatch.setattr(settings, "READER_ACCOUNT_ID", reader.id)
    monkeypatch.setattr(settings, "CONFIRM_FIRST_N", 0)
    clock = ManualClock()
    fake_reader = FakeTelegramAccount(reader.id)
    fake_reader.work_chat[CHAT] = []
    async with transaction(session_factory) as s:
        await s.execute(insert(Template).values(name="greeting", intent="default", body=TEMPLATE))
    ops = [TimedFake(a.id, clock), TimedFake(b.id, clock)]
    e = Env(engine, session_factory, clock, fake_reader, ops)
    yield e
    for w in e.workers:
        await w.close()


async def _auto_mode(factory: Factory) -> None:
    async with transaction(factory) as s:
        await enable_auto_mode(s, OWNER)


async def _lead(factory: Factory, tg_message_id: int) -> Message:
    async with factory() as s:
        return (
            await s.scalars(select(Message).where(Message.tg_message_id == tg_message_id))
        ).one()


async def _status(factory: Factory, tg_message_id: int) -> tuple[str, str | None]:
    lead = await _lead(factory, tg_message_id)
    return lead.status, lead.review_reason


# --- the send loop -------------------------------------------------------------------


async def test_senders_alternate_over_six_ticks_one_send_per_interval(
    env: Env, settings: Settings
) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for user in range(1, 9):
        env.post(user)
    await worker.poll_work_chat()

    await worker.run_sender(max_ticks=6)

    a, b = (f.account_id for f in env.ops)
    assert env.senders() == [a, b, a, b, a, b]
    times = sorted(t for f in env.ops for t in f.sent_at)
    assert len(times) == 6  # 8 leads waiting, one send per tick, never two
    interval = settings.SEND_INTERVAL_SECONDS
    assert [(y - x).total_seconds() for x, y in pairwise(times)] == [interval] * 5
    assert env.clock.sleeps == [interval] * 6
    pending = await rows(env.factory, Message, Message.status == "pending")
    assert len(pending) == 2


async def test_sent_lead_gets_a_heart_from_the_sending_account(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    assert (result.kind, result.status) == ("sent", "sent")
    sender = next(f for f in env.ops if f.account_id == result.account_id)
    assert sender.reactions == [(CHAT, tg_id, "❤️")]
    assert sender.sent[0][0].tg_user_id == 1  # the resolved author, not a username
    lead = await _lead(env.factory, tg_id)
    assert (lead.status, lead.reaction) == ("answered", "❤️")


async def test_daily_limit_takes_an_account_out_of_the_rotation(env: Env) -> None:
    a, b = (f.account_id for f in env.ops)
    async with transaction(env.factory) as s:
        await s.execute(update(Account).where(Account.id == a).values(daily_send_limit=1))
        await s.execute(update(Account).where(Account.id == b).values(daily_send_limit=2))
    worker = env.worker()
    await _auto_mode(env.factory)
    for user in range(1, 6):
        env.post(user)
    await worker.poll_work_chat()

    kinds = [(await worker.send_tick()).kind for _ in range(5)]

    assert env.senders() == [a, b, b]
    assert kinds == ["sent", "sent", "sent", "idle", "idle"]
    assert len(await rows(env.factory, Message, Message.status == "pending")) == 2


async def _sent_by(factory: Factory, account_id: int) -> int:
    return len(
        await rows(factory, Outbox, Outbox.account_id == account_id, Outbox.status == "sent")
    )


async def test_approved_drafts_count_against_the_daily_limit_when_sent(
    env: Env, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_DAILY_LIMIT", 1)
    worker = env.worker()  # confirm mode: every lead becomes a draft, nothing is sent
    for user in range(1, 5):
        env.post(user)
    await worker.poll_work_chat()
    await worker.run_sender(max_ticks=4)
    drafts = await rows(env.factory, Draft, Draft.status == "pending")
    assert len(drafts) == 4
    async with transaction(env.factory) as s:
        for draft in drafts:
            await approve(s, draft.message_id, OWNER)

    await worker.run_sender(max_ticks=6)

    assert [await _sent_by(env.factory, f.account_id) for f in env.ops] == [1, 1]
    assert len(await rows(env.factory, Outbox, Outbox.status == "queued")) == 2
    # Next day: the deferred replies go out, still one per account per day.
    async with transaction(env.factory) as s:
        await s.execute(update(Outbox).values(sent_at=Outbox.sent_at - timedelta(days=1)))
    await worker.run_sender(max_ticks=3)
    assert sum(len(f.sent) for f in env.ops) == 4
    assert [len(f.sent) for f in env.ops] == [2, 2]


async def test_resumed_lead_waits_while_its_account_is_at_the_daily_limit(
    env: Env, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_DAILY_LIMIT", 1)
    worker = env.worker()
    await _auto_mode(env.factory)
    a, b = env.ops
    a.script(SendTimeout())
    for user in (1, 2, 3, 4):
        env.post(user)
    await worker.poll_work_chat()
    first = await worker.send_tick()  # newest lead -> a, delivery unknown
    assert (first.account_id, first.status) == (a.account_id, "uncertain")
    assert (await worker.send_tick()).account_id == b.account_id
    assert (await worker.send_tick()).account_id == a.account_id  # a: 1 of 1
    uncertain = (await rows(env.factory, Outbox, Outbox.status == "uncertain"))[0]
    async with transaction(env.factory) as s:
        await resolve_uncertain(s, uncertain.id, "not_sent", OWNER)  # back to a, assigned

    assert (await worker.send_tick()).kind == "idle"  # b is at its limit too

    assert await _sent_by(env.factory, a.account_id) == 1
    lead = await get(env.factory, Message, first.message_id)
    assert (lead.status, lead.operator_account_id) == ("assigned", a.account_id)


async def test_resumed_lead_waits_while_its_account_is_switched_off(env: Env) -> None:
    a, b = env.ops
    flooded_once = False

    async def dialog_check(tg_user_id: int) -> bool:
        nonlocal flooded_once
        if not flooded_once:
            flooded_once = True
            raise FloodWait(5)
        return False

    a.has_dialog_with = dialog_check  # type: ignore[method-assign]
    worker = env.worker()
    await _auto_mode(env.factory)
    env.post(1)
    await worker.poll_work_chat()
    assert (await worker.send_tick()).kind == "deferred"
    async with transaction(env.factory) as s:  # wait over, then the owner switches a off
        await s.execute(
            update(Account)
            .where(Account.id == a.account_id)
            .values(state="active", flood_wait_until=None, enabled=False)
        )

    assert (await worker.send_tick()).kind == "idle"
    assert a.calls == [] and b.calls == []
    async with transaction(env.factory) as s:
        await s.execute(update(Account).where(Account.id == a.account_id).values(enabled=True))
    result = await worker.send_tick()
    assert (result.kind, result.account_id) == ("sent", a.account_id)


async def test_flood_wait_on_a_dialog_check_benches_only_that_account(env: Env) -> None:
    a, b = env.ops
    checks = 0

    async def flooded(tg_user_id: int) -> bool:
        nonlocal checks
        checks += 1
        raise FloodWait(3600)

    a.has_dialog_with = flooded  # type: ignore[method-assign]
    worker = env.worker()
    await _auto_mode(env.factory)
    for user in range(1, 5):
        env.post(user)
    await worker.poll_work_chat()

    kinds = [(await worker.send_tick()).kind for _ in range(5)]

    assert kinds == ["deferred", "sent", "sent", "sent", "idle"]
    assert len(b.sent) == 3 and checks == 1  # a is not asked again during its wait
    account = await get(env.factory, Account, a.account_id)
    assert account.state == "flood_wait" and account.flood_wait_until is not None


async def test_second_worker_cannot_take_the_sender_lock(env: Env) -> None:
    first, second = env.worker(name="w1"), env.worker(name="w2")
    assert (await first.send_tick()).kind == "idle"
    # A send of the first worker is in flight ('sending' between its two transactions).
    main_id = env.reader.account_id
    message_id, account_id = await processing_message(env.factory, main_id, client=9)
    outbox = await queued_reply(env.factory, message_id, account_id)
    async with transaction(env.factory) as s:
        await s.execute(update(Outbox).where(Outbox.id == outbox.id).values(status="sending"))
    await _auto_mode(env.factory)
    env.post(1)
    await first.poll_work_chat()

    assert (await second.send_tick()).kind == "locked_out"
    assert (await second.send_tick()).kind == "locked_out"
    assert not any(f.calls for f in env.ops)
    assert (await get(env.factory, Outbox, outbox.id)).status == "sending"  # no sweep

    await first.close()  # the first worker stops (or dies): its lock is gone
    assert (await second.send_tick()).kind == "sent"
    # The startup sweep runs when the lock is taken over.
    assert (await get(env.factory, Outbox, outbox.id)).status == "uncertain"


async def test_newest_lead_first_also_across_a_batch_arriving_mid_run(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    first_batch = [env.post(user) for user in (1, 2, 3)]
    await worker.poll_work_chat()
    order = [(await worker.send_tick()).message_id]
    second_batch = [env.post(user) for user in (4, 5)]
    await worker.poll_work_chat()
    order += [(await worker.send_tick()).message_id for _ in range(4)]

    expected = [second_batch[1], second_batch[0], first_batch[1], first_batch[0]]
    expected = [first_batch[2], *expected]
    assert order == [(await _lead(env.factory, tg)).id for tg in expected]


async def test_first_replies_wait_for_the_owner_then_auto_mode_only_by_command(
    env: Env, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "CONFIRM_FIRST_N", 2)
    worker = env.worker()
    leads = [env.post(user) for user in (1, 2, 3, 4)]
    await worker.poll_work_chat()

    for n, tg_id in enumerate(reversed(leads[2:])):
        result = await worker.send_tick()
        assert result.kind == "draft"
        assert await _status(env.factory, tg_id) == ("awaiting_review", "needs_approval")
        assert sum(len(f.sent) for f in env.ops) == n  # nothing went out unapproved
        if n == 0:
            async with transaction(env.factory) as s:
                with pytest.raises(AutoModeLocked):
                    await enable_auto_mode(s, OWNER)
        async with transaction(env.factory) as s:
            await approve(s, result.message_id, OWNER)  # type: ignore[arg-type]
        sent = await worker.send_tick()  # the approved draft goes first
        assert (sent.kind, sent.message_id, sent.status) == ("sent", result.message_id, "sent")

    # N approvals reached: auto mode is still off until the owner switches it on.
    assert (await worker.send_tick()).kind == "draft"
    assert await _status(env.factory, leads[1]) == ("awaiting_review", "needs_approval")
    async with transaction(env.factory) as s:
        await enable_auto_mode(s, OWNER)
    result = await worker.send_tick()
    assert (result.kind, result.status) == ("sent", "sent")
    assert await _status(env.factory, leads[0]) == ("answered", None)
    assert sum(len(f.sent) for f in env.ops) == 3


@dataclass
class FixedDrafts:
    body: str
    source: DraftSource = DraftSource.LLM

    async def draft(self, session: AsyncSession, message_id: int) -> Reply:
        return Reply(self.body, self.source)


async def test_guard_sends_a_risky_draft_to_the_owner_even_in_auto_mode(env: Env) -> None:
    await _auto_mode(env.factory)
    risky = env.worker(drafts=FixedDrafts("Квартира свободна, 45 000 руб., показ 12.10"))
    tg_id = env.post(1)
    await risky.poll_work_chat()

    result = await risky.send_tick()

    assert result.kind == "draft"
    assert not any(f.calls for f in env.ops)
    assert await _status(env.factory, tg_id) == ("awaiting_review", "needs_approval")
    (draft,) = await rows(env.factory, Draft)
    assert draft.issues == ["price", "date", "availability"]
    await risky.close()

    plain = env.worker(drafts=FixedDrafts("Здравствуйте! Какой район вам удобен?"))
    env.post(2)
    await plain.poll_work_chat()
    assert (await plain.send_tick()).kind == "sent"


async def test_pause_stops_sending_until_unpaused(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    async with transaction(env.factory) as s:
        await set_paused(s, True, OWNER)
    tg_id = env.post(1)
    await worker.poll_work_chat()  # intake goes on while paused

    assert [(await worker.send_tick()).kind for _ in range(3)] == ["paused"] * 3
    assert not any(f.calls for f in env.ops)
    assert await _status(env.factory, tg_id) == ("pending", None)

    async with transaction(env.factory) as s:
        await set_paused(s, False, OWNER)
    assert (await worker.send_tick()).kind == "sent"


async def test_approved_draft_waits_while_paused(env: Env) -> None:
    worker = env.worker()
    env.post(1)
    await worker.poll_work_chat()
    drafted = await worker.send_tick()
    async with transaction(env.factory) as s:
        await approve(s, drafted.message_id, OWNER)  # type: ignore[arg-type]
        await set_paused(s, True, OWNER)
    assert (await worker.send_tick()).kind == "paused"
    assert not any(f.calls for f in env.ops)
    async with transaction(env.factory) as s:
        await set_paused(s, False, OWNER)
    assert (await worker.send_tick()).message_id == drafted.message_id


@pytest.mark.parametrize(
    ("outcome", "message_status", "reaction"),
    [
        (PrivacyRestricted, "failed", "🚫"),
        (PeerInvalid, "failed", None),
        (SendTimeout, "awaiting_review", None),
    ],
)
async def test_reaction_follows_the_send_outcome(
    env: Env, outcome: type[Exception], message_status: str, reaction: str | None
) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.script(outcome)
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    lead = await _lead(env.factory, tg_id)
    assert (lead.status, lead.reaction) == (message_status, reaction)
    sender = next(f for f in env.ops if f.account_id == result.account_id)
    assert sender.reactions == ([(CHAT, tg_id, reaction)] if reaction else [])


async def test_failed_reaction_is_audited_and_the_send_stays_sent(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.script_reactions(SourceUnavailable("reaction refused"))
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    assert (result.kind, result.status) == ("sent", "sent")
    lead = await _lead(env.factory, tg_id)
    assert (lead.status, lead.reaction) == ("answered", None)
    (audit,) = await rows(env.factory, AuditLog, AuditLog.action == "reaction_failed")
    assert audit.entity_id == lead.id
    assert audit.details["error"] == "source_unavailable"


async def test_never_again_when_the_account_already_has_a_dialog(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.dialogs.add(1)  # someone already wrote to user 1 from this account
    known = env.post(1)
    fresh = env.post(2)
    await worker.poll_work_chat()

    result = await worker.send_tick()  # newest first: user 2 is sent ...
    assert result.message_id == (await _lead(env.factory, fresh)).id
    result = await worker.send_tick()  # ... then user 1 is a duplicate, nothing to send
    assert result.kind == "idle"

    assert await _status(env.factory, known) == ("cancelled", "duplicate_client")
    assert [peer.tg_user_id for f in env.ops for peer, _ in f.calls] == [2]
    assert all((CHAT, known, "❤️") not in f.reactions for f in env.ops)


async def test_never_again_after_a_sent_reply_from_any_account(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    env.post(1)
    await worker.poll_work_chat()
    assert (await worker.send_tick()).kind == "sent"

    again = env.post(1, text="Ещё раз: ищу квартиру")
    await worker.poll_work_chat()

    assert await _status(env.factory, again) == ("cancelled", "duplicate_client")
    assert (await worker.send_tick()).kind == "idle"
    assert sum(len(f.calls) for f in env.ops) == 1


async def test_lead_without_template_goes_to_review(env: Env) -> None:
    async with transaction(env.factory) as s:
        await s.execute(update(Template).values(active=False))
    worker = env.worker()
    await _auto_mode(env.factory)
    tg_id = env.post(1)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "idle"
    assert await _status(env.factory, tg_id) == ("awaiting_review", "no_template")


async def test_failed_dialog_check_goes_to_review(env: Env) -> None:
    class Broken(TimedFake):
        async def has_dialog_with(self, tg_user_id: int) -> bool:
            raise SourceUnavailable("cannot read dialogs")

    env.ops = [Broken(f.account_id, env.clock) for f in env.ops]
    worker = env.worker()
    await _auto_mode(env.factory)
    tg_id = env.post(1)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "idle"
    assert await _status(env.factory, tg_id) == ("awaiting_review", "dialog_check_failed")


# --- the intake loop -----------------------------------------------------------------


async def test_start_skips_leads_people_already_reacted_to(env: Env) -> None:
    worker = env.worker()
    hearted = env.post(1, reactions={"❤️"})
    blocked = env.post(2, reactions={"🚫"})
    fresh = env.post(3, reactions={"👍"})

    assert await worker.poll_work_chat() == 1

    stored = await rows(env.factory, Message)
    assert [m.tg_message_id for m in stored] == [fresh]
    async with env.factory() as s:
        cursor = await s.scalar(
            select(AppSetting.value).where(AppSetting.key == CURSOR_KEY.format(chat_id=CHAT))
        )
    assert cursor == fresh > max(hearted, blocked)
    assert await worker.poll_work_chat() == 0  # nothing new


async def test_backfill_takes_only_the_last_messages_on_first_start(
    env: Env, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "WORK_CHAT_BACKFILL", 2)
    worker = env.worker()
    ids = [env.post(user) for user in (1, 2, 3, 4)]

    assert await worker.poll_work_chat() == 2
    assert sorted(m.tg_message_id for m in await rows(env.factory, Message)) == ids[2:]
    newer = env.post(5)
    assert await worker.poll_work_chat() == 1
    assert (await _lead(env.factory, newer)).status == "pending"


async def test_lead_without_a_link_or_author_goes_to_review_with_an_external_key(
    env: Env,
) -> None:
    worker = env.worker()
    no_link = env.post(linked=False)
    hidden = env.post(author=SourceUnavailable("private chat"))

    await worker.poll_work_chat()

    assert await _status(env.factory, no_link) == ("awaiting_review", "no_link")
    assert await _status(env.factory, hidden) == ("awaiting_review", "source_unavailable")
    keys = {c.external_key for c in await rows(env.factory, Client)}
    assert keys == {f"msg:{CHAT}/{no_link}", f"msg:{CHAT}/{hidden}"}


async def test_same_text_is_a_duplicate_only_without_an_author(env: Env) -> None:
    worker = env.worker()
    text = "Сниму двушку у метро, бюджет обсуждаем"
    known_a = env.post(1, text=text)
    known_b = env.post(2, text=text)
    unknown_1 = env.post(linked=False, text=text)
    unknown_2 = env.post(linked=False, text=text)

    await worker.poll_work_chat()

    assert await _status(env.factory, known_a) == ("pending", None)
    assert await _status(env.factory, known_b) == ("pending", None)
    assert await _status(env.factory, unknown_1) == ("cancelled", "duplicate_text")
    assert await _status(env.factory, unknown_2) == ("cancelled", "duplicate_text")


async def test_flood_wait_while_resolving_waits_and_goes_on(env: Env) -> None:
    class Flooded(FakeTelegramAccount):
        floods = 2

        async def resolve_author(self, link: LeadLink) -> ResolvedAuthor:
            if self.floods:
                self.floods -= 1
                raise FloodWait(30)
            return await super().resolve_author(link)

    reader = Flooded(env.reader.account_id)
    reader.work_chat = env.reader.work_chat
    reader.authors = env.reader.authors
    env.reader = reader
    worker = env.worker()
    tg_id = env.post(7)

    assert await worker.poll_work_chat() == 1

    assert env.clock.sleeps == [30, 30]
    lead = await _lead(env.factory, tg_id)
    async with env.factory() as s:
        client = await s.scalar(
            select(Client.tg_user_id)
            .join(Conversation, Conversation.client_id == Client.id)
            .where(Conversation.id == lead.conversation_id)
        )
    assert (lead.status, client) == ("pending", 7)
    assert await _count_messages(env.factory) == 1


async def _count_messages(factory: Factory) -> int:
    async with factory() as s:
        return (await s.scalar(select(func.count()).select_from(Message))) or 0


class _PoisonReader(FakeTelegramAccount):
    """resolve_author raises what Telethon raises for an id it cannot serialize."""

    async def resolve_author(self, link: LeadLink) -> ResolvedAuthor:
        if link.message_id == 1000 + 1:
            raise struct.error("'i' format requires -2147483648 <= number <= 2147483647")
        return await super().resolve_author(link)


async def test_unexpected_resolve_error_sends_the_lead_to_review_and_intake_goes_on(
    env: Env,
) -> None:
    reader = _PoisonReader(env.reader.account_id)
    reader.work_chat, reader.authors = env.reader.work_chat, env.reader.authors
    env.reader = reader
    worker = env.worker()
    bad, good = env.post(1), env.post(2)

    assert await worker.poll_work_chat() == 2

    assert await _status(env.factory, bad) == ("awaiting_review", "error")
    assert await _status(env.factory, good) == ("pending", None)


async def test_message_that_keeps_failing_is_stored_for_review_after_retries(env: Env) -> None:
    worker = env.worker()
    bad = env.post(author=ResolvedAuthor(2**63, "huge", "Huge"))  # beyond bigint
    good = env.post(2)

    assert [await worker.poll_work_chat() for _ in range(INTAKE_ATTEMPTS)] == [0, 0, 2]

    assert await _status(env.factory, bad) == ("awaiting_review", "intake_failed")
    assert await _status(env.factory, good) == ("pending", None)
    assert await worker.poll_work_chat() == 0


async def test_message_that_cannot_be_stored_at_all_is_skipped_and_audited(env: Env) -> None:
    worker = env.worker()
    bad = env.post(1, text="заявка\x00")  # PostgreSQL text cannot hold NUL
    good = env.post(2)

    for _ in range(INTAKE_ATTEMPTS):
        await worker.poll_work_chat()

    assert [m.tg_message_id for m in await rows(env.factory, Message)] == [good]
    skipped = await rows(env.factory, AuditLog, AuditLog.action == "work_chat_message_skipped")
    assert [(e.entity_type, e.entity_id) for e in skipped] == [("work_chat_message", bad)]


async def test_worker_exits_with_a_message_when_a_session_is_missing(
    engine: AsyncEngine, make_account, monkeypatch, settings: Settings, tmp_path
) -> None:
    reader = await make_account(kind="main", session_env="TG_SESSION_NOWHERE")
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("TG_SESSION_NOWHERE", raising=False)
    monkeypatch.setattr(settings, "TG_API_ID", 1)
    monkeypatch.setattr(settings, "TG_API_HASH", SecretStr("h"))
    monkeypatch.setattr(settings, "WORK_CHAT_ID", CHAT)
    monkeypatch.setattr(settings, "READER_ACCOUNT_ID", reader.id)
    monkeypatch.setattr(entry, "configure_logging", lambda level: None)
    monkeypatch.setattr(entry, "create_engine", lambda: engine)

    with pytest.raises(SystemExit) as exc:
        await entry.main()

    assert str(exc.value.code) == f"account {reader.id}: TG_SESSION_NOWHERE is not set"


# --- stage 4 tails (P9) ----------------------------------------------------------------


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", buf)
    yield buf
    structlog.reset_defaults()


def _log_records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def _reader_message(env: Env, tg_id: int) -> WorkChatMessage:
    return next(m for m in env.reader.work_chat[CHAT] if m.message_id == tg_id)


async def _outboxes(factory: Factory, tg_id: int) -> list[Outbox]:
    lead = await _lead(factory, tg_id)
    return await rows(factory, Outbox, Outbox.message_id == lead.id)


async def test_privacy_refusal_gets_the_configured_reaction(
    env: Env, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "REACTION_PRIVACY", "🙈")
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.script(PrivacyRestricted)
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    sender = next(f for f in env.ops if f.account_id == result.account_id)
    assert sender.reactions == [(CHAT, tg_id, "🙈")]
    assert (await _lead(env.factory, tg_id)).reaction == "🙈"


async def test_privacy_reaction_the_chat_refuses_falls_back_to_thumbs_down(
    env: Env, log_output: io.StringIO
) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.script(PrivacyRestricted)
        fake.script_reactions(ReactionInvalid())  # 🚫 is not allowed in this chat
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    sender = next(f for f in env.ops if f.account_id == result.account_id)
    assert sender.reactions == [(CHAT, tg_id, "👎")]
    lead = await _lead(env.factory, tg_id)
    assert (lead.status, lead.reaction) == ("failed", "👎")
    (warning,) = [r for r in _log_records(log_output) if r["event"] == "reaction_invalid"]
    assert (warning["level"], warning["emoji"], warning["fallback"]) == ("warning", "🚫", "👎")
    (audit,) = await rows(env.factory, AuditLog, AuditLog.action == "reaction_set")
    assert audit.details["emoji"] == "👎"


async def test_lead_handled_by_hand_before_the_auto_send_is_cancelled(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    tg_id = env.post(1)
    await worker.poll_work_chat()
    _reader_message(env, tg_id).reactions.add("❤️")  # a person answered it meanwhile

    assert (await worker.send_tick()).kind == "idle"

    assert not any(f.calls for f in env.ops)
    assert await _status(env.factory, tg_id) == ("cancelled", "handled_manually")
    (outbox,) = await _outboxes(env.factory, tg_id)
    assert (outbox.status, outbox.error) == ("failed", "handled_manually")
    assert env.reader.reaction_reads == [(CHAT, tg_id)]
    assert all(not f.reactions for f in env.ops)


async def test_approved_draft_of_a_lead_handled_by_hand_is_not_sent(env: Env) -> None:
    worker = env.worker()
    tg_id = env.post(1)
    await worker.poll_work_chat()
    drafted = await worker.send_tick()
    assert drafted.kind == "draft"
    async with transaction(env.factory) as s:
        await approve(s, drafted.message_id, OWNER)  # type: ignore[arg-type]
    _reader_message(env, tg_id).reactions.add("🚫")

    assert (await worker.send_tick()).kind == "idle"

    assert not any(f.calls for f in env.ops)
    assert await _status(env.factory, tg_id) == ("cancelled", "handled_manually")
    (outbox,) = await _outboxes(env.factory, tg_id)
    assert (outbox.status, outbox.error) == ("failed", "handled_manually")


async def test_flood_wait_on_the_reaction_reread_defers_the_send(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    env.reader.script_reaction_reads(FloodWait(30))
    tg_id = env.post(1)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "deferred"
    assert not any(f.calls for f in env.ops)

    result = await worker.send_tick()  # the queued reply goes out once the read works
    assert (result.kind, result.status) == ("sent", "sent")
    assert await _status(env.factory, tg_id) == ("answered", None)


async def test_user_restricted_fails_the_lead_and_pauses_the_account(env: Env) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.script(UserRestricted())
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    assert (result.kind, result.status) == ("sent", "failed")
    assert await _status(env.factory, tg_id) == ("failed", "user_restricted")
    (outbox,) = await _outboxes(env.factory, tg_id)
    assert (outbox.status, outbox.error) == ("failed", "user_restricted")
    account = await get(env.factory, Account, result.account_id)
    assert account.state == "paused"
    other = next(f for f in env.ops if f.account_id != result.account_id)
    other._outcomes.clear()  # the other account sends normally
    nxt = env.post(2)
    await worker.poll_work_chat()
    assert (await worker.send_tick()).account_id == other.account_id
    assert await _status(env.factory, nxt) == ("answered", None)


async def test_unknown_definitive_refusal_fails_the_lead(
    env: Env,
) -> None:
    worker = env.worker()
    await _auto_mode(env.factory)
    for fake in env.ops:
        fake.script(SendRefused("CHAT_WRITE_FORBIDDEN"))
    tg_id = env.post(1)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    assert result.status == "failed"
    assert await _status(env.factory, tg_id) == ("failed", "send_refused")
    assert (await get(env.factory, Account, result.account_id)).state == "active"


async def test_intake_errors_reach_the_log_without_their_text(
    env: Env, log_output: io.StringIO
) -> None:
    secret = "Ищу квартиру, тел +7 900 000-00-00"

    class Broken(FakeTelegramAccount):
        async def fetch_work_chat(
            self, chat_id: int, *, min_id: int = 0, limit: int
        ) -> list[WorkChatMessage]:
            raise RuntimeError(f"could not parse: {secret}")

    env.reader = Broken(env.reader.account_id)
    worker = env.worker()

    await worker.run_intake(max_polls=1)

    (record,) = [r for r in _log_records(log_output) if r["event"] == "intake_poll_failed"]
    assert record["error"] == "runtime_error"
    assert record["where"].startswith("tests.test_worker:")
    assert "exception" not in record
    assert secret not in log_output.getvalue()
    assert "could not parse" not in log_output.getvalue()


async def test_stale_username_is_cleared_when_the_author_has_none(env: Env) -> None:
    worker = env.worker()
    env.post(1)  # user1
    await worker.poll_work_chat()
    # The profile could not be read: "no username" is unknown, the stored one stays.
    env.post(author=ResolvedAuthor(1, None, None, username_known=False))
    await worker.poll_work_chat()
    (client,) = await rows(env.factory, Client)
    assert client.username == "user1"

    env.post(author=ResolvedAuthor(1, None, "User 1"))  # the profile has no username now
    await worker.poll_work_chat()

    (client,) = await rows(env.factory, Client)
    assert (client.tg_user_id, client.username) == (1, None)


async def test_approval_older_than_the_ttl_goes_back_to_the_owner(
    env: Env, settings: Settings
) -> None:
    assert settings.DRAFT_TTL_HOURS == 24  # the default
    worker = env.worker()
    tg_id = env.post(1)
    await worker.poll_work_chat()
    drafted = await worker.send_tick()
    async with transaction(env.factory) as s:
        await approve(s, drafted.message_id, OWNER)  # type: ignore[arg-type]
        # Approved 25 hours ago (the account sat at its daily limit since).
        await s.execute(update(Draft).values(decided_at=func.now() - timedelta(hours=25)))

    assert (await worker.send_tick()).kind == "idle"

    assert not any(f.calls for f in env.ops)
    assert await _status(env.factory, tg_id) == ("awaiting_review", "stale_approval")
    (outbox,) = await _outboxes(env.factory, tg_id)
    assert (outbox.status, outbox.error) == ("failed", "stale_approval")
    old, again = await rows(env.factory, Draft)
    assert (old.status, again.status) == ("approved", "pending")
    assert (again.body, again.message_id) == (old.body, old.message_id)

    async with transaction(env.factory) as s:  # the owner approves it again
        await approve(s, drafted.message_id, OWNER)  # type: ignore[arg-type]
    result = await worker.send_tick()
    assert (result.kind, result.status) == ("sent", "sent")
    assert await _status(env.factory, tg_id) == ("answered", None)


async def test_approval_within_the_ttl_is_sent(env: Env) -> None:
    worker = env.worker()
    env.post(1)
    await worker.poll_work_chat()
    drafted = await worker.send_tick()
    async with transaction(env.factory) as s:
        await approve(s, drafted.message_id, OWNER)  # type: ignore[arg-type]
        await s.execute(update(Draft).values(decided_at=func.now() - timedelta(hours=23)))

    assert (await worker.send_tick()).status == "sent"


# --- Claude drafts (DRAFT_PROVIDER=llm) -----------------------------------------------

LEAD_TEXT = "Ищем квартиру в Барселоне на два месяца, заявка с секретной деталью"


class _ScriptedClaude(FakeClient):
    """A substituted Anthropic client answering each call with the next outcome."""

    def __init__(self, *outcomes: object) -> None:
        super().__init__(None)
        self.outcomes = list(outcomes)

        async def parse(**kwargs: object) -> object:
            self.messages.calls.append(dict(kwargs))
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.messages.parse = parse  # type: ignore[method-assign]


def _claude(env: Env, *outcomes: object, profile: bool = True) -> tuple[Worker, _ScriptedClaude]:
    client = _ScriptedClaude(*outcomes)
    provider = ClaudeDraftProvider(client, LLM_PROFILE)
    return env.worker(drafts=provider, profile=LLM_PROFILE if profile else None), client


async def test_llm_draft_is_sent_in_auto_mode(env: Env, log_output: io.StringIO) -> None:
    worker, client = _claude(env, llm_response(llm_result(review_id="villa")))
    await _auto_mode(env.factory)
    tg_id = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    result = await worker.send_tick()

    assert (result.kind, result.status) == ("sent", "sent")
    assert [text for f in env.ops for _, text in f.sent] == [GOOD_DRAFT]
    (call,) = client.messages.calls
    assert LEAD_TEXT in call["messages"][0]["content"]  # type: ignore[index]
    lead = await _lead(env.factory, tg_id)
    assert lead.meta["draft_info"] == {
        "provider": "llm",
        "country": "Испания",
        "need": "квартира в Барселоне на два месяца",
        "review_id": "villa",
        "confidence": 0.9,
    }
    # Neither the lead's text nor the draft reaches the log or the audit.
    assert LEAD_TEXT not in log_output.getvalue()
    assert GOOD_DRAFT not in log_output.getvalue()
    for event in await rows(env.factory, AuditLog):
        assert "секретной" not in json.dumps(event.details, ensure_ascii=False)
        assert "Барселон" not in json.dumps(event.details, ensure_ascii=False)


async def test_llm_draft_waits_for_the_owner_in_confirm_mode(env: Env) -> None:
    worker, _ = _claude(env, llm_response(llm_result()))
    tg_id = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "draft"

    assert await _status(env.factory, tg_id) == ("awaiting_review", "needs_approval")
    (draft,) = await rows(env.factory, Draft)
    assert (draft.body, draft.source, draft.issues) == (GOOD_DRAFT, "llm", [])
    assert not any(f.calls for f in env.ops)


@pytest.mark.parametrize(
    ("body", "issues"),
    [
        ("Здравствуйте! Советую сервис Chit Trip, там без предоплаты.", ["no_disclosure"]),
        (
            "Работаю с Chit Trip. Мы сами бронировали там виллу, скидка 20%.",
            ["percent", "customer_claim"],
        ),
        ("Работаю с Chit Trip. Клиенты пишут: «Лучший сервис на свете»", ["misquote"]),
    ],
)
async def test_llm_draft_breaking_the_rules_goes_to_the_owner_even_in_auto_mode(
    env: Env, body: str, issues: list[str]
) -> None:
    worker, _ = _claude(env, llm_response(llm_result(draft=body)))
    await _auto_mode(env.factory)
    tg_id = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "draft"

    assert not any(f.calls for f in env.ops)
    assert await _status(env.factory, tg_id) == ("awaiting_review", "needs_approval")
    (draft,) = await rows(env.factory, Draft)
    assert draft.issues == issues


@pytest.mark.parametrize(
    ("outcome", "reason", "error"),
    [
        (
            llm_response(llm_result(in_scope=False, country="Италия", draft=None)),
            "out_of_scope",
            None,
        ),
        (
            llm_response(None, "refusal", stop_details=SimpleNamespace(category=None)),
            "llm_refusal",
            "refusal",
        ),
        (llm_status_error(400), "llm_error", "http_400"),
        (llm_status_error(401), "llm_error", "http_401"),
    ],
    ids=["out_of_scope", "refusal", "bad_request", "unauthorized"],
)
async def test_lead_without_a_draft_goes_to_the_owner(
    env: Env, outcome: object, reason: str, error: str | None
) -> None:
    worker, client = _claude(env, outcome)
    await _auto_mode(env.factory)
    tg_id = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "idle"

    assert await _status(env.factory, tg_id) == ("awaiting_review", reason)
    assert await rows(env.factory, Draft) == []
    assert await rows(env.factory, Outbox) == []
    assert not any(f.calls for f in env.ops)
    lead = await _lead(env.factory, tg_id)
    assert lead.claimed_by is None
    (event,) = await rows(env.factory, AuditLog, AuditLog.action == "lead_not_drafted")
    assert event.details["reason"] == reason
    assert event.details["error"] == error
    if reason == "out_of_scope":
        assert lead.meta["draft_info"]["country"] == "Италия"
    # Nothing more happens to it on later ticks.
    assert (await worker.send_tick()).kind == "idle"
    assert len(client.messages.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        llm_status_error(429),
        llm_status_error(500),
        llm_status_error(529),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.test")),
    ],
    ids=["429", "500", "529", "network"],
)
async def test_passing_api_error_keeps_the_lead_for_the_next_tick(
    env: Env, error: BaseException, log_output: io.StringIO
) -> None:
    worker, client = _claude(env, error, llm_response(llm_result()))
    tg_id = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    first = await worker.send_tick()

    assert first.kind == "deferred"
    lead = await _lead(env.factory, tg_id)
    assert (lead.status, lead.claimed_by) == ("assigned", None)
    assert await rows(env.factory, Draft) == []
    (record,) = [r for r in _log_records(log_output) if r["event"] == "draft_unavailable"]
    assert record["message_id"] == lead.id
    assert record["error"].startswith("llm_")

    second = await worker.send_tick()

    assert second.kind == "draft"
    assert second.account_id == first.account_id  # the same operator keeps the lead
    assert await _status(env.factory, tg_id) == ("awaiting_review", "needs_approval")
    assert len(client.messages.calls) == 2
    assert LEAD_TEXT not in log_output.getvalue()


async def test_duplicate_leads_cost_no_claude_call(env: Env) -> None:
    worker, client = _claude(env, llm_response(llm_result()))
    await _auto_mode(env.factory)
    first = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()
    assert (await worker.send_tick()).kind == "sent"
    again = env.post(1, text="Ещё раз: ищу квартиру в Барселоне")
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "idle"

    assert len(client.messages.calls) == 1
    assert await _status(env.factory, first) == ("answered", None)
    status, _ = await _status(env.factory, again)
    assert status == "cancelled"


async def test_an_existing_dialog_costs_no_claude_call(env: Env) -> None:
    worker, client = _claude(env)
    for fake in env.ops:
        fake.dialogs.add(1)
    tg_id = env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    await worker.send_tick()

    assert client.messages.calls == []
    assert await _status(env.factory, tg_id) == ("cancelled", "duplicate_client")


async def test_worker_without_profile_checks_only_the_generic_rules(env: Env) -> None:
    undisclosed = "Здравствуйте! Подскажите, какой район вам удобен?"
    worker, _ = _claude(env, llm_response(llm_result(draft=undisclosed)), profile=False)
    await _auto_mode(env.factory)
    env.post(1, text=LEAD_TEXT)
    await worker.poll_work_chat()

    assert (await worker.send_tick()).kind == "sent"


async def test_worker_refuses_llm_mode_without_the_key(
    engine: AsyncEngine, make_account, monkeypatch, settings: Settings, tmp_path
) -> None:
    reader = await make_account(kind="main", session_env="TG_SESSION_READER")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "TG_API_ID", 1)
    monkeypatch.setattr(settings, "TG_API_HASH", SecretStr("h"))
    monkeypatch.setattr(settings, "WORK_CHAT_ID", CHAT)
    monkeypatch.setattr(settings, "READER_ACCOUNT_ID", reader.id)
    monkeypatch.setattr(settings, "DRAFT_PROVIDER", "llm")
    monkeypatch.setattr(settings, "PROFILE_PATH", str(LLM_FIXTURE))
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(entry, "configure_logging", lambda level: None)
    monkeypatch.setattr(entry, "create_engine", lambda: engine)

    with pytest.raises(SystemExit) as exc:
        await entry.main()
    assert str(exc.value.code) == "DRAFT_PROVIDER=llm needs ANTHROPIC_API_KEY"

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", SecretStr("sk-test-only"))
    monkeypatch.setattr(settings, "PROFILE_PATH", str(tmp_path / "missing.yaml"))
    with pytest.raises(SystemExit) as exc:
        await entry.main()
    assert "profile file not found" in str(exc.value.code)
