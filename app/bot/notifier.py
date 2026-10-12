"""Sends the owner a card for every pending draft not shown yet.

Drafts are created by the worker (another process), so the bot polls the database.
A card is remembered in messages.meta['bot_card'] with the draft id, so each draft
gets one card, and a new draft of the same lead (stale approval) gets a new one.
"""

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

from app.bot.handlers import Factory, send_card
from app.bot.panel import drafts_without_card
from app.logging import get_logger

CARD_POLL_SECONDS = 10


class CardNotifier:
    def __init__(
        self, bot: Bot, factory: Factory, owner_id: int, interval: float = CARD_POLL_SECONDS
    ) -> None:
        self.bot = bot
        self.factory = factory
        self.owner_id = owner_id
        self.interval = interval
        self._last_error: str | None = None
        self._log = get_logger(__name__)

    async def send_new_cards(self) -> int:
        """One round: cards for pending drafts without one. Returns how many were sent.
        A card Telegram refuses (e.g. too long) is replaced by a brief one, so it does
        not hold up the drafts after it; an error for the whole chat (bot blocked, flood
        wait, network) ends the round, and the drafts are tried again on the next one."""
        async with self.factory() as s:
            draft_ids = await drafts_without_card(s)
        sent = 0
        for draft_id in draft_ids:
            try:
                try:
                    shown = await send_card(self.bot, self.factory, self.owner_id, draft_id)
                except TelegramBadRequest as exc:
                    # This card only: the brief card has no text that can be refused.
                    # If Telegram refuses that too, the chat is the problem (below).
                    self._log.warning(
                        "bot_card_refused", draft_id=draft_id, error=type(exc).__name__
                    )
                    shown = await send_card(
                        self.bot, self.factory, self.owner_id, draft_id, brief=True
                    )
                if shown:
                    sent += 1
            except TelegramRetryAfter as exc:
                self._log.warning("bot_card_flood_wait", seconds=exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                break
            except TelegramAPIError as exc:
                # E.g. the owner has not started the bot yet: say so once, not every round.
                code = type(exc).__name__
                if code != self._last_error:
                    self._log.warning("bot_card_failed", draft_id=draft_id, error=code)
                    self._last_error = code
                break
        else:
            self._last_error = None
        return sent

    async def run(self) -> None:
        while True:
            try:
                await self.send_new_cards()
            except Exception as exc:  # database or network hiccup: next round; no text
                self._log.warning("bot_cards_round_failed", error=type(exc).__name__)
            await asyncio.sleep(self.interval)
