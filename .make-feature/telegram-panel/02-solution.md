# Решение: черновики Claude, бот-панель владельца, хвосты этапа 4

Статус: **accepted v1**. Входы: [01-request.md](01-request.md); этап 4 [../telegram-adapter/02-solution.md](../telegram-adapter/02-solution.md), [../telegram-adapter/03-verification.md](../telegram-adapter/03-verification.md).

## Требования
| ID | Критерий |
|---|---|
| P1 | Черновик пишет Claude (`anthropic` 1.13, модель `claude-opus-5-5`, `messages.parse` со схемой Pydantic) под конкретную заявку: страна/город/что ищут; 2–5 предложений, по-русски, «на вы» |
| P2 | **Раскрытие связи обязательно:** текст рекомендует сервис Chit Trip от лица человека, который с ним сотрудничает («советую/рекомендую сервис Chit Trip, с которым сотрудничаю», «работаю с Chit Trip»). Запрещено выдавать себя за обычного клиента («мы сами бронировали», «летали с мужем» вне кавычек отзыва) |
| P3 | Факты — только из профиля сервиса: страны (Индонезия, Таиланд, ОАЭ, Черногория, Испания), жильё напрямую от собственников, без предоплаты, «помогаем найти выгодно». Никаких цифр цен/скидок, дат, адресов, «в 2 раза дешевле», «за полцены». Отзыв — только дословная цитата из профиля в «», не чаще одной на сообщение, можно без цитаты |
| P4 | Если страна заявки вне списка или запрос не про жильё/туры/билеты → черновик не создаётся, заявка `awaiting_review('out_of_scope')` |
| P5 | Guard: проверка P2/P3 кодом (раскрытие есть; нет клиентских «я/мы бронировали» вне «»; нет цифр с валютой/процентами, «полцены», «в N раза»; цитата совпадает с профилем дословно). Нарушение → в автомате на ручную проверку; в confirm — показывается владельцу с пометкой |
| P6 | Профиль сервиса и отзывы — в `profile.local.yaml` (в .gitignore); в репозитории — `profile.example.yaml` без реальных отзывов. Загрузка и валидация при старте |
| P7 | Бот-панель (aiogram 3.31) только для `OWNER_TG_USER_ID`: карточка черновика (ID заявки, страна/запрос кратко, ссылка, черновик, пометки guard) с кнопками «Отправить» / «Изменить» (следующее сообщение владельца = новый текст, проходит guard с предупреждением) / «Пропустить»; команды `/status` (очередь, отправлено сегодня по аккаунтам, лимиты, режим), `/pause`, `/resume`, `/auto` (через `enable_auto_mode`, отказ до N подтверждений). Все действия по внутреннему ID, проверка в БД; чужие пользователи игнорируются и пишутся в audit |
| P8 | `ACCOUNT_DAILY_LIMIT` по умолчанию 25 (50 в сутки на два аккаунта) |
| P9 | Хвосты этапа 4: реакция приватности настраивается `REACTION_PRIVACY` (по умолчанию 🚫, при `REACTION_INVALID` — 👎 и предупреждение); перед отправкой перечитать реакции заявки (если человек уже поставил ❤️/🚫 — `cancelled('handled_manually')`); USER_RESTRICTED/неизвестные окончательные отказы → `failed` + пауза аккаунта при USER_RESTRICTED; текст исключений приёма не попадает в лог; устаревший ник очищается, если у автора ника нет; одобренные черновики старше `DRAFT_TTL_HOURS` (24) не отправляются → `awaiting_review('stale_approval')` |
| P10 | Без сети: Anthropic и Telegram подменены в тестах; ключ `ANTHROPIC_API_KEY` только из окружения; текст заявки и черновика не логируется |

## Контракт
- `app/drafts/profile.py` — `ServiceProfile` (pydantic): name, aliases, countries, services, facts[], reviews[{id, text}], disclosure_phrases[]. `load_profile(path)`.
- `app/drafts/llm.py` — `ClaudeDraftProvider(client, profile)`: один вызов `messages.parse(model="claude-opus-5-5", output_format=DraftResult, system=<профиль + правила P2–P4>, messages=[заявка])`, `output_config={"effort": "medium"}`; `DraftResult{in_scope: bool, country: str|None, need: str, draft: str|None, review_id: str|None, confidence: float}`. Обработка `stop_reason == "refusal"` → нет черновика, `awaiting_review('llm_refusal')`. Ошибки API: 429/5xx/сеть → заявка остаётся в очереди (повтор в следующий тик), 4xx → `awaiting_review('llm_error')`. Серверный fallback при отказе: `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`, если поддерживается методом `parse` в установленном SDK (проверить по исходникам SDK; иначе обычный вызов и обработка refusal).
- Системный промпт стабилен (кэширование: `cache_control` на system), заявка — в user.
- `app/drafts/guard.py` — расширить существующий `check_draft` правилами P5 с параметром профиля.
- Воркер: `DraftProvider` выбирается настройкой `DRAFT_PROVIDER=llm|template`; LLM-черновик идёт по прежнему пути confirm/auto.
- `app/bot/` — aiogram Dispatcher, long polling, `python -m app.bot` (или задача внутри `app.worker` под тем же TaskGroup — на выбор исполнителя, с объяснением). Callback data — `act:<draft_id>`; повторное нажатие идемпотентно.
- `.env.example`: `ANTHROPIC_API_KEY`, `BOT_TOKEN`, `OWNER_TG_USER_ID`, `DRAFT_PROVIDER`, `PROFILE_PATH`, `REACTION_PRIVACY`, `DRAFT_TTL_HOURS`; README обновить.
- Зависимости: `anthropic==1.13.*`, `aiogram==3.31.*`, `pyyaml` (если нет).

## Тесты (минимум)
Промпт содержит правила и профиль; разбор DraftResult; out_of_scope; refusal; ошибки API по классам; guard: без раскрытия, «мы сами бронировали» вне кавычек, цифры/«полцены»/«в 2 раза», искажённая цитата, корректный черновик проходит; бот: чужой пользователь игнорируется, Отправить/Изменить/Пропустить меняют состояние ровно один раз при двойном нажатии, `/auto` до N отказывает, `/pause`/`/resume`, `/status`; все хвосты P9; лимит 25; полный `uv run pytest -q` зелёный.
