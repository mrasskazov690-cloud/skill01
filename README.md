# Telegram dispatcher: ядро

Backend-ядро диспетчера входящих заявок Telegram. Оно принимает сообщения без дублей, хранит их в PostgreSQL со статусами и историей и даёт основу для назначения операторам и контролируемой отправки ответов.

Telegram подключён через Telethon (MTProto, пользовательские аккаунты): `app/telegram/telethon_account.py` реализует интерфейс `TelegramAccount` (`app/telegram/base.py`). Тесты работают с подменённым клиентом и фейковым адаптером (`app/telegram/fake.py`) и в сеть не ходят.

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

`tests/integration/test_test_dc.py` (маркер `telegram_test_dc`) проверяет адаптер на тестовых серверах Telegram: два тестовых аккаунта, группа-источник, рабочий чат со скрытой ссылкой, ответ автору, повтор с тем же `random_id` (гипотеза H1) и реакция. Без `TG_API_ID`, `TG_API_HASH`, `TG_TEST_SESSION_A` и `TG_TEST_SESSION_B` он пропускается. Сессии тестовых аккаунтов создаются через `python -m app.telegram.login --test-dc`. Запуск только его: `uv run pytest -m telegram_test_dc`.

## Раскладка

```
app/
  config.py           настройки (pydantic-settings), DATABASE_URL хранится как SecretStr
  logging.py          JSON-логи structlog с вырезанием чувствительных ключей
  db/base.py          DeclarativeBase и соглашение об именах ограничений
  db/models.py        все таблицы: accounts, clients, conversations, messages,
                      assignments, templates, settings, outbox, drafts, audit_log
  db/session.py       create_engine(), create_sessionmaker(), transaction()
  domain/states.py    статусы и таблица разрешённых переходов (ensure_transition)
  domain/errors.py    доменные ошибки (ConversationBusy, SendInFlight, NotOwner и др.)
  services/ingest.py  ingest_message(): идемпотентный приём сообщения
  services/dispatcher.py   dispatch_one(), dispatch_batch(): закрепление и round-robin
  services/assignments.py  assign_manual(), reassign_conversation(): ручное переназначение
  services/claims.py  claim_message(), release(), recover_stale(): аренда сообщений в работе
  services/leads.py   normalize_text(), text_hash(), classify_duplicate(): дубли заявок
  services/approvals.py    черновики, approve()/reject(), автомат после CONFIRM_FIRST_N, пауза
  services/guard.py   check_draft(): цены, адреса, даты, наличие, длина → на проверку
  services/outbox.py  enqueue_reply(), execute_send(), resolve_uncertain(),
                      recover_interrupted_sends(), expire_flood_waits()
  services/transitions.py  смена статусов: проверка по таблице переходов + audit_log
  services/audit.py   write_audit(): запись в audit_log
  telegram/base.py    протокол TelegramAccount, PeerRef (кому отвечать), ResolvedAuthor,
                      WorkChatMessage и ошибки адаптера
  telegram/links.py   LeadLink, extract_links(), pick_lead_link(): ссылки t.me/tg:// в заявке
  telegram/telethon_account.py  TelethonAccount: реализация на Telethon + StringSession
  telegram/login.py   вход в аккаунт, запись сессии в файл 0600
  telegram/fake.py    FakeTelegramAccount: сценарий исходов, рабочий чат, реакции
  worker/             python -m app.worker: цикл приёма и цикл отправки (worker.py),
                      Clock (clock.py), DraftProvider и TemplateDraftProvider (drafts.py),
                      advisory lock единственного отправителя (lock.py)
migrations/           Alembic (асинхронный env.py), ревизии 0001_initial, 0002_leads_and_drafts
tests/                pytest на реальном PostgreSQL
```

Ключевые гарантии схемы:

- `messages`: `UNIQUE (source_account_id, tg_chat_id, tg_message_id)`. Повторная доставка того же update возвращает прежний `id`.
- `conversations.client_id UNIQUE`: у клиента один диалог, и диалог закрепляется за оператором.
- `outbox`: частичный уникальный индекс по `message_id` для статусов `queued`, `sending`, `sent`, `uncertain`. На одно сообщение возможна не больше чем одна живая отправка.
- Статусы хранятся как `text` с `CHECK`. Список значений зафиксирован в миграции, поэтому его изменение требует новой ревизии.

## Назначение и отправка

- Блокировки везде берутся в одном порядке: диалог → сообщение → outbox → аккаунт. Так же их берёт и стартовый sweep. Диалог диспетчер берёт через `FOR UPDATE SKIP LOCKED`. Аккаунт оператора при выборе по round-robin и при заданном `max_active` он берёт через `FOR NO KEY UPDATE SKIP LOCKED`. Поэтому параллельные диспетчеры не ждут друг друга, а внешние ключи других транзакций на аккаунт (`KEY SHARE`) не отнимают у оператора очередь.
- Порядок выдачи — `DISPATCH_ORDER`: `newest_first` (по умолчанию, самая новая заявка первой, `received_at DESC, id DESC`) или `oldest_first`.
- В окно кандидатов (`DISPATCH_CANDIDATE_LIMIT`) попадает одно сообщение от каждого диалога. Поэтому заблокированный диалог или диалог с длинной очередью занимает одно место, а не всё окно.
- Диалог закрепляется за первым оператором. Следующие сообщения клиента идут тому же оператору. Если он недоступен, сообщения остаются `pending` с `review_reason='pinned_operator_unavailable'`. Автоматического переназначения нет. Диалог переносят вручную через `assign_manual()` или `reassign_conversation()`.
- Новый диалог получает допустимый аккаунт, который дольше всех не отправлял (`last_sent_at NULLS FIRST`; при равенстве — самый давний `last_assigned_at`, затем `id`). Так отправители чередуются. `last_sent_at` ставит только успешная отправка. Допустимый аккаунт: `kind='operator'`, включён, на смене, `state='active'`, нет активного flood wait, нагрузка меньше `max_active`, отправлено сегодня меньше `coalesce(daily_send_limit, ACCOUNT_DAILY_LIMIT)`. Сутки начинаются в полночь по `TIMEZONE` (по умолчанию `Europe/Moscow`), считаются строки outbox в статусе `sent`.
- `execute_send()` фиксирует `sending` до вызова API и вызывает адаптер вне транзакции. Адаптер получает `PeerRef` с `tg_user_id` и username клиента, а не голый ник: ник мог освободиться и достаться другому человеку. Тайм-аут переводит отправку в `uncertain`, а сообщение на ручную проверку (`resolve_uncertain()`). Отмена задачи во время вызова API делает то же сразу, в отдельной задаче, которую повторная отмена не прерывает. Автоповтора нет. FLOOD_WAIT возвращает отправку в очередь с `not_before`. Аренда сообщения продлевается до этого срока, и для отправок, отложенных из-за FLOOD_WAIT аккаунта, тоже. После `DISPATCH_MAX_SEND_ATTEMPTS` попыток отправка уходит на проверку. PEER_FLOOD ставит аккаунт в `restricted`. Ответ на другой аккаунт не перебрасывается.
- Клиент, найденный по `tg_user_id`, получает username из нового сообщения. Если этот ник записан за другим клиентом с `tg_user_id`, у того он стирается как устаревший.
- При старте процесса до запуска воркеров вызовите `recover_interrupted_sends()`: строки, оставшиеся в `sending`, станут `uncertain`. Периодически вызывайте `recover_stale()` и `expire_flood_waits()`.

## Заявки, дубли и подтверждение

- Каждой заявке при приёме считается `text_hash`: sha256 текста в нижнем регистре, с ё→е, только буквы и цифры, пробелы схлопнуты. Разобранная ссылка на источник хранится в `messages.link` и уходит адаптеру в `PeerRef.source`. Заявка с неопределённым автором получает клиента с `external_key` вида `msg:<chat>/<message_id>`.
- `classify_duplicate()`: `duplicate_client`, если клиенту уже ушёл ответ (outbox `sent` с любого аккаунта) или другая его заявка в работе (`assigned`, `processing`, `awaiting_review`). `duplicate_text` — только когда автор неизвестен и есть другое неотменённое сообщение с тем же `text_hash`. Разные известные авторы с одинаковым текстом — разные клиенты.
- Ответ, который нужно подтвердить, сохраняется как черновик (`drafts`), сообщение уходит в `awaiting_review('needs_approval')`. `approve()` ставит ответ в outbox (можно с исправленным текстом), `reject()` отменяет заявку с `review_reason='rejected_by_owner'`. Автомат (`settings.auto_mode`) включает только явный вызов `enable_auto_mode()`, и только после `CONFIRM_FIRST_N` одобренных черновиков. Пауза — `set_paused()`.
- `check_draft()` находит в тексте цены, адреса, даты, слова о наличии и длину больше 1000 символов. Такой черновик в автомате всё равно идёт на проверку. Утверждённые шаблоны (`source='template'`) не проверяются.
- Каждая строка outbox при постановке в очередь получает `random_id` (63 бита) для MTProto.
- Откат `0002 → 0001` отказывается работать, пока есть клиенты, известные только по `external_key`: их заявки не удаляются молча.

## Telegram-слой

- Ссылки на источник заявки берутся из скрытых ссылок (entity `text_url`), видимых (`url`) и из текста без entity (`tg://`). Поддерживаются `t.me/<user>`, `t.me/<group>/<msg>`, `t.me/<group>/<topic>/<msg>`, `t.me/c/<id>/<msg>` (и с темой), `t.me/s/<channel>/<msg>`, `tg://user?id=`, `tg://resolve?domain=…[&post=…]`, `tg://privatepost?channel=…&post=…`; со схемой `https://`, `http://` или без неё, на `t.me` и `telegram.me`. Смещения entity считаются в UTF-16, как в Telegram. Источник заявки (`pick_lead_link()`) — последняя ссылка на сообщение, если её нет — последняя ссылка на профиль.
- `send_text()` отправляет `messages.SendMessageRequest` с `random_id` строки outbox и `no_webpage`. Получатель берётся из исходного сообщения, которое отправляющий аккаунт открывает сам (`access_hash` действует только для получившего его аккаунта). Если сообщение недоступно — по нику, и только если ник сейчас принадлежит тому же `tg_user_id`; иначе `PeerMismatch`. Без `tg_user_id` или без способа найти автора — `PeerInvalid`, ничего не отправляется.
- Ошибки Telethon: `FLOOD_WAIT_X` → `FloodWait`, `PEER_FLOOD` → `PeerFlood`, `USER_PRIVACY_RESTRICTED` и `PRIVACY_PREMIUM_REQUIRED` → `PrivacyRestricted`, `USER_IS_BLOCKED` → `UserBlocked`, `INPUT_USER_DEACTIVATED`/`PEER_ID_INVALID` → `PeerInvalid`, `RANDOM_ID_DUPLICATE` → `SentMessage(None, duplicate=True)` (отправка с этим `random_id` уже дошла), тайм-аут, обрыв связи и ошибки сервера → `SendTimeout`. Остальные ошибки пробрасываются как есть, и outbox считает отправку неопределённой. Клиент Telethon создаётся с `flood_sleep_threshold=0` и без собственных повторов запросов.
- Реакции ставятся через `messages.SendReactionRequest`; «❤️» уходит в Telegram как «❤» и читается обратно как «❤️».

### Вход в аккаунт

```bash
TG_API_ID=... TG_API_HASH=... uv run python -m app.telegram.login --session-file ./anna.session.txt
# тестовые серверы Telegram: добавить --test-dc
```

Скрипт спрашивает телефон, код и пароль 2FA и записывает `StringSession` в файл с правами 0600. На экран сессия не выводится. На сервере положите содержимое файла в переменную окружения, например `TG_SESSION_ANNA` (через `.env` с правами 600 или менеджер секретов), впишите её имя в `accounts.session_env` и удалите файл. В `accounts.session_env` допускаются только имена вида `TG_SESSION_<ИМЯ>` (и `TG_TEST_SESSION_<ИМЯ>`), чтобы запись в базе не могла указать на другой секрет.

## Воркер

```bash
uv run alembic upgrade head
uv run python -m app.worker
```

Один процесс, два цикла под общим `asyncio.TaskGroup`. Время и сон берутся из `Clock` (`app/worker/clock.py`), поэтому тесты идут с ручными часами и без реального ожидания.

**Цикл приёма** (каждые `WORK_CHAT_POLL_SECONDS`). Аккаунт `READER_ACCOUNT_ID` читает `WORK_CHAT_ID` после последнего обработанного `message_id` (курсор хранится в таблице `settings`, ключ `work_chat_cursor:<чат>`). При первом запуске берутся последние `WORK_CHAT_BACKFILL` сообщений. Сообщения обрабатываются от старых к новым, каждое в своей транзакции:

- есть реакция ❤️ или 🚫 — заявку уже обработали люди, она пропускается;
- нет ссылки — клиент с внешним ключом `msg:<чат>/<id>`, заявка в `awaiting_review('no_link')`;
- автор не определился (`SourceUnavailable`, `AuthorUnavailable`, бот) — то же с причиной `source_unavailable`/`author_unavailable`;
- иначе клиент по `tg_user_id` автора;
- затем `classify_duplicate()`: дубль сразу `cancelled` с причиной, без реакции. FLOOD_WAIT при разборе автора — ждать и продолжить. Любая другая ошибка разбора автора — как неопределённый автор, с кодом ошибки в `review_reason`.
- Ссылки с ошибками или с номерами вне диапазонов Telegram (номер сообщения больше int32, id пользователя или канала больше int64) не считаются ссылками. Сообщение, которое не удаётся сохранить `INTAKE_ATTEMPTS` (3) опроса подряд, сохраняется без автора в `awaiting_review('intake_failed')`. Если не сохраняется и так, оно пропускается с записью `work_chat_message_skipped` в audit и ошибкой `intake_message_skipped` в логе. Одно плохое сообщение не останавливает приём.

**Цикл отправки** (тик раз в `SEND_INTERVAL_SECONDS`, не больше одной отправки за тик, значит не больше одной отправки за интервал во всей системе). Работает, только пока процесс держит `pg_try_advisory_lock`: второй экземпляр не отправляет и пишет предупреждение `sender_lock_taken`, а если первый остановился, забирает блокировку на следующем тике. Получив блокировку, процесс выполняет стартовый sweep (`recover_interrupted_sends()`). Тик:

1. `settings.paused` — ничего не делать.
2. Ответы в очереди (одобренные черновики, отложенные FLOOD_WAIT) — `execute_send()`.
3. Иначе следующая заявка: оставшаяся `assigned` за аккаунтом этого процесса (например, после сбоя между шагами) или новая через `dispatch_one()` (самая новая первой, дневной лимит, чередование по `last_sent_at`).
4. `has_dialog_with()` отправляющего аккаунта и `classify_duplicate()`: уже писали — `cancelled('duplicate_client')`, без реакции. FLOOD_WAIT при этой проверке ставит аккаунт в `flood_wait` до конца ожидания; заявка остаётся за ним, остальные идут другим аккаунтам.
5. `claim_message()` и черновик от `DraftProvider`. `TemplateDraftProvider` берёт активный шаблон с intent `default`; нет шаблона — `awaiting_review('no_template')`.
6. Автомат включён и `check_draft()` пуст — `enqueue_reply()` и `execute_send()`. Иначе `create_draft()`, заявка в `awaiting_review('needs_approval')`, тик закончен.
7. Отправлено — ❤️ на заявке в рабочем чате от отправившего аккаунта (`messages.reaction`). `PrivacyRestricted` — 🚫. Остальное — без реакции. Ошибка реакции не меняет итог отправки, она пишется в audit как `reaction_failed`.

Шаги 2 и 3 берут ответы и заявки только аккаунтов, которым можно отправлять сейчас: включён, на смене, `state='active'`, нет активного flood wait, отправлено сегодня меньше дневного лимита (`CAN_SEND` в `dispatcher.py`). Лимит проверяется при отправке, а не только при назначении, поэтому одобренные черновики сверх лимита ждут следующих суток, а заявки выключенного аккаунта ждут, пока его включат или человек их перенесёт.

Владелец одобряет черновики через `approvals.approve()`/`reject()`, автомат включает `enable_auto_mode()` (только после `CONFIRM_FIRST_N` одобрений), пауза — `set_paused()`. Интерфейса для этого пока нет.

Все включённые аккаунты должны иметь `session_env`. Включённый оператор без сессии получал бы заявки, на которые некому ответить, поэтому воркер в этом случае не запускается. Аккаунт, которому нельзя отправлять, выключите (`enabled=false`) или снимите со смены (`on_shift=false`).

### Переменные окружения воркера

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | — | ключи приложения с my.telegram.org (`TG_API_HASH` — секрет) |
| `WORK_CHAT_ID`, `READER_ACCOUNT_ID` | — | рабочий чат и аккаунт, который его читает |
| `TG_SESSION_<ИМЯ>` | — | сессия аккаунта; имя переменной в `accounts.session_env` |
| `WORK_CHAT_POLL_SECONDS` | 15 | как часто читать рабочий чат |
| `WORK_CHAT_BACKFILL` | 200 | сколько последних сообщений взять при первом запуске |
| `SEND_INTERVAL_SECONDS` | 120 | одна отправка за интервал во всей системе |
| `ACCOUNT_DAILY_LIMIT` | 20 | отправок в сутки с аккаунта, если `accounts.daily_send_limit` пуст |
| `CONFIRM_FIRST_N` | 20 | сколько первых ответов только после одобрения |
| `TIMEZONE` | Europe/Moscow | в каком поясе начинаются сутки для лимита |
| `DISPATCH_ORDER` | newest_first | порядок заявок |
| `SEND_RETRY_UNCERTAIN_WITH_RANDOM_ID` | false | не используется: неопределённую отправку решает человек |

### Сессия на сервере

1. На своей машине: `TG_API_ID=... TG_API_HASH=... uv run python -m app.telegram.login --session-file ./anna.session.txt`. Файл создаётся с правами 0600, сессия не печатается.
2. Перенесите значение на сервер, не показывая его на экране и не оставляя в истории оболочки, например: `scp ./anna.session.txt server:/tmp/` и на сервере `printf 'TG_SESSION_ANNA=%s\n' "$(cat /tmp/anna.session.txt)" >> .env && shred -u /tmp/anna.session.txt` (`.env` с правами 600) или положите его в менеджер секретов.
3. `UPDATE accounts SET session_env = 'TG_SESSION_ANNA' WHERE id = ...;`

   Воркер ищет `TG_SESSION_<ИМЯ>` там же, где остальные настройки: сначала в окружении, потом в `.env` текущего каталога (файл читается, в окружение не выгружается). Если переменной нет, воркер завершается с сообщением `account <id>: TG_SESSION_<ИМЯ> is not set`.
4. Удалите локальный файл (`shred -u ./anna.session.txt`).

### Безопасность пилота

- Запускать на реальных людях только с разрешения владельца, с пилотными лимитами: `ACCOUNT_DAILY_LIMIT=20`, `SEND_INTERVAL_SECONDS=120`, `CONFIRM_FIRST_N=20`. Увеличивать их — решение владельца.
- Воркер не обходит ограничения Telegram: FLOOD_WAIT выжидается, PEER_FLOOD выключает аккаунт до решения человека, ответ не перебрасывается на другой аккаунт, нет автоповтора неопределённых отправок, нет смены аккаунтов, прокси или текста ради обхода фильтров.
- Пишем только автору заявки (по `tg_user_id`) и только один раз: «никогда больше» по outbox и по переписке аккаунта.
- Пауза (`set_paused(True)`) останавливает отправку на следующем тике; приём продолжается.
- В логах и audit нет текста клиентов, черновиков и сессий.

## Безопасность

- Секреты задаются только через окружение или `.env` с правами 600. `.env`, `.env.*` и `*.session*` закрыты в `.gitignore`. Исключение — `.env.example`, в нём только ключи без значений.
- `repr` и `str` настроек не показывают `DATABASE_URL`. В объектах URL SQLAlchemy пароль замаскирован. Ошибка проверки настроек не выводит значения из окружения и `.env`.
- Логирующий процессор вырезает ключи `text`, `body`, `password`, `token`, `secret`, `api_hash`, `session`, а также составные ключи вроде `message_text` и `bot_token`. Это работает на любой глубине вложенности. Трейсбеки пишутся без локальных переменных.
- Движок БД создаётся с `hide_parameters=True`, поэтому текст клиента не попадает в тексты ошибок SQL.
- В `audit_log.details` пишутся только ID, статусы и коды ошибок. `write_audit()` дополнительно вырезает чувствительные ключи.
- Сессии Telegram — секреты: только в переменных окружения, не в базе, не в логах и не в тексте исключений. Файл от скрипта входа создаётся с правами 0600 и не печатается.
