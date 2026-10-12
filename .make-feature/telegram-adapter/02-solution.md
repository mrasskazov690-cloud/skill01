# Решение: этап 4 — Telethon-коннекторы и поочерёдная отправка

Статус: **accepted v1** (контракт для исполнителей). Автор: оркестратор make-feature, 2026-10-12.
Входы: [01-request.md](01-request.md) и ответы владельца в [00-index.md](00-index.md); D1=Б2 в [../telegram-dispatcher/02-solution.md](../telegram-dispatcher/02-solution.md); ядро и замечания приёмки [../telegram-core/02-solution.md](../telegram-core/02-solution.md), [../telegram-core/03-verification.md](../telegram-core/03-verification.md).

## Требования

| ID | Критерий | Проверка |
|---|---|---|
| A1 | Замечания приёмки ядра M1, M2 и все minor закрыты | тест на каждое |
| A2 | Скрытые ссылки (entity `text_url`) и обычные URL разбираются: `t.me/<user>`, `t.me/<group>/<msg>`, `t.me/c/<id>/<msg>`, `t.me/<group>/<topic>/<msg>`, `tg://user?id=`, `tg://resolve?domain=…[&post=…]`, `tg://privatepost?channel=…&post=…`, с `https://`/`http://`/без схемы, `telegram.me` | pytest, табличные кейсы |
| A3 | Автор заявки определяется по ссылке; клиент идентифицируется по `tg_user_id` автора | pytest с фейковым клиентом |
| A4 | «Никогда больше не писать»: клиенту, у которого уже есть отправленный ответ (с любого аккаунта), или с которым у аккаунта уже есть переписка, не пишем; заявка — дубль без реакции | pytest |
| A5 | Похожий/одинаковый текст — дубль **только если автора определить не удалось**; разные авторы с похожим текстом — разные клиенты | pytest |
| A6 | Порядок: всегда самая новая необработанная заявка первой | pytest |
| A7 | В системе одна отправка раз в `SEND_INTERVAL_SECONDS` (120), аккаунты чередуются, никогда одновременно; суточный лимит на аккаунт (пилот 20); один активный отправитель на всю систему | pytest с фейковыми часами; advisory lock |
| A8 | Первые `CONFIRM_FIRST_N` (20) отправок — только после подтверждения владельцем; затем автомат включается только явной командой; в автомате черновик с ценами/адресами/датами/наличием уходит на проверку; пауза в любой момент | pytest |
| A9 | После отправки — ❤️ на заявке от отправившего аккаунта; закрытые ЛС → 🚫; дубль/ошибка → без реакции; заявки с ❤️/🚫 при старте не берутся | pytest |
| A10 | Ответ уходит только автору: отправка по сущности из исходного сообщения или по нику, **только если ник сейчас указывает на тот же `tg_user_id`** | pytest |
| A11 | Ошибки MTProto отображаются в контракт ядра: FLOOD_WAIT, PEER_FLOOD, закрытые ЛС, недоступный источник, заблокировал, удалён | pytest |
| A12 | Сессии и ключи только из окружения; скрипт входа пишет сессию в файл 0600 и не печатает её; в логах нет сессий, текста клиента и черновиков | pytest + ревью |
| A13 | Интеграционный тест на тестовых серверах Telegram готов и пропускается без `TG_TEST_*` переменных | pytest skip-маркер |

Реальные отправки людям, вход в аккаунты владельца и тестовый DC в этом запуске **не выполняются** (нет api_id — владелец создаёт сам).

## Основания
- Telethon 1.45.0 (MIT, 2026-09-10) проверен импортом: `messages.SendMessageRequest(..., random_id=...)`, `RandomIdDuplicateError`, `messages.SendReactionRequest(peer, msg_id, reaction=[ReactionEmoji])`, `MessageEntityTextUrl(offset,length,url)`, `PeerFloodError`, `FloodWaitError`, `UserPrivacyRestrictedError`, `ChannelPrivateError`, `UserIsBlockedError`, `InputUserDeactivatedError`; `PrivacyPremiumRequiredError` в 1.45.0 отсутствует → ловить `RPCError` по `message == 'PRIVACY_PREMIUM_REQUIRED'`.
- `access_hash` пользователя действует только для того аккаунта, который его получил → отправляющий аккаунт сам открывает исходное сообщение перед отправкой; `tg_user_id` глобален → годится для дедупликации.
- Гипотеза H1: повтор `SendMessageRequest` с тем же `random_id` после тайм-аута даёт `RANDOM_ID_DUPLICATE`, если первая отправка дошла. Не проверено → по умолчанию выключено (`SEND_RETRY_UNCERTAIN_WITH_RANDOM_ID=false`), `uncertain` по-прежнему решает человек.

## Контракт

### Зависимости
`telethon==1.45.*`. Больше ничего нового. aiogram, Anthropic SDK — следующие этапы.

### Миграция 0002 (Alembic, обратимая)
- `clients`: `external_key text UNIQUE NULL`; CHECK идентичности → `tg_user_id OR username OR external_key IS NOT NULL`. Ключ `msg:<chat>/<message_id>` для заявок с неопределённым автором.
- `messages`: `text_hash text NULL` (sha256 нормализованного текста: lower, ё→е, только буквы/цифры, схлопнутые пробелы) + индекс; `link jsonb NULL` (разобранная ссылка); `reaction text NULL` (поставленная нами реакция).
- `accounts`: `daily_send_limit int NULL` (NULL → `ACCOUNT_DAILY_LIMIT`), `last_sent_at timestamptz NULL`, `session_env text NULL` (имя переменной окружения с сессией, не сама сессия).
- `outbox`: `random_id bigint UNIQUE NULL` (генерируется при enqueue, 63-битное случайное).
- `drafts`: `id`, `message_id FK`, `account_id FK`, `body text`, `source CHECK in ('template','llm','manual')`, `status CHECK in ('pending','approved','rejected')`, `issues jsonb default '[]'`, `created_at`, `decided_at NULL`, `decided_by text NULL`; частичный UNIQUE `(message_id) WHERE status='pending'`.
- `settings` получает ключи (через сервис, не миграцией): `auto_mode` (bool, default false), `paused` (bool, default false).

### Ядро: исправления (A1)
M1 — structlog без локальных переменных в трейсбеке (`show_locals=False`/свой форматтер) + тест: `log.exception` с локальной переменной `text=<секрет>` не выводит её. M2 — при совпадении `tg_user_id` обновлять `username`; outbox передаёт адаптеру `PeerRef` (см. ниже), а не строку. Minor из [03-verification.md](../telegram-core/03-verification.md): порядок блокировок в startup sweep (conversation → message → outbox), отменённый `execute_send` (`CancelledError`) переводит `sending` в `uncertain` в отдельной защищённой транзакции, flood-отсрочка на уровне аккаунта продлевает аренду, `release()` → `InvalidTransition` для запрещённого перехода, ValidationError конфигурации без значений, round-robin не пропускает оператора из-за FK KEY SHARE (блокировать через `FOR NO KEY UPDATE SKIP LOCKED`), окно кандидатов не забивается одним заблокированным диалогом.

### Ядро: расширения
- `DISPATCH_ORDER` = `newest_first` (по умолчанию) | `oldest_first`; `newest_first` сортирует по `received_at DESC, id DESC`.
- Eligibility дополнительно: отправленных этим аккаунтом сегодня (`outbox.status='sent'`, `sent_at >= начало суток в TIMEZONE`, по умолчанию `Europe/Moscow`) `< coalesce(daily_send_limit, ACCOUNT_DAILY_LIMIT)`. Round-robin для отправки — по `last_sent_at NULLS FIRST, id` (чередование), `last_sent_at` ставит успешная отправка.
- `services/leads.py`:
  - `normalize_text`, `text_hash`.
  - `classify_duplicate(session, client_id | None, text_hash) -> reason | None`: `duplicate_client`, если у клиента есть outbox `sent` **или** сообщение в `assigned/processing/awaiting_review` (не текущее); `duplicate_text`, только если клиент неизвестен (`external_key`) и есть другое сообщение с тем же `text_hash` в статусе не `cancelled`.
- `services/approvals.py`: `create_draft`, `list_pending`, `approve(message_id, actor, edited_body=None)` → черновик `approved`, `enqueue_reply` с этим текстом; `reject(...)` → черновик `rejected`, сообщение `cancelled` (`review_reason='rejected_by_owner'`), без реакции; `approved_count()`; `enable_auto_mode(actor)` разрешено только при `approved_count() >= CONFIRM_FIRST_N` (иначе `DomainError`); `set_paused(bool, actor)`. Всё с audit без текста.
- `services/guard.py`: `check_draft(body) -> list[issue]` — цифры с валютой/«руб/₽/$/€/тыс/к»; адресные слова (ул., улица, дом, д., кв., пр-т, проспект) с номером; даты (`\d{1,2}[./]\d{1,2}`, названия месяцев с числом); слова наличия («свободна», «есть в наличии», «забронирована»); длина > 1000. Шаблоны, явно утверждённые владельцем, проверку не проходят (source='template' пропускается).

### Telegram-слой (`app/telegram/`)
- `links.py` (чистые функции): `LeadLink(kind: 'message'|'user', chat_username|None, chat_internal_id|None, message_id|None, username|None, user_id|None, url)`; `extract_links(text, entities) -> list[LeadLink]` (entities: `text_url` и `url`; offsets в UTF-16, как в Telegram), `pick_lead_link(links)` — последняя ссылка в сообщении, предпочтение `message` над `user`.
- `base.py` (расширение протокола; фейк обновить):
  - `PeerRef(tg_user_id: int|None, username: str|None, source: LeadLink|None)`.
  - `ResolvedAuthor(tg_user_id, username, display_name, is_bot)`.
  - `WorkChatMessage(chat_id, message_id, date, text, links: list[LeadLink], reactions: set[str], forwarded: bool)`.
  - Протокол `TelegramAccount`: `account_id`; `async fetch_work_chat(chat_id, *, min_id=0, limit) -> list[WorkChatMessage]` (новые → старые); `async resolve_author(link) -> ResolvedAuthor`; `async has_dialog_with(tg_user_id) -> bool` (есть ли в ЛС хотя бы одно наше сообщение); `async send_text(peer: PeerRef, text, *, random_id) -> SentMessage`; `async react(chat_id, message_id, emoji)`; `async check_connection() -> AccountHealth`.
  - Новые исключения: `SourceUnavailable` (нет доступа/удалено/не участник), `AuthorUnavailable` (анонимный админ, канал, бот, удалённый аккаунт), `PrivacyRestricted` (закрытые ЛС, PRIVACY_PREMIUM_REQUIRED), `UserBlocked`, `PeerMismatch` (ник указывает на другого пользователя) — все `PermanentSendError`, кроме `SourceUnavailable`/`AuthorUnavailable` (ошибки разрешения, не отправки).
- `telethon_account.py`: реализация протокола на `TelegramClient` + `StringSession` (из переменной окружения, имя в `accounts.session_env`); `send_text` — `SendMessageRequest(peer=InputPeerUser, message, random_id, no_webpage=True)`; получение `InputPeerUser`: из исходного сообщения (`get_messages(chat, ids=message_id)` → `sender`), иначе `get_entity(username)` с проверкой `id == tg_user_id` (иначе `PeerMismatch`), иначе `PeerInvalid`. Отображение ошибок: `FloodWaitError.seconds` → `FloodWait`; `PeerFloodError` → `PeerFlood`; `UserPrivacyRestrictedError`/`PRIVACY_PREMIUM_REQUIRED` → `PrivacyRestricted`; `UserIsBlockedError` → `UserBlocked`; `InputUserDeactivatedError`/`PeerIdInvalidError` → `PeerInvalid`; `RandomIdDuplicateError` → вернуть `SentMessage(tg_message_id=None, duplicate=True)` (H1); `asyncio.TimeoutError`/`ConnectionError`/`ServerError` во время отправки → `SendTimeout`. Реакции: `SendReactionRequest(peer, msg_id, reaction=[ReactionEmoji(emoticon)])`. Тесты — на подменённом клиенте (без сети).
- `login.py`: `python -m app.telegram.login --session-file PATH` — интерактивно телефон/код/пароль 2FA, использует `TG_API_ID`/`TG_API_HASH` из окружения; пишет `StringSession` в файл с правами 0600, **не печатает**; `--test-dc` для тестовых серверов. README: как перенести значение в переменную окружения на сервере.
- `tests/integration/test_test_dc.py`: помечен `@pytest.mark.telegram_test_dc`, пропускается без `TG_API_ID`, `TG_API_HASH`, `TG_TEST_SESSION_A`, `TG_TEST_SESSION_B`; сценарий: A создаёт группу, B пишет «заявку», A пересылает в рабочий чат со скрытой ссылкой, воркер обрабатывает, проверка доставки, реакции и H1.

### Воркер (`app/worker/`)
- `python -m app.worker` — один процесс; два цикла под общим `asyncio.TaskGroup`; отправитель держит `pg_try_advisory_lock(<const>)` (второй экземпляр не отправляет, пишет предупреждение).
- **Цикл приёма** (каждые `WORK_CHAT_POLL_SECONDS`, 15): аккаунт-читатель (`READER_ACCOUNT_ID`) читает `WORK_CHAT_ID` от последнего сохранённого `message_id`; при первом запуске — последние `WORK_CHAT_BACKFILL` (200). Для каждого сообщения: есть ❤️/🚫 → пропустить (уже обработано людьми); нет ссылки → ingest c внешним ключом и `awaiting_review('no_link')`; иначе `resolve_author` → ingest с `ClientRef(tg_user_id, username)` (при `SourceUnavailable`/`AuthorUnavailable` → внешний ключ `msg:` + `awaiting_review`); затем `classify_duplicate` → дубль: `cancelled` с причиной, без реакции. FloodWait при разрешении → ждать и продолжить.
- **Цикл отправки** (тик раз в `SEND_INTERVAL_SECONDS`, 120; одна отправка за тик максимум): если `paused` → ничего. Сначала одобренные черновики в очереди → `execute_send`. Иначе `dispatch_one` (newest_first, eligibility с дневным лимитом, чередование по `last_sent_at`) → `claim_message` → `has_dialog_with` (true → `cancelled('duplicate_client')`, без реакции) → черновик от `DraftProvider` (`TemplateDraftProvider`: активный шаблон из `templates` по intent `default`; нет шаблона → `awaiting_review('no_template')`) → если `auto_mode` и `check_draft` пуст → `enqueue_reply` + `execute_send`; иначе `create_draft` → `awaiting_review('needs_approval')` и тик закончен. Итог отправки: `sent` → ❤️ на заявке от отправившего аккаунта, `messages.reaction='❤️'`; `PrivacyRestricted` → 🚫; прочее → без реакции. Ошибка реакции не меняет статус отправки (audit `reaction_failed`).
- Часы и сон инжектируются (`Clock`), чтобы тесты шли без реального ожидания.

### Конфигурация (`.env.example`, только имена)
`TG_API_ID`, `TG_API_HASH`, `WORK_CHAT_ID`, `READER_ACCOUNT_ID`, `TG_SESSION_<ИМЯ>` (по одной на аккаунт), `SEND_INTERVAL_SECONDS=120`, `ACCOUNT_DAILY_LIMIT=20`, `CONFIRM_FIRST_N=20`, `TIMEZONE=Europe/Moscow`, `WORK_CHAT_POLL_SECONDS=15`, `WORK_CHAT_BACKFILL=200`, `DISPATCH_ORDER=newest_first`, `SEND_RETRY_UNCERTAIN_WITH_RANDOM_ID=false`. Секреты — `SecretStr`.

### Тесты (минимум, все без сети)
Исправления A1; таблица ссылок A2 (включая UTF-16 offsets с эмодзи перед ссылкой); ingest+resolve с фейком; «никогда больше» по outbox и по `has_dialog_with`; дубли по тексту только без автора; разные авторы — не дубли; newest_first; чередование A/B/A/B за 6 тиков; дневной лимит выключает аккаунт; второй воркер не берёт advisory lock; confirm-first-N: 20 одобрений → автомат не включается сам, `enable_auto_mode` до N → ошибка; guard; пауза; реакции ❤️/🚫/нет; старт пропускает заявки с ❤️/🚫; отображение каждой ошибки Telethon; PeerMismatch; скрипт входа пишет файл 0600 и ничего не печатает о сессии; полный прогон `uv run pytest -q` зелёный.

## Главный риск и выход
Отправка не тому человеку или повторная отправка. Защита: идентификация по ID автора, проверка ника на совпадение ID, «никогда больше» по двум источникам, outbox без автоповтора, подтверждение первых N, пилот только с разрешения владельца.
