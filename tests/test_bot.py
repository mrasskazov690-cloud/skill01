"""The owner's bot panel at handler level: real aiogram Dispatcher and Bot, a fake
Bot API session (no network), real PostgreSQL. Owner-only access, idempotent
buttons, the edit flow, commands and draft cards."""

# ruff: noqa: RUF001  (Russian panel texts by design)

import asyncio
import io
import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot import __main__ as bot_entry
from app.bot.handlers import FOREIGN_USER, build_dispatcher
from app.bot.notifier import CardNotifier
from app.bot.panel import CARD_KEY
from app.config import Settings
from app.db.models import AuditLog, Draft, Outbox
from app.db.models import Message as Lead
from app.db.session import transaction
from app.drafts.profile import load_profile
from app.logging import configure_logging
from app.services.approvals import (
    create_draft,
    expire_stale_approvals,
    is_auto_mode,
    is_paused,
)
from tests.helpers import Factory, get, processing_message, rows
from tests.test_drafts_llm import FIXTURE as PROFILE_FIXTURE

OWNER = 5_000_001
STRANGER = 5_000_002
PROFILE = load_profile(PROFILE_FIXTURE)
GOOD = (
    "Здравствуйте! Советую сервис Chit Trip, с которым сотрудничаю: жильё на Бали "
    "напрямую от собственников и без предоплаты. Напишите им, помогут найти выгодно."
)
RISKY = "Здравствуйте! Мы сами бронировали <b>виллу</b> со скидкой 30%, советую."


class FakeSession(BaseSession):
    """Bot API without a network: records every method and answers like Telegram."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.fail_with: type[Exception] | None = None
        self._ids = count(1000)

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109  (aiogram's signature)
    ) -> Any:
        if self.fail_with is not None:
            raise self.fail_with(method=method, message="Forbidden: bot was blocked")  # type: ignore[call-arg]
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText)):
            return True
        raise AssertionError(f"unexpected Bot API call {type(method).__name__}")

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    def of(self, kind: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, kind)]

    def texts(self) -> list[str]:
        return [r.text for r in self.of(SendMessage)]


@dataclass
class Panel:
    factory: Factory
    session: FakeSession
    bot: Bot
    dp: Dispatcher
    main_id: int
    updates: Iterator[int]

    def _user(self, user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name="Olga")

    async def say(self, text: str, *, user: int = OWNER, chat_type: str = "private") -> None:
        chat = Chat(id=user if chat_type == "private" else -100_123, type=chat_type)
        message = Message(
            message_id=next(self.updates),
            date=datetime.now(UTC),
            chat=chat,
            from_user=self._user(user),
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self.updates), message=message))

    async def press(self, data: str, *, user: int = OWNER, card_text: str = "card") -> None:
        card = Message(
            message_id=77,
            date=datetime.now(UTC),
            chat=Chat(id=user, type="private"),
            text=card_text,
        )
        query = CallbackQuery(
            id=str(next(self.updates)),
            from_user=self._user(user),
            chat_instance="ci",
            data=data,
            message=card,
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=next(self.updates), callback_query=query)
        )

    async def draft(
        self, client: int = 1, body: str = GOOD, issues: list[str] | None = None
    ) -> Draft:
        message_id, account_id = await processing_message(self.factory, self.main_id, client)
        async with transaction(self.factory) as s:
            return await create_draft(
                s,
                message_id=message_id,
                account_id=account_id,
                body=body,
                source="llm",
                issues=issues,
            )


@pytest.fixture
async def panel(session_factory: Factory, make_account) -> AsyncIterator[Panel]:
    await make_account(display_name="Анна")  # the operator that sends
    main = await make_account(kind="main")
    session = FakeSession()
    bot = Bot("42:TEST-TOKEN", session=session)
    dp = build_dispatcher(session_factory, owner_id=OWNER, profile=PROFILE)
    yield Panel(session_factory, session, bot, dp, main.id, count(1))


async def _audit(factory: Factory, action: str) -> list[AuditLog]:
    return await rows(factory, AuditLog, AuditLog.action == action)


# --- owner only --------------------------------------------------------------------------


async def test_stranger_is_ignored_and_audited_without_the_text(panel: Panel) -> None:
    draft = await panel.draft()

    await panel.say("/pause", user=STRANGER)
    await panel.press(f"send:{draft.id}", user=STRANGER)
    await panel.say("/status", user=OWNER, chat_type="group")  # the owner, but not in private

    assert panel.session.requests == []  # not a single answer
    assert not await is_paused_now(panel.factory)
    assert (await get(panel.factory, Draft, draft.id)).status == "pending"
    records = await _audit(panel.factory, FOREIGN_USER)
    assert [(r.actor, r.entity_type, r.entity_id) for r in records] == [
        (f"tg:{STRANGER}", "tg_user", STRANGER),
        (f"tg:{STRANGER}", "tg_user", STRANGER),
        (f"tg:{OWNER}", "tg_user", OWNER),
    ]
    assert [r.details for r in records] == [
        {"tg_user_id": STRANGER, "update": "message", "chat_type": "private"},
        {"tg_user_id": STRANGER, "update": "callback_query", "chat_type": "private"},
        {"tg_user_id": OWNER, "update": "message", "chat_type": "group"},
    ]


async def is_paused_now(factory: Factory) -> bool:
    async with factory() as s:
        return await is_paused(s)


# --- buttons ---------------------------------------------------------------------------


async def test_send_twice_queues_the_reply_once(panel: Panel) -> None:
    draft = await panel.draft()

    await panel.press(f"send:{draft.id}")
    await panel.press(f"send:{draft.id}")

    outbox = await rows(panel.factory, Outbox)
    assert [(o.message_id, o.body, o.status) for o in outbox] == [
        (draft.message_id, GOOD, "queued")
    ]
    assert (await get(panel.factory, Draft, draft.id)).status == "approved"
    assert (await get(panel.factory, Lead, draft.message_id)).status == "processing"
    assert len(await _audit(panel.factory, "draft_approved")) == 1
    answers = [a.text for a in panel.session.of(AnswerCallbackQuery)]
    assert answers == [
        "Ответ поставлен в очередь на отправку.",
        "Уже одобрено: ответ в очереди или отправлен.",
    ]
    # The pressed card shows the outcome and loses its buttons.
    edited = panel.session.of(EditMessageText)[0]
    assert "Одобрено" in edited.text and edited.reply_markup is None


async def test_concurrent_presses_change_the_state_once(panel: Panel) -> None:
    draft = await panel.draft()

    await asyncio.gather(*(panel.press(f"send:{draft.id}") for _ in range(4)))

    assert len(await rows(panel.factory, Outbox)) == 1
    assert len(await _audit(panel.factory, "draft_approved")) == 1


async def test_skip_twice_cancels_the_lead_once(panel: Panel) -> None:
    draft = await panel.draft()

    await panel.press(f"skip:{draft.id}")
    await panel.press(f"skip:{draft.id}")
    await panel.press(f"send:{draft.id}")  # too late: it was skipped

    lead = await get(panel.factory, Lead, draft.message_id)
    assert (lead.status, lead.review_reason) == ("cancelled", "rejected_by_owner")
    assert (await get(panel.factory, Draft, draft.id)).status == "rejected"
    assert await rows(panel.factory, Outbox) == []
    assert len(await _audit(panel.factory, "draft_rejected")) == 1
    answers = [a.text for a in panel.session.of(AnswerCallbackQuery)]
    assert answers == ["Пропущено.", "Уже пропущено.", "Уже пропущено."]


async def test_unknown_or_missing_draft(panel: Panel) -> None:
    await panel.press("send:999")
    await panel.press("drop:1")
    await panel.press("send:abc")

    answers = [a.text for a in panel.session.of(AnswerCallbackQuery)]
    assert answers == ["Черновик не найден.", "Неизвестная кнопка.", "Неизвестная кнопка."]


async def test_draft_of_a_lead_moved_elsewhere_is_refused(panel: Panel, make_account) -> None:
    draft = await panel.draft()
    other = await make_account()
    async with transaction(panel.factory) as s:
        await s.execute(
            update(Lead).where(Lead.id == draft.message_id).values(operator_account_id=other.id)
        )

    await panel.press(f"send:{draft.id}")

    assert await rows(panel.factory, Outbox) == []
    answer = panel.session.of(AnswerCallbackQuery)[0]
    assert answer.show_alert and "другому аккаунту" in answer.text


# --- the edit flow -----------------------------------------------------------------------


async def test_edit_replaces_the_text_with_a_guard_warning_then_send_uses_it(
    panel: Panel,
) -> None:
    draft = await panel.draft()
    notifier = CardNotifier(panel.bot, panel.factory, OWNER)
    assert await notifier.send_new_cards() == 1
    first_card = panel.session.of(SendMessage)[0]

    await panel.press(f"edit:{draft.id}")
    assert "Пришлите новый текст" in panel.session.texts()[-1]
    await panel.say(RISKY)

    stored = await get(panel.factory, Draft, draft.id)
    assert (stored.status, stored.body, stored.source) == ("pending", RISKY, "manual")
    assert stored.issues == ["percent", "customer_claim", "no_disclosure"]
    card = panel.session.of(SendMessage)[-1]
    assert card.chat_id == OWNER
    assert "Текст изменён. ⚠️ Проверка нашла замечания" in card.text
    assert "проценты, рассказ от лица клиента, нет раскрытия связи с сервисом" in card.text
    assert "&lt;b&gt;виллу&lt;/b&gt;" in card.text  # the owner's text is escaped
    assert isinstance(card.reply_markup, InlineKeyboardMarkup)
    # The old card loses its buttons; the new one is remembered for the draft.
    removed = panel.session.of(EditMessageReplyMarkup)
    assert [(r.chat_id, r.reply_markup) for r in removed] == [(OWNER, None)]
    lead = await get(panel.factory, Lead, draft.message_id)
    assert lead.meta[CARD_KEY]["draft_id"] == draft.id
    assert removed[0].message_id != lead.meta[CARD_KEY]["message_id"]
    assert first_card.text != card.text
    # No new card for the same draft from the notifier.
    assert await notifier.send_new_cards() == 0
    audit = (await _audit(panel.factory, "draft_edited"))[0]
    assert audit.details == {
        "message_id": draft.message_id,
        "source_before": "llm",
        "issues": ["percent", "customer_claim", "no_disclosure"],
    }

    await panel.press(f"send:{draft.id}")
    outbox = (await rows(panel.factory, Outbox))[0]
    assert (outbox.body, outbox.kind) == (RISKY, "manual")


async def test_edit_with_a_clean_text_passes_the_guard(panel: Panel) -> None:
    draft = await panel.draft(body=RISKY)

    await panel.press(f"edit:{draft.id}")
    await panel.say(GOOD)

    assert (await get(panel.factory, Draft, draft.id)).issues == []
    card = panel.session.texts()[-1]
    assert "Текст изменён." in card and "✅ Проверка пройдена" in card


async def test_edit_waits_for_text_and_can_be_cancelled(panel: Panel) -> None:
    draft = await panel.draft()

    await panel.press(f"edit:{draft.id}")
    await panel.say("/cancel")
    await panel.say("Совсем другой текст")  # no longer an edit

    assert (await get(panel.factory, Draft, draft.id)).body == GOOD
    assert panel.session.texts()[-2] == "Изменение отменено."


async def test_edit_of_a_draft_decided_meanwhile_changes_nothing(panel: Panel) -> None:
    draft = await panel.draft()

    await panel.press(f"edit:{draft.id}")
    await panel.press(f"skip:{draft.id}")
    await panel.say("Новый текст")
    await panel.press(f"edit:{draft.id}")

    assert (await get(panel.factory, Draft, draft.id)).body == GOOD
    assert await _audit(panel.factory, "draft_edited") == []
    assert panel.session.texts()[-1] == "Уже пропущено."
    assert panel.session.of(AnswerCallbackQuery)[-1].text == "Черновик уже решён или не найден."


# --- commands ----------------------------------------------------------------------------


async def test_pause_and_resume(panel: Panel) -> None:
    await panel.say("/pause")
    assert await is_paused_now(panel.factory)
    await panel.say("/resume")
    assert not await is_paused_now(panel.factory)

    assert [t.split(".")[0] for t in panel.session.texts()] == [
        "⏸ Отправка на паузе",
        "▶️ Отправка продолжается",
    ]
    actors = [r.actor for r in await _audit(panel.factory, "setting_changed")]
    assert actors == [f"owner:tg:{OWNER}"] * 2


async def test_auto_is_refused_before_n_approvals(
    panel: Panel, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "CONFIRM_FIRST_N", 2)
    drafts = [await panel.draft(client=c) for c in (1, 2)]

    await panel.say("/auto")
    assert panel.session.texts()[-1] == ("Автомат пока нельзя включить: одобрено 0 из 2 ответов.")
    await panel.press(f"send:{drafts[0].id}")
    await panel.say("/auto")
    assert panel.session.texts()[-1].endswith("одобрено 1 из 2 ответов.")
    async with panel.factory() as s:
        assert not await is_auto_mode(s)

    await panel.press(f"send:{drafts[1].id}")
    await panel.say("/auto")
    assert panel.session.texts()[-1].startswith("🤖 Автомат включён.")
    async with panel.factory() as s:
        assert await is_auto_mode(s)
    await panel.say("/manual")
    async with panel.factory() as s:
        assert not await is_auto_mode(s)


async def test_status_shows_mode_queue_and_sends_per_account(
    panel: Panel, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "CONFIRM_FIRST_N", 20)
    draft = await panel.draft()
    await panel.draft(client=2)
    await panel.press(f"send:{draft.id}")
    async with transaction(panel.factory) as s:
        outbox_id = await s.scalar(select(Outbox.id))
        await s.execute(
            update(Outbox)
            .where(Outbox.id == outbox_id)
            .values(status="sent", sent_at=datetime.now(UTC))
        )
    await panel.say("/pause")

    await panel.say("/status")

    report = panel.session.texts()[-1]
    assert "<b>Режим:</b> подтверждение (одобрено 1 из 20)" in report
    assert "<b>Отправка:</b> на паузе" in report
    assert "на проверке: 1 (needs_approval: 1)" in report
    assert "черновиков ждут решения: 1" in report
    assert "в работе: 1" in report
    assert "Анна (#1): 1/25 · active" in report
    assert "всего: 1/25" in report


async def test_help_for_anything_else(panel: Panel) -> None:
    await panel.say("/start")
    await panel.say("привет")
    assert all(t.startswith("Панель владельца.") for t in panel.session.texts())


# --- cards -------------------------------------------------------------------------------


async def test_card_shows_the_lead_the_draft_and_the_guard_marks(panel: Panel) -> None:
    draft = await panel.draft(body=RISKY, issues=["percent", "customer_claim"])
    async with transaction(panel.factory) as s:
        await s.execute(
            update(Lead)
            .where(Lead.id == draft.message_id)
            .values(
                meta={"draft_info": {"country": "Индонезия", "need": "вилла на Бали"}},
                link={"kind": "message", "url": "https://t.me/flats_src/1001"},
                tg_chat_id=-1001234567,
                tg_message_id=55,
            )
        )
    notifier = CardNotifier(panel.bot, panel.factory, OWNER)

    assert await notifier.send_new_cards() == 1
    assert await notifier.send_new_cards() == 0  # one card per draft

    card = panel.session.of(SendMessage)[0]
    assert card.chat_id == OWNER
    text = card.text
    assert f"<b>Заявка #{draft.message_id}</b> · черновик #{draft.id} · аккаунт Анна" in text
    assert "Страна: Индонезия" in text and "Запрос: вилла на Бали" in text
    assert "Ссылка: https://t.me/flats_src/1001" in text
    assert "В рабочем чате: https://t.me/c/1234567/55" in text
    assert "<blockquote>Здравствуйте! Мы сами бронировали &lt;b&gt;виллу&lt;/b&gt;" in text
    assert "⚠️ <b>Проверка:</b> проценты, рассказ от лица клиента." in text
    buttons = [b.callback_data for b in card.reply_markup.inline_keyboard[0]]
    assert buttons == [f"send:{draft.id}", f"edit:{draft.id}", f"skip:{draft.id}"]
    lead = await get(panel.factory, Lead, draft.message_id)
    assert lead.meta[CARD_KEY] == {
        "draft_id": draft.id,
        "chat_id": OWNER,
        "message_id": 1000,  # the id Telegram gave the card
    }


async def test_card_without_summary_shows_an_excerpt_of_the_lead(panel: Panel) -> None:
    await panel.draft()
    await CardNotifier(panel.bot, panel.factory, OWNER).send_new_cards()
    assert "Заявка: Здравствуйте, нужна квартира" in panel.session.texts()[0]


async def test_stale_approval_gets_a_new_card(panel: Panel, monkeypatch, settings) -> None:
    draft = await panel.draft()
    notifier = CardNotifier(panel.bot, panel.factory, OWNER)
    await notifier.send_new_cards()
    await panel.press(f"send:{draft.id}")
    async with transaction(panel.factory) as s:
        await s.execute(
            update(Draft)
            .where(Draft.id == draft.id)
            .values(decided_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        assert await expire_stale_approvals(s) == 1

    assert await notifier.send_new_cards() == 1
    card = panel.session.of(SendMessage)[-1].text
    assert "одобрение устарело" in card
    assert f"черновик #{draft.id + 1}" in card


async def test_card_that_cannot_be_delivered_is_tried_again(panel: Panel) -> None:
    draft = await panel.draft()
    notifier = CardNotifier(panel.bot, panel.factory, OWNER)
    panel.session.fail_with = TelegramForbiddenError

    assert await notifier.send_new_cards() == 0
    assert CARD_KEY not in (await get(panel.factory, Lead, draft.message_id)).meta

    panel.session.fail_with = None
    assert await notifier.send_new_cards() == 1


# --- privacy and the entry point ---------------------------------------------------------


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    yield buf
    structlog.reset_defaults()


async def test_texts_do_not_reach_the_log_or_the_audit(
    log_output: io.StringIO, panel: Panel
) -> None:
    draft = await panel.draft()
    await panel.say("секретный текст незнакомца", user=STRANGER)
    await panel.press(f"edit:{draft.id}")
    await panel.say("Новый текст владельца с Chit Trip")
    await panel.press(f"send:{draft.id}")

    logged = log_output.getvalue()
    assert "bot_foreign_user" in logged
    for text in ("секретный текст", "Новый текст владельца", GOOD):
        assert text not in logged
    async with panel.factory() as s:
        details = json.dumps(
            [a.details for a in (await s.scalars(select(AuditLog))).all()], ensure_ascii=False
        )
    assert "Новый текст" not in details and "секретный" not in details


async def test_bot_exits_without_token_or_owner(
    engine: AsyncEngine, monkeypatch, settings: Settings
) -> None:
    monkeypatch.setattr(bot_entry, "configure_logging", lambda level: None)
    monkeypatch.setattr(settings, "BOT_TOKEN", None)
    with pytest.raises(SystemExit) as exc:
        await bot_entry.main()
    assert exc.value.code == "BOT_TOKEN and OWNER_TG_USER_ID must be set"


def test_bot_settings_default_to_unset_and_keep_the_token_secret(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("BOT_TOKEN", "42:very-secret")
    monkeypatch.setenv("OWNER_TG_USER_ID", "5000001")
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.OWNER_TG_USER_ID == 5_000_001
    assert loaded.BOT_TOKEN is not None
    assert loaded.BOT_TOKEN.get_secret_value() == "42:very-secret"
    assert "very-secret" not in repr(loaded)


async def test_owner_id_setting_is_not_trusted_without_a_match(panel: Panel) -> None:
    # A message without a sender (channel post style) is not the owner's either.
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=OWNER, type="private"), text="/pause"
    )
    await panel.dp.feed_update(panel.bot, Update(update_id=999, message=message))
    assert panel.session.requests == []
    assert not await is_paused_now(panel.factory)
    assert [r.actor for r in await _audit(panel.factory, FOREIGN_USER)] == ["tg:unknown"]
