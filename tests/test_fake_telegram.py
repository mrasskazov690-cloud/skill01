import pytest

from app.domain.states import AccountState
from app.telegram.base import (
    AccountHealth,
    FloodWait,
    PeerFlood,
    PeerInvalid,
    PeerRef,
    PermanentSendError,
    SendTimeout,
    SentMessage,
    TelegramAccount,
)
from app.telegram.fake import FakeTelegramAccount


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
