import io
import json
from collections.abc import Iterator

import pytest
import structlog
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Conversation, Message
from app.db.session import transaction
from app.logging import configure_logging, get_logger, is_sensitive_key
from app.services.ingest import ClientRef, ingest_message

CLIENT_TEXT = "Здравствуйте, нужна квартира на Ленина 5, тел +7 900 000-00-00"


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", buf)
    yield buf
    structlog.reset_defaults()


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def test_sensitive_keys_are_removed_at_any_depth(log_output: io.StringIO) -> None:
    get_logger("test").info(
        "message_ingested",
        message_id=42,
        text=CLIENT_TEXT,
        body="reply body",
        password="pw-1",
        bot_token="tok-1",
        api_hash="hash-1",
        session_string="sess-1",
        client_secret="sec-1",
        details={
            "status": "pending",
            "message_text": CLIENT_TEXT,
            "items": [{"Body": "b-2", "id": 7}],
        },
    )
    output = log_output.getvalue()
    for leaked in (CLIENT_TEXT, "reply body", "pw-1", "tok-1", "hash-1", "sess-1", "sec-1", "b-2"):
        assert leaked not in output
    [record] = _records(log_output)
    assert record["event"] == "message_ingested"
    assert record["message_id"] == 42
    assert record["logger"] == "test"
    assert record["details"] == {"status": "pending", "items": [{"id": 7}]}


def test_level_filtering(log_output: io.StringIO) -> None:
    log = get_logger()
    log.debug("hidden")
    log.warning("shown")
    assert [r["event"] for r in _records(log_output)] == ["shown"]


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("text", True),
        ("TEXT", True),
        ("reply-body", True),
        ("database_url", True),
        ("dsn", True),
        ("context", False),
        ("message_id", False),
        ("textual", False),
        ("status", False),
    ],
)
def test_is_sensitive_key(key: str, sensitive: bool) -> None:
    assert is_sensitive_key(key) is sensitive


async def test_db_errors_do_not_carry_client_text(
    session_factory: async_sessionmaker[AsyncSession], make_account
) -> None:
    account = await make_account(kind="main")
    async with transaction(session_factory) as s:
        await ingest_message(
            s,
            source="direct",
            source_account_id=account.id,
            tg_chat_id=1,
            tg_message_id=1,
            client=ClientRef(tg_user_id=1),
            text=CLIENT_TEXT,
        )
    async with session_factory() as s:
        conversation_id = await s.scalar(select(Conversation.id))
    with pytest.raises(IntegrityError) as exc:
        async with transaction(session_factory) as s:
            await s.execute(
                insert(Message).values(
                    source="direct",
                    source_account_id=account.id,
                    tg_chat_id=1,
                    tg_message_id=1,
                    conversation_id=conversation_id,
                    text=CLIENT_TEXT,
                )
            )
    assert CLIENT_TEXT not in str(exc.value)
