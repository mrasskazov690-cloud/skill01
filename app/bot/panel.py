"""What the owner's panel reads and shows: draft cards and the /status report.

Cards and reports go only to the owner's private chat. They hold the lead and the
draft text, which is why nothing built here is ever logged or audited.
"""

# ruff: noqa: RUF001  (Russian panel texts by design)

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, or_, select, update
from sqlalchemy import literal as sa_literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Account, Draft, Message, Outbox
from app.domain.states import AccountKind, DraftSource, DraftStatus, MessageStatus, SendStatus
from app.services.approvals import approved_count, is_auto_mode, is_paused
from app.services.dispatcher import DAILY_LIMIT, SENT_TODAY
from app.worker.worker import DRAFT_INFO_KEY

# messages.meta key: the card the bot last sent for the message's pending draft.
CARD_KEY = "bot_card"
# Callback data: "<action>:<draft id>". Actions are looked up by the internal id.
SEND, EDIT, SKIP = "send", "edit", "skip"
CALLBACK = re.compile(r"(send|edit|skip):([1-9][0-9]{0,18})")
LEAD_EXCERPT = 300  # characters of the lead shown when the provider gave no summary

ISSUE_LABELS: Mapping[str, str] = {
    "price": "цена",
    "address": "адрес",
    "date": "дата",
    "availability": "наличие",
    "percent": "проценты",
    "half_price": "«полцены»",
    "times_comparison": "«в N раз»",
    "customer_claim": "рассказ от лица клиента",
    "no_disclosure": "нет раскрытия связи с сервисом",
    "misquote": "цитата не совпадает с отзывом",
    "too_many_quotes": "больше одной цитаты",
    "too_long": "слишком длинный",
}
REASON_LABELS: Mapping[str, str] = {
    "needs_approval": "ждёт подтверждения",
    "stale_approval": "одобрение устарело, ответ не ушёл вовремя — решите заново",
}
SOURCE_LABELS: Mapping[str, str] = {
    DraftSource.LLM: "Claude",
    DraftSource.TEMPLATE: "шаблон",
    DraftSource.MANUAL: "ваш текст",
}


@dataclass(frozen=True, slots=True)
class Card:
    draft_id: int
    message_id: int
    account_id: int
    account_name: str
    body: str
    source: str
    issues: tuple[str, ...]
    review_reason: str | None
    lead_text: str
    info: Mapping[str, Any]
    link_url: str | None
    chat_url: str | None
    shown: Mapping[str, Any] | None  # the card sent earlier (CARD_KEY), if any


async def load_card(session: AsyncSession, draft_id: int) -> Card | None:
    """The card of a pending draft; None if the draft is gone or already decided."""
    row = (
        await session.execute(
            select(Draft, Message, Account.display_name)
            .join(Message, Message.id == Draft.message_id)
            .join(Account, Account.id == Draft.account_id)
            .where(Draft.id == draft_id, Draft.status == DraftStatus.PENDING.value)
        )
    ).one_or_none()
    if row is None:
        return None
    draft, message, account_name = row
    meta = message.meta or {}
    return Card(
        draft_id=draft.id,
        message_id=message.id,
        account_id=draft.account_id,
        account_name=account_name,
        body=draft.body,
        source=draft.source,
        issues=tuple(draft.issues or ()),
        review_reason=message.review_reason,
        lead_text=message.text,
        info=meta.get(DRAFT_INFO_KEY) or {},
        link_url=(message.link or {}).get("url"),
        chat_url=_work_chat_url(message.tg_chat_id, message.tg_message_id),
        shown=meta.get(CARD_KEY),
    )


async def drafts_without_card(session: AsyncSession, limit: int = 20) -> Sequence[int]:
    """Pending drafts the owner has not been shown yet, oldest first."""
    shown = Message.meta[CARD_KEY]["draft_id"].as_integer()
    return (
        await session.scalars(
            select(Draft.id)
            .join(Message, Message.id == Draft.message_id)
            .where(
                Draft.status == DraftStatus.PENDING.value,
                or_(shown.is_(None), shown != Draft.id),
            )
            .order_by(Draft.id)
            .limit(limit)
        )
    ).all()


async def remember_card(
    session: AsyncSession, message_id: int, draft_id: int, chat_id: int, card_message_id: int
) -> None:
    card = {"draft_id": draft_id, "chat_id": chat_id, "message_id": card_message_id}
    await session.execute(
        update(Message)
        .where(Message.id == message_id)
        .values(meta=Message.meta.op("||")(sa_literal({CARD_KEY: card}, type_=JSONB)))
    )


def render_card(card: Card, note: str | None = None) -> str:
    """HTML for the bot (parse_mode=HTML); every value from the database is escaped."""
    e = html.escape
    lines = [
        f"<b>Заявка #{card.message_id}</b> · черновик #{card.draft_id} · "
        f"аккаунт {e(card.account_name)} (#{card.account_id})"
    ]
    if note:
        lines.append(e(note))
    if card.review_reason and card.review_reason in REASON_LABELS:
        lines.append(f"Статус: {e(REASON_LABELS[card.review_reason])}")
    country = card.info.get("country")
    need = card.info.get("need")
    if country:
        lines.append(f"Страна: {e(str(country))}")
    if need:
        lines.append(f"Запрос: {e(str(need))}")
    else:
        excerpt = card.lead_text.strip()
        if len(excerpt) > LEAD_EXCERPT:
            excerpt = excerpt[:LEAD_EXCERPT].rstrip() + "…"
        lines.append(f"Заявка: {e(excerpt)}")
    if card.link_url:
        lines.append(f"Ссылка: {e(card.link_url)}")
    if card.chat_url:
        lines.append(f"В рабочем чате: {e(card.chat_url)}")
    source = SOURCE_LABELS.get(card.source, card.source)
    lines += ["", f"<b>Черновик</b> ({e(source)}):", f"<blockquote>{e(card.body)}</blockquote>"]
    if card.issues:
        labels = ", ".join(ISSUE_LABELS.get(code, code) for code in card.issues)
        lines.append(f"⚠️ <b>Проверка:</b> {e(labels)}. Отправляйте, только если это верно.")
    else:
        lines.append("✅ Проверка пройдена")
    return "\n".join(lines)


def card_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить", callback_data=f"{SEND}:{draft_id}"),
                InlineKeyboardButton(text="Изменить", callback_data=f"{EDIT}:{draft_id}"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"{SKIP}:{draft_id}"),
            ]
        ]
    )


async def status_report(session: AsyncSession) -> str:
    """/status: mode, pause, the queue, today's sends and limits per sending account."""
    settings = get_settings()
    auto = await is_auto_mode(session)
    approved = await approved_count(session)
    lines = [
        "<b>Режим:</b> "
        + (
            "автомат"
            if auto
            else f"подтверждение (одобрено {approved} из {settings.CONFIRM_FIRST_N})"
        ),
        "<b>Отправка:</b> " + ("на паузе" if await is_paused(session) else "идёт"),
        "",
        "<b>Очередь</b>",
    ]
    by_status = dict(
        (await session.execute(select(Message.status, func.count()).group_by(Message.status))).all()
    )
    reviews = (
        await session.execute(
            select(Message.review_reason, func.count())
            .where(Message.status == MessageStatus.AWAITING_REVIEW.value)
            .group_by(Message.review_reason)
            .order_by(func.count().desc(), Message.review_reason)
        )
    ).all()
    pending_drafts = await session.scalar(
        select(func.count()).select_from(Draft).where(Draft.status == DraftStatus.PENDING.value)
    )
    sends = dict(
        (
            await session.execute(
                select(Outbox.status, func.count())
                .where(Outbox.status.in_([SendStatus.QUEUED.value, SendStatus.UNCERTAIN.value]))
                .group_by(Outbox.status)
            )
        ).all()
    )
    review_text = ", ".join(f"{reason or '—'}: {n}" for reason, n in reviews)
    lines += [
        f"новые: {by_status.get(MessageStatus.PENDING.value, 0)}",
        f"назначены: {by_status.get(MessageStatus.ASSIGNED.value, 0)}",
        f"в работе: {by_status.get(MessageStatus.PROCESSING.value, 0)}",
        f"на проверке: {by_status.get(MessageStatus.AWAITING_REVIEW.value, 0)}"
        + (f" ({html.escape(review_text)})" if review_text else ""),
        f"черновиков ждут решения: {pending_drafts or 0}",
        f"ответов в очереди: {sends.get(SendStatus.QUEUED.value, 0)}, "
        f"неясных отправок: {sends.get(SendStatus.UNCERTAIN.value, 0)}",
        "",
        "<b>Отправлено сегодня</b>",
    ]
    accounts = (
        await session.execute(
            select(
                Account.id,
                Account.display_name,
                Account.state,
                Account.enabled,
                Account.on_shift,
                SENT_TODAY.label("sent"),
                DAILY_LIMIT.label("limit"),
            )
            .where(Account.kind == AccountKind.OPERATOR.value)
            .order_by(Account.id)
        )
    ).all()
    total_sent = total_limit = 0
    for a in accounts:
        marks = [a.state]
        if not a.enabled:
            marks.append("выключен")
        if not a.on_shift:
            marks.append("не на смене")
        lines.append(
            f"{html.escape(a.display_name)} (#{a.id}): {a.sent}/{a.limit} · {', '.join(marks)}"
        )
        if a.enabled:
            total_sent += a.sent
            total_limit += a.limit
    if not accounts:
        lines.append("аккаунтов-отправителей нет")
    else:
        lines.append(f"всего: {total_sent}/{total_limit} (включённые аккаунты)")
    return "\n".join(lines)


def _work_chat_url(chat_id: int, message_id: int) -> str | None:
    """t.me/c link to the lead in the work chat (supergroups and channels only)."""
    text = str(chat_id)
    if not text.startswith("-100") or len(text) <= 4:
        return None
    return f"https://t.me/c/{text[4:]}/{message_id}"
