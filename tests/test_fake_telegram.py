from datetime import UTC, datetime

import pytest

from app.domain.states import AccountState
from app.telegram.base import (
    AccountHealth,
    AuthorUnavailable,
    FloodWait,
    PeerFlood,
    PeerInvalid,
    PeerMismatch,
    PeerRef,
    PermanentSendError,
    PrivacyRestricted,
    ResolvedAuthor,
    SendTimeout,
    SentMessage,
    SourceUnavailable,
    TelegramAccount,
    TelegramError,
    UserBlocked,
    WorkChatMessage,
)
from app.telegram.fake import FakeTelegramAccount
from app.telegram.links import LeadLink


async def test_scripted_outcomes_then_default_success() -> None:
    fake = FakeTelegramAccount(3, [FloodWait(30), SendTimeout, SentMessage(900)])
    assert isinstance(fake, TelegramAccount)
    peer = PeerRef(tg_user_id=10, username="user")

    with pytest.raises(FloodWait) as exc:
        await fake.send_text(peer, "first")
    assert exc.value.seconds == 30
    with pytest.raises(SendTimeout):
        await fake.send_text(peer, "second")
    assert await fake.send_text(peer, "third") == SentMessage(900)
    assert (await fake.send_text(PeerRef(tg_user_id=11), "fourth")).tg_message_id == 1

    assert [text for _, text in fake.calls] == ["first", "second", "third", "fourth"]
    assert fake.sent == [(peer, "third"), (PeerRef(tg_user_id=11), "fourth")]


async def test_health_and_error_hierarchy() -> None:
    fake = FakeTelegramAccount(1, health=AccountHealth(AccountState.RESTRICTED, "peer flood"))
    health = await fake.check_connection()
    assert not health.ok and health.state is AccountState.RESTRICTED
    assert issubclass(PeerInvalid, PermanentSendError)
    assert not issubclass(PeerFlood, PermanentSendError)


async def test_repeated_random_id_is_a_duplicate_not_a_second_message() -> None:
    fake = FakeTelegramAccount(1, [SendTimeout])
    peer = PeerRef(tg_user_id=10)
    with pytest.raises(SendTimeout):
        await fake.send_text(peer, "hi", random_id=5)  # not delivered: may be repeated
    assert await fake.send_text(peer, "hi", random_id=5) == SentMessage(1)
    assert await fake.send_text(peer, "hi", random_id=5) == SentMessage(None, duplicate=True)
    assert fake.sent == [(peer, "hi")] and fake.random_ids == [5, 5, 5]
    assert await fake.has_dialog_with(10) and not await fake.has_dialog_with(11)


async def test_work_chat_authors_and_reactions() -> None:
    fake = FakeTelegramAccount(1)
    link = LeadLink(kind="message", chat_username="arenda_msk", message_id=7, url="u")
    when = datetime(2026, 10, 12, tzinfo=UTC)
    for message_id in (3, 1, 2):
        fake.add_work_message(WorkChatMessage(-100, message_id, when, f"m{message_id}", [link]))
    assert [m.message_id for m in await fake.fetch_work_chat(-100, min_id=1, limit=5)] == [3, 2]
    assert [m.message_id for m in await fake.fetch_work_chat(-100, limit=1)] == [3]
    with pytest.raises(SourceUnavailable):
        await fake.fetch_work_chat(-200, limit=1)

    author = ResolvedAuthor(42, "ivan", "Иван")
    fake.set_author(link, author)
    assert await fake.resolve_author(link) == author
    fake.set_author(link, AuthorUnavailable("bot"))
    with pytest.raises(AuthorUnavailable):
        await fake.resolve_author(link)
    with pytest.raises(SourceUnavailable):
        await fake.resolve_author(LeadLink(kind="user", username="nobody", url="u2"))

    fake.script_reactions(FloodWait(3))
    with pytest.raises(FloodWait):
        await fake.react(-100, 3, "❤️")
    await fake.react(-100, 3, "❤️")
    assert fake.reactions == [(-100, 3, "❤️")]


def test_new_errors_are_permanent_except_resolution_ones() -> None:
    for error in (PeerMismatch, PrivacyRestricted, UserBlocked):
        assert issubclass(error, PermanentSendError)
    for error in (SourceUnavailable, AuthorUnavailable):
        assert issubclass(error, TelegramError) and not issubclass(error, PermanentSendError)
