"""`python -m app.worker`: one process, intake and sender loops, real Telegram accounts.

Every enabled account with a session takes part: the reader (READER_ACCOUNT_ID) reads
the work chat, operator accounts send. An enabled operator account without a session
would be given leads it can never answer, so the worker refuses to start instead.
"""

import asyncio
import sys
from contextlib import AsyncExitStack

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Account
from app.db.session import create_engine, create_sessionmaker
from app.domain.states import AccountKind
from app.drafts.llm import ClaudeDraftProvider
from app.drafts.profile import ProfileError
from app.drafts.setup import DraftSetupError, make_draft_provider
from app.logging import configure_logging, get_logger
from app.telegram.base import TelegramAccount
from app.telegram.telethon_account import TelethonAccount
from app.worker.worker import Worker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    log = get_logger("app.worker")
    if settings.TG_API_ID is None or settings.TG_API_HASH is None:
        sys.exit("TG_API_ID and TG_API_HASH must be set")
    if settings.WORK_CHAT_ID is None or settings.READER_ACCOUNT_ID is None:
        sys.exit("WORK_CHAT_ID and READER_ACCOUNT_ID must be set")
    try:
        drafts, profile = make_draft_provider(settings)
    except (ProfileError, DraftSetupError) as exc:  # names the problem, not the contents
        sys.exit(str(exc))
    engine = create_engine()
    factory = create_sessionmaker(engine)
    try:
        async with factory() as s:
            accounts = (
                await s.execute(
                    select(Account.id, Account.kind, Account.session_env).where(
                        Account.enabled.is_(True)
                    )
                )
            ).all()
        missing = [a.id for a in accounts if a.kind == AccountKind.OPERATOR and not a.session_env]
        if missing:
            sys.exit(f"enabled operator accounts without session_env: {missing}")
        if not any(a.id == settings.READER_ACCOUNT_ID and a.session_env for a in accounts):
            sys.exit("READER_ACCOUNT_ID must be an enabled account with session_env")
        async with AsyncExitStack() as stack:
            if isinstance(drafts, ClaudeDraftProvider):
                stack.push_async_callback(drafts.aclose)
            adapters: dict[int, TelegramAccount] = {}
            for account in accounts:
                if not account.session_env:
                    continue
                try:
                    adapter = TelethonAccount.from_env(
                        account.id,
                        account.session_env,
                        api_id=settings.TG_API_ID,
                        api_hash=settings.TG_API_HASH.get_secret_value(),
                    )
                except ValueError as exc:  # names the variable, never its value
                    sys.exit(f"account {account.id}: {exc}")
                adapters[account.id] = await stack.enter_async_context(adapter)
            log.info(
                "worker_starting",
                accounts=sorted(adapters),
                draft_provider=settings.DRAFT_PROVIDER,
                profile=profile is not None,
            )
            await Worker(engine, factory, adapters, drafts=drafts, profile=profile).run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
