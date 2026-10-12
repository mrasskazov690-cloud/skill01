"""`python -m app.bot`: the owner's panel, long polling, a process of its own.

It is not a task of `python -m app.worker`: getUpdates allows one poller per bot
token, while workers may run as several instances (only one sends, the others wait
for the sender lock); and the panel must keep working while the worker is stopped or
restarted. The two processes share only the database.
"""

import asyncio
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions

from app.bot.handlers import build_dispatcher
from app.bot.notifier import CardNotifier
from app.config import Settings, get_settings
from app.db.session import create_engine, create_sessionmaker
from app.drafts.profile import ProfileError, ServiceProfile, load_profile
from app.logging import configure_logging, get_logger


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    log = get_logger("app.bot")
    if settings.BOT_TOKEN is None or settings.OWNER_TG_USER_ID is None:
        sys.exit("BOT_TOKEN and OWNER_TG_USER_ID must be set")
    try:
        profile = _profile(settings)
    except ProfileError as exc:  # names the field, not its contents
        sys.exit(str(exc))
    engine = create_engine()
    factory = create_sessionmaker(engine)
    bot = Bot(
        settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )
    dp = build_dispatcher(factory, owner_id=settings.OWNER_TG_USER_ID, profile=profile)
    notifier = CardNotifier(bot, factory, settings.OWNER_TG_USER_ID)
    log.info("bot_starting", profile=profile is not None)
    try:
        async with asyncio.TaskGroup() as tasks:
            cards = tasks.create_task(notifier.run())
            try:
                await dp.start_polling(bot)  # returns on SIGINT/SIGTERM
            finally:
                cards.cancel()
    finally:
        await engine.dispose()


def _profile(settings: Settings) -> ServiceProfile | None:
    """The guard checks the owner's own texts against the profile, as the worker does:
    required with DRAFT_PROVIDER=llm, used when the file exists otherwise."""
    if settings.DRAFT_PROVIDER == "llm" or Path(settings.PROFILE_PATH).is_file():
        return load_profile(settings.PROFILE_PATH)
    return None


if __name__ == "__main__":
    asyncio.run(main())
