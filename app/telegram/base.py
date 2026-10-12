"""Library-agnostic Telegram account interface and the errors adapters must map to.

Adapters (Bot API, MTProto, ...) translate their library's errors into these
exceptions so that services/outbox can decide what to do without knowing the library.
Exception messages must never contain client text or reply bodies.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.states import AccountState
from app.telegram.links import LeadLink


@dataclass(frozen=True, slots=True)
class PeerRef:
    """The author a reply is for. Adapters deliver only to `tg_user_id`: through the
    sender of the lead's original message (`source`, opened by the sending account
    itself) or through `username` if it resolves to that same id right now. A stored
    username may have been released and taken by someone else since."""

    tg_user_id: int | None
    username: str | None = None
    source: LeadLink | None = None


@dataclass(frozen=True, slots=True)
class SentMessage:
    tg_message_id: int


@dataclass(frozen=True, slots=True)
class AccountHealth:
    state: AccountState
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state is AccountState.ACTIVE


@runtime_checkable
class TelegramAccount(Protocol):
    @property
    def account_id(self) -> int: ...

    async def send_text(self, peer: PeerRef, text: str) -> SentMessage: ...

    async def check_connection(self) -> AccountHealth: ...


class TelegramError(Exception):
    """Base class for adapter errors."""


class SendTimeout(TelegramError):
    """The request timed out; the message may or may not have been delivered."""


class FloodWait(TelegramError):
    """The server refused the request (nothing sent) and asks to wait `seconds`."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class PeerFlood(TelegramError):
    """The account is limited for sending; stop sending from it, do not retry elsewhere."""


class PermanentSendError(TelegramError):
    """The message will never be delivered as requested; do not retry."""


class PeerInvalid(PermanentSendError):
    """The recipient does not exist or cannot be messaged."""
