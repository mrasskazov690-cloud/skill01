import pytest

from app.domain.states import AccountState
from app.telegram.base import (
    AccountHealth,
    FloodWait,
    PeerFlood,
    PeerInvalid,
    PermanentSendError,
    SendTimeout,
    SentMessage,
    TelegramAccount,
)
from app.telegram.fake import FakeTelegramAccount


async def test_scripted_outcomes_then_default_success() -> None:
    fake = FakeTelegramAccount(3, [FloodWait(30), SendTimeout, SentMessage(900)])
    assert isinstance(fake, TelegramAccount)

    with pytest.raises(FloodWait) as exc:
        await fake.send_text(10, "first")
    assert exc.value.seconds == 30
    with pytest.raises(SendTimeout):
        await fake.send_text(10, "second")
    assert await fake.send_text(10, "third") == SentMessage(900)
    assert (await fake.send_text("@user", "fourth")).tg_message_id == 1

    assert [text for _, text in fake.calls] == ["first", "second", "third", "fourth"]
    assert fake.sent_texts == ["third", "fourth"]


async def test_health_and_error_hierarchy() -> None:
    fake = FakeTelegramAccount(1, health=AccountHealth(AccountState.RESTRICTED, "peer flood"))
    health = await fake.check_connection()
    assert not health.ok and health.state is AccountState.RESTRICTED
    assert issubclass(PeerInvalid, PermanentSendError)
    assert not issubclass(PeerFlood, PermanentSendError)
