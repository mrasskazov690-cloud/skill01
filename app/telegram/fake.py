"""In-memory TelegramAccount for tests and local runs. Never touches the network."""

import asyncio
from collections import deque
from collections.abc import Iterable

from app.domain.states import AccountState
from app.telegram.base import (
    AccountHealth,
    PeerRef,
    ResolvedAuthor,
    SentMessage,
    SourceUnavailable,
    WorkChatMessage,
)
from app.telegram.links import LeadLink

# A scripted outcome for one send_text call: a SentMessage to return, an exception
# (instance or class) to raise, or None for an automatic success.
Outcome = SentMessage | BaseException | type[BaseException] | None
# A scripted outcome for one react call: an exception to raise, or None for success.
ReactOutcome = BaseException | type[BaseException] | None


def _is_error(outcome: object) -> bool:
    return isinstance(outcome, BaseException) or (
        isinstance(outcome, type) and issubclass(outcome, BaseException)
    )


class FakeTelegramAccount:
    def __init__(
        self,
        account_id: int,
        outcomes: Iterable[Outcome] = (),
        *,
        health: AccountHealth | None = None,
        delay: float = 0.0,
    ) -> None:
        self._account_id = account_id
        self._outcomes: deque[Outcome] = deque(outcomes)
        self._react_outcomes: deque[ReactOutcome] = deque()
        self.health = health or AccountHealth(AccountState.ACTIVE)
        self.delay = delay
        self.calls: list[tuple[PeerRef, str]] = []  # every send_text attempt
        self.sent: list[tuple[PeerRef, str]] = []  # attempts that returned success
        self.random_ids: list[int | None] = []  # random_id of every send_text attempt
        self.reactions: list[tuple[int, int, str]] = []  # successful (chat, message, emoji)
        self.work_chat: dict[int, list[WorkChatMessage]] = {}
        self.authors: dict[LeadLink, ResolvedAuthor | BaseException] = {}
        self.dialogs: set[int] = set()  # users this account has written to in private
        self._delivered: dict[int, SentMessage] = {}  # random_id -> first delivery
        self._next_message_id = 1

    @property
    def account_id(self) -> int:
        return self._account_id

    @property
    def sent_texts(self) -> list[str]:
        return [text for _, text in self.sent]

    def script(self, *outcomes: Outcome) -> None:
        """Queue outcomes for the next send_text calls; when empty, sends succeed."""
        self._outcomes.extend(outcomes)

    def script_reactions(self, *outcomes: ReactOutcome) -> None:
        """Queue outcomes for the next react calls; when empty, reactions succeed."""
        self._react_outcomes.extend(outcomes)

    def add_work_message(self, message: WorkChatMessage) -> None:
        self.work_chat.setdefault(message.chat_id, []).append(message)

    def set_author(self, link: LeadLink, outcome: ResolvedAuthor | BaseException) -> None:
        self.authors[link] = outcome

    async def fetch_work_chat(
        self, chat_id: int, *, min_id: int = 0, limit: int
    ) -> list[WorkChatMessage]:
        if chat_id not in self.work_chat:
            raise SourceUnavailable(f"chat {chat_id}")
        newer = [m for m in self.work_chat[chat_id] if m.message_id > min_id]
        return sorted(newer, key=lambda m: m.message_id, reverse=True)[:limit]

    async def resolve_author(self, link: LeadLink) -> ResolvedAuthor:
        outcome = self.authors.get(link)
        if outcome is None:
            raise SourceUnavailable("unknown link")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def has_dialog_with(self, tg_user_id: int) -> bool:
        return tg_user_id in self.dialogs

    async def send_text(
        self, peer: PeerRef, text: str, *, random_id: int | None = None
    ) -> SentMessage:
        self.calls.append((peer, text))
        self.random_ids.append(random_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if random_id is not None and random_id in self._delivered:
            # What Telegram answers to a repeated random_id: RANDOM_ID_DUPLICATE.
            return SentMessage(None, duplicate=True)
        outcome = self._outcomes.popleft() if self._outcomes else None
        if _is_error(outcome):
            raise outcome  # type: ignore[misc]
        if outcome is None:
            outcome = SentMessage(self._next_message_id)
            self._next_message_id += 1
        assert isinstance(outcome, SentMessage)
        self.sent.append((peer, text))
        if random_id is not None:
            self._delivered[random_id] = outcome
        if peer.tg_user_id is not None:
            self.dialogs.add(peer.tg_user_id)
        return outcome

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        outcome = self._react_outcomes.popleft() if self._react_outcomes else None
        if _is_error(outcome):
            raise outcome  # type: ignore[misc]
        self.reactions.append((chat_id, message_id, emoji))

    async def check_connection(self) -> AccountHealth:
        return self.health
