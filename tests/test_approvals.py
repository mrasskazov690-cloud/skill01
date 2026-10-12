"""Owner approvals: drafts, confirm-first-N, explicit auto mode, pause."""

# ruff: noqa: RUF001  (Russian reply texts by design)

import pytest

from app.config import Settings
from app.db.models import AppSetting, AuditLog, Draft, Message, Outbox
from app.db.session import transaction
from app.domain.errors import AutoModeLocked, DomainError, WrongState
from app.services import approvals
from app.services.approvals import (
    approve,
    approved_count,
    create_draft,
    enable_auto_mode,
    is_auto_mode,
    is_paused,
    list_pending,
    reject,
    set_paused,
)
from app.services.outbox import execute_send
from app.telegram.fake import FakeTelegramAccount
from tests.helpers import CLIENT_TEXT, Factory, get, processing_message, rows

DRAFT = "Здравствуйте! Квартира свободна, 45 000 руб., смотреть можно 12.10"
OWNER = "owner:olga"


@pytest.fixture
async def main_id(make_account) -> int:
    await make_account()  # the operator account that sends
    return (await make_account(kind="main")).id


async def _drafted(
    factory: Factory, main_id: int, client: int = 1, body: str = DRAFT, source: str = "llm"
) -> tuple[int, int, Draft]:
    message_id, account_id = await processing_message(factory, main_id, client)
    async with transaction(factory) as s:
        draft = await create_draft(
            s, message_id=message_id, account_id=account_id, body=body, source=source
        )
    return message_id, account_id, draft


async def test_draft_parks_the_message_for_the_owner(
    session_factory: Factory, main_id: int
) -> None:
    message_id, account_id, draft = await _drafted(session_factory, main_id)

    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason, message.claimed_by) == (
        "awaiting_review",
        "needs_approval",
        None,
    )
    assert (draft.status, draft.account_id, draft.source) == ("pending", account_id, "llm")
    assert draft.issues == ["price", "date", "availability"]  # the guard's findings
    async with session_factory() as s:
        assert [d.id for d in await list_pending(s)] == [draft.id]


async def test_one_pending_draft_per_message(session_factory: Factory, main_id: int) -> None:
    message_id, account_id, _ = await _drafted(session_factory, main_id)
    with pytest.raises(WrongState):  # no longer processing
        async with transaction(session_factory) as s:
            await create_draft(
                s, message_id=message_id, account_id=account_id, body="x", source="manual"
            )


async def test_approve_queues_the_draft_and_it_is_sent(
    session_factory: Factory, main_id: int
) -> None:
    message_id, account_id, draft = await _drafted(session_factory, main_id, source="template")
    async with transaction(session_factory) as s:
        outbox = await approve(s, message_id, OWNER)

    assert (outbox.status, outbox.body, outbox.kind, outbox.account_id) == (
        "queued",
        DRAFT,
        "template",
        account_id,
    )
    decided = await get(session_factory, Draft, draft.id)
    assert (decided.status, decided.decided_by) == ("approved", OWNER)
    assert decided.decided_at is not None
    assert (await get(session_factory, Message, message_id)).status == "processing"

    adapter = FakeTelegramAccount(account_id)
    assert await execute_send(session_factory, outbox.id, adapter) == "sent"
    assert adapter.sent_texts == [DRAFT]
    assert (await get(session_factory, Message, message_id)).status == "answered"


async def test_approve_with_an_edited_body(session_factory: Factory, main_id: int) -> None:
    message_id, _, draft = await _drafted(session_factory, main_id, source="template")
    async with transaction(session_factory) as s:
        outbox = await approve(s, message_id, OWNER, edited_body="Добрый день!")

    assert (outbox.body, outbox.kind) == ("Добрый день!", "manual")
    assert (await get(session_factory, Draft, draft.id)).body == "Добрый день!"


async def test_reject_cancels_the_lead_without_a_reply(
    session_factory: Factory, main_id: int
) -> None:
    message_id, _, draft = await _drafted(session_factory, main_id)
    async with transaction(session_factory) as s:
        await reject(s, message_id, OWNER)

    message = await get(session_factory, Message, message_id)
    assert (message.status, message.review_reason, message.reaction) == (
        "cancelled",
        "rejected_by_owner",
        None,
    )
    assert (await get(session_factory, Draft, draft.id)).status == "rejected"
    assert await rows(session_factory, Outbox) == []
    with pytest.raises(WrongState):  # decided drafts are final
        async with transaction(session_factory) as s:
            await approve(s, message_id, OWNER)


async def test_confirm_first_n_then_auto_mode_only_by_command(
    session_factory: Factory, main_id: int, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "CONFIRM_FIRST_N", 3)
    for client in range(1, 4):
        message_id, _, _ = await _drafted(session_factory, main_id, client)
        async with transaction(session_factory) as s:
            if client < 3:
                with pytest.raises(AutoModeLocked) as exc:
                    await enable_auto_mode(s, OWNER)
                assert isinstance(exc.value, DomainError)
            await approve(s, message_id, OWNER)

    async with transaction(session_factory) as s:
        assert await approved_count(s) == 3
        assert await is_auto_mode(s) is False  # reaching N switches nothing on by itself
        await enable_auto_mode(s, OWNER)
    async with session_factory() as s:
        assert await is_auto_mode(s) is True
    assert (await get(session_factory, AppSetting, "auto_mode")).updated_by == OWNER


async def test_default_confirm_first_n_is_twenty(
    session_factory: Factory, main_id: int, settings: Settings
) -> None:
    assert settings.CONFIRM_FIRST_N == 20
    rejected, _, _ = await _drafted(session_factory, main_id, client=99)
    async with transaction(session_factory) as s:
        await reject(s, rejected, OWNER)  # rejections do not count
    for client in range(1, 20):
        message_id, _, _ = await _drafted(session_factory, main_id, client)
        async with transaction(session_factory) as s:
            await approve(s, message_id, OWNER)
    with pytest.raises(AutoModeLocked):
        async with transaction(session_factory) as s:
            await enable_auto_mode(s, OWNER)

    message_id, _, _ = await _drafted(session_factory, main_id, client=20)
    async with transaction(session_factory) as s:
        await approve(s, message_id, OWNER)
        assert await is_auto_mode(s) is False
        await enable_auto_mode(s, OWNER)
        assert await is_auto_mode(s) is True


async def test_pause_any_time(session_factory: Factory) -> None:
    async with session_factory() as s:
        assert await is_paused(s) is False  # default
    async with transaction(session_factory) as s:
        await set_paused(s, True, OWNER)
    async with session_factory() as s:
        assert await is_paused(s) is True
    async with transaction(session_factory) as s:
        await set_paused(s, False, OWNER)
        assert await is_paused(s) is False
        await approvals.disable_auto_mode(s, OWNER)
        assert await is_auto_mode(s) is False


async def test_audit_has_no_draft_or_client_text(session_factory: Factory, main_id: int) -> None:
    first, _, _ = await _drafted(session_factory, main_id, client=1)
    second, _, _ = await _drafted(session_factory, main_id, client=2)
    async with transaction(session_factory) as s:
        await approve(s, first, OWNER, edited_body="Исправленный ответ владельца")
        await reject(s, second, OWNER)
        await set_paused(s, True, OWNER)

    audit = await rows(session_factory, AuditLog)
    actions = {a.action for a in audit}
    assert {"draft_created", "draft_approved", "draft_rejected", "setting_changed"} <= actions
    rendered = str([a.details for a in audit])
    for secret in (DRAFT, "Исправленный ответ владельца", CLIENT_TEXT):
        assert secret not in rendered
