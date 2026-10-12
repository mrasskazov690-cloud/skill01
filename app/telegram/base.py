"""Library-agnostic Telegram account interface and the errors adapters must map to.

Adapters (Bot API, MTProto, ...) translate their library's errors into these
exceptions so that services/outbox can decide what to do without knowing the library.
Exception messages must never contain client text or reply bodies.
"""

from dataclasses import dataclass, field
from datetime import datetime
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
    """A delivered message. `duplicate=True`: Telegram answered RANDOM_ID_DUPLICATE, so
    an earlier request with the same random_id was delivered; its id is unknown."""

    tg_message_id: int | None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedAuthor:
    tg_user_id: int
    username: str | None
    display_name: str | None
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class WorkChatMessage:
    chat_id: int
    message_id: int
    date: datetime
    text: str
    links: list[LeadLink] = field(default_factory=list)
    reactions: set[str] = field(default_factory=set)
    forwarded: bool = False


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

    async def fetch_work_chat(
        self, chat_id: int, *, min_id: int = 0, limit: int
    ) -> list[WorkChatMessage]:
        """Messages newer than `min_id`, newest first, at most `limit`."""
        ...

    async def resolve_author(self, link: LeadLink) -> ResolvedAuthor:
        """The user behind a lead link: the sender of the linked message or the linked
        profile. Raises SourceUnavailable or AuthorUnavailable."""
        ...

    async def has_dialog_with(self, tg_user_id: int) -> bool:
        """Whether this account has sent at least one message to the user in private."""
        ...

    async def send_text(self, peer: PeerRef, text: str, *, random_id: int) -> SentMessage: ...

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None: ...

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


class PeerMismatch(PermanentSendError):
    """The username now points to another user than the client's tg_user_id."""


class PrivacyRestricted(PermanentSendError):
    """The user does not accept private messages from us (incl. PRIVACY_PREMIUM_REQUIRED)."""


class UserBlocked(PermanentSendError):
    """The user has blocked this account."""


class SourceUnavailable(TelegramError):
    """The lead's source (chat or message) cannot be read: private, deleted, not a member."""


class AuthorUnavailable(TelegramError):
    """The lead has no reachable user author: anonymous admin, channel, bot, deleted account."""
