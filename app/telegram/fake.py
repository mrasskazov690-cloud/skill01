"""In-memory TelegramAccount for tests and local runs. Never touches the network."""

import asyncio
from collections import deque
from collections.abc import Iterable

from app.domain.states import AccountState
from app.telegram.base import AccountHealth, PeerRef, SentMessage

# A scripted outcome for one send_text call: a SentMessage to return, an exception
# (instance or class) to raise, or None for an automatic success.
Outcome = SentMessage | BaseException | type[BaseException] | None


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
        self.health = health or AccountHealth(AccountState.ACTIVE)
        self.delay = delay
        self.calls: list[tuple[PeerRef, str]] = []  # every send_text attempt
        self.sent: list[tuple[PeerRef, str]] = []  # attempts that returned success
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

    async def send_text(self, peer: PeerRef, text: str) -> SentMessage:
        self.calls.append((peer, text))
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self._outcomes.popleft() if self._outcomes else None
        if isinstance(outcome, BaseException) or (
            isinstance(outcome, type) and issubclass(outcome, BaseException)
        ):
            raise outcome
        if outcome is None:
            outcome = SentMessage(self._next_message_id)
            self._next_message_id += 1
        self.sent.append((peer, text))
        return outcome

    async def check_connection(self) -> AccountHealth:
        return self.health
