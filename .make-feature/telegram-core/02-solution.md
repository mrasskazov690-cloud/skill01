# Решение: ядро диспетчера (этапы ТЗ 3, 5, 6 + outbox)

Статус: **accepted v1** (контракт для исполнителей). Автор: оркестратор make-feature, 2026-10-12.
Входы: [01-request.md](01-request.md) (ответы владельца), канонический план [../telegram-dispatcher/02-solution.md](../telegram-dispatcher/02-solution.md), исходное ТЗ [../telegram-dispatcher/01-request.md](../telegram-dispatcher/01-request.md).

## Требования этого запуска

Telegram-библиотеки в этом запуске **не подключаются**: способ отправки ещё не выбран (см. открытый вопрос в индексе). Всё, что ниже, работает с абстракцией аккаунта и фейковым адаптером.

| ID | Критерий (подмножество C* канонического плана) | Проверка |
|---|---|---|
| K1 (C1) | Повторный приём того же сообщения не создаёт дубля, возвращает тот же внутренний ID | pytest: двойной и параллельный ingest |
| K2 (C2) | Каждое сообщение имеет `id` (bigint identity), статус и историю в `audit_log` + `assignments` | pytest |
| K3 (C3) | Одно сообщение не назначается двум операторам; два сообщения одного нового диалога не уходят разным операторам | pytest: ≥20 параллельных диспетчеров на реальном PostgreSQL |
| K4 (C4) | Round-robin по допустимым операторам; диалог закреплён; без авто-переназначения; ручное переназначение | pytest |
| K5 | Переходы статусов только по таблице; запрещённые падают `InvalidTransition` | pytest по всей матрице |
| K6 | Зависшие `processing` с истёкшей арендой возвращаются в `assigned`, если нет отправки в полёте | pytest |
| K7 (C7) | Отправка через outbox: не более одной активной отправки на сообщение; тайм-аут → `uncertain` и ручная проверка без авто-повтора; FLOOD_WAIT → ждать; PEER_FLOOD → пауза аккаунта, без переброса на другой аккаунт | pytest с фейковым адаптером |
| K8 (C9) | Секреты только из окружения, `.env.example` без значений, `.gitignore` закрывает `.env`, `*.session`; в логах нет текста клиента и секретов | проверка + pytest |
| K9 (C10) | README: установка, миграции, тесты с нуля | ревьюер воспроизводит |

## Контракт

### Стек и раскладка
Python ≥3.11 (локально 3.13), uv, `pyproject.toml` (hatchling или setuptools, пакет `app`). Зависимости: `sqlalchemy[asyncio]>=2.1,<2.2`, `asyncpg>=0.32`, `alembic>=1.20`, `pydantic-settings>=2.15`, `structlog>=26.1`. Dev: `pytest>=9.1`, `pytest-asyncio>=1.4`. Ничего сверх этого без причины.

```
app/
  __init__.py
  config.py            # Settings(BaseSettings): DATABASE_URL (SecretStr), LOG_LEVEL, DISPATCH_* ; repr без секретов
  logging.py           # structlog JSON; процессор, вырезающий ключи text/body/password/token/secret/api_hash/session
  db/
    __init__.py
    base.py            # DeclarativeBase, naming convention
    models.py          # все таблицы ниже
    session.py         # create_engine(url), sessionmaker, async with transaction()
  domain/
    states.py          # MessageStatus, SendStatus, ALLOWED_TRANSITIONS, InvalidTransition, ensure_transition()
    errors.py          # доменные ошибки
  services/
    ingest.py          # ingest_message(...)
    dispatcher.py      # dispatch_one(), dispatch_batch(), eligible-логика
    assignments.py     # assign_manual(), reassign_conversation()
    claims.py          # claim_message(), release(), recover_stale()
    outbox.py          # enqueue_reply(), execute_send(), resolve_uncertain()
    audit.py           # write_audit(session, actor, action, entity_type, entity_id, details)
  telegram/
    base.py            # Protocol TelegramAccount + исключения адаптера (см. ниже); без внешних библиотек
    fake.py            # FakeTelegramAccount для тестов и локальных прогонов
migrations/            # Alembic (env.py асинхронный, читает DATABASE_URL из Settings), 0001_initial
alembic.ini
tests/
  conftest.py          # TEST_DATABASE_URL; новая БД на сессию pytest (CREATE DATABASE <uuid>), alembic upgrade head, TRUNCATE между тестами
  test_*.py
.env.example  .gitignore  README.md  pyproject.toml
```
Миграции через Alembic, не `create_all` (тесты тоже гоняют `upgrade head`; отдельный тест: `downgrade base` → `upgrade head`).

### Таблицы (PostgreSQL)
Все `timestamptz`, `created_at/updated_at default now()`. Статусы — `text` + `CHECK (... IN (...))` (проще миграций, чем ENUM).

- **accounts**: `id bigint identity PK`, `tg_user_id bigint UNIQUE NULL`, `kind CHECK in ('main','operator','bot')`, `display_name text`, `state CHECK in ('active','paused','flood_wait','restricted','disconnected')`, `enabled bool default true`, `on_shift bool default true`, `max_active int NULL` (NULL = без лимита), `flood_wait_until timestamptz NULL`, `last_assigned_at timestamptz NULL`, `diagnostics jsonb default '{}'`.
- **clients**: `id`, `tg_user_id bigint UNIQUE NULL`, `username text NULL` (храним lower-case, `UNIQUE`), `display_name text NULL`, `CHECK (tg_user_id IS NOT NULL OR username IS NOT NULL)`.
- **conversations**: `id`, `client_id FK UNIQUE` (один диалог на клиента = закрепление), `source_chat_id bigint NULL`, `operator_account_id FK accounts NULL` (закреплённый оператор), `status CHECK in ('open','closed') default 'open'`, `last_activity_at`.
- **messages**: `id bigint identity PK` (внутренний ID), `source CHECK in ('direct','work_chat')`, `source_account_id FK accounts`, `tg_chat_id bigint`, `tg_message_id bigint`, `conversation_id FK`, `text text`, `meta jsonb default '{}'` (ссылка, entities и т.п.), `status` (см. states), `operator_account_id FK NULL`, `template_id FK NULL`, `template_version int NULL`, `review_reason text NULL`, `claimed_by text NULL`, `lease_expires_at timestamptz NULL`, `received_at`, `assigned_at`, `answered_at`, `updated_at`. **`UNIQUE (source_account_id, tg_chat_id, tg_message_id)`** — ключ идемпотентности. Индекс `(status, received_at)`.
- **assignments**: `id`, `conversation_id FK`, `message_id FK NULL`, `from_account_id NULL`, `to_account_id`, `reason CHECK in ('auto_round_robin','pinned','manual_assign','manual_reassign')`, `actor text`, `at`.
- **templates**: `id`, `name text UNIQUE`, `intent text`, `language text default 'ru'`, `body text`, `conditions jsonb default '{}'`, `allowed_fields text[] default '{}'`, `active bool default true`, `version int default 1`.
- **settings**: `key text PK`, `value jsonb`, `updated_at`, `updated_by text`.
- **outbox**: `id`, `message_id FK`, `account_id FK` (отправитель), `idempotency_key text UNIQUE`, `kind CHECK in ('template','manual')`, `template_id NULL`, `template_version NULL`, `body text`, `status CHECK in ('queued','sending','sent','uncertain','failed')`, `attempts int default 0`, `not_before timestamptz NULL`, `tg_sent_message_id bigint NULL`, `error text NULL`, `created_at`, `updated_at`, `sent_at NULL`. **Частичный уникальный индекс `ON outbox(message_id) WHERE status IN ('queued','sending','sent','uncertain')`** — не более одной живой отправки на сообщение.
- **audit_log**: `id`, `at`, `actor text`, `action text`, `entity_type text`, `entity_id bigint`, `details jsonb` — **без текста клиента и тела ответа** (только ID, статусы, коды ошибок).

### Статусы сообщения (`domain/states.py`)
`pending, assigned, processing, awaiting_review, answered, failed, cancelled`. Разрешённые переходы:

| из \ в | assigned | processing | awaiting_review | answered | failed | cancelled | pending |
|---|---|---|---|---|---|---|---|
| pending | ✓ | | ✓ | | | ✓ | |
| assigned | ✓ (reassign) | ✓ | ✓ | | | ✓ | |
| processing | ✓ (recover/release) | | ✓ | ✓ | ✓ | | |
| awaiting_review | ✓ | | | ✓ (оператор подтвердил ручную отправку) | | ✓ | ✓ |
| failed | | | | | | ✓ | ✓ (retry) |
| answered, cancelled | терминальные | | | | | | |

Любой другой переход → `InvalidTransition`. Каждый переход пишет `audit_log`.

### Аккаунт допустим для назначения (eligible)
`kind='operator' AND enabled AND on_shift AND state='active' AND (flood_wait_until IS NULL OR flood_wait_until <= now())` и при `max_active IS NOT NULL` — число его сообщений в `('assigned','processing')` `< max_active`. Локальный флаг не единственный критерий: состояние `state` обновляют адаптер/outbox по ответам API.

### Порядок блокировок (обязателен везде): conversation → message → account
- **ingest_message**(session, *, source, source_account_id, tg_chat_id, tg_message_id, client(tg_user_id|username, display_name), text, meta, source_chat_id) → `(message_id, created: bool)`. Upsert клиента и диалога через `INSERT … ON CONFLICT DO NOTHING` + повторный SELECT; сообщение — `INSERT … ON CONFLICT (source_account_id, tg_chat_id, tg_message_id) DO NOTHING RETURNING id`, при конфликте вернуть существующий id и `created=False`. Безопасно при параллельных вызовах. Обновляет `conversations.last_activity_at`.
- **dispatch_one**(session) → `message_id | None`. Одна транзакция:
  1. Кандидат: самое старое `pending` сообщение (без блокировки) → его `conversation_id`.
  2. `SELECT … FROM conversations WHERE id=:c FOR UPDATE SKIP LOCKED` (занято — попробовать следующего кандидата, ограниченно).
  3. `SELECT … FROM messages WHERE id=:m FOR UPDATE`, перепроверить `status='pending'`.
  4. Если у диалога есть `operator_account_id`: назначить ему, если он eligible; **если не eligible — оставить `pending`, записать `review_reason='pinned_operator_unavailable'` и audit, взять следующего кандидата** (владелец: переназначение только вручную).
  5. Иначе выбрать оператора: `eligible ORDER BY last_assigned_at NULLS FIRST, id LIMIT 1 FOR UPDATE SKIP LOCKED`; закрепить диалог, назначить сообщение, `last_assigned_at=now()`, запись в `assignments` (`auto_round_robin` или `pinned`) и audit.
  Нет eligible операторов → `None`, сообщение остаётся `pending`.
- **dispatch_batch**(session_factory, limit) — цикл `dispatch_one` в отдельных транзакциях.
- **assign_manual**(message_id, account_id, actor) и **reassign_conversation**(conversation_id, to_account_id, actor): блокировки в порядке conversation → messages; переносят закрепление и все `assigned` сообщения диалога; `processing` и сообщения с живой отправкой не трогают (ошибка `ConversationBusy`). Целевой аккаунт должен быть `kind='operator'` и `enabled` (on_shift не обязателен для ручного действия).
- **claim_message**(message_id, account_id, worker, lease_seconds) → `assigned → processing`, только если `operator_account_id = account_id`; ставит `claimed_by`, `lease_expires_at`.
- **recover_stale**(now) → для `processing` с `lease_expires_at < now`: если у сообщения есть outbox в `sending`/`uncertain` → `awaiting_review` (`review_reason='send_in_flight_on_recovery'`), иначе → `assigned`. Возвращает количество.

### Outbox и отправка (`services/outbox.py`, `telegram/base.py`)
`TelegramAccount` (Protocol): `account_id: int`, `async send_text(peer, text) -> SentMessage(tg_message_id: int)`, `async check_connection() -> AccountHealth`. Исключения: `SendTimeout` (результат неизвестен), `FloodWait(seconds)` (сервер отказал, не отправлено), `PeerFlood` (аккаунт ограничен), `PeerInvalid`/`PermanentSendError` (окончательно).
- **enqueue_reply**(message_id, account_id, kind, body, template_id/version, idempotency_key) — сообщение должно быть `processing` и назначено этому аккаунту; ключ по умолчанию `f"msg:{message_id}:reply"`; повтор с тем же ключом возвращает существующую строку; вторая живая отправка на то же сообщение → ошибка (индекс).
- **execute_send**(outbox_id, adapter): транзакция 1 — `FOR UPDATE` строки outbox, проверить `queued` и `not_before`, перевести в `sending`, `attempts+=1`, **commit до вызова API**. Вызов адаптера вне транзакции. Транзакция 2 — по результату:
  - успех → outbox `sent` + `tg_sent_message_id`, сообщение `answered`, `answered_at`;
  - `SendTimeout` → outbox `uncertain`, сообщение `awaiting_review` (`review_reason='send_uncertain'`), **без автоповтора**;
  - `FloodWait(s)` → outbox снова `queued` с `not_before=now+s`, аккаунт `state='flood_wait'`, `flood_wait_until=now+s`;
  - `PeerFlood` → outbox `failed`, аккаунт `state='restricted'`, сообщение `awaiting_review` (`review_reason='account_restricted'`); **никакого переброса на другой аккаунт**;
  - `PermanentSendError`/`PeerInvalid` → outbox `failed`, сообщение `failed`.
  Строка в `sending`, найденная при старте (процесс упал между транзакциями), трактуется как `uncertain`.
- **resolve_uncertain**(outbox_id, outcome in ('sent','not_sent'), actor) — ручное решение оператора: `sent` → сообщение `answered`; `not_sent` → outbox `failed`, сообщение `assigned` (новая отправка возможна с новым ключом).
- Лимит повторов FloodWait: `DISPATCH_MAX_SEND_ATTEMPTS` (по умолчанию 3) → затем `failed` + `awaiting_review`.

### Тесты (минимум)
ingest: дубль последовательно и 20 параллельных одинаковых → одна строка; разные клиенты. Состояния: полная матрица разрешённых/запрещённых. Dispatcher: round-robin по 3 операторам; закрепление; недоступный закреплённый → остаётся pending с причиной; disabled/off_shift/flood_wait/max_active исключаются; **20 параллельных dispatch_batch по 200 сообщениям 50 клиентов → каждое сообщение назначено ровно один раз, у каждого диалога один оператор, assignments без дублей**. Reassign/manual. Claims и recover_stale. Outbox: успех, тайм-аут → uncertain без повтора, FloodWait → queued+not_before+аккаунт flood_wait, PeerFlood → restricted без переброса, permanent, ровно одна живая отправка при параллельном enqueue, `sending` после «краха» → uncertain. Логи: текст клиента не попадает в вывод. Миграции: upgrade/downgrade/upgrade.

## Главный риск и выход
Гонки при назначении и дубли отправки. Закрываются блокировками в одном порядке, уникальными индексами и тестами на реальном PostgreSQL. Если тест конкурентности нестабилен — это дефект, не флейк.
