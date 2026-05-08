# План на завтра (Сре́да)

Живой документ. Задачи добавляем по ходу, завершённые помечаем `✅ DONE
YYYY-MM-DD` и **переносим в weekly-архив** `docs/done/2026-WNN.md`
(ISO week, понедельник-воскресенье). В этом файле остаются только
pending задачи. В начало ставим самое приоритетное.

Архив завершённых задач: [docs/done/index.md](done/index.md).
Workflow и конвенция хранения — `docs/done/README.md`.

**Последнее обновление:** 2026-05-07 (вечер).
**Что задеплоено сегодня (2026-05-07):** перенесено в
`docs/done/2026-W19.md` — Free-tier subscription Phase 1+2 shipped
(grandfather 15 tenants, sreda_free quota, EntitlementGate, voice
gates, admin suspend/unsuspend). Codex retroactive review: 1
CRITICAL + 6 MAJOR + 1 MINOR применены и развёрнуты (commit
`0f3df1d`). Прод-инцидент с safe_restart.sh (long-poll vs webhook
конфликт) восстановлен и зафиксен. Записаны два новых ПРАВИЛА #3
и #4 в memory.

---

## План на 2026-05-08 (следующий рабочий день)

### Pending — Поднять стейджевый контур (приоритет высокий)

Триггер: 2026-05-07 после Codex retroactive review Phase 2 нашлось
1 CRITICAL + 6 MAJOR в задеплоенном коде. CRITICAL (suspend
фактически не блокировал) висел на проде несколько часов. Без
staging-контура каждое нетривиальное изменение тестируется на
живых юзерах — недопустимо когда юзеров уже больше пары.

**Минимально-жизнеспособный staging:**

- Отдельный VDS (или namespace на текущем) с своим
  `bot.staging.sredaspace.ru` (TG+MAX) и отдельными бот-токенами.
- Своя PostgreSQL DB (snapshot `pg_dump` с прод-схемой; данные
  либо anonymized, либо тестовый seed с 5-10 юзерами).
- Конфиг через `/etc/sreda/.env.staging` — отдельный `SREDA_DATABASE_URL`,
  `SREDA_TELEGRAM_BOT_TOKEN`, `SREDA_MAX_BOT_TOKEN`, прочие secrets.
- Deploy-pipeline: `git push staging` → `ssh staging 'safe_restart.sh'` —
  то же что прод, но другой хост.
- Smoke-чеклист, который надо прогонять на staging ПЕРЕД prod-деплоем
  (новый юзер, голос, suspend/unsuspend, free-tier limits).
- Мониторинг свой (cheap) — отдельный telegram_api_health и т.п.

**Acceptance:**
1. Я пишу боту в @sreda_staging_bot и получаю онбординг.
2. Codex/Xiaomi-найденные CRITICAL/MAJOR можно reproduce на staging
   ДО prod-деплоя.
3. `safe_restart.sh` на staging работает идентично проду.
4. Boris отдельный SSH-доступ к staging-юзеру.

**Не в scope MVP:** load testing, blue/green, DB replication.
Это статический контур для smoke + replay багов.

**Когда:** после фиксов Phase 2 (текущий релиз) — отдельной задачей.
Возможно W20.

### Pending — Lazy-provision orphan tenant при первом MAX → text message

Сейчас если юзер в MAX пишет НЕ `/start lnk_X` (любой текст до
ссылки) — `ensure_max_user_bundle` создаёт orphan `tenant_max_<id>`,
который потом блокирует channel-link с error
«account_already_registered_separately». Решение: либо мигрировать
orphan в TG-tenant при consume_link (destructive merge — out of MVP
scope), либо удалить orphan-tenant в момент consume_link вместо
блокировки. **Решение Boris:** скорее всего «удалить orphan» —
MVP-уровень. Полный destructive merge — после weekly Claude reset.

### Pending — Subscription follow-up (хвосты после Phase 2)

Phase 1+2 free-tier subscription и Codex review fixes shipped
2026-05-07 (см. W19). Остались follow-up задачи:

**1. pending_bot tour: 11 → 4 шага** (низкий риск, изначальный
scope Phase 2). План: `_BRANCHES` сократить до
`intro → voice → memory → done`. Прод сейчас на старом 11-step
tour. Acceptance: новый юзер проходит tour за 4 тапа, конец ставит
`onboarding_tour_completed=True`.

**2. BudgetService `is_subscribed` regression** (investigate).
5 unit-тестов в `tests/unit/test_budget_service.py` фейлятся на
HEAD: `get_quota_status()` возвращает `is_subscribed=False` при
active sub. Phase 2 metering работает мимо BudgetService через
UsageLedger напрямую — не блокировало shipping, но нужно перед
premium-tier sprint. Возможно clock-dependent test fixtures (нужно
заглянуть на active_until даты).

**3. Live smoke-чеклист Phase 2 на staging** (когда подниму
staging-контур, см. ниже). Изначальный план Phase 2 имел 9
сценариев: новый юзер сегодня, signup-rate-limit на 4-й попытке,
LLM exhaustion 21-й turn, voice 5-min/day cap, voice STT failure
refund, suspend → unsuspend, web_search блокирован для free,
admin/approve → 410, и т.д. Не прогоняли — слишком рисково на
живых юзерах.

**4. Deferred Codex/Xiaomi findings** (несрочные):
- **MAJOR-5** Gate проверка `active_until` — для unlimited subs
  не критично; станет важно когда добавятся trial subs.
- **Xiaomi m1**: shared quota-gate helper между TG voice и MAX
  voice (DRY refactor — сейчас дублирование).
- **Xiaomi m2**: EntitlementGate result cache per turn (perf —
  сейчас 3 одинаковых query за turn).
- **MINOR**: отдельный `SREDA_SIGNUP_HASH_KEY` env (currently
  reuse `tg_account_salt` — cryptographic isolation).

**5. Premium tier** (out of scope изначально). Когда будем делать:
цены (≥55% gross margin per project memory), top-up vs upgrade
семантика на exhaustion, интеграция с pricing-страницей сайта,
ЮKassa wiring, миграция grandfathered → paid (вопрос: остаётся
ли `grandfathered_at` flag после оплаты или переходят на paid
tier с metering).

### Pending — Reminder idempotency + weather UX (наблюдения 2026-05-08)

**1. Дубль reminder при повторном context'е** (MEDIUM).

Прецедент 2026-05-08 12:09 MSK (tenant_max_40921122). Boris дважды
переслал автоматическое сообщение про запись в Колесо.ру (Зеленоград,
13:10). Между первым и вторым он голосом скорректировал время
напоминания. Среда:
1. Создала reminder #1 на 13:00 (за 10 мин до 13:10).
2. На голос «исправь, ехать долго» — `update_reminder` или
   `cancel + new` → reminder с trigger 12:20.
3. На повторное paste'нутое сообщение про запись (09:09 UTC) —
   создала **второй** reminder с тем же trigger 12:20, не проверив
   существующий.
4. **12:20 fired ОБА** → юзер получил дубль уведомления.

**Root cause:** LLM не вызывает `list_reminders` перед
`schedule_reminder` чтобы проверить semantically-similar duplicate
(close-in-time + similar title).

**Fix candidates:**
- App-level: pre-check в `schedule_reminder` — если найден
  active reminder в окне ±15 мин с похожим title (cosine similarity
  на embedding или fuzzy matching на keywords) → return existing
  reminder ID + ack как «обновила существующее».
- Prompt-level: добавить strict rule «before `schedule_reminder`,
  ALWAYS call `list_reminders` first; if any active reminder
  matches the new event by ±15 min trigger window AND similar
  title — call `update_reminder` or skip, do NOT create duplicate».

**Verification:** repro test — fake conversation с двумя
повторяющимися paste'ами + assert ровно один reminder создан.

---

**2. Weather: hourly forecast — 78s web research вместо 1-2s tool call** (MEDIUM, severity bumped).

Прецедент 2026-05-08 12:18-12:35 MSK + повторный 12:34
(`trace_2e248c2277d94e47`, **78510ms** total, 7 LLM iters).
Дамп через `scripts/debug_trace_full.py run_bd4101ad6edb42028903b997`:

- Запрос: «Почему у тебя нет почасового прогноза или хотя бы более
  подробного, не почасового, например, день, утро, вечер? Поищи
  в своих пузах [STT-искажение «источниках»/«базах»]».
- Среда **проигнорировала собственный `get_weather`** и сделала 6
  последовательных `fetch_url` к wttr.in за 35s + 18s+18s LLM
  synthesis = **78s wall-time**.
- В финальном ответе синтезировала почасовой прогноз с разбивкой
  «Ночь / Утро / День / Вечер».

**Root cause (architectural):** `services/weather_tool.py` использует
Open-Meteo **только в daily режиме** (`forecast?daily=...`).
Open-Meteo бесплатно поддерживает hourly endpoint
(`&hourly=temperature_2m,precipitation,weathercode,windspeed_10m`).
Среда **не знает** что hourly доступно, поэтому уходит в web research
через `fetch_url` к wttr.in.

**Fix (рекомендованный, ~1 день):** расширить `get_weather`:

```python
def get_weather(
    location: str,
    day_offset: int = 0,
    days_count: int = 1,
    granularity: Literal["daily", "part_of_day", "hourly"] = "daily",
):
    """...
    granularity:
      - daily: dawn-to-dusk summary (по умолчанию для общих вопросов)
      - part_of_day: «утром +5°, днём +12°, вечером +8°»
        (для запросов «как одеться сегодня»)
      - hourly: разбивка по часам (для запросов
        «во сколько пойдёт дождь?»)
    """
```

Internally: один HTTP к Open-Meteo с `&hourly=...` параметрами,
groupby по part_of_day (00-06, 06-12, 12-18, 18-24) или
непосредственно hourly array.

**Что это даёт:**
- Latency: 78s → ~1.5s (один tool call).
- Tokens: 4× iter с 27K→46K context input → 1 iter с 27K. Экономия
  ~120K токенов на одном слабом запросе.
- UX: Среда сразу отвечает структурно, не путает юзера фразой
  «не могу — но могу через web research».

**Кросс-польза с Phase A+B:** Phase A+B оптимизируют
parallel-fetch для случаев когда web research **реально** нужен
(новости, рецепты, специфические магазины). Этот фикс убирает
weather из этого списка совсем.

**Verification:** unit test с mocked Open-Meteo response для
hourly endpoint + integration test через fake LLM (assert
get_weather вызван с granularity="hourly" а не fetch_url).

**Когда:** после стабилизации Phase A+B observations (через 1-2
дня). Слегка приоритетнее чем reminder dedup потому что reminder
дубль происходит раз в неделю, weather lag — на каждом hourly
запросе.

---

**3. Двойная отправка тех же reminder'ов через outbox?**

Дополнительная гипотеза для Issue #1: возможно проблема не в
двух reminder rows, а в outbox emit одного reminder дважды
(retry race в outbox worker). Пока не проверено — артефакты
прода (family_reminders 2 row) подтверждают что всё-таки два
distinct reminder. Тем не менее — для будущей debug'а имеет
смысл прогнать outbox dedup audit.

### Pending — Latency: follow-up оптимизации после Phase A+B (наблюдения 2026-05-08)

Phase A (prompt batching) + Phase B (parallel dispatch для
allowlisted I/O tools) задеплоены 2026-05-08 (`b06fae3`, `cd687ff`).
Verified в проде на `trace_4cf5a9567d3a4c50` — 53s вместо ~110s
без оптимизации. Ниже — что осталось на столе.

**1. web_search parallel-safe** (MEDIUM, ~1 день).

В trace `trace_4cf5a9567d3a4c50` iter.0 эмитнул `tools=[web_search × 5]`,
но они исполнились **последовательно** (~10s) потому что web_search
исключён из `_PARALLEL_SAFE_TOOLS`: пишет в `WebSearchUsageCounter`
через shared SQLAlchemy session. Если 5 параллельных
`counter.record_tavily()` сделать thread-safe — экономия 8s на
research-heavy turns.

**Подходы:**
- **A.** Каждый поток получает свою session через factory →
  `record_tavily` коммитит независимо. Простой fix, но ×5 connections
  на одной iter — пресс на pool.
- **B.** In-memory counter aggregation внутри turn → один commit
  в конце через main session. Меньше connections, но нужна
  consistency на abort.
- **C.** Использовать atomic UPDATE с RETURNING — один SQL
  per record_tavily, но на shared session всё равно нужна lock'ом
  обернуть.

**Acceptance:** unit test где 5 web_search запускаются конкурентно
через `_dispatch_tool_calls_batch`, assert quota инкрементируется
ровно 5 раз.

**2. LLM streaming** (BIG UX win, ~2-3 дня).

iter.2 в `trace_4cf5a9567d3a4c50` — synthesis 21695ms на 1286
output tokens (~60 tok/sec). Сейчас ответ генерится атомарно:
юзер ждёт 53s полностью молча после ack'а. С streaming:
- Первые токены через ~3s.
- Текст подгружается в outbox по мере генерации.
- Telegram editMessageText обновляет сообщение через 1-2s паузы
  (TG rate limit на edit'ы).

**Архитектурная боль:** наш текущий path `llm.invoke(...)` →
полный response. Нужно switching на `llm.stream(...)`. Это
затрагивает:
- handlers.py:execute_conversation_chat — collecting chunks vs
  one shot.
- outbox emit — заранее create+edit, или делать chunked appends.
- Anti-hallucination scrubber — сейчас работает на финальном
  тексте, со streaming нужно либо post-stream scrub либо
  per-chunk (хуже).
- Phase B на write turns (когда придёт) — template вместо LLM
  text, со streaming не сочетается.

**Решение приоритетов:** только для read-only turns (где
`called_tools` не содержит write tools). Write turns — атомарный
template (anti-hallucination guarantee). Read turns — streaming
для UX.

**Acceptance:** 53s turn → юзер видит первый токен через ≤3s,
финальный edit через 53s. Plus integration тест с fake LLM
streaming chunks.

**3. weather get_weather hourly granularity** ✅ **DONE 2026-05-08**
(commit `8706da0`). 6 раундов Codex review до consensus. Phase 0
API probes verified. 37 тестов pass. Backward-compatible
расширение с `granularity={daily, part_of_day, hourly}`. Live
smoke на проде подтвердил: LLM вызывает `get_weather(...,
granularity="hourly")` на запрос «во сколько дождь» (system
prompt update эффективен), tool отвечает за 1-2s.

**4. weather: HTTP retry on transient Open-Meteo errors** (LOW,
recorded 2026-05-08).

Прецедент 2026-05-08 15:00:07 после deploy weather hourly: первый
тест от Boris — Open-Meteo вернул HTTP 502 Bad Gateway (transient,
тот же URL через 30s = HTTP 200). Среда сказала «сервис не
отвечает», fallback'нулась на fetch_url(wttr.in). Юзер видит
суррогатный ответ.

**Что фиксим:** в `_fetch_forecast` (и `_geocode`) обернуть
`httpx.get` в retry loop:
- 2 retry'я максимум
- Exponential backoff: 0.5s, 1.5s
- Retry только на 5xx (transient) и timeouts, НЕ на 4xx (наша
  ошибка params)
- Total worst-case wall-time: 8s + 0.5s + 8s + 1.5s + 8s = ~26s,
  но на самом деле успешный second call займёт +1-2s.

**Где:** `services/weather_tool.py`. Можно использовать
`tenacity` (уже в проекте? — проверить) или ручной try/except loop.

**Размер:** ~15-25 LOC + 2 unit теста (502→retry→200, 4xx→no retry).

**Когда:** при следующем weather sprint'е. Не блокер — fallback
через web_search/fetch_url работает.

**Plan-mistakes followup:** в Out of scope изначального плана было
«HTTP retry на timeout (Open-Meteo надёжный)». Это assumption
оказался слишком оптимистичным — записать в
`~/.claude/plan-mistakes/python.md` про «не считать external API
надёжным даже если документация говорит «99.9% uptime»».

---

### Pending — Ревизия legacy backlog

Список ниже («План на 2026-04-30», «0. Hot-fix'ы», секции 2-7)
требует переоценки приоритетов на свежую голову. Многие пункты
могут быть уже закрыты предыдущими коммитами но не помечены —
пройтись грепом, помечать ✅ DONE и переносить в W19/W18 архивы.

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

#### 9.3. Заменить транспорт RU↔EU (один из двух вариантов)

Радикальный архитектурный фикс outbound-транспорта. Сейчас:
RU VDS → `ssh -D 1080` → EU egress (89.110.77.78) → TG.
SSH dynamic-forward = TCP-over-TCP, head-of-line blocking возможен:
один retransmit на одном channel'е тормозит остальные.

**Решение делать если 9.1 (логи) покажет ack физически создан позже
final** — это будет означать что race на стороне транспорта (а не
client-side ordering как мы изначально подозревали). 9.2 (placeholder)
снят 2026-05-04 — больше не зависимость для 9.3.

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

### 11. recall_memory proactive policy — staged roadmap

**Контекст.** 30 апреля 2026 ~19:42 МСК юзер tg=755682022 написала
«покажи все ткани с характеристиками ширина и усадка». Бот ответил
«пока только одна ткань: Лён хлопок пудра». В реальности в
`assistant_memories` уже было 5+ записей про другие ткани (Тенсель
шампань, Страйп шампань, Страйп лайм, Индийская сирень, Пепельная
сирень тенцель), созданных 22-25 апреля и активно использовавшихся.

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

#### 12.2. Динамические напоминания (real product gap) — ⏸️ DEFERRED после п.10 (MAX integration), 2026-05-04

Boris: «откладываем после Макса». Логика — MAX-канал это критичная
infra-задача (long-poll bot for второго мессенджера, sprint 2-3 дня),
динамические reminder'ы — feature для одного канала. Лучше выкатить
MAX, а потом расширить reminder'ы.

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

#### 12.5. Дубликат task при follow-up reminder (Boris incident 2026-05-05) 🟡

**Симптом.** Boris попросил голосом «замена колёс пятница 13:10
Зеленоград 514 строение 1». Бот ответил «Записала! ... Нужно ли
напоминание?». На голосовое «да напомни утром» бот создал **вторую
задачу** с reminder вместо того чтобы привязать reminder к первой.

В UI видны 2 разных entry на пятницу 8 мая 13:10:
- «Замена колёс в сервисном центре» (без reminder, из turn 1)
- «Замена колёс — Зеленоград 514, строение 1» (с reminder в 9:00,
  из turn 2)

**Корневая причина.** LLM не имеет тула привязать reminder к
существующей task. На follow-up «да напомни» модель пошла по «лёгкому»
пути — пересоздала задачу с reminder в одном tool-call'е (single-turn
create+add). Title в turn 2 чуть отличается потому что был voice
transcript, не точная копия.

**Известный паттерн.** Тот же failure-mode что в:
- 0.7 «Чек-листы: дубликат при single-turn create+add»
- 12.3 «update_reminder tool отсутствует»

Все три — одна корневая дыра в tool-set'е: **нет find_X + add_Y_to_X
тулов**, бот вынужден create_X_with_Y которое в follow-up turn'е
дублирует.

**Fix (минимум):**
- `find_task_by_text(query, tenant_id) → task_id | None` —
  semantic search по recent tasks
- `add_reminder_to_task(task_id, when, advance_minutes)` —
  привязка reminder к существующей task без re-create
- `update_task_metadata(task_id, address|notes|title)` —
  если из голоса пришли уточнения

Plus prompt-level addendum: «when user says "да напомни" or similar
follow-up to a confirmation question — DO NOT create a new task,
look up the just-mentioned one and add reminder to it».

**Acceptance.** Multi-turn flow:
1. «замена колёс пятница 13:10 …» → 1 task created
2. «да напомни утром» → reminder added to existing task, **0 new tasks**
3. БД: ровно 1 row в `tasks` с linked_reminder_id

**Когда делать:** P2. После того как несколько юзеров репортят
дубли — будет видна частота. Pre-existing prod-state у Boris'а:
2 задачи 8 мая, не чистим (Boris решил не трогать БД).

**Файлы:**
- `src/sreda/runtime/handlers.py` — добавить find_task / add_reminder
  тулы
- `src/sreda/runtime/tools/...` — новый tool registration
- `tests/unit/test_follow_up_reminder.py` — regression scenario
  (с conversation graph mocked LLM)

#### 12.6. Misleading log «chat: fallback LLM built provider=X» 🟢

**Симптом.** При обычном invoke'е LLM в логах появляется
`chat: fallback LLM built provider=openrouter-grok ...`. Звучит как
«мы используем fallback (т.е. primary упал)», но на самом деле это
момент **сборки backup-instance**, не вызова. Реальный invoke идёт
через primary (MiMo) если не было LLMCallTimeout. Меня (Boris)
запутало 2026-05-05 при разборе MAX deploy — подумал что грок
реально активирован.

**Файл.** `src/sreda/runtime/handlers.py:1883-1886`

**Fix:** переформулировать log → `chat: fallback LLM ready as backup
provider=...` или передвинуть лог внутрь fallback-branch invoke loop'а
(только на реальное переключение). Не делает ничего вредного, просто
шум в диагностике.

**Когда:** P3. Косметика логов, делать когда мешает.

#### 12.7. LLM provider content-filter leaks English to user (incident tg=634496616) 🔴

**Симптом.** 2026-05-03 20:45 МСК юзер `tg=634496616` прислал voice
«Напомни завтра в 12 часов написать томатологу-хирургу» (очепятка
вместо «стоматологу»). MiMo (xiaomi mimo-v2.5-pro) забанил запрос
через content-filter и вернул pre-canned text:

> «The request was rejected because it was considered high risk»

Среда отправила это юзеру дословно — английским, без перевода. Юзер
видит непонятный reject от китайского cloud'а вместо своей Среды.
Через минуту юзер переспросил, тот же домен — MiMo пропустил, бот
поставил напоминание корректно. Filter **недетерминирован** (Boris
проверил: «поставь напоминание написать хирургу» сейчас работает,
но та же фраза в другом surface — не работала).

**Trace markers:** `in_tok=0/out_tok=0` от LLM в trace event (token
count = 0 — характерный признак provider safety refusal, не настоящий
LLM-ответ). Также `chars_out=60` при `text starts with "The request..."`.

**Fix (MVP):**

```python
_PROVIDER_REFUSAL_PATTERNS = (
    "the request was rejected because it was considered high risk",
    "i cannot fulfill this request",
    "i'm sorry, but i can't",
    # добавлять по мере появления других providers
)

def _is_provider_refusal(text: str) -> bool:
    if not text:
        return False
    return any(p in text.lower().strip() for p in _PROVIDER_REFUSAL_PATTERNS)

def _is_predominantly_non_russian(text: str, threshold: float = 0.3) -> bool:
    """Cyrillic ratio < threshold (default 30%) → не наш язык."""
    if not text or len(text) < 20:
        return False
    cyrillic = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    return (cyrillic / len(text)) < threshold

# В точке consume LLM response:
if _is_provider_refusal(reply) or _is_predominantly_non_russian(reply):
    logger.warning("LLM refusal/non-Russian — substituting (provider=%s tenant=%s)", ...)
    trace.record("llm.refusal_substituted", original_chars=len(reply))
    reply = (
        "Прости, не получилось понять запрос. "
        "Попробуй переформулировать или спросить иначе."
    )
```

**Файлы:** `src/sreda/runtime/handlers.py` — после получения LLM-reply,
перед persist в outbox.

**Acceptance:**
- Юзер на проде шлёт сообщение которое триггерит MiMo filter →
  получает русский fallback вместо английского reject
- Trace event `llm.refusal_substituted` появляется в diagnostic logs
- TG path не сломан (regression: ASCII-only ack-message не triggers
  the substitution — `len < 20` guard)

**Long-term следствие:** смена провайдера на менее агрессивный
(YandexGPT-Lite / GigaChat / Claude direct) уберёт root cause. Но это
отдельный sprint, не сейчас.

**Когда делать:** P1. Visible UX bug — юзер видит непонятный английский
reject вместо ответа Среды. Малая площадь (~30 мин код + 1 unit test +
deploy). После MAX voice (10.4) и merge accounts.

#### 12.8. Proactive notifications не route'ятся по каналам 🟡

**Симптом.** После merge accounts (10.1+10.3) юзер имеет
`telegram_account_id` + `max_account_id` в одной row, но все
proactive notifications (напоминания, EDS-alerts, onboarding aha,
checklist nudges) hardcoded `channel_type="telegram"` в 4 worker'ах:

- `housewife_reminder_worker.py:154`
- `housewife_onboarding_worker.py:158`
- `onboarding_aha_worker.py:189`
- `proactive_events.py:251`

`tenants.preferred_channel` колонка существует (schema 0035), но в
коде producer'ов не читается.

**Что нужно (минимум):**

```python
# Helper в shared module
def resolve_outbox_channel(tenant: Tenant, user: User) -> str:
    """Choose where to deliver proactive notification.

    Priority: tenant.preferred_channel → first non-null account → 'telegram'.
    """
    if tenant.preferred_channel in ("telegram", "max"):
        return tenant.preferred_channel
    if user.telegram_account_id:
        return "telegram"
    if user.max_account_id:
        return "max"
    return "telegram"  # legacy default
```

Producers читают этот helper при enqueue outbox.

**Опции UX:**
- (a) preferred_channel один — юзер выбирает в settings → notifications туда
- (b) duplicate — слать в **оба** канала (если оба acc'а есть). Красиво
  но 2x cost (LLM + delivery), risk дублирующих нотиф если юзер видит оба
- (c) primary/fallback — основной + резерв если primary fail'ит (`OutboxDeliveryWorker`
  уже умеет fallback chain)

**Acceptance:** юзер с `preferred_channel='max'` получает напоминание
в МАКС, а не TG. Юзер с обоими аккаунтами без явного preference — TG
(legacy default).

**Файлы:**
- `src/sreda/workers/{housewife_reminder,housewife_onboarding,onboarding_aha,proactive_events}.py`
- `src/sreda/services/channel_routing.py` (новый, helper)
- `src/sreda/runtime/dispatcher.py` (alignment с outbox_channel)
- Tests: `test_channel_routing.py` + 4 worker regression tests

**Когда делать:** P2. После того как juзеры начнут реально использовать
МАКС (нужен 10.2 mini-app linking чтобы flow был доступен) — пока
только Boris и его test acc'ы.

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
