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
  domain/errors.py    базовая доменная ошибка
  services/ingest.py  ingest_message(): идемпотентный приём сообщения
  services/audit.py   write_audit(): запись в audit_log
  telegram/base.py    протокол TelegramAccount и ошибки адаптера
  telegram/fake.py    FakeTelegramAccount: сценарий исходов, запись отправленного
migrations/           Alembic (асинхронный env.py), ревизия 0001_initial
tests/                pytest на реальном PostgreSQL
```

Ключевые гарантии схемы:

- `messages`: `UNIQUE (source_account_id, tg_chat_id, tg_message_id)`. Повторная доставка того же update возвращает прежний `id`.
- `conversations.client_id UNIQUE`: у клиента один диалог, и диалог закрепляется за оператором.
- `outbox`: частичный уникальный индекс по `message_id` для статусов `queued`, `sending`, `sent`, `uncertain`. На одно сообщение возможна не больше чем одна живая отправка.
- Статусы хранятся как `text` с `CHECK`. Список значений зафиксирован в миграции, поэтому его изменение требует новой ревизии.

## Безопасность

- Секреты задаются только через окружение или `.env` с правами 600. `.env`, `.env.*` и `*.session*` закрыты в `.gitignore`. Исключение — `.env.example`, в нём только ключи без значений.
- `repr` и `str` настроек не показывают `DATABASE_URL`. В объектах URL SQLAlchemy пароль замаскирован.
- Логирующий процессор вырезает ключи `text`, `body`, `password`, `token`, `secret`, `api_hash`, `session`, а также составные ключи вроде `message_text` и `bot_token`. Это работает на любой глубине вложенности.
- Движок БД создаётся с `hide_parameters=True`, поэтому текст клиента не попадает в тексты ошибок SQL.
- В `audit_log.details` пишутся только ID, статусы и коды ошибок. `write_audit()` дополнительно вырезает чувствительные ключи.
- Telegram session-файлы будущих адаптеров считаются секретами и не должны попадать в репозиторий или логи.
