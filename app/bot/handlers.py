"""aiogram handlers of the owner's panel.

Every update passes OwnerOnly first: only OWNER_TG_USER_ID in a private chat with the
bot gets through. Anyone else is ignored (no answer at all) and written to the audit
log by Telegram user id. Buttons carry the internal draft id ("send:<id>",
"edit:<id>", "skip:<id>"); each press is checked against the database under the
draft's locks, so pressing a button twice changes the state once.
"""

# ruff: noqa: RUF001  (Russian panel texts by design)

from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.panel import (
    CALLBACK,
    EDIT,
    SEND,
    card_keyboard,
    load_card,
    remember_card,
    render_brief_card,
    render_card,
    status_report,
)
from app.config import get_settings
from app.db.session import transaction
from app.domain.errors import AutoModeLocked, DomainError, NotFound, NotOwner, WrongState
from app.logging import get_logger
from app.services.approvals import (
    DraftDecision,
    approve_draft,
    approved_count,
    disable_auto_mode,
    edit_draft,
    enable_auto_mode,
    reject_draft,
    set_paused,
)
from app.services.audit import write_audit

if TYPE_CHECKING:
    from app.drafts.profile import ServiceProfile

Factory = async_sessionmaker[AsyncSession]
FOREIGN_USER = "bot_foreign_user"

HELP = (
    "Панель владельца.\n"
    "Карточки черновиков приходят сюда сами: «Отправить», «Изменить», «Пропустить».\n\n"
    "/status — очередь, отправлено сегодня, лимиты, режим\n"
    "/pause — остановить отправку\n"
    "/resume — продолжить отправку\n"
    "/auto — включить автомат (после нужного числа одобрений)\n"
    "/manual — выключить автомат, всё снова через подтверждение\n"
    "/cancel — отменить изменение текста"
)
_ALREADY = {
    "already_approved": "Уже одобрено: ответ в очереди или отправлен.",
    "already_rejected": "Уже пропущено.",
    "not_found": "Черновик не найден.",
}


class EditDraft(StatesGroup):
    text = State()  # the owner's next message is the new text of data["draft_id"]


def owner_actor(user: User) -> str:
    return f"owner:tg:{user.id}"


class OwnerOnly(BaseMiddleware):
    """Lets through only the owner in a private chat; everyone else is ignored and
    audited (user id and update kind, never the text)."""

    def __init__(self, factory: Factory, owner_id: int) -> None:
        self.factory = factory
        self.owner_id = owner_id
        self._log = get_logger(__name__)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        private = chat is None or chat.type == "private"
        if user is not None and user.id == self.owner_id and private:
            return await handler(event, data)
        kind = "callback_query" if isinstance(event, CallbackQuery) else "message"
        details = {
            "tg_user_id": user.id if user else None,
            "update": kind,
            "chat_type": chat.type if chat else None,
        }
        self._log.warning(FOREIGN_USER, **details)
        try:
            async with transaction(self.factory) as s:
                await write_audit(
                    s,
                    f"tg:{user.id}" if user else "tg:unknown",
                    FOREIGN_USER,
                    "tg_user",
                    user.id if user else None,
                    details,
                )
        except Exception as exc:  # the audit must not crash polling; no text in it
            self._log.warning("bot_audit_failed", error=type(exc).__name__)
        return None


def build_dispatcher(
    factory: Factory,
    *,
    owner_id: int,
    profile: "ServiceProfile | None" = None,
    storage: BaseStorage | None = None,
) -> Dispatcher:
    """The Dispatcher of the panel. `factory` and `profile` reach handlers as kwargs."""
    dp = Dispatcher(storage=storage or MemoryStorage(), factory=factory, profile=profile)
    guard = OwnerOnly(factory, owner_id)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(make_router())
    return dp


def make_router() -> Router:
    """A new Router (a router can be attached to one Dispatcher only). Order matters:
    commands first, then the edit flow, then the fallback."""
    router = Router(name="owner_panel")
    router.message.register(on_help, CommandStart())
    router.message.register(on_help, Command("help"))
    router.message.register(on_status, Command("status"))
    router.message.register(on_pause, Command("pause"))
    router.message.register(on_resume, Command("resume"))
    router.message.register(on_auto, Command("auto"))
    router.message.register(on_manual, Command("manual"))
    router.message.register(on_cancel, Command("cancel"))
    router.message.register(on_new_text, StateFilter(EditDraft.text), F.text)
    router.message.register(on_new_text_not_text, StateFilter(EditDraft.text))
    router.message.register(on_other)
    router.callback_query.register(on_button)
    return router


# --- commands --------------------------------------------------------------------------


async def on_help(message: Message) -> None:
    await message.answer(HELP)


async def on_status(message: Message, factory: Factory) -> None:
    async with factory() as s:
        report = await status_report(s)
    await message.answer(report)


async def on_pause(message: Message, factory: Factory) -> None:
    assert message.from_user is not None
    async with transaction(factory) as s:
        await set_paused(s, True, owner_actor(message.from_user))
    await message.answer("⏸ Отправка на паузе. Приём заявок продолжается. /resume — продолжить.")


async def on_resume(message: Message, factory: Factory) -> None:
    assert message.from_user is not None
    async with transaction(factory) as s:
        await set_paused(s, False, owner_actor(message.from_user))
    await message.answer("▶️ Отправка продолжается.")


async def on_auto(message: Message, factory: Factory) -> None:
    assert message.from_user is not None
    required = get_settings().CONFIRM_FIRST_N
    try:
        async with transaction(factory) as s:
            await enable_auto_mode(s, owner_actor(message.from_user))
    except AutoModeLocked:
        async with factory() as s:
            approved = await approved_count(s)
        await message.answer(
            f"Автомат пока нельзя включить: одобрено {approved} из {required} ответов."
        )
        return
    await message.answer(
        "🤖 Автомат включён. Черновики без замечаний проверки уходят сами, "
        "с замечаниями — по-прежнему к вам. /manual — выключить."
    )


async def on_manual(message: Message, factory: Factory) -> None:
    assert message.from_user is not None
    async with transaction(factory) as s:
        await disable_auto_mode(s, owner_actor(message.from_user))
    await message.answer("Автомат выключен: каждый ответ — после вашего подтверждения.")


async def on_cancel(message: Message, state: FSMContext) -> None:
    editing = await state.get_state() is not None
    await state.clear()
    await message.answer("Изменение отменено." if editing else "Нечего отменять.")


# --- the edit flow ---------------------------------------------------------------------


async def on_new_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    factory: Factory,
    profile: "ServiceProfile | None",
) -> None:
    assert message.from_user is not None and message.text is not None
    draft_id = (await state.get_data()).get("draft_id")
    await state.clear()
    if not isinstance(draft_id, int):
        await message.answer("Нечего изменять.")
        return
    try:
        async with transaction(factory) as s:
            decision = await edit_draft(
                s, draft_id, message.text, owner_actor(message.from_user), profile
            )
    except DomainError as exc:
        await message.answer(_domain_error_text(exc))
        return
    if decision.outcome != "edited":
        await message.answer(_ALREADY.get(decision.outcome, "Черновик уже решён."))
        return
    note = (
        "Текст изменён. ⚠️ Проверка нашла замечания — посмотрите перед отправкой."
        if decision.issues
        else "Текст изменён."
    )
    await send_card(bot, factory, message.chat.id, draft_id, note=note)


async def on_new_text_not_text(message: Message) -> None:
    await message.answer("Пришлите новый текст одним текстовым сообщением или /cancel.")


async def on_other(message: Message) -> None:
    await message.answer(HELP)


# --- buttons ---------------------------------------------------------------------------


async def on_button(callback: CallbackQuery, state: FSMContext, bot: Bot, factory: Factory) -> None:
    match = CALLBACK.fullmatch(callback.data or "")
    if match is None:
        await callback.answer("Неизвестная кнопка.")
        return
    action, draft_id = match.group(1), int(match.group(2))
    actor = owner_actor(callback.from_user)
    if action == EDIT:
        async with factory() as s:
            card = await load_card(s, draft_id)
        if card is None:
            await callback.answer("Черновик уже решён или не найден.", show_alert=True)
            await _close_card(callback, None)
            return
        await state.set_state(EditDraft.text)
        await state.update_data(draft_id=draft_id)
        await callback.answer()
        await bot.send_message(
            callback.from_user.id,
            f"Пришлите новый текст ответа для заявки #{card.message_id} одним сообщением. "
            "Он пройдёт проверку, и карточка придёт снова. /cancel — отменить.",
        )
        return
    decide = approve_draft if action == SEND else reject_draft
    try:
        async with transaction(factory) as s:
            decision: DraftDecision = await decide(s, draft_id, actor)
    except DomainError as exc:
        await callback.answer(_domain_error_text(exc), show_alert=True)
        return
    if decision.outcome == "approved":
        await callback.answer("Ответ поставлен в очередь на отправку.")
        await _close_card(callback, "✅ Одобрено: ответ в очереди на отправку.")
    elif decision.outcome == "rejected":
        await callback.answer("Пропущено.")
        await _close_card(callback, "⏭ Пропущено: заявка закрыта без ответа.")
    else:
        await callback.answer(_ALREADY.get(decision.outcome, "Уже решено."))
        await _close_card(callback, None)


async def send_card(
    bot: Bot,
    factory: Factory,
    chat_id: int,
    draft_id: int,
    note: str | None = None,
    *,
    brief: bool = False,
) -> bool:
    """Send the card of a pending draft and remember it; the previous card of the
    same draft loses its buttons. False if the draft is no longer pending. `brief`:
    the card without the draft's text, for one Telegram refused."""
    async with factory() as s:
        card = await load_card(s, draft_id)
    if card is None:
        return False
    text = render_brief_card(card) if brief else render_card(card, note)
    sent = await bot.send_message(chat_id, text, reply_markup=card_keyboard(draft_id))
    async with transaction(factory) as s:
        await remember_card(s, card.message_id, draft_id, chat_id, sent.message_id)
    old = card.shown
    if old and old.get("draft_id") == draft_id and old.get("message_id") != sent.message_id:
        # Too old to edit or deleted: fine, every press is checked in the database anyway.
        with suppress(TelegramAPIError):
            await bot.edit_message_reply_markup(
                chat_id=old["chat_id"], message_id=old["message_id"], reply_markup=None
            )
    return True


async def _close_card(callback: CallbackQuery, outcome: str | None) -> None:
    """Remove the buttons from the pressed card and, if given, append the outcome."""
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        if outcome is not None and message.text is not None:
            await message.edit_text(f"{message.html_text}\n\n<b>{outcome}</b>", reply_markup=None)
        else:
            await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:  # not modified, too long: removing the buttons is enough
        with suppress(TelegramAPIError):
            await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


def _domain_error_text(exc: DomainError) -> str:
    if isinstance(exc, NotOwner):
        return "Заявка уже передана другому аккаунту — решение не принято."
    if isinstance(exc, WrongState):
        return "Заявка уже в другом состоянии — решение не принято."
    if isinstance(exc, NotFound):
        return "Заявка не найдена."
    return "Не получилось: " + type(exc).__name__
