# Telegram dispatcher: ядро

Backend-ядро диспетчера входящих заявок Telegram. Оно принимает сообщения без дублей, хранит их в PostgreSQL со статусами и историей и даёт основу для назначения операторам и контролируемой отправки ответов.

Telegram-библиотеки пока не подключены: способ отправки ещё не выбран. Вместо них есть интерфейс `TelegramAccount` (`app/telegram/base.py`) и фейковый адаптер для тестов (`app/telegram/fake.py`). Реальных отправок код не делает.

## Требования

- Python 3.11 или новее (проверено на 3.13)
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 13 или новее (проверено на 16)

## Установка

```bash
uv sync                      # создаёт .venv и ставит зависимости из uv.lock
cp .env.example .env
chmod 600 .env               # заполните DATABASE_URL и TEST_DATABASE_URL
```

Настройки читаются из переменных окружения и из файла `.env` в текущем каталоге. Переменные окружения важнее `.env`. Все ключи описаны в `.env.example`.

## База данных

Создайте роль и базы от имени суперпользователя, например через `sudo -u postgres psql`:

```sql
CREATE ROLE dispatcher LOGIN PASSWORD 'придумайте-пароль' CREATEDB;
CREATE DATABASE dispatcher OWNER dispatcher;       -- рабочая база, DATABASE_URL
CREATE DATABASE dispatcher_test OWNER dispatcher;  -- для тестов, TEST_DATABASE_URL
```

Формат строки: `postgresql+asyncpg://dispatcher:ПАРОЛЬ@127.0.0.1:5432/dispatcher`. Схему `postgresql://` код тоже принимает и сам переключает её на драйвер asyncpg.

`CREATEDB` нужен только для тестов. Для рабочей роли без тестов его можно не давать.

## Миграции

Схема создаётся только миграциями Alembic. Строку подключения Alembic берёт из `DATABASE_URL`, в `alembic.ini` её нет.

```bash
uv run alembic upgrade head    # создать или обновить схему
uv run alembic current         # текущая ревизия
uv run alembic check           # модели и миграции совпадают
uv run alembic downgrade base  # удалить все таблицы вместе с данными
```

## Тесты

```bash
uv run pytest -q
```

Тестам нужен работающий PostgreSQL и `TEST_DATABASE_URL`. На сервере из этой строки каждый запуск pytest создаёт временную базу `dispatcher_pytest_<случайный суффикс>`, выполняет на ней `alembic upgrade head` и удаляет её в конце. Перед каждым тестом таблицы очищаются (`TRUNCATE … RESTART IDENTITY CASCADE`). Сама база из `TEST_DATABASE_URL` не меняется. Тест миграций отдельно проверяет цепочку `upgrade head → downgrade base → upgrade head`.

Без `TEST_DATABASE_URL` тесты с базой падают с понятной ошибкой. Остальные тесты при этом проходят.

## Раскладка

```
app/
  config.py           настройки (pydantic-settings), DATABASE_URL хранится как SecretStr
  logging.py          JSON-логи structlog с вырезанием чувствительных ключей
  db/base.py          DeclarativeBase и соглашение об именах ограничений
  db/models.py        все таблицы: accounts, clients, conversations, messages,
                      assignments, templates, settings, outbox, audit_log
  db/session.py       create_engine(), create_sessionmaker(), transaction()
  domain/states.py    статусы и таблица разрешённых переходов (ensure_transition)
  domain/errors.py    доменные ошибки (ConversationBusy, SendInFlight, NotOwner и др.)
  services/ingest.py  ingest_message(): идемпотентный приём сообщения
  services/dispatcher.py   dispatch_one(), dispatch_batch(): закрепление и round-robin
  services/assignments.py  assign_manual(), reassign_conversation(): ручное переназначение
  services/claims.py  claim_message(), release(), recover_stale(): аренда сообщений в работе
  services/outbox.py  enqueue_reply(), execute_send(), resolve_uncertain(),
                      recover_interrupted_sends(), expire_flood_waits()
  services/transitions.py  смена статусов: проверка по таблице переходов + audit_log
  services/audit.py   write_audit(): запись в audit_log
  telegram/base.py    протокол TelegramAccount, PeerRef (кому отвечать) и ошибки адаптера
  telegram/links.py   LeadLink: ссылка на источник заявки (сообщение или пользователь)
  telegram/fake.py    FakeTelegramAccount: сценарий исходов, запись отправленного
migrations/           Alembic (асинхронный env.py), ревизия 0001_initial
tests/                pytest на реальном PostgreSQL
```

Ключевые гарантии схемы:

- `messages`: `UNIQUE (source_account_id, tg_chat_id, tg_message_id)`. Повторная доставка того же update возвращает прежний `id`.
- `conversations.client_id UNIQUE`: у клиента один диалог, и диалог закрепляется за оператором.
- `outbox`: частичный уникальный индекс по `message_id` для статусов `queued`, `sending`, `sent`, `uncertain`. На одно сообщение возможна не больше чем одна живая отправка.
- Статусы хранятся как `text` с `CHECK`. Список значений зафиксирован в миграции, поэтому его изменение требует новой ревизии.

## Назначение и отправка

- Блокировки везде берутся в одном порядке: диалог → сообщение → outbox → аккаунт. Так же их берёт и стартовый sweep. Диалог диспетчер берёт через `FOR UPDATE SKIP LOCKED`. Аккаунт оператора при выборе по round-robin и при заданном `max_active` он берёт через `FOR NO KEY UPDATE SKIP LOCKED`. Поэтому параллельные диспетчеры не ждут друг друга, а внешние ключи других транзакций на аккаунт (`KEY SHARE`) не отнимают у оператора очередь.
- В окно кандидатов (`DISPATCH_CANDIDATE_LIMIT`) попадает одно сообщение от каждого диалога. Поэтому заблокированный диалог или диалог с длинной очередью занимает одно место, а не всё окно.
- Диалог закрепляется за первым оператором. Следующие сообщения клиента идут тому же оператору. Если он недоступен, сообщения остаются `pending` с `review_reason='pinned_operator_unavailable'`. Автоматического переназначения нет. Диалог переносят вручную через `assign_manual()` или `reassign_conversation()`.
- Новый диалог получает допустимый оператор с самым давним `last_assigned_at`. Допустимый оператор: `kind='operator'`, включён, на смене, `state='active'`, нет активного flood wait, нагрузка меньше `max_active`.
- `execute_send()` фиксирует `sending` до вызова API и вызывает адаптер вне транзакции. Адаптер получает `PeerRef` с `tg_user_id` и username клиента, а не голый ник: ник мог освободиться и достаться другому человеку. Тайм-аут переводит отправку в `uncertain`, а сообщение на ручную проверку (`resolve_uncertain()`). Отмена задачи во время вызова API делает то же сразу, в отдельной задаче, которую повторная отмена не прерывает. Автоповтора нет. FLOOD_WAIT возвращает отправку в очередь с `not_before`. Аренда сообщения продлевается до этого срока, и для отправок, отложенных из-за FLOOD_WAIT аккаунта, тоже. После `DISPATCH_MAX_SEND_ATTEMPTS` попыток отправка уходит на проверку. PEER_FLOOD ставит аккаунт в `restricted`. Ответ на другой аккаунт не перебрасывается.
- Клиент, найденный по `tg_user_id`, получает username из нового сообщения. Если этот ник записан за другим клиентом с `tg_user_id`, у того он стирается как устаревший.
- При старте процесса до запуска воркеров вызовите `recover_interrupted_sends()`: строки, оставшиеся в `sending`, станут `uncertain`. Периодически вызывайте `recover_stale()` и `expire_flood_waits()`.

## Безопасность

- Секреты задаются только через окружение или `.env` с правами 600. `.env`, `.env.*` и `*.session*` закрыты в `.gitignore`. Исключение — `.env.example`, в нём только ключи без значений.
- `repr` и `str` настроек не показывают `DATABASE_URL`. В объектах URL SQLAlchemy пароль замаскирован. Ошибка проверки настроек не выводит значения из окружения и `.env`.
- Логирующий процессор вырезает ключи `text`, `body`, `password`, `token`, `secret`, `api_hash`, `session`, а также составные ключи вроде `message_text` и `bot_token`. Это работает на любой глубине вложенности. Трейсбеки пишутся без локальных переменных.
- Движок БД создаётся с `hide_parameters=True`, поэтому текст клиента не попадает в тексты ошибок SQL.
- В `audit_log.details` пишутся только ID, статусы и коды ошибок. `write_audit()` дополнительно вырезает чувствительные ключи.
- Telegram session-файлы будущих адаптеров считаются секретами и не должны попадать в репозиторий или логи.
