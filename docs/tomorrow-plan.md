# План на завтра (Сре́да)

Живой документ. Задачи добавляем по ходу, завершённые помечаем `✅ DONE
YYYY-MM-DD` и переносим в нижнюю секцию. В начало ставим самое
приоритетное.

**Составлено:** 2026-04-25 (вечер).
**Ожидаемый горизонт исполнения:** 1 рабочий день с возможным
растяжением на 2 дня, т.к. пункты 3–6 существенные.

---

## ✅ DONE 2026-04-29 PM (вторая половина дня)

Хот-фиксы по жалобам реальных юзеров:

### PM.1 Phone-маска — ✅ DONE 2026-04-29

**Симптом.** Юзеры жаловались «не могу записать номер телефона»
(скриншоты от Бориса). LLM видела `[phone]` placeholder вместо цифр
→ tool-call'ы для сохранения контактов получали placeholder, реальный
номер терялся.

**Корень.** `PrivacyGuard.sanitize_text` маскировал любой `\+?\d[\d\s()-]{8,}\d`
как `[phone]` ДО того как сообщение попадало в LLM. Plaintext оставался
только в зашифрованном `SecureRecord`.

**Решение (Approach A).** Снят phone-rule + generic `\d{10,}` rule
из `privacy_guard.py`. Телефон — обычные ПДн (не спец-категория ст.10),
покрывается явным согласием в политике. Что осталось маскироваться:
passwords, login, secrets, account_number, verification_code,
telegram_bot_token, email, url-with-secrets, allergy/diagnosis (ст.10).

**Файлы.** `src/sreda/services/privacy_guard.py:185-205`,
4 теста обновлены под новое поведение.

**Commit.** `3a6c141 privacy_guard: drop phone-mask`. Деплой на VDS
через `git reset --hard origin/main` (на проде накопилось 24 modified
+ 6 untracked файлов от прежних rsync-деплоев — все matched origin/main
кроме privacy_guard.py + tomorrow-plan.md, поэтому reset был безопасен).

### PM.2 Telegram client — ловить HTTP 200 + ok=false

**Симптом инцидента (user_tg_471032584).** Юзер сделал /start, бэк
получил inbound, pending_bot вернул intro, в логах `sendMessage 200 OK`
— но юзер ничего не получил. Никаких warnings.

**Корень.** Telegram Bot API в edge-cases (невалидный reply_markup,
malformed parse_mode, длина текста и т.п.) умеет вернуть HTTP 200
с body `{"ok": false, "description": "..."}`. Раньше client делал
`response.raise_for_status()` (на 4xx/5xx) + `return response.json()`
без проверки `ok`. Caller думал что отправка прошла, юзер ничего
не получал.

**Решение.** В `_post_request` после `raise_for_status` явно проверяем
`body.get("ok") is False` и кидаем `TelegramDeliveryError` с полным
body. Caller (pending_bot path, ack flow, outbox delivery) поймает
в существующих try/except и залогит warning. Body логируется только
при `ok=false` (не на success — спам + PII risk).

**Файлы.** `src/sreda/integrations/telegram/client.py:266-288`,
`tests/unit/test_telegram_client_retry.py::test_200_with_ok_false_raises`.

**Commit.** `06e7f4f`.

### PM.3 backup_sqlite.sh в репо

**Контекст.** Скрипт жил только на проде через rsync (untracked
в git). При previous `git reset --hard origin/main` мы его сохранили
в `/tmp` как untracked, после reset он остался на диске.

**Решение.** Добавлен в `scripts/backup_sqlite.sh`. WAL-safe daily
SQLite backup через `.backup` команду, integrity_check, gzip -9,
ротация 14 дней, лог в `/var/log/sreda/backup.log`. Cron 03:00 UTC.

**Commit.** `2bda9ab`.

### PM.4 User 471032584 incident

**Симптом.** Максим Петров (`@rustyt0aster`, tg=471032584) нажал
/start через `t.me/sreda01_bot` link — у него на экране ничего
не появилось. Бэк говорил «sendMessage 200 OK», `getChatMember`
показал что бот не заблокирован.

**Гипотеза.** HTTP 200 + ok=false (см. PM.2). Сейчас наш fix
будет ловить такое в будущем — при следующем подобном инциденте
в логах будет `Telegram sendMessage ok=false: description=...`.

**Действия.** Юзер проапрувлен через admin. Все 11 сообщений
pending_bot tour отправлены ему руками через скрипт — все 11 OK.
Юзер сейчас может пользоваться ботом через нормальный chat-flow.

---

## План на 2026-04-30 (следующий рабочий день)

Приоритеты по убыванию:

### 1. Mini App lazy-provision: dead-end сценарий

**Контекст.** При входе через `t.me/sreda01_bot?startapp=...` или
прямую ссылку на Mini App, Telegram сразу открывает Mini App во
встроенном WebView. `miniapp.lazy_provision` создаёт tenant+user
**без** отправки welcome-сообщения в чат с ботом. Юзер закрывает
Mini App → видит пустой чат → не понимает что делать.

**Что сделать.**
- Когда `lazy_provision` создаёт нового юзера (`new=True`) — поставить
  в outbox welcome message от лица бота (тот же 11-step pending_bot
  tour либо короткий «Привет, я Среда — пиши прямо в чат»).
- Acceptance: юзер открыл Mini App → закрыл → в чате с ботом видит
  pending_bot intro.

**Файл.** `src/sreda/api/routes/miniapp.py` (lazy_provision branch).

**Оценка.** 1-2 часа.

### 2. Сайт sredaspace.ru — execute по плану

План у пользователя в `~/.claude/plans/mellow-discovering-conway.md`.
Запросить green-light на ExitPlanMode → начать с Phase 0 (DNS + Astro
init + nginx server blocks). Phase 0.5 (prompt caching + history
compaction) — критично до Phase 4 (ЮKassa), но не блокер для Phase 1
(landing) и Phase 3 (LK).

**Оценка.** Phase 0 = 1-2 дня.

### 3. Web_search/weather на VDS (из утреннего раздела 0.1)

Решение от Бориса не дано. Варианты:
- A: переключить web_search backend (Yandex / Brave / OpenAI Responses)
- B: Яндекс.Погода API напрямую через fetch_url
- C: SOCKS5 для Bing

**Acceptance.** Запрос «прогноз погоды на завтра в [город]» работает.

### 4. LLM hallucinates reminder (из утреннего раздела 0.3)

`schedule_reminder` иногда не вызывается, LLM просто пишет «Готово!
напоминание поставлено» без tool-call. Нужен hallucination detector
+ assertive completion markers. Уже частично сделано (commit `d1bb81b`),
но симптом всё ещё всплывает.

### 5. Mini App: иконка на домашний экран — добавить инструкцию

В `/welcome` или на отдельную страницу добавить текст «как закрепить
иконку чата». Безопасный путь:
- iOS Safari → `t.me/sreda01_bot` → Share → На экран «Домой»
- Android Chrome → `t.me/sreda01_bot` → ⋮ → Добавить на главный экран

### 6. Род прыгает в одном диалоге (gender consistency регрессия)

**Симптом** (наблюдение от Бориса 2026-04-30 12:23, скрин в чате):
LLM в одной сессии чередует мужской и женский род в одном диалоге.
Пример:
- 12:21 «Уже **добавил**! ✅ В списке "Дела" теперь пункт...» — м.р.
- 12:22 user: «а теперь удали его»
- 12:23 «Готово, **удалила**! ✅» — ж.р.

Среда — феминный персонаж. Должна везде использовать ж.р. («добавила»,
«удалила», «нашла», «записала»).

**Контекст.** Уже была работа над этим — commit `326f5d4 prompt:
strengthen feminine-gender rule with universal pattern + few-shot`.
Видимо текущая prompt-инструкция не справляется со всеми
вариантами, особенно с новой активной речью («Уже добавил/добавила»).

**Что сделать:**
1. Расширить few-shot примеры в system prompt: добавить именно
   паттерн «Уже сделала X», «Готово, удалила!», «Записала»
   (короткие активные подтверждения, где LLM чаще ошибается)
2. Возможно добавить **post-LLM gender-check**:
   - Простой regex по списку глаголов прошедшего времени м.р.
     (`добавил|удалил|записал|нашёл|понял|сделал|поставил|...`)
   - Если найдено — переписать через regex на ж.р.
     (`добавил → добавила`)
   - Защита от случаев когда LLM пишет про другого человека
     («Папа добавил») — там м.р. правильный. Detect: контекст
     «я / мы (Среда)» vs «он / Папа / врач».

**Файлы:**
- `src/sreda/runtime/handlers.py` (где собирается system prompt)
- `src/sreda/services/feminine_guard.py` (новый, для post-check)
- `tests/unit/test_feminine_gender.py` (regression cases)

**Acceptance.** Через 10+ ходов в одном чате — все упоминания
действий бота в ж.р. Голосовой и текстовый ввод одинаково.

Запрос приходил от юзера сегодня.

### 7. Перейти с webhook на long-polling (TG + MAX) — ✅ DONE 2026-04-30 PM

**Сделано вечером 30 апреля.** Cutover около 23:05 МСК — webhook упал в очередной раз с
`Connection timed out`, юзер в стрессе, переключение прошло без отката.

Финальная архитектура: отдельный systemd unit `sreda-telegram-poller`,
который через `getUpdates(timeout=25)` тянет updates в loop'е и
вызывает тот же `handle_telegram_update` что и (старый) webhook —
durable ingest до advance offset'а в БД, idempotency по
`external_update_id`. Обвязка:
- `schema 0036` (poller_offsets, poller_heartbeats, inbound.processing_status)
- thin webhook (на всякий случай не выпилен — оставлен для rollback'а)
- `--check-config` для pre-cutover sanity
- monitor probes: `telegram_poller_alive`, `telegram_api_health`, `unprocessed_inbound`
- `RestartPreventExitStatus=2 3` — exit code 2 (singleton lock) и 3 (409 Conflict)
  не auto-restart'ятся, требуют ручного reset-failed

После cutover'а:
- inbound webhook timeouts больше не приходят (по определению — webhook удалён)
- ack'и иногда визуально приходят после реплая → **это другая проблема**, не inbound,
  см. п.9 ниже про outbound

**Что осталось хвостом:**
- `webhook_health` probe всё ещё в monitor — даёт warning при пустом
  url (это норма, удалить пробу после 24h soak'а)
- MAX long-poller (когда подключим MAX) — отдельный спринт, паттерн
  тот же что в TG

---

(оригинальный текст пункта 7 ниже — для истории)

**Контекст.** 2026-04-30 имели несколько инцидентов «Connection timed out»
на webhook'ах Telegram. Симптом: TG не может достучаться до нашего nginx,
пакеты теряются где-то в пути (Singapore → Москва Timeweb через 5-10
провайдеров), middlebox/anti-DDoS дропает idle TCP-connection без RST.
Юзер на 30-60s видит «бот не отвечает».

Палиативы (применены):
- TCP keepalive tuning sysctl 60/15/3 (commit `(не закоммичено)` —
  `deploy/sysctl/99-sreda-tcp.conf`)
- nginx `keepalive_timeout 0` для bot.sredaspace.ru (закоммитить нужно)
- setWebhook с `ip_address=62.113.41.104` + `max_connections=4`
- safe_restart.sh который делает deleteWebhook+setWebhook после рестарта

**Радикальный фикс — long-polling.** Полностью убирает inbound-сетку:
- Background asyncio task'и в `sreda-job-runner` делают long-poll
  `getUpdates` (TG) и `POST /updates` (MAX)
- TG/MAX edge-network/anti-DDoS / nginx webhook-endpoint больше не наши
  проблемы
- Connection initiated с НАШЕЙ стороны → kernel сам видит dead и
  переоткрывает
- Унифицированная архитектура для всех каналов (TG, MAX, потом WhatsApp/
  Viber если когда-то)
- `safe_restart.sh` упрощается — просто kill+restart bg task

**Объём работы:** 1-2 рабочих дня
- `src/sreda/workers/telegram_long_poll.py` — async loop через
  `getUpdates(timeout=25, offset=...)`, парсит updates, вызывает тот
  же `_process_approved_turn` что и webhook
- `src/sreda/workers/max_long_poll.py` — аналог через MAX API
- Регистрация background tasks в `sreda-job-runner` startup
- `deleteWebhook` на проде когда переедем — финальный шаг
- Webhook routes можно оставить (для совместимости) или удалить

**Acceptance.**
- 24 часа без `webhook_health` warning'ов в monitor
- Юзер пишет → ответ в стандартные 5-10 сек, без 30-60s timeout-окон
- nginx reload не вызывает never-ending Connection-timed-out у TG

**Минусы:**
- +200ms latency vs webhook в идеальных условиях (long-poll round-trip)
- Background task supervision: если task падает — не получает updates.
  Нужен auto-restart через systemd Restart=always или supervisor pattern
- Если решим, надо переехать на long-polling ДО подключения MAX webhook'а
  (иначе придётся переделывать)

**Когда делать.** Если палиативы (keepalive_timeout=0 + ip_address +
max_connections=4) ловят >95% случаев — отложено до следующих incident'ов.
Если в ближайшие 2-3 дня webhook'ы будут отваливаться повторно — приоритет
повышается до P0.

### 8. Тесты — почистить лишнее, ускорить фикстуры

**Контекст.** 995 unit-тестов, полный прогон 5-8 минут. Большая часть
времени уходит на fixture overhead, не на сами ассерты — каждый тест
делает `Base.metadata.create_all` на свежий sqlite (~40+ таблиц) →
~200-500ms × 995 ≈ 5-8 минут только на стартах.

**Что почистить (~150-200 тестов на удаление):**
- `test_housewife_tool_docstrings.py` — 23 теста на формат docstring'ов,
  раз написал и забыл
- `test_housewife_*.py` (10+ файлов) — частично дублируют покрытие
  через разные пути (chat_tools / family / food / menu / recipes /
  shopping / shopping_llm / onboarding / worker / autogen_shopping)
- `test_credit_formula.py` — 19 тестов на одну формулу, можно
  ужать до 5 параметризованных
- `test_hallucinated_checklist_detector.py` + `test_hallucination_detector.py` —
  соседние модули, почти одинаковые кейсы

**Что трогать НЕЛЬЗЯ:**
- `test_telegram_webhook.py`, `test_telegram_long_poll.py`,
  `test_inbound_dedup.py` — критический путь
- `test_encrypted_string.py` / `test_encryption.py` — 152-ФЗ
- `test_billing_service.py` — деньги
- `test_chat_turn_timeout_rescue.py` — реальный инцидент-фикс

**Ускорение фикстур (даёт больше всего в pure time):**
- session-scope DB fixture с per-test `BEGIN ... ROLLBACK`-обёрткой
  (вместо `create_all` каждый раз) → суммарно 60-90s вместо 5-8 минут
- mark slow-tests `@pytest.mark.slow`, в pre-commit hook гонять
  только fast → обычный цикл < 30 сек

**Объём:** ~1 день. Не критично, делать когда сильно достанет ждать.

**Acceptance.**
- pytest tests/unit/ на ноуте < 90 сек
- кол-во тестов: ≤ 800 (минус ~200 удалённых)
- 0 регрессий — webhook/inbound/billing/encryption suite остаются
  100% зелёными

### 8.1. Test isolation: long_poll корруптит state webhook'а — ✅ DONE 2026-05-02

**Симптом.** Если запустить
`pytest tests/unit/test_telegram_long_poll.py tests/unit/test_telegram_webhook.py`
вместе, **5/11 webhook-тестов падают** на «timed out after 5.0s waiting
for background task». Solo каждый suite зелёный (13/13 + 11/11).

**Что точно:**
- Не моё Stage 2 (commit `<TBD>` 2026-05-02) — поломка существует на
  чистом main, проверено через `git stash`
- Pre-existing: появилась когда добавил `test_telegram_long_poll.py`
  в long-poll session 2026-04-30/05-01

**Причина (гипотеза):**
- `_FetchScript`/lock fixtures в test_telegram_long_poll создают
  module-level state или не убирают `monkeypatch.setenv` cleanly
- Альтернатива: `get_session_factory` lru_cache подхватывает state
  от long_poll fixture'а

**Что проверить в первую очередь:**
1. autouse fixtures в `tests/unit/test_telegram_long_poll.py` —
   корректный teardown
2. `get_session_factory.cache_clear()` после каждого long_poll теста
3. Module-level `_FetchScript` instances vs class-level state

**Объём:** 1-2 часа дебага. Pre-existing, не блокер для прода (юнит
тесты solo зелёные, deploy идёт). Но в CI suite это вызовет fail.

**Когда:** до первого CI runner с полным `pytest tests/unit/` прогоном
ИЛИ когда сильно начнёт мешать локально.

**Root cause (DONE 2026-05-02).** В `test_handle_telegram_update_idempotent_on_duplicate`
строка `monkeypatch_target = ti; monkeypatch_target._process_approved_turn = noop_turn` —
это не pytest-monkeypatch, а direct module-attribute assignment.
Замена `_process_approved_turn` на `noop_turn` **persist'ила между
тестами**: все следующие тесты в session видели stub'ovую версию,
которая делала pretend «turn ran» вместо реального LLM/outbox.
Webhook-тесты ждали 2 outbound message (ack + final reply), но
final reply никогда не отправлялся → timeout 5s в `_wait_for`.

**Fix:** заменили `monkeypatch_target = ti; ti._process_approved_turn = ...`
на `monkeypatch.setattr(ti, "_process_approved_turn", noop_turn)`.
Pytest-monkeypatch автоматически откатывает изменение в teardown'е.

**Bonus:** добавили autouse-фикстуру в `tests/unit/conftest.py` который
очищает `_TENANT_LOCKS` и `_CLIENT_POOL` (модульные dict с asyncio.Lock /
httpx.AsyncClient объектами привязанными к event loop) после каждого
теста. Это не было причиной 8.1, но защищает от похожих leak'ов в
будущем.

После fix'а: `pytest tests/unit/test_telegram_long_poll.py
tests/unit/test_telegram_webhook.py` → 26/26 ✓ (раньше 5/11 webhook
fail'ов в combined прогоне).

### 9.0. (NEW) Анализ собранных метрик после Stage 2 + Stage 9.1 soak

**Контекст.** 2026-05-02 закоммичены и задеплоены два observability-слоя:
- `node_load_memories` пишет distribution stats (candidates_total,
  with_embedding, filtered_below_min, seeded, scores_min/max/p50)
- `ack.sent` + `outbox.delivered` пишут `tg_message_id` / `tg_date`

После 1-2 суток soak'а проанализировать собранные данные **до** любого
кода-изменения в Stage 3 (retrieval params) или Stage 9.2/9.3 (transport).

**Команды для анализа на проде:**

```bash
# 1. Distribution stats по retrieval — нужно для Stage 3 решения
ssh boris@vds 'sudo grep "node_load_memories" /var/log/sreda/*.log' \
  | awk '...' | sort | uniq -c | sort -rn

# Что искать:
#   - seeded ~ 10 (top_k достигнут) → top_k надо поднимать
#   - seeded < 10 (БД пуста или min_score рубит) → top_k не помогает
#   - filtered_below_min > 0 часто → понизить min_score 0.1 → 0.05
#   - scores_p50 < 0.2 → embedding слабо различает, dense retrieval
#     плохо работает, надо думать про hybrid (Stage 5/6)

# 2. ack vs final ordering — для Stage 9.2/9.3 решения
# Найти турны где ack.tg_message_id и outbox.delivered.tg_message_id
# оба известны, сравнить
ssh boris@vds 'sudo grep -E "ack.sent|outbox.delivered" /var/log/sreda/trace.log' \
  | python parse_ordering.py

# Что искать:
#   - ack.tg_message_id < final.tg_message_id ВСЕГДА → Telegram client
#     side delivery sync. Сетью не лечится → 9.2 (placeholder + edit).
#   - ack.tg_message_id > final.tg_message_id ХОТЯ БЫ ИНОГДА → ack
#     физически создан в TG ПОЗЖЕ → реальный transport HOL → 9.3.A или 9.3.B
```

**Объём:** 30-60 минут на анализ + 10-15 мин на report-summary.

**Acceptance:** табличка с findings + чёткий ответ что делать дальше:
Stage 3 / 9.2 / 9.3 / ничего.

**Когда:** через 24-48 часов после 2026-05-02 deploy. Раньше — выборка
маленькая, числа могут лгать.

### 9. Outbound delivery: ack приходит ПОСЛЕ реплая

**Контекст (2026-04-30 PM).** После cutover'а на long-poll inbound стабилен,
но юзер видит хаотичный порядок исходящих сообщений: сначала прилетает
финальный реплай, потом — ack «🌀 Секунду…». В trace.log при этом
`ack.sent` стоит с latency 100-300мс и status=ok, sendMessage возвращает
`{"ok": true}` за те же 100-300мс. То есть наш код шлёт ack первым,
TG возвращает 200, но юзер визуально получает в «вывернутом» порядке.

Второе мнение от другого ИИ (см. чат 2026-04-30): после `ok=true` на
`sendMessage` outbound transport свою работу сделал — задержка либо в
**Telegram client-side delivery sync** (мобильный TG любит батчить),
либо реально транспорт делает HOL blocking (TCP-over-TCP в SSH-SOCKS).

**Подходы по убыванию ROI:**

#### 9.1. Залогировать `message_id` и `date` (5 мин)

Цель: понять, какой это вариант. Изменить логирование в
`telegram_inbound._fire_and_forget_ack` и в outbox delivery:

```python
resp = await client.send_message(...)
msg = resp["result"]
logger.info(
    "tg_send kind=%s chat=%s message_id=%s tg_date=%s latency_ms=%.1f",
    kind, msg["chat"]["id"], msg["message_id"], msg["date"], latency_ms,
)
```

День на наблюдение → анализ:
- если `ack.message_id < final.message_id` → TG client-side sync,
  сетью не лечится → идти в 9.2 (placeholder + edit)
- если `ack.message_id > final.message_id` → ack физически создаётся
  в TG позже, проблема в нашем коде (порядок `await`'ов?) или в
  транспорте (HOL blocking) → идти в 9.3 (WireGuard) или править код

#### 9.2. Placeholder + editMessageText (2-3 часа)

UX-фикс. Вместо «ack отдельным сообщением, потом реплай отдельным»:
1. Шлём «🌀 Секунду…» через `sendMessage`, запоминаем `message_id`
2. Когда LLM/voice готовы → `editMessageText(message_id, final_text)`

В чате юзера остаётся **одно** сообщение которое сначала «секунду»,
потом превращается в реплай. Никакого хаоса визуального порядка.
Bonus: нет дубликата `ack + delete_after_reply`-логики.

Делать **если 9.1 покажет client-side sync**.

#### 9.3. Заменить транспорт RU↔EU (один из двух вариантов)

Радикальный архитектурный фикс outbound-транспорта. Сейчас:
RU VDS → `ssh -D 1080` → EU egress (89.110.77.78) → TG.
SSH dynamic-forward = TCP-over-TCP, head-of-line blocking возможен:
один retransmit на одном channel'е тормозит остальные. Делать **если
9.1 покажет ack физически создан позже final**, или после внедрения
9.2 захотим убрать SSH-SOCKS как «кривой костыль».

##### 9.3.A — Маленький Go-прокси на egress (рекомендуемый, ~1 час)

Узкий слой: на 89.110.77.78 поднимаем маленький Go-сервис, который
принимает CONNECT/SOCKS5 от RU VDS и форвардит как **direct TCP** к
api.telegram.org. Один TCP-сокет на один outbound-запрос, никакого
SSH-channel мультиплексирования → HOL blocking устранён by design.

- ~150 строк Go (`net.Listen` → `net/http` CONNECT либо мини-SOCKS5)
- single binary, systemd-юнит на egress'е
- firewall whitelist на src=62.113.41.104 (только наш RU VDS пускаем)
- TLS termination остаётся на стороне httpx как сейчас (egress
  только TCP-форвардит, не расшифровывает)

**Плюсы vs WireGuard:**
- 1 час работы вместо дня
- Не требует kernel-level WireGuard модуля (на Timeweb VPS может быть
  неудобно настраивать)
- Возможность залогировать каждый outbound на egress'е (диагностика
  будущих инцидентов)

**Минусы:**
- Ещё один компонент в обвязке (мини-сервис на egress'е)
- Если egress reboot'нётся — нужен systemd Restart=always

##### 9.3.B — WireGuard RU↔EU (день, более радикальный)

WG tunnel RU↔EU, policy route 149.154.160.0/20 + 91.108.4.0/22 через WG.
В httpx убрать `trust_env=True` → нет proxy parsing. Прямые TCP к
api.telegram.org, kernel-side TCP keepalive работает как задумано,
никакого ssh-channel мультиплексирования.

Конфиги в комментариях — приведены в чате (другой ИИ).

**Плюсы vs Go-прокси:**
- Kernel-level, ничего не парсится в userspace
- Решение «промышленное», переиспользуемо для других egress-задач
- Нет дополнительного процесса на egress'е

**Минусы:**
- День работы вместо часа
- Требует root + WG kernel module на обоих VPS
- Конфиги ключей — лишний вектор для ошибки

##### Какой выбирать

**Сначала пробуем 9.3.A (Go-прокси).** Если решит — оставляем как есть.
Если поведение не улучшится — копаем глубже, возможно идём в 9.3.B
(WireGuard) либо разбираемся с самим egress-провайдером.

#### Acceptance
- 9.1 даст диагностику в логах за 1 день
- После 9.2: юзер видит плавную трансформацию одного сообщения, никаких
  «ack после реплая». 0 жалоб 24 часа.
- (если делаем 9.3): outbound `sendMessage` p95 latency < 800мс
  стабильно, без spike'ов >5 секунд

### 10. MAX integration — sprint целиком, сразу long-poll

**Контекст.** Бот в МАКС зарегистрирован, токен в `/etc/sreda/.env`
как `SREDA_MAX_BOT_TOKEN`. Schema 0035 уже добавила `users.max_account_id`
+ `tenants.preferred_channel`. Inbound/outbound для MAX **не реализован**.
Юзер с MAX сейчас не может ничего ни отправить ни получить.

**Решение принято 2026-04-30 PM:** делаем **сразу long-poll**, без webhook'а.
Webhook путь для TG сегодня показал свою фрагильность (middlebox'ы между
TG и нашим nginx); у MAX по тому же сценарию могут быть аналогичные
проблемы. Нет смысла идти `webhook → потом ломаться → переделывать на
long-poll`. Шаблон `TelegramLongPoller` готов и протестирован — копировать
проще чем писать webhook путь с нуля.

**Объём:** 2-3 рабочих дня сплошным спринтом (не куски — иначе контекст MAX
API будет каждый раз заново грузить).

**Файлы:**
- `src/sreda/integrations/max/client.py` — `MaxClient` (httpx-обёртка
  над `platform-api.max.ru`). Аутентификация через `Authorization`
  header, не Bearer-token как у TG
- `src/sreda/services/max_inbound.py::handle_max_update` — channel-agnostic
  durable ingest, тот же lifecycle `ingested → processing_started → processed`
  на `inbound_messages.processing_status`
- `src/sreda/services/onboarding.py::ensure_max_user_bundle` — создаёт
  tenant/user/workspace для MAX-юзера (по `max_account_id` вместо
  `telegram_account_id`)
- `src/sreda/workers/max_long_poll.py` — `MaxLongPoller`, advisory
  lock с другим `LOCK_KEY`, heartbeat в той же `poller_heartbeats`
  с `channel='max'`, offset в `poller_offsets` с `channel='max'`
- `deploy/systemd/sreda-max-poller.service` — отдельный systemd unit
- `tests/unit/test_max_long_poll.py` — копия паттернов test_telegram_long_poll.py
- `scripts/dev/probe_max_api.py` — research-script для понимания формата
  updates, ack-механики, error codes (нет публичной доки уровня TG)

**Что НЕ пишем:**
- ❌ FastAPI webhook route для MAX — обходимся long-poll'ом сразу
- ❌ MAX-specific privacy_guard — общий privacy_guard работает на тексте
  независимо от канала

**Acceptance:**
- Юзер регистрируется в MAX → `ensure_max_user_bundle` создаёт tenant
- Юзер пишет → ответ за 5-15 секунд (как TG)
- 0 алертов от `unprocessed_inbound` за 24 часа
- Tenant с `preferred_channel='max'` получает outbound в MAX, не TG
- Tenant с обоими каналами (`max_account_id IS NOT NULL AND
  telegram_account_id IS NOT NULL`) — отдельный тест-кейс на маршрутизацию

**Когда:** после закрытия пункта 9 (outbound фикс), ориентир — после
завтрашнего разбора `message_id` логов.

**Риск:** MAX API менее зрелый чем TG, нет публичной доки. Возможно
придётся реверс-инжинирить через probe. On probe заложить ~0.5 дня.

### 11. recall_memory proactive policy — staged roadmap

**Контекст.** 30 апреля 2026 ~19:42 МСК юзер tg=755682022 написала
«покажи все ткани с характеристиками ширина и усадка». Бот ответил
«пока только одна ткань: Лён хлопок пудра». В реальности в
`assistant_memories` уже было 5+ записей про другие ткани (Тенсель
шампань, Страйп шампань, Страйп лайм, Индийская сирень, Пепельная
сирень тенцель), созданных 22-25 апреля и активно использовавшихся.

**Stage 1 — ✅ DONE 2026-05-01 (commit `<TBD>`)** — hotfix
prompt/tool contract:
- `recall_memory` docstring переписан на императивные триггеры
  (ALWAYS на списочных запросах, BEFORE на негативных ответах).
- `_CORE_SYSTEM_PROMPT` блок про память: добавлены ОБЯЗАТЕЛЬНО
  и ВСЕГДА директивы, плюс анти-internals и анти-confabulation
  правила.
- `tests/unit/test_recall_memory_prompt.py` — 4 unit-теста на
  prompt builder (4/4 зелёные).
- `docs/qa/recall_memory_smoke.md` — 5-сценарный manual checklist
  для прогона на dev-боте после deploy'а.

**Stage 2 — ✅ DONE 2026-05-02** (commit `<TBD>`)

Реализован структурированный лог в `node_load_memories`:

```
INFO sreda.runtime.graph node_load_memories tenant=<id> user=<id>
  candidates_total=N with_embedding=N
  filtered_below_min=N seeded=N
  min_score=X.XXX top_k=N
  scores_min=X.XXX scores_max=X.XXX scores_p50=X.XXX
```

`MemoryRepository.recall` обёрнут в backward-compatible thin wrapper над
новым `recall_with_stats(...) -> (hits, RecallStats)`. Старые caller'ы
не затронуты.

Прогон **1-2 суток на проде**. Анализ через awk/grep по trace.log:
- Сколько в среднем `seeded` в `[ПАМЯТЬ]`? (целевое — близко к top_k=10)
- Сколько отфильтровалось `min_score`'ом?
- Какая медиана `scores_p50`?

После анализа решаем — нужен ли Stage 3 (retrieval params tuning) и
с какими параметрами.

**Stage 3+ — conditional design notes** (не обязательный путь, активировать
только если evidence из Stage 2 + smoke checklist'а покажут что Stage 1
недостаточно):

- **Stage 3 (retrieval params tuning):** менять `top_k` / `min_score`
  через `RuntimeConfig` (admin-toggleable), не in-place в коде.
  Risks: понижение порога → больше irrelevant memories → возможно
  ухудшение ответов на specific factual queries.
- **Stage 4 (broad recall + rerank):** candidate pool 50 + rerank до
  top-10. ~1 день кода. Без schema changes.
- **Stage 5 (structured facts):** новая таблица `tenant_facts` с
  типизацией entities — `fabric`, `contact`, `order`. Plain
  metadata flags (`has_width`) рядом с encrypted attributes.
  Detеrministic SQL для list-style queries. ~2-3 дня на первый
  домен. Требует отдельный HMAC ключ `MEMORY_FACT_NAME_HMAC_KEY`.
- **Stage 6 (blind token index):** keyword index с per-tenant HMAC
  (`HMAC(tenant_token_key, normalized_token)`). ~2 дня.
  Leakage: внутри tenant'а frequency/equality раскрывается, across
  tenants — нет (благодаря per-tenant ключу).

Полный детализированный staged plan + risk register — в
`~/.claude/plans/mellow-discovering-conway.md` (одобренный 1 мая 2026).

**Открытые вопросы для Stage 5+** (требуют отдельной discovery-сессии):
1. Какие fabric-атрибуты обязательные/опциональные? (минимум:
   width_cm, shrinkage_edge_pct, shrinkage_cross_pct)
2. Migration legacy assistant_memories с темой «ткани» в
   `tenant_facts` — автоматически или вручную?
3. Threat model: 152-ФЗ или защита от mole внутри SaaS?
   От этого зависит выбор HMAC-стратегии.
4. Stage 4 rerank choice: cross-encoder (CPU latency?) vs simple
   score formula vs LLM-rerank (latency?). Нужен бенчмарк.

### 12. Findings из conversation review 2026-05-03 (5 дней соака)

**Контекст.** 2026-05-03 проанализировал 179 turns / 9 active users
за период 28 апреля — 3 мая. Расшифровал outbox + inbound через prod
encryption key. Полная сводка в чате 2026-05-03.

**Здоровье системы:** 0 stuck, 0 ignored, 0 outbox drops. Все 9 юзеров
получили ответы. Stage 1 recall_memory hotfix эффективен (5 вызовов
recall_memory, юзер 755682022 после 1 мая работает чисто).

**Найденные проблемы (4):**

#### 12.1. Capability-confabulation (Boris 1 мая 8:10-8:12) 🔴

**Симптом.** Юзер: «Можешь сделать себе задачу — каждое утро в 8
присылать погоду?». Бот ответил **«Готово!» дважды** (8:10, 8:11),
описал что «настроил» — и только на третий turn (8:12) признался:
«К сожалению, напоминание отправляет только статический текст — я не
могу автоматически проснуться, посмотреть погоду и прислать актуальный
прогноз. Это ограничение системы».

То есть бот сначала **соврал что выполнил**, потом исправился. Это
фактический log_unsupported_request (запись в feature-requests.log
от 5:12 этого дня), но **с trail of false confirmations**.

**Тип проблемы.** Capability-level confabulation — отличается от
memory-level (закрытой Stage 1). Stage 1 запрещал выдумывать
ретроспективу действий. Capability-confabulation — это выдумывание
будущей способности.

**Fix (Stage 1.1, ~30 мин кода + тесты):**

a) Расширить system prompt в `_CORE_SYSTEM_PROMPT`:

```
- НЕ говори "Готово" / "Сделала" / "Настроила" пока в этом ЖЕ turn'е
  не было реального tool-call'а который это выполнил. Если решил
  что-то сделать — сначала вызов tool'а, ПОТОМ рапорт.
- Если запрос требует CAPABILITY которой у тебя нет (например,
  "автоматически каждое утро рассчитывать данные и присылать") — НЕ
  обещай что сделаешь. Сразу вызови `log_unsupported_request` и
  честно объясни ограничение.
```

b) Уже есть похожее правило в _HOUSEWIFE_FOOD_PROMPT (line 990):
   «Не отчитывайся о несделанном». Но оно про housewife scope —
   eds_monitor / generic chat не покрыт. Расширить на core-level.

c) Regression-тесты в `test_recall_memory_prompt.py`:
   - `test_anti_capability_confabulation_in_system_prompt` —
     инструкция против "Готово без tool-call" присутствует в
     compiled prompt
   - `test_log_unsupported_request_called_on_capability_gap` — на
     запрос вида «делай X каждое утро автоматически» (где X требует
     dynamic compute) bot вызывает log_unsupported_request, не «Готово».

**Когда делать:** P1, ~30 мин. Рядом с Stage 1 hotfix'ом по логике.

#### 12.2. Динамические напоминания (real product gap) 🟡

**Симптом.** Юзер запросил то, что bot не умеет: cron + fetch_data +
LLM-format + send. Текущая `schedule_reminder` шлёт статический
заранее заданный текст.

**Архитектурное решение:**
- Добавить тип reminder'а `dynamic` (схема: рекуррент + LLM-prompt
  template который при срабатывании генерирует свежий контент)
- При срабатывании worker вызывает LLM с template + tools (включая
  `get_weather`, `web_search`) → LLM формирует свежий ответ → outbox
- Dedup по `dynamic_key` чтобы при перезапусках worker'а не дублить

**Effort:** 2-3 дня. Требует:
- schema migration (`reminder_type`, `dynamic_template`, `dynamic_key`)
- доработка `proactive_events` worker'а
- LLM-prompt для генерации контента из template
- ~10 unit-тестов

**Когда делать:** P2. Не блокер, но фича востребованная (Boris лично
её попросил).

#### 12.3. update_reminder tool отсутствует — UX trash 🟡

**Симптом.** Юзер «Дорогая Юлечка» (893811320) 1 мая 11:26-11:30:
- 11:26 «Не приходят напоминания» → bot пересоздал
- 11:27 «Должно быть сегодня каждый час до 20 ч» → bot пересоздал
- 11:27 «Учти, у меня +1 час к московскому» → bot пересоздал ещё раз
- 11:28 bot выдал 2 разных ответа подряд про разные timezone
- 11:29 «Начни оповещать сегодня» → bot пересоздал в 4-й раз

Каждое уточнение от юзера → delete+create нового reminder'а. Это
плохой UX: state дёргается, юзер не понимает что **актуально**, и
если тот период был glitch — не уверен что reminders пойдут.

**Fix:** добавить tool `update_reminder(id, **fields)`. LLM при
получении уточнения должен вызывать `update_reminder` с известным
`reminder_id` (видимым через `list_reminders`), а не `cancel + create`.

**Effort:** ~3-4 часа. Доработка housewife_chat_tools + обучение
LLM-у через docstring + 3-4 теста.

**Когда делать:** P2. Хорошее улучшение UX для всех reminder-flow.

#### 12.4. Onboarding /start spam (1089832184, 4 раза подряд) 🟡

**Симптом.** Юзер 28 апреля 22:28-23:00 написал:
```
22:28 USER: Расписание
22:49 USER: /start
22:49 USER: /start
23:00 USER: /start
04-29 10:43 USER: /start
```

В этом dump НЕ ВИДЕЛ ответов от бота на эти `/start`. Возможные
объяснения:
- Юзер был в pending-approval статусе → silent-drop через
  `pending_bot.match()` flow
- Pending-bot отвечал, но в outbox`status='sent'` query это не
  попало (есть фильтр `is_interactive=True` который мог отрезать?)

**Investigation (~1 час):**
- Посмотреть actual outbox-row'ы для этого тенанта 28-29 апреля
- Проверить `pending_bot.match()` flow на конкретном /start
- Если pending-bot молчит на `/start` — это баг (он должен слать
  «Заявка принята, ждите модератора»)

**Fix:** TBD после investigation. Возможно нужно:
- pending_bot отвечать на ВСЕ /start даже если уже отвечал ранее
  (idempotent welcome)
- ИЛИ показать loading-индикатор / typing статус юзеру чтобы понимал
  что бот видит сообщение

**Когда делать:** P3. Onboarding rough edge, но в текущем размере
(1 юзер за 5 дней) не критично.

---

**Открытые блокеры.**
- Решение по pricing (тиры sredaspace.ru) ещё не финализировано
  — есть в plan-файле, но вы не подтвердили. Без этого Phase 1
  лендинг не запустить (pricing block в hero).
- LLM-провайдер — текущая MiMo-Pro Singapore работает, но 152-ФЗ
  риск растёт со scale. Миграция на YandexGPT-Lite — отдельный трек.

---

## 0. Hot-fix'ы после cloud-migration (2026-04-29)

После переезда на VDS 62.113.41.104 (Phase 1-8 done 2026-04-28) видны
два косяка из реальной эксплуатации:

### 0.1 Погода/web_search не работает на VDS

**Симптом.** Юзер: «какая завтра погода в Сходне». Сейчас два пути
оба сломаны:
1. `wttr.in` через `fetch_url` — отдаёт **только текущую**, не прогноз.
2. `web_search` через DuckDuckGo (Bing backend) — `ConnectError` от
   `bing.com/search` (RU IP блокирует / RKN, не идёт через текущий
   NO_PROXY либо не туда роутится).

**Лог 2026-04-28 23:46 (МСК):**
```
WARNING sreda.services.web_search_tool web_search failed for
'погода Сходня завтра': ConnectError: bing.com/search
```

**Что сделать (выбрать ОДНО, обсудить):**
- **A.** Переключить web_search backend с DDG/Bing на что-то более
  стабильное от RU egress (Yandex Search API? OpenAI Responses
  search? Brave Search API?). Bing с RU блок'ит часто.
- **B.** Заменить fetch_url(wttr.in) на API Яндекс.Погоды (есть
  бесплатный тариф 50 запросов/сутки, прогноз на 7 дней) — direct
  call without web_search detour.
- **C.** Маршрутизировать web_search через SOCKS5 (как Telegram /
  Groq) — добавить Bing в proxy-routes.

**Файлы:** `src/sreda/services/web_search_tool.py`,
`src/sreda/services/fetch_url_tool.py`, `src/sreda/runtime/handlers.py`
(если меняем provider).

**Acceptance.** Реальный запрос «прогноз погоды на завтра в [город]»
→ корректный ответ с температурой / осадками на нужную дату.

### 0.2 Таймзона в логах

**Симптом.** Логи на VDS пишутся в UTC (`2026-04-28 20:47:43`), хотя
для дебага удобнее MSK. На Mac mini было локальное (тоже не идеально
для distributed setup, но удобнее в моменте).

**Что сделать.** Установить системную TZ на VDS либо явно указать в
logging formatter:

```python
# src/sreda/logging_config.py (или там где configure_logging)
import time
logging.Formatter.converter = time.localtime  # или явно pytz Moscow
```

Альтернатива системная: `sudo timedatectl set-timezone Europe/Moscow`
(но это сменит TZ для всего что пишет журналы — postgres, cron'ы и
т.п. — на 1 ноду тоже норм).

**Решение какое выбираем — обсудить.** Я склоняюсь к ISO timestamp
+ суффикс TZ (`2026-04-29T01:47:43+03:00`) — однозначно и при
любой ноде понятно. Но ломает грепы по «20:47», поэтому уточнить.

**Acceptance.** `tail /var/log/sreda/uvicorn.log` показывает MSK
(или явный TZ-суффикс), та же запись в трейсе и в админке /admin/logs.

### 0.3 LLM hallucinates reminder creation (incident 2026-04-29 00:17 MSK)

**Симптом.** Юзер: «поставь напоминалку на 9 утра каждый день на год —
принимать лекарства». LLM (MiMo-v2.5) ответил «Готово! ⏰ Каждый день
в 9:00 утра будет напоминание «Принять лекарства»...» **с пустым
tools=[]** — `schedule_reminder` НЕ вызван. В БД ничего не появилось.

**Trace:**
```
iter=0 tokens=23192/326 tools=[] text='Готово! ⏰ Каждый день в 9:00 утра ...'
```

**Это та же модель галлюцинации что была с checklist'ами 2026-04-28**
(см. commit a39a662). Detector добавили только для checklist
hallucination, не для reminders.

**Что сделать.**
1. Расширить detector в `src/sreda/runtime/handlers.py` (или там где
   сидит `_chat_response_validator`):
   - Если ответ LLM содержит «готово», «поставил», «напомнить»,
     «напоминание создано/добавлено», «✅», «⏰» — а в tools_used нет
     `schedule_reminder` → reject reply, force re-iter с подсказкой
     «вы заявили о создании напоминания но не вызвали schedule_reminder
     tool, обязательно вызовите его сначала».
2. Усилить prompt rule в `_HOUSEWIFE_FOOD_PROMPT` (или скил-промпте):
   - Добавить «MUST CALL TOOL FIRST: для reminders/tasks/checklists
     любое заявление об успешном создании ОБЯЗАТЕЛЬНО предваряется
     вызовом соответствующего tool. Без tool-call'а — отвечай "не
     получилось", не выдумывай результат».

**Acceptance.** Юзер просит создать reminder/task/checklist → LLM
**обязательно** вызывает соответствующий tool → если tool вернул
ошибку, ответ юзеру правдивый («не получилось, попробуй
переформулировать»). Никогда не «Готово!» без тула.

**Тесты:**
- Добавить unit-тест: дать LLM mock'нутый response без tool_call,
  проверить что детектор reject'ит.

**Файлы:**
- `src/sreda/runtime/handlers.py` (детектор)
- `src/sreda/services/housewife_chat_tools.py` или где сидит prompt
- `tests/unit/test_hallucination_detector.py` (новый)

### 0.4 Schedule counter в Mini App home показывает «пока пусто» в полночь МСК

**Симптом.** Юзер открыл Mini App в 00:30 MSK 29 апреля. На главной
карточка «📅 Расписание / пока пусто», но при тапе drill-down
показывает 2 задачи на 29 апреля.

**Корень.** В
`sreda-private-features/src/sreda_feature_housewife_assistant/plugin.py`
schedule counter использует `today = datetime.now(UTC).date()`. В
00:30 MSK это 21:30 UTC 28 апреля → ищет задачи на 28-е, ничего не
находит. Drill-down (`/api/v1/schedule/week`) использует ту же
функцию `list_today` но через 7-day window, поэтому 29-е попадает
в окно и показывается.

**Fix.**
```python
from zoneinfo import ZoneInfo
from sreda.db.repositories.user_profile import UserProfileRepository

profile = UserProfileRepository(session).get_profile(tenant_id, user_id)
tz_name = (profile.timezone or "Europe/Moscow") if profile else "Europe/Moscow"
today = _dt.now(ZoneInfo(tz_name)).date()
```

Дефолт `Europe/Moscow` чтобы существующие профили с `timezone='UTC'`
не показывали путаницу. В будущем: при онбординге спрашивать TZ
или определять по `tg.from.language_code`.

**Файл:** `sreda-private-features/.../plugin.py` строка 117.

### 0.5 Убрать форму ввода из списка покупок

**Симптом.** В Mini App «Покупки» сверху висит инпут «Что добавить в
список?» + кнопка «Добавить». Это противоречит позиционированию
Среды — «голос как главный режим». Юзер должен использовать голос
или текст в чате, а не вбивать в форму.

**Что сделать.**
- Убрать `<input>` + `<button>Добавить</button>` из шапки экрана
  «Покупки» в Mini App.
- Заменить на read-only-надпись (можно с иконкой 🎙️):
  «Просто скажи мне что добавить или удалить из списка»
- Для удаления отдельного пункта оставить чекбокс (current behavior),
  но кнопку «очистить весь список» (мусорка справа сверху) — обсудить
  оставлять или нет.

**Файл:** Mini App template для shopping (`miniapp/templates/...`
или JS-роут #/shopping). Найти grep'ом по «Что добавить в список».

**Acceptance.** В «Покупки» нет input-поля. Сверху совет «скажи мне
голосом или текстом». Чекбоксы для отметки купленного остаются.

---

### 0.6 Fire-and-forget ack всё равно ощущается медленным

**Симптом.** 2026-04-29 ~01:30 МСК. После деплоя fire-and-forget
(commit `0737119`) trace показывает `voice.download` стартует на 4ms
параллельно с ack — но юзер по ощущениям не видит ускорения, ack
всё равно «приходит поздно».

**Trace подтверждает деплой:**
```
0ms webhook.received type=voice
0ms ack.sent [304ms] phrase=Посмотрю
4ms voice.download [716ms]
```
Технически `ack` и `voice.download` стартуют одновременно. Но user
perception ≠ trace timing.

**Гипотезы корня:**
1. **Telegram delivery ordering.** Telegram сервер может ставить ack
   message в очередь чата ПОСЛЕ longer-running operations того же
   chat'а. Нужно проверить timestamp в самом Telegram (когда юзер
   видит сообщение) vs когда мы вернули sendMessage.
2. **Connection pool warming.** Первый sendMessage после рестарта
   uvicorn делает TLS handshake (~150-300мс через SOCKS5). После —
   keepalive. Если ack — первый запрос, он stalls dependent voice.
   Pool prewarm на startup мог бы помочь.
3. **TCP head-of-line blocking** на shared connection — ack и
   download делят connection из pool. Если ack задерживается на
   server side, download стримит на ту же connection и ждёт.
4. **Async scheduling**. `asyncio.create_task` не гарантирует что
   coroutine стартует немедленно — event loop может промедлить.
   Передавать через `loop.call_soon` / `eager_task_factory` могло
   бы стартовать ack synchronously до return.

**Что сделать.**
- Добавить trace.step с реальным wall-clock при отправке ack:
  `ack.posted_at` (когда наш sendMessage вернул 200) vs
  `ack.network_visible_at` (когда поллинг увидел свой message_id).
- Замерить ack delivery time в Telegram client side (проверить через
  message_id sequence — если ack message_id < reply message_id,
  Telegram посылает ack первым).
- Если корень — pool warming, добавить prewarm на uvicorn startup
  (ping `getMe` через все pool connections).
- Если корень — call ordering, попробовать `asyncio.shield` +
  явный `await asyncio.sleep(0)` для немедленного yield.
- Если корень — Telegram chat queue ordering, добиться невозможно
  на стороне клиента; стратегия — посылать ack БЫСТРЕЕ через
  separate-channel (например через bot's getUpdates emulation —
  не реализовать).

**Acceptance.** Юзер видит ack «Посмотрю/Сейчас» в течение 1 секунды
после отправки голосового, при том что full reply ещё crunch'ится.
Метрика — wall-clock от user_send_voice до ack_visible_in_chat
< 1500ms.

**Файлы:**
- `src/sreda/api/routes/telegram_webhook.py` — trace доплнения
- `src/sreda/integrations/telegram/client.py` — pool prewarm
- Возможно `src/sreda/main.py` (FastAPI lifespan startup hook)

---

### 0.7 Чек-листы: дубликат создаётся при single-turn create+add

**Симптом.** 2026-04-29 ~01:29 МСК. Юзер просит «создай чек-лист
„Доработки среды" и добавь туда „Создать сайт для среды"». LLM
ответил «Готово ✅ Создала чек-лист „Доработки среды" и добавила
первый пункт». В Mini App «Дела» появились **ДВА** одинаковых
чек-листа «Доработки среды» с одним пунктом «Создать сайт для
среды» в каждом.

**Скриншот:**
```
📋 Дела

Доработки среды        1/1
☐ Создать сайт для среды

Доработки среды        1/1
☐ Создать сайт для среды
```

**Гипотеза.** LLM в одном turn'е делает 2 tool-call'а:
1. `create_checklist(title="Доработки среды", items=["Создать сайт..."])`
2. `add_checklist_items(...)` или второй `create_checklist(...)`

Что-то из этого создаёт второй row. Или dedup по title не работает,
или сама pre-existing БД хранит две записи (Mac DB накачен on top
of VDS — могла быть существующая запись + новая создалась).

**Что сделать:**
1. Получить trace turn'а — посмотреть какие tool-calls сделал LLM.
   `grep -A 50 "0:29" /var/log/sreda/trace.log` или поиск по
   tenant=tenant_tg_352612382 в районе времени.
2. Проверить ChecklistService.create — есть ли там idempotency / dedup
   по (tenant_id, user_id, title).
3. Проверить prompt: даёт ли LLM явное правило «один tool-call на
   создание + add_items в том же call'е, либо create_checklist с
   items=...»?
4. Если LLM делает 2 create_checklist подряд — добавить dedup в
   `ChecklistService.create`: если уже есть row с таким же title для
   этого user'а в последние 60 секунд — return existing вместо create.

**Файлы:**
- `src/sreda/services/checklist.py` (или где сидит ChecklistService)
- `src/sreda/services/housewife_chat_tools.py` (tool definitions)
- `src/sreda/runtime/handlers.py` (prompt)

**Acceptance.** Юзер просит «создай Х и добавь туда Y» → создаётся
ровно ОДИН чек-лист с пунктами.

---

### 0.8 Чек-листы: тап по галочке перезагружает страницу

**Симптом.** В Mini App «Дела» юзер тапает чекбокс возле пункта.
Вместо плавного toggle'а (галочка ставится/снимается inline через
fetch() PATCH) — вся страница перезагружается. UX портится:
скролл сбрасывается, скрин мигает, видны loading states.

**Гипотеза.** Чекбокс рендерится как `<input type="checkbox">`
внутри `<form>` — submit формы по default'у делает navigation.
Или JS handler не вызывает `event.preventDefault()`. Или fetch
завершается, потом window.location.reload() вызывается явно.

**Что сделать:**
1. Найти HTML/JS где рендерится чекбокс (вероятно
   `src/sreda/miniapp/templates/...` или dynamic JS в shopping-style).
2. Убедиться что:
   - Click handler вызывает `e.preventDefault()`
   - PATCH идёт через fetch() без перезагрузки
   - DOM обновляется inline (toggle класс «checked» / replaced span)
3. Скриншот текущего поведения в bug-репорт.

**Acceptance.** Тап галочку → состояние меняется без reload, скролл
не прыгает, нет flash of empty content.

**Файлы:**
- `src/sreda/miniapp/templates/...` (checklist screen template)
- Соответствующий JS в template или отдельный .js файл

---

### 0.9 Не писать содержимое сообщений / ответов в логи (152-ФЗ)

**Симптом.** В `/var/log/sreda/uvicorn.log` пишется raw payload:

```
2026-04-29 07:48:18 INFO sreda.llm response tenant=tenant_tg_352612382
feature=housewife_assistant iter=0 tokens=23933/72 tools=[]
text='Спасибо, что спрашиваешь! 😊 Работаю, всё хорошо. У тебя как дела?
Чем могу помочь сегодня?'
```

Также видны:
- `sreda.llm invoke ... last='привет'` — последняя human-message
- `sreda.runtime.handlers chat: fallback LLM built provider=...`
  (no leak, OK)

**Почему критично.** 152-ФЗ + general PII hygiene: сообщения юзеров и
ответы ассистента — персональные данные. Логи на VDS читаются при
дебаге, могут попасть в backup, в Object Storage. Не должны
содержать сырого контента.

**Что сделать.**
1. Убрать `text=...` поле из `sreda.llm response` лога. Заменить на
   `chars=N` (длина) и `tools=[...]` — этого хватает для дебага
   разрывов вроде «бот молчит» / «бот не вызвал tool».
2. Убрать `last=...` из `sreda.llm invoke` лога. Заменить на
   `last_chars=N` и `last_role`.
3. Проверить весь stack: `runtime/handlers.py`, `services/llm.py`,
   `runtime/graph.py`, `services/inbound_messages.py`,
   `services/housewife_*.py`. Любое логирование `text`, `payload`,
   `content`, `message` должно быть с redaction или вообще снято.
4. Trace тоже — `chat.reply chars=271` (OK, длина), но проверить нет
   ли `text=` где-то.

**Acceptance.** В `/var/log/sreda/uvicorn.log` НЕТ raw text юзера или
бота. Только metadata (длины, tool-имена, ID'ы, latency, tenant_id).

**Файлы:** `src/sreda/services/llm.py`, `src/sreda/runtime/handlers.py`,
любые `*.py` где `logger.info("... text=%r", text)` или подобное.

---

### 0.10 web_search полностью переписать на Tavily

**Статус:** key уже в `/etc/sreda/.env::TAVILY_API_KEY`, доступность
api.tavily.com c VDS подтверждена 2026-04-29.

**Симптом сейчас.** `duckduckgo_search==8.1.1` через `backend="auto"`
ходит на html-scraping DDG, который под капотом форвардит query
через bing.com. Bing блочит RU egress (`89.110.77.78`) → ConnectError.
«Только Yandex+Google» в этой библиотеке не настраивается — она
сделана исключительно под DuckDuckGo SERP'ы.

**Решение.** Заменить `build_web_search_tool` на новый
`tavily_search` tool через Tavily API:
* `pip install tavily-python`
* `TavilyClient(api_key=settings.tavily_api_key).search(q, max_results=3)`
* Адаптировать output под текущий контракт `web_search` (форматированный
  блок «N. Title\n<snippet>\n<url>») чтобы prompt rules не ломались.

**Что не трогать.** `fetch_url` — он годен для чтения конкретных URL
(когда LLM получил ссылку и хочет вытащить content). Tavily может
сам предоставлять content, но `fetch_url` — это другой контракт.

**Acceptance.** Запрос вне погодного домена («новости», «когда
открывается ИКЕА», «что такое Х») → Tavily возвращает 1-3 результата,
LLM формулирует ответ. Никаких ConnectError на Bing. 0.1 закрывается
как FIXED.

---

### 0.11 Pre-existing FK bug (carry-over) — FIXED 2026-04-29

✅ Заfix'ен в commit `c39a11c`. Регрессия покрыта 3 тестами.

**Симптом (был до VDS-миграции).** Webhook `tenant_tg_1089832184` →
500 на insert outbox: `FOREIGN KEY constraint failed`,
`user_id='user_tg_[phone]'` — какой-то PII-маскировщик `[phone]`
подменяет 10-цифровой ID в data path вместо log path.

**Файлы для расследования.** Любая sanitizer-функция применяемая к
user_id перед insert. Вероятно `src/sreda/services/sanitize.py` или
runtime/handlers.py при сборке payload outbox_messages.

**Acceptance.** Тенант с 10-цифровым telegram_id (например 1089832184)
получает корректный outbox без FK-violation.

---

## 1. Доработка онбординга — 🗑 REMOVED 2026-04-29

Не делаем. Wizard-onboarding (intro → voice → ... → done) реализован
2026-04-29 как edit-based wizard в чате. Дополнительные шаги
(«ты/вы», авто-approve) сняты — см. п.8 (только «ты», полностью).

---

<details><summary>Историческое содержимое (не делать)</summary>

## 1. ~~Доработка онбординга~~

**Что входит.**

- **Вопрос про обращение (ты/вы)** — не реализовано, висит с
  2026-04-23. При первом welcome после approval показать кнопки
  «На ты» / «На вы», сохранить в `TenantUserProfile.address_form`,
  инжектить тон в `_HOUSEWIFE_FOOD_PROMPT` и ack-фразы.
  - Файлы: `src/sreda/db/models/user_profile.py` (+колонка),
    `migrations/versions/*`, `src/sreda/services/onboarding.py`,
    `src/sreda/runtime/handlers.py`,
    `src/sreda/services/ack_messages.py`.
  - Оценка: 1–2 часа.

- **Пройти онбординг вживую под новым юзером** (после сегодняшнего
  сброса `tenant_tg_352612382`) — найти оставшиеся шероховатости
  в репликах или переходах. Добавить в `docs/copy/welcome.md` при
  правке.

- **Авто-approve для «доверенных» источников** (опционально — обсудить
  нужно ли до запуска тарифов).

**Acceptance.**
- Новый юзер /start → подключение → welcome с вопросом «ты/вы» →
  выбор сохранён → следующие ответы уже в выбранном тоне.
- `docs/copy/welcome.md` обновлён с новыми текстами.

</details>

---

## 2. Доработка aha-моментов — ⏸️ DEFERRED после п.3 (2026-04-29)

Откладываем до завершения п.3 (сайт sredaspace.ru). Aha-моменты — UX
fine-tuning, имеют смысл когда есть приток новых юзеров с лендинга.

**Что входит.**

- **Aha-3 (частый продукт → список покупок)** — в `docs/copy/aha-moments.md`
  помечен как ⚠️ draft. Реализовать:
  - детектор повторов в `FamilyContext.mentioned_products` (новое
    поле / таблица `mentioned_products`);
  - worker в стиле `onboarding_aha_worker`;
  - sentinel-запись для идемпотентности.
  - **Открытый вопрос:** какой threshold — 2 упоминания за 2 дня, 3?

- **Aha-4 (recall обещания через 3 дня)** — ⚠️ draft. Требует:
  - новый tool `flag_unfulfilled_mention` для LLM (пометка фактов
    без привязанного reminder'а);
  - новая таблица `unfulfilled_mentions`;
  - worker + resolve-handler для кнопок.

- **Проверить Aha-2 на проде** — после сегодняшнего сброса юзера
  пройти флоу: упомянуть диету в первый день → через 20ч должно
  прилететь предложение меню. Проверить sentinel, окно времени,
  текст.

- **Унификация кнопок на `ui_labels`** — сейчас `BUTTON_ACK` и
  `BUTTON_SNOOZE` используется только в `housewife_reminder_worker`.
  Протянуть тот же источник в остальные места (pending_bot демо-примеры
  уже синхронизированы текстом, но не через константы).

**Acceptance.**
- Aha-3 и Aha-4 переведены из ⚠️ в ✅ в `docs/copy/aha-moments.md`.
- `test_anti_stalker_tone.py` green после добавления новых workers.

---

## 3. Сайт sredaspace.ru (лендинг + тарифы + регистрация + оплата)

**Зафиксировано:**
- Домен: **sredaspace.ru**.
- Платежи: **ЮKassa**.
- Юр.лицо: **самозанятый** (ограничения: без найма, лимит дохода
  2.4 млн ₽/год, нельзя ряд видов деятельности — проверить что
  «подписка на AI-сервис» проходит под код ОКВЭД разрешённых).

**Бренд:** домен `sredaspace.ru` прямо намекает что сайт — про Среду.
Заголовок лендинга «Среда — персональный ассистент», URL-компонент
`/space` можно позже использовать как название каталога скилов
(если появятся новые скилы кроме housewife).

**Открыто утром решить:**
- **Стек сайта.** Astro (статика + 1 форма регистрации) — рекомендую.
  Альтернатива — Next.js (если хотим серверный рендер / API в том
  же проекте). Для самозанятого Astro проще — меньше зависимостей.

**Подготовить для ЮKassa (день-1 задача):**
- [ ] Зарегистрировать магазин в личном кабинете самозанятого
      (через «Мой налог» + ЮKassa OAuth).
- [ ] Сгенерировать `shop_id` + `secret_key` (положить в
      `.secrets/yookassa_*`).
- [ ] Настроить webhook `/webhooks/yookassa` в Среде (новый route).
- [ ] Оферта на сайте + политика обработки перс.данных
      (template'ы ЮKassa — можно брать за основу).
- [ ] Чек формируется ЮKassa-стороной через 54-ФЗ интеграцию
      (самозанятый → НПД режим, чеки автоматом).

**Минимум MVP лендинга:**
- Hero + УТП из `docs/gtm-plan.md` (3 дифференциатора — память,
  проактивность, голос).
- 3 примера диалога из `docs/copy/welcome.md` (ветки
  `demo_morning`, `menu_example`, `schedule`).
- Скриншоты Mini App (расписание + покупки).
- Секция тарифов: читает цену из БД Среды (`services/pricing.py`
  + эндпоинт `GET /public/pricing` на сервере).
- CTA → прямая ссылка в Telegram-бота + «Оплатить подписку» через
  ЮKassa Checkout (opens в редирект после регистрации).

**Acceptance для п.3:**
- Сайт задеплоен на sredaspace.ru, SSL работает.
- Нажатие «Оплатить 990 ₽/мес» → ЮKassa → чек в Telegram от юзера
  + активация подписки в БД Среды через webhook.
- Политика 152-ФЗ и оферта опубликованы как отдельные страницы.

**Оценка:** лендинг без платежей — 4–6 часов. С ЮKassa webhook
+ политиками — 1–1.5 дня.

---

## 4. Перенос Среды в облако — ✅ DONE 2026-04-29

Cloud migration на VDS Timeweb (62.113.41.104) полностью завершена.
nginx + TLS + DNS + SOCKS5 egress + systemd services + backup automation.
Mac mini оставлен на 7-дневный rollback период.

<details><summary>Историческое содержимое</summary>

## 4. ~~Перенос Среды в облако~~ (объединённо п.4+п.5)

**Решено:** п.4 и п.5 — одно и то же. Один prod-контур. Staging
пока не делаем (ранний этап, MVP).

**Открыто утром:**
- **Провайдер.** Timeweb Cloud / Selectel / Yandex Cloud / Beget?
  Для 152-ФЗ нужны серверы в РФ — значит НЕ Hetzner/AWS/DO.
  Рекомендую **Timeweb Cloud** или **Selectel** — оба РФ, гибкие.
- **Характеристики.** Эквивалент Mac mini (M1, 16 ГБ) для prod =
  4 vCPU + 8 ГБ RAM + 40 ГБ SSD. Стартово достаточно 2 vCPU + 4 ГБ,
  расширим по метрикам.

**Пошагово:**

- [ ] Заказать VDS (РФ-регион), SSH-ключ.
- [ ] Установить Python 3.13+, Nginx, certbot (Let's Encrypt для
      `bot.sredaspace.ru` + `miniapp.sredaspace.ru`).
- [ ] **Миграция SQLite → PostgreSQL** — важный шаг:
  - `pip install psycopg[binary]`.
  - Создать PG в облаке (managed или на том же VDS).
  - Прогнать `alembic upgrade head` на пустой PG.
  - Дамп SQLite `.dump` → конвертация в PG-compatible SQL
    (pgloader / вручную с фиксами типов).
  - ВАЖНО: зашифрованные колонки (`EncryptedString`) везут bytes,
    ключ `SREDA_ENCRYPTION_KEY` обязан быть тот же на новом сервере
    — иначе мёртвые данные (см. п.6).
- [ ] Перенести `sreda-deploy`:
  - Код через `git clone` (или `rsync` если worktree).
  - `.venv` — пересоздать.
  - `.secrets/` — переложить руками, НЕ коммитить.
  - `launch-sreda.sh` → systemd-юниты `sreda-uvicorn.service` +
    `sreda-job-runner.service`.
- [ ] DNS: `A`-записи для `bot`, `miniapp`, `sredaspace.ru` → новый IP.
- [ ] Переключить Telegram webhook на
      `https://bot.sredaspace.ru/webhooks/telegram/sreda`.
- [ ] Backup: `pg_dump` каждую ночь в cron → S3/Yandex Object Storage.
- [ ] Healthcheck: простой cron `curl /miniapp/ | grep 200` на
      внешнем сервисе (uptimerobot / свой).

**Acceptance.**
- Все 5 существующих тенантов-тестовых юзеров продолжают работать.
- Новый `/start` ведёт на прод-бота.
- Бэкап создался и проверяемо восстанавливается в тестовой PG.

**Оценка.** 1 день (VDS + PG миграция + DNS + тестовый прогон).

---

</details>

---

## 5. (объединён с п.4)

Был отдельным пунктом в первой версии плана, теперь часть п.4.

---

## 6. Шифрование + 152-ФЗ

Две связанные задачи: (а) compliance с законом о перс.данных,
(б) технически усилить E2E-like защиту переписки.

### 6.0 Политика данных: полное обезличивание (УТВЕРЖДЕНО 2026-04-25)

**Решение:** не становимся оператором ПДн. Никаких персональных данных
не хранится. Пока (до возможного пересмотра) — Вариант A из анализа
выше, **единственный** путь. Варианты B/C/D отклонены на этом этапе.

**Эффект:**
- 152-ФЗ формально не применяется к нашей обработке.
- Не нужна регистрация в Роскомнадзоре.
- Не нужно согласие на обработку ПДн как обязательный юридический
  шаг (оставим краткую «политику обезличенных данных» для доверия).
- MiMo-LLM можно оставить — в Китай уходит только обезличенный
  контекст, трансгран.передача ПДн не возникает.

**Что нельзя хранить** (выносим наружу):
- Прямые идентификаторы: `telegram_account_id` в открытом виде,
  настоящие имена родственников, телефоны, адреса, email,
  даты рождения, номера документов, мед.учреждения.
- Спец.категории (ст. 10 152-ФЗ): диагнозы, болезни, лекарственные
  термины, пищевые аллергии как мед.термин.

**Что можно хранить** (обезличенные эквиваленты):
- `tg_account_hash` = HMAC-SHA256(tg_id, server_salt). В БД —
  только хеш, не сам id. Mapping chat_id↔hash — эфемерный,
  не на диске.
- Роли семьи: `partner`, `child_1`, `child_2`, `parent`, `other`.
  Никаких имён «Петя»/«Маша».
- Возрастные группы: `preschool`, `primary`, `teen`, `adult`,
  `senior`. Не точный возраст.
- Кулинарные предпочтения (не мед.): «не ест молочное»,
  «без глютена», «любит курицу» — это кулинария. НЕ «лактозная
  непереносимость», НЕ «аллергия».
- Расписания без контекста места: «по понедельникам в 9:00
  кружок» — нет названия школы, адреса, контакта.

**Изменения в коде (план на реализацию):**

1. **Миграция БД + модели:**
   - Новая колонка `User.tg_account_hash` — HMAC с сервер.солью.
   - `User.telegram_account_id` → deprecate, не читать для новых юзеров.
   - `Tenant.name` — если юзер дал имя, хешировать до роли («you»).
   - `FamilyMember.name` (сейчас EncryptedString) → заменить на
     `FamilyMember.role_label` + `FamilyMember.nickname_hash`
     (если хотим различать «старший ребёнок» vs «младший»).
   - `FamilyMember.notes` — фильтр по blacklist мед.терминов при
     сохранении (LLM перегоняет в «не ест X», privacy guard
     блокирует сохранение если осталось «аллерг», «диагноз», имя
     врача, название лекарства).

2. **Privacy guard расширение:**
   - Текущие правила (телефоны, пароли, email, токены) — оставить.
   - Добавить мед.термины в blacklist: «аллерг», «непереносимост»,
     «диагноз», «заболевани», названия хронических болезней.
   - Добавить имена собственные — если LLM видит контекст «моя
     семья X», X заменяется ролью в сохраняемом факте.

3. **LLM prompt изменения:**
   - В `_HOUSEWIFE_FOOD_PROMPT` новый блок: «При сохранении фактов
     в память — НЕ используй имена, только роли. НЕ используй
     мед.термины, только кулинарные эквиваленты».
   - При диалоге с юзером бот может обращаться к имени («Пете»)
     в текущем turn'е (это не сохраняется), но в `save_core_fact`
     пишет «старший ребёнок».

4. **Webhook-слой:**
   - При получении update: хешируем `chat_id` → смотрим User по
     hash. Сам chat_id используем только для отправки ответа, в
     БД не попадает.
   - Memory-cache (LRU, 1000 entries) для mapping — чтобы не
     хешировать каждый раз.

5. **Оплата ЮKassa:**
   - Самозанятый принимает только сумму + «назначение: подписка на
     персональный ассистент». Плательщик → его банк, в нашу БД имя
     плательщика НЕ попадает.
   - ЮKassa webhook передаёт `payment.id` + `metadata.tenant_hash`
     (который мы подставили при создании счёта). Линкуем по хешу.

6. **Welcome + политика:**
   - Убираем из welcome формулировки типа «расскажи про семью —
     имена и возрасты». Заменяем: «расскажи о ролях — кто в семье
     (ребёнок, партнёр…). Имена мне знать не нужно».
   - Публикуем `sredaspace.ru/privacy` — «Политика обезличенных
     данных»: описываем что мы храним (хеши + роли), чего не
     храним (имена, контакты, мед.), как удалить аккаунт.

**Что теряем в UX:**
- Нельзя «Петя любит курицу» → «старший ребёнок любит курицу».
  Объясняется в welcome.
- «У Пети температура, напомни сироп» — сохраняется только «напомни
  лекарство через 4ч», без контекста «температура».

**Персонализация с хранением ПДн — отложено, не реализуем:**
Ранее рассматривался опциональный personalization-mode (юзер даёт
явное согласие, мы храним имена и становимся оператором в рамках
его tenant'а). На данном этапе — **отклонено**. Если продукт
оттестируется на обезличенных данных и появится явный спрос
(жалобы «не запоминает имена»), повторно рассмотрим — но это
потребует отдельного этапа compliance (регистрация, политика,
согласие, МиМо → YandexGPT).

**Acceptance критерии отвязки от 152-ФЗ:**

- [ ] В БД нет ни одного поля с именем в plaintext (кроме Telegram
      `chat_id` в эфемерном mapping-cache).
- [ ] В БД нет мед.терминов (проверка через grep по дампу + test).
- [ ] `telegram_account_id` заменён на хеш везде.
- [ ] ЮKassa не передаёт в нашу БД имя плательщика.
- [ ] Опубликована страница `sredaspace.ru/privacy` с пояснением
      «политика обезличенных данных».
- [ ] Юрист (или консультация) подтвердил: «с такой схемой не
      являетесь оператором ПДн по 152-ФЗ».

**Задел для MiMo (Китай):** если не храним имён и болезней — то в
LLM летит только обезличенный контекст. Передача в Китай
технически остаётся, но содержательно там нет ПДн → не трансгран.
передача ПДн. Компромисс по качеству: можно остаться на MiMo.

---

### 6.1 Аудит 152-ФЗ (что мы задеваем)

**Что надо выяснить:**

- [ ] **Какие перс.данные собираем.** Пройти по схеме БД с фильтром
      «это можно связать с личностью»:
  - `telegram_account_id` (прямой идентификатор)
  - `display_name` в `Tenant.name`
  - `FamilyMember.name` + `notes` (диеты, болезни — ПДн спец.категории!)
  - Raw payload в `SecureRecord` (номер телефона, имена, адреса)
  - `InboundMessage.message_text_sanitized` (после privacy guard)
  - `assistant_memories` (сохранённые факты)
- [ ] **Регистрация оператора** (в роли самозанятого): нужно
      уведомление в Роскомнадзор о начале обработки ПДн.
- [ ] **Политика обработки** — публикуется на `sredaspace.ru/privacy`.
      Включает: цели обработки, состав ПДн, сроки хранения,
      процедуру удаления по запросу, список получателей.
- [ ] **Согласие юзера** — при `/start` юзер должен согласиться с
      политикой (можно кнопкой в welcome: «Я согласен с обработкой
      ПДн → см. sredaspace.ru/privacy»).
- [ ] **Локализация хранения** — БД в РФ (VDS из п.4 обеспечивает).
      Но LLM-провайдеры (MiMo = Xiaomi, Китай) — КРИТИЧНО, спец.
      категории (болезни) туда отправлять нельзя без доп.согласия.
      Возможно нужен переход на отечественный LLM (YandexGPT,
      GigaChat) для housewife-скила.
- [ ] **Сроки хранения + процедура удаления.** Сейчас нет — всё
      хранится forever. Нужно:
      - `DELETE /api/v1/account/delete` — полное стирание тенанта
        (есть admin-reset как прототип).
      - Auto-retention для старых `InboundMessage` (365 дней?).
- [ ] **Трансграничная передача ПДн** (передача в Китай в MiMo) —
      формально требует отдельного уведомления или перехода на
      местный LLM.

**Приоритет:** критично до запуска платежей. Без политики и
согласия — риск штрафа 300k–500k ₽ (2026-04 ставки по ст. 13.11
КоАП).

### 6.2 Усиление шифрования — переписка только между ботом и юзером

**Цель.** Даже у админа БД нет доступа к тексту переписки.

**Техническое ограничение:** полный E2E невозможен, т.к. LLM-модель
находится на сервере и должна читать контент для ответа. Минимум
ОДНА сторона (сервер) должна иметь ключ расшифровки, хотя бы временно.

**Что можно усилить (в порядке impact):**

1. **Encrypted-at-rest с per-tenant ключом** — сейчас единый
   `SREDA_ENCRYPTION_KEY` шифрует `SecureRecord`. Заменить на
   **per-tenant data-encryption-key (DEK)**, шифруемый master
   `KEK` (envelope encryption):
   - Каждый tenant имеет свой DEK в столбце `tenants.encrypted_dek`
     (wrapped KEK'ом).
   - Admin не может читать `InboundMessage.message_text_sanitized`
     без tenant'а — нужно расшифровать DEK через KEK, это логируется.
   - KEK хранится в secrets-manager провайдера (Yandex Lockbox),
     НЕ в файле на диске.

2. **Расширить scope шифрования.** Сейчас шифруется только
   `SecureRecord` (raw payload) + `EncryptedString` колонки
   (имена семьи, notes). НЕ шифруется:
   - `inbound_messages.message_text_sanitized`
   - `outbox_messages.payload_json`
   - `assistant_memories.content`
   Надо: расширить на всё что содержит реплики.

3. **Retention policy (автоудаление).** Текст переписки в открытом
   виде живёт только N дней, дальше — только зашифрованные агре-
   гаты (для recall memory). После 365 дней — `DELETE`.

4. **Audit log доступов.** Каждое чтение зашифрованных данных
   (админ, отчёт, debug) пишется в `access_log(user, resource,
   timestamp, reason)`. Юзер по запросу может получить свой лог.

5. **LLM-провайдер** — компромисс.
   - **MiMo (Китай):** дёшево, но формально передача ПДн в Китай.
   - **YandexGPT / GigaChat (РФ):** compliance-friendly, но дороже
     и качество ниже на housewife-use-cases.
   - **OpenRouter + anthropic/openai:** дорого, с Китаем/США.
   Предложение: housewife — на YandexGPT, остальные скилы — ok
   через MiMo. Но вся переписка при этом отмечена как «может быть
   отправлено в РФ-LLM», в политике 152-ФЗ указываем именно это.

6. **«Режим инкогнито»** (опционально, UX). Команда `/incognito`
   временно отключает сохранение диалога в memory/history. Только
   текущий turn, без follow-up.

**Порядок реализации:**
- Шаг 1: закрыть compliance-минимум (6.1) — политика +
  согласие + удаление аккаунта — ДО платежей.
- Шаг 2: расширить шифрование на все message-колонки (6.2 п.2).
- Шаг 3: envelope encryption + per-tenant DEK (6.2 п.1) —
  при переносе в облако (п.4), заодно отрабатывается миграция ключа.
- Шаг 4: auto-retention + audit log — после первых 100 юзеров.

**Открытый вопрос утром:**
- **LLM-провайдер для housewife** — оставляем MiMo или
  переезжаем на YandexGPT? От этого зависит текст политики ПДн.

---

## 10. Рассылка нового welcome — ✅ DONE 2026-04-29

Существующим юзерам (9 одобренных) разосланы intro-сообщения через
manual API-call (script `scripts/broadcast_welcome_v2.py` для массовых
рассылок). Wizard-навигация (edit-based) реализована — юзеры могут
пройти tour без накопления 11 сообщений в чате.

**Цель.** Все юзеры, одобренные ДО введения новых welcome-цепочек,
никогда их не видели — не знают про чек-листы (Дела), память,
позиционирование «голос как главный режим». Сделать одноразовую
рассылку с нуля, как будто они новые.

**Скоуп.** Все `tenant.approved_at IS NOT NULL` — на 2026-04-27
вечер это 9 юзеров (включая 3 свежеактивированных
`2095569245 / 144679081 / 702229240`).

**Логика по юзеру:**

```
для каждого approved тенанта:
  1. отправить pending welcome chain (intro → 10 шагов через кнопки →
     done). Тур = 11 сообщений; юзер сам кликает дальше или закрывает.
  2. ПОСЛЕ done:
     - если у юзера в `tenant_user_profiles.display_name IS NOT NULL`
       → НЕ слать вопрос про имя (уже знаем как обращаться)
     - если `display_name IS NULL` → отправить `build_post_approve_message`
       («✅ Готово! Подскажи, как к тебе обращаться?...»)
```

**Технические задачи:**

1. **Webhook routing для `pb:` callbacks от approved юзеров.**
   Сейчас `pb:` обрабатывается ТОЛЬКО для pending. Нужно расширить:
   approved юзер тоже может тапать кнопки тура. Делаем флаг
   `tenant.product_tour_active` (или скилл-параметр) — пока тур
   активен, `pb:<branch>` обрабатывается через `pending_bot.match()`,
   не идёт в LLM.

2. **Idempotency.** Колонка `tenant.welcome_v2_sent_at`
   (DateTime nullable). Скрипт рассылки фильтрует по
   `WHERE welcome_v2_sent_at IS NULL`. После первого сообщения
   тура помечает таймстампом. Повторный запуск никого не задвоит.

3. **Скрипт рассылки** (`scripts/broadcast_welcome_v2.py`):
   - SELECT всех approved тенантов с `welcome_v2_sent_at IS NULL`
   - Для каждого:
     - Decrypt `telegram_account_id`
     - Уважаем `quiet_hours` юзера — если quiet → откладываем
     - Отправляем INTRO (msg 1 из pending_bot._INTRO) с кнопкой
       «🎙️ Голос →»
     - Помечаем `welcome_v2_sent_at = NOW()`
     - Throttle: 1 msg/sec (Telegram per-chat limit с запасом)
   - Не идём в БД пакетом — по одному, чтобы при ошибке
     не упали все 9.

4. **Состояние тура.** После тапа `pb:voice` → ... → `pb:done`,
   webhook видит callback `pb:done` → шлёт closing → если у юзера
   `display_name IS NULL` → шлёт `build_post_approve_message`
   и снимает `product_tour_active` флаг → следующий ход уже LLM.

5. **Логи и отчёт.** Скрипт пишет в `/tmp/sreda-broadcast-v2.log`:
   sent → tenant_X (chat_id=...), skipped → tenant_Y (quiet_hours).
   После завершения — короткое summary в админку.

**Открытые вопросы:**

- **Время рассылки.** Утро (10-11 локального TZ) лучше всего —
  люди не работают и не спят. По UTC у нас MSK (UTC+3); 10:00 MSK
  = 07:00 UTC. Делаем в worker'е по schedule, не в ручную.
- **Opt-out на будущие рассылки.** Добавить кнопку «Не присылать
  обновления» в closing? Может быть избыточно для первой рассылки;
  можно отложить до 2-й рассылки если будет.
- **Текст INTRO.** Сейчас в `pending_bot._INTRO` нет упоминания
  «обновлено» / «нового». Юзер может удивиться: «зачем мне это,
  я знаю Среду уже месяц». Mitigation: новая первая фраза в INTRO
  для рассылочной версии: «Привет! Я обновилась — добавила несколько
  вещей. Сейчас расскажу.» Делаем НЕ изменением `_INTRO`, а
  отдельным `_INTRO_BROADCAST` который отправляется именно скриптом
  рассылки.
- **Юзеры без display_name** на 2026-04-27 вечер — все 9 (никто
  не проходил новый flow). Значит после тура ВСЕ получат вопрос
  про имя — норм.

**Зависимости:**
- Миграция новой колонки `tenant.welcome_v2_sent_at`.
- Миграция или skill_param `product_tour_active` (можно положить
  в `tenant_skill_states` без миграции).
- Расширение webhook routing.

**Оценка.** ~3-4 часа:
- Миграция: 15 мин
- Webhook routing extension: 30 мин
- Скрипт рассылки + idempotency: 1.5 часа
- Тесты (новые + обновить telegram_webhook): 1 час
- Деплой + smoke на одном тестовом тенанте: 30 мин

**Verification.**
- Прогнать на тестовом 352612382 в одиночку — пройти полный тур,
  убедиться что после `pb:done` пришёл вопрос про имя (т.к.
  display_name пуст после очистки).
- Прогнать на тенанте с уже заполненным display_name — после
  `pb:done` НЕ должно быть вопроса про имя.

---

## 9. Полный housewife-онбординг — ✅ DONE 2026-04-29

Реализован как edit-based wizard tour (intro → voice → schedule →
reminders → checklists → shopping → recipes → family → memory →
dont_do → done) с prev/next навигацией в одном сообщении.

**Контекст.** В `HousewifeOnboardingService` был набор из 6 тем для
сбора первичного среза при первом разговоре после approve:
`addressing`, `self_intro`, `family`, `diet`, `routine`, `pain_point`.
LLM по очереди проводила юзера по списку. Свернули до одной темы
(`addressing` — только имя), потому что:
- Анкета после длинной pending-цепочки воспринималась как
  избыточная настройка.
- Тема `diet` упоминала «аллергии» — спецкатегория ст. 10 152-ФЗ,
  конфликт с обещанием «не записываю медицинские данные» в
  pending welcome.
- Расспросы про семью / рутину — лучше в естественный диалог по
  ситуации, не как опрос.

**Что сделано на 90%, ждёт возврата:**
- Все константы тем (`TOPIC_SELF_INTRO`, `TOPIC_FAMILY`, `TOPIC_DIET`,
  `TOPIC_ROUTINE`, `TOPIC_PAIN_POINT`) сохранены.
- `TOPIC_DESCRIPTIONS` сохранён (с `diet` уже без слова «аллергии»).
- Логика `_next_topic`, `mark_answered`, `mark_deferred`,
  `record_follow_up`, `format_for_prompt` поддерживает любое количество
  тем — менялись только содержимое `TOPIC_ORDER` и тесты.

**Что нужно вернуть** (всё в git history коммита 920f4cd):
1. `TOPIC_ORDER` — добавить обратно нужные темы.
2. Тесты `test_next_topic_first_pending_wins`,
   `test_next_topic_skipped_once_gets_second_pass`,
   `test_mark_answered_advances_topic_and_saves_summary`,
   `test_mark_deferred_once_moves_to_skipped_once_and_retries_later`,
   `test_format_for_prompt_shows_current_topic_and_status_markers`,
   `test_state_survives_service_recreation` — переписать обратно под
   multi-topic flow.

**Открытый UX-вопрос.** Какие темы вернуть и в каком порядке:
- A. Все 5 (полный опрос).
- B. Только `family` + `pain_point` (минимум для персонализации).
- C. Только `pain_point` (один вопрос «с чем больше всего нужна помощь»).
- D. Сделать темы опциональными в Mini App settings (юзер сам
  выбирает что хочет рассказать заранее).

**Зависимости.** Решить открытый UX-вопрос. Также: тема `diet` должна
формулироваться без мед-маркеров (диагноз/аллергия/лекарство) — это
обязательно по 152-ФЗ.

**Оценка.** ~1 час правок + ~30 минут на тесты.

---

## 8. Форма обращения «вы» — 🗑 REMOVED 2026-04-29

Не делаем. Решение 2026-04-29: бот всегда обращается на «ты», без
выбора. Существующие наработки (колонка `address_form`, миграция
`0026_add_address_form.py`, `update_profile(address_form=...)`,
`pick_ack(address_form=...)`) — остаются в БД/коде как dormant
infrastructure без active code paths. Удалять не обязательно — не
мешают.

<details><summary>Историческое содержимое</summary>

## 8. ~~Форма обращения «вы»~~ (отложено с 2026-04-27)

**Контекст.** Изначально в pending-плане после approve бот
спрашивал две вещи: имя и форму обращения («на ты или на вы»).
Реализовано полностью, задеплоено, потом свернуто — слишком много
шагов после длинной pending-цепочки. С 2026-04-27 (вечер) post-
approve flow упрощён до одного сообщения (только имя), форма «вы»
перенесена в backlog.

**Что сделано на 80%, ждёт возврата:**
- Колонка `tenant_user_profiles.address_form` (тип `String(8)`,
  значения `"ty"` / `"vy"` / `NULL`).
- Миграция `0026_add_address_form.py`.
- `UserProfileRepository.update_profile(address_form=...)` метод
  + `ADDRESS_FORMS` enum-const.
- Тесты `test_address_form_flow.py` (round-trip + repo validation).

**Что нужно вернуть** (всё лежит в git history коммита `51879df`):

1. Шаг 2 онбординга — `build_address_form_question_message` +
   state-machine в webhook.
2. Callback-handler `_handle_address_form_callback` +
   ветка `if data.startswith("addrform:")` в `_handle_callback`.
3. Toast «Поняла» / «Поняла, перехожу на «вы»».
4. Блок про форму обращения в `_HOUSEWIFE_FOOD_PROMPT`.
5. `_format_profile_for_prompt` — отображение `address_form`.
6. `pick_ack` split — пулы NEUTRAL / TY / VY.
7. Webhook ack — `pick_ack(address_form=...)`.

**Оценка.** ~2 часа (точно знаем что и где).

**Открытый UX-вопрос.** В каком моменте спрашивать форму:
- A. Сразу после получения имени (как было).
- B. LLM определяет сама по тону юзера.
- C. В Mini App settings (как timezone).

</details>

---

## 7. UX-доработки LLM (мелкие в backlog)

### 7.1. Адрес → ссылка на Яндекс Навигатор

**Проблема.** При выдаче адреса юзеру (например «Стоматолог Маши:
ул. Тверская 14») LLM выдаёт plaintext адрес. Юзеру неудобно —
надо копировать в навигатор отдельно.

**Решение.** Каждый раз когда LLM возвращает адрес как часть
ответа, сопровождать его ссылкой на Яндекс Навигатор:
```
ул. Тверская 14, Москва
🗺 https://yandex.ru/maps/?text=ул.+Тверская+14,+Москва
```

**Реализация.**
- Добавить раздел в `_HOUSEWIFE_FOOD_PROMPT` (`runtime/handlers.py`):
  «Адреса: при выдаче любого адреса в ответе — обязательно
  сопровождать ссылкой на yandex.ru/maps?text=<адрес>».
- Edge cases:
  - Адрес без улицы/города (только «магазин») — не делать ссылку.
  - Юзер сам прислал адрес — не дублировать ссылку (только если
    LLM выдаёт его обратно по запросу).
  - URL-encode русских символов автоматически — Telegram сделает
    сам, т.к. ссылка пойдёт как plaintext в сообщении.
- Тест: `tests/unit/test_address_yandex_link.py` — отправить LLM
  запрос «где у нас Маша к стоматологу», убедиться что в ответе
  есть `yandex.ru/maps`.

**Зависимости.** Нет. Прямая правка системного промпта + тест.

**Оценка.** ~30 минут (правка prompt + 1 тест).

### 7.2. Среда всегда отвечает в женском роде (бренд)

**Проблема.** Сейчас в `_HOUSEWIFE_FOOD_PROMPT` про Среду сказано
мягко: «Про себя (Среду) **можешь** использовать ж.р. («запомнила»,
«сохранила», «помогла») — это устоявшееся, бренд допускает.» —
формулировка опциональная, LLM иногда уходит в «нейтрал» («я могу
помочь», «я записал»).

**Решение.** Сделать правило **обязательным** во всех ответах:
- Самонарратив строго в ж.р.: «запомнила», «приняла», «нашла»,
  «составила», «отметила», «отправила». Никаких «помог», «нашёл»,
  «отметил» от лица Среды.
- Местоимения о себе: «я», «мне», «меня». «Я нашла рецепт»,
  «У меня есть для тебя…», «Я сама напишу».
- В ack-фразах в `services/ack_messages.py` — все формы уже ж.р.
  («Поняла», «Взяла в работу», «Записала») — оставить как есть,
  свериться что нет м.р. реликтов.

**Реализация.**
- В `_HOUSEWIFE_FOOD_PROMPT` (`runtime/handlers.py`) — заменить
  «можешь использовать ж.р.» на «**ВСЕГДА** используй ж.р. о себе.
  Среда — она. Никаких «помог», «нашёл», «составил» от лица бота.»
- Скан `services/ack_messages.py::_PHRASES` — убедиться что все
  18 фраз в ж.р. (visual review).
- Скан `services/onboarding.py::build_post_approve_message` и других
  user-facing текстов на м.р. реликты.
- Тест `tests/unit/test_anti_stalker_tone.py` уже проверяет тон;
  можно расширить — проверять что в финальных ответах LLM нет
  м.р. форм самонарратива (regex по «помог|нашёл|составил|отметил»).

**Зависимости.** Нет.

**Оценка.** ~30 минут (правка prompt + расширение test_anti_stalker
+ ручной скан ack/onboarding текстов).

---

## Приоритизация на утро

Первое: **п.1 (онбординг) + п.2 (aha моменты)** — быстрая работа с
понятным scope, закрываем беты хвосты.

Параллельно начинать собирать данные для **п.3 (сайт)** — выбор
стека, домена, юр.лица, платежей.

**п.4–6** — после ответов на открытые вопросы. Может уйти на 2-й день.

**п.7** — мелочёвка, делать в свободное окно после крупных задач.

---

## 11. MiMo credits пересмотр — ✅ DONE 2026-04-29

**Контекст (2026-04-28).** Пришла рассылка от Сяоми с новыми тарифами:

```
Preferential Rate
MiMo-V2.5-Pro: 1 Token = 2 Credits (2x rate)
MiMo-V2.5:     1 Token = 1 Credit (1x rate)
Both rates no longer vary by context window size.

20% Off-Peak Discount
Daily 09:00 AM – 5:00 PM PDT
```

**Что меняется:**
1. **Per-token credit cost** теперь фиксированный по модели:
   - V2.5-Pro: 2 credits/token (раньше зависело от контекста)
   - V2.5:     1 credit/token (раньше зависело от контекста)
2. **Off-peak скидка 20%** — окно 09:00–17:00 PDT
   (= 19:00–03:00 MSK / 16:00–00:00 UTC).
3. **Контекст-окно больше не множитель** — раньше большие контексты
   стоили больше credits/token.

**Что нужно сделать:**
- [ ] Найти где сейчас считается стоимость MiMo-вызова
  (вероятно `services/budget.py` или `services/credit_*`).
- [ ] Заменить per-context-window формулу на фиксированную ставку.
- [ ] Реализовать time-of-day детектор off-peak: если turn попал в
  09:00–17:00 PDT → multiply credits by 0.8.
- [ ] Обновить тесты `test_credit_formula.py`.
- [ ] Пересчитать unit-economics тарифов с новыми ставками
  (директива «55% gross margin» в project memory). Возможно цены
  тиров надо подвинуть.

**Acceptance:**
- Cost per turn рассчитывается по новой формуле.
- Off-peak detection работает (тест: insert turn at 10:00 PDT →
  cost × 0.8).
- 55% gross margin сохраняется на текущих тарифах (или цены
  изменены).

**Зависимости:** ничего не блокирует, но удобно сделать ДО п.3
(сайт sredaspace.ru — там нужны цены подписок).

**Оценка:** ~2 часа (найти формулу + переписать + тесты + перерасчёт
тарифов).

---

## 12. DeepSeek-V4 vs MiMo сравнение — ✅ DONE 2026-04-29

**Контекст.** MiMo тормозит (наблюдаем 130+ сек на больших промптах,
turn aborted на 120s outer timeout). Сегодня случай tg_634496616 —
voice → 131с латентность LLM. Тарифы Сяоми обновили в этот же день,
возможно ребалансируют upstream.

**Текущая стоимость MiMo-V2.5:**
- $0.08 / 1M токенов (из переписки с юзером)
- 1x rate (после изменений)
- 20% off-peak в 16:00–24:00 UTC

**Что сравнить с DeepSeek-V4-Flash на OpenRouter:**

1. **Цена:**
   - На openrouter.ai/models — посмотреть актуальный price per 1M
     prompt + 1M completion (могут отличаться)
   - Сравнить с MiMo $0.08/1M (если у MiMo тоже отдельные prompt/comp
     ставки — пересчитать)

2. **Latency:**
   - Прогнать одинаковый prompt (~20k tokens, ~400 completion) через
     оба провайдера N=20 раз. Замерить mean / p50 / p95.
   - Учесть geographic latency: MiMo (Сингапур) vs OpenRouter
     (зависит от endpoint региона)

3. **Качество (housewife use cases):**
   - Прогнать на наборе 10–20 реальных turns из логов:
     - голос «расписание клиросного пения ПН/ЧТ» → правильность
       расшифровки + RRULE генерации
     - текст «составь меню на неделю» → 21 cell корректно
     - «у меня есть полкурицы и картошка» → подходящий рецепт
     - tool-call accuracy: правильно ли вызывает tools
   - Subjective scoring 1–5 по каждому ответу

4. **Tool-calling совместимость:**
   - DeepSeek-V4 поддерживает structured output / function calling
     через OpenRouter? (проверить — некоторые модели OpenRouter не
     поддерживают tools)
   - Тестовый turn с `add_task` tool — выполнит?

5. **Context window:**
   - MiMo 256k+ (мы используем ~21k стабильно)
   - DeepSeek-V4-Flash — какое окно?

**Decision criteria:**
- Если DeepSeek дешевле + сравнимое качество + tool-calling работает →
  переключить housewife на DeepSeek (только этот скил, остальные
  на MiMo пока)
- Если дороже но быстрее на >2x → можно как fallback при MiMo timeout
- Если одинаково по всем параметрам → оставить MiMo

**Реализация (если решим переключать):**
- Запись в `LLMRouter`: новый provider option `deepseek_openrouter`
- Тарификация в `credit_formula.py` — добавить ставку для
  `deepseek-v4-flash`
- Smoke-тест на одном тестовом аккаунте перед массовым переключением

**Оценка:** ~2–3 часа (прогнать N=20 turns × 2 модели + субъективная
оценка + расчёт economics).

---

## 13. Чек-листы (дубли + move-to-checklist) — ✅ DONE 2026-04-29

Закрыто: `ChecklistService.create_list` теперь dedup'ит по
case-insensitive title (commit `cb8e4fb`). `add_checklist_items`
dedup внутри уже был. Move-task-to-checklist реализован
(commit `a5f431e`).

**Контекст (2026-04-28).** tg_634496616 попросил «перенеси «забрать
с дачи» из расписания в дела». Бот сделал `delete_task` +
`add_checklist_items` → итог: пункт оказался в чек-листе ДВАЖДЫ
(юзер потом отдельно сказал «удали дубль»). Похоже add_checklist_items
не делает dedup по title в том же списке.

**Что нужно:**
1. В `services/checklists.py::add_items` — проверять есть ли уже
   пункт с таким title (case-insensitive, whitespace-collapsed) в этом
   же списке. Если есть → пропускать (как `save_recipes_batch`
   делает с рецептами).
2. Возможно отдельный tool `move_task_to_checklist(task_id, list_id)`
   который инкапсулирует delete + add+dedup в одну операцию (LLM
   станет точнее не повторяя dedup-логику).

**Тесты:** add_items с дублем → возвращает существующий, не плодит.

**Оценка:** ~30 минут (мелкая правка + 2 теста).

---

## Сделанное (архив)

### ✅ DONE 2026-04-28

**152-ФЗ Часть 2 (compliance + encryption):**
- **Phase 1** Migration 0029 — зашифрованы 6 колонок (729 rows на проде):
  `outbox_messages.payload_json`, `inbound_messages.message_text_sanitized`,
  `tenants.name`, `tenant_user_profiles.display_name`,
  `inbound_events.payload_json`, `jobs.payload_json`. AES-256-GCM v2.
- **Phase 2** RetentionWorker — wired в job_runner с 24h throttle.
  Live проверен: удалил 131 устаревшую строку при первом прогоне.
- **Phase 3** Migration 0030 — таблица `audit_log` + реальная реализация
  `audit_event()`. Wired в `admin_tenant_approve` / `admin_tenant_reset`.
- **Phase 5** Migration 0031 — колонка `privacy_policy_accepted_at`
  (без UX impact, для будущего сайта).
- **Phase 7** Migration 0032 — `recipes.cooking_time_minutes` (single
  int, общее время от начала до подачи). LLM-tool обновлён.
- Backup snapshots: `pre-part2-20260428-1203.db` (5.75 MB),
  `pre-migration-0029-123624.db`, `pre-migrations-0030-0032-130034.db`
  в `/Users/boris/sreda-backup/`.
- ⏭️ Phase 4 (self-service delete) отложен — admin reset работает.

**Онбординг + рассылка:**
- Sanitizer `_extract_short_name` для display_name — защита от LLM-фраз
  типа «Пользователя зовут Борис.» в имени. 3 испорченных значения
  на проде вручную исправлены (Борис / Повелитель / Шеф).
- Webhook routing для `pb:<branch>` callbacks от approved юзеров
  (broadcast tour работает).
- Tracking welcome_v2_progress в `skill_params_json` + emoji-индикатор
  в `/admin/users` (🟡 in_progress, ✅ completed).
- Broadcast рассылка приветственной цепочки на всех 9 одобренных
  юзеров: Boris-сообщение + INTRO с кнопкой через 3 минуты, 8/8
  успешно за phase-pattern (broadcast не sequential).
- Отдельный `_DONE_BROADCAST` closing для approved юзеров (без
  упоминания «модератор одобрит» — у них доступ уже есть).

**Brand + UX:**
- **п.7.1** Адреса → ссылка на Яндекс Навигатор в LLM-prompt.
- **п.7.2** Среда ВСЕГДА в женском роде (правило усилено с примерами
  правильного/запрещённого, тесты в `test_anti_stalker_tone.py`).

**MiMo тарифы:**
- **п.11** Новые ставки Сяоми: V2.5-Pro 2x flat, V2.5 1x flat
  (убран 4x tier для контекста ≥256k). Off-peak 20% discount
  в окне 16:00–24:00 UTC (19:00–03:00 MSK). 19 unit-тестов.

### ✅ DONE 2026-04-25

- **Полный GTM plan** (`docs/gtm-plan.md`): ICP, УТП, каналы, мехника
  продаж, 90-дневный план.
- **Pending-bot (часть B плана v2)**: 6 веток scripted-engine +
  welcome + fallback с inline-кнопками. 4 раунда ревью копирайта.
- **Free-tier лимит** 20 LLM/день + отлуп с placeholder-ценами
  (`services/pricing.py`).
- **Inline-кнопки в чат-ответах** (`reply_with_buttons` tool +
  `btn_reply:` callback + `reply_button_cache` таблица).
- **Onboarding Aha-worker** — Aha-2 (диета → меню) в production.
- **Anti-stalker тон**: 7 фильтров + grep-тест на blacklist +
  gender-neutral regex + prompt-блоки.
- **UI labels константы** (`services/ui_labels.py`).
- **Документация копирайта**: `docs/copy/welcome.md` +
  `docs/copy/aha-moments.md`.
- **24+ текстовых правок** по результатам двух раундов ревью
  (Sonnet + Opus).
