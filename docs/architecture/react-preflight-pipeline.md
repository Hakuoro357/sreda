# Preflight-конвейер ReAct (интент + домены)

**Owner:** эпик #191 — harness-обвязка ReAct; интент — #197, домены — #215/#221
**Source code:** `src/sreda/runtime/react_preflight.py` (классификаторы и роутер — отслеживается на актуальность)
**Related code:** `src/sreda/runtime/react_loop.py` (узел `chat`, `_build_graph` — потребление; меняется по многим причинам, freshness НЕ трекаем)
**Tests:** `tests/unit/` — калибровка `_must_task` (`test_must_task_high_precision`), классификаторы интента/доменов, политика `compute_allowed_domains`
**Status:** задеплоено. Флаг `SREDA_REACT_PREFLIGHT_ENABLED` ВКЛ на проде (2026-06-24); доменный роутер #221 — execute глобально
**Verified-against:** `75f9ab8` (сверено с кодом 2026-06-27)
**Флаги:** `SREDA_REACT_PREFLIGHT_ENABLED` (default `False`); `SREDA_REACT_PREFLIGHT_CHAT_PROVIDER` (default `openrouter-deepseek`; на проде по #224 переведён на `gemini-2.5-flash-lite` ради скорости)

## Зачем это существует

Раньше КАЖДОЕ сообщение шло в полный ReAct-цикл со ВСЕМИ инструментами на ОДНОЙ модели (Фредди). Две беды (#197):

1. На болтовне/викторине модель хватала неподходящие productivity-инструменты и зацикливала поиск → упор в предел проходов → «не получилось довести до конца».
2. Фредди (быстрая диффузионная) слаба на знаниях/рассуждении → выдумывала факты в разговоре.

Идея: **ДО цикла понять намерение** и дать под него (а) нужную МОДЕЛЬ, (б) нужные инструменты. Задаче — быстрый Фредди; разговору/фактам — рассуждающая модель с веб-поиском.

Рефрейм (план #221 v5): **интент `task/chat/fact` — авторитет (#197); домены — надстройка ТОЛЬКО на task-пути** (какие семьи инструментов привязать + можно ли писать). Домен сам по себе НЕ может сделать ход task'ом.

## Обзор конвейера

```mermaid
flowchart TD
    IN["Сообщение + контекст<br/>(≤4 реплики, prev_intent)"]
    IN --> MT{"_must_task<br/>детерм. паттерны"}
    MT -->|матч| TASK
    MT -->|нет матча| CI["classify_intent · Фредди (LLM)<br/>→ task / chat / fact<br/>сбой → task"]
    CI -->|chat / fact| CF["Рассуждающая модель<br/>web-only · 1 поиск · honesty"]
    CI -->|task| TASK["task-путь:<br/>Фредди + полный набор"]
    TASK --> RD["route_domains (детерм.)<br/>текст → раздел, семьи, cross"]
    RD -->|раздел найден| CA
    RD -->|не определён| CD["classify_domains · Фредди (фолбэк)<br/>→ раздел + confidence"]
    CD --> CA["compute_allowed_domains<br/>→ allowed_read / allowed_write<br/>запись только по явной команде"]
    CA --> G["граф ReAct:<br/>привязка разрешённых семей + гейт записи"]
```

При `SREDA_REACT_PREFLIGHT_ENABLED=False` весь трафик идёт как раньше (task + полный набор), byte-identical — `effective_intent` читается из state ТОЛЬКО при включённом флаге.

## Слой 1 — интент (#197): намерение → модель

Цель: определить намерение и выбрать модель + класс инструментов на этот ход.

### `_must_task(text, prev_intent=None) -> bool` (слой 0, детерминированный)

- **Вход:** нормализованный текст (lower, дефисы убраны).
- **Логика:** подстрочный матч по узкому high-precision списку `_MUST_TASK_PATTERNS` (явные productivity-команды / обращения к своим данным: `напомни`, `поставь задач`, `мои задачи`, `список покупок`, `запомни`, `что у меня`, …). Намеренно узко: false-positive = чат зря уйдёт в task и потеряет рассуждающую модель; false-negative безопасен (доедет до LLM-классификатора). `prev_intent` НЕ используется (оставлен для стабильности сигнатуры).
- **Выход:** `True` → сразу `task` (LLM-классификатор не зовётся); `False` → решает слой 1.

### `classify_intent(recent_messages, user_text, prev_intent, freddie_llm, timeout=4.0, raw_sink=None) -> Intent` (слой 1, LLM на Фредди)

- **Вход:** последние ~4 реплики (роль: текст, обрезка 300 симв., `_format_recent`); текущее сообщение; `prev_intent` — **мягкой подсказкой** («Прошлый интент: X. Интент МОЖЕТ смениться — если явно про задачу, ставь task»). Системный промпт `_CLASSIFIER_SYSTEM` требует РОВНО ОДНО слово.
- **Выход:** `Intent = Literal["task", "chat", "fact"]`.
  - `task` — сделать что-то / свои данные (напоминания, задачи, списки, покупки, меню, рецепты, семья, заметки);
  - `fact` — вопрос на общее (публичное) знание о мире;
  - `chat` — болтовня, игра, викторина, смолток, мнение, приветствие.
- **Парс (`_parse_intent`):** первое слово lower ∈ {task,chat,fact}; иначе **fail-open → `task`**.
- **Fail-open:** любой сбой/таймаут/мусор → `task` (лучше быстрый ассистент с инструментами, чем сломать задачу).
- **Наблюдаемость:** `raw_sink` (опц.) собирает сырой ответ модели → трейс `classifier_raw` (#192).

### Роутинг интента → модель + инструменты (узел `chat` в `react_loop`)

Граф строится ОДИН раз с обеими моделями; узел выбирает по `effective_intent` из state.

- **`chat` / `fact`** → рассуждающая модель (`SREDA_REACT_PREFLIGHT_CHAT_PROVIDER`) + **только web-семья** `_WEB_ONLY_TOOL_NAMES = {web_search, fetch_url, get_weather}` + промпт `chat_fact_system_prompt` (honesty, анти-флейл, без productivity-инструментов). Инвариант: web-only scope зашит ДО `try`; при сбое рассуждающей модели → fallback на Фредди с **тем же web-only**, НЕ на task.
- **`task`** (или флаг OFF) → Фредди (быстрый) + полный набор инструментов.

Лимит «1 поиск на ход» в chat/fact обязателен — он делает флейл-петлю поиска невозможной (исходный инцидент 2026-06-23 был циклом поиска ×5+).

## Слой 2 — домены (#215/#221): раздел → инструменты + право записи

Работает ТОЛЬКО на task-пути. Сужает инструменты по разделу и гейтит запись.

### `route_domains(text) -> RouteResult` (детерминированный, чистая функция)

- **Вход:** текст сообщения.
- **Логика:** токены → разделы по онтологии (`_ontology`, слияние `FAMILY_ROOTS` + `_SEC_*` #215). Домены: `reminders, tasks, checklists, shopping, menu, recipes, household, memory, web`. Longest-match фразы («список покупок» → shopping, «список дел» → checklists); action-домены (глагол: напомни/запомни) приоритетнее content; составное (compound) — только при союзе МЕЖДУ клаузами; направленное кросс-намерение «X из Y» (единственное — «покупки **из** меню»).
- **Выход — `RouteResult`:** `primary_domain`, `secondary_domains`, `suppressed_domains`, `compound_by_connector`, `intent_hint` (`"task"` только от task-сигнала #197/#215, иначе `None`), `intent_only`, `active_families` (ленивые семьи на предзагрузку), `directive` (подсказка-промпт раздела), `all_domains`, `cross_intent`.

### `classify_domains(recent_messages, user_text, freddie_llm, timeout=4.0, raw_sink=None) -> DomainClassResult` (LLM-фолбэк на Фредди)

Зовётся ТОЛЬКО когда intent=`task` И `route_domains` не дал детерминированного домена.

- **Вход:** последние реплики + текущее сообщение; системный промпт `_DOMAIN_CLASSIFIER_SYSTEM` (вернуть одно слово-раздел).
- **Выход — `DomainClassResult`:** `domains` (кортеж разделов; пусто = не определил) + `confidence` (`"high"` при РОВНО одном домене / `"low"` при ноль/несколько/сбой). Сбой/таймаут → пусто+`low` (fail-open).

### `compute_allowed_domains(route, classified) -> (frozenset allowed_read, frozenset allowed_write)`

Политика «**запись никогда по догадке**»:

- кросс «X из Y» (`cross_intent`) → read оба домена, write — целевой;
- детерминированный ОДИН домен → read + write (явная команда);
- детерминированное СОСТАВНОЕ (≥2, союз) → read оба, **write ∅** (compound-запись → уточнение, не авто-запись);
- LLM-фолбэк `high` (ровно один) → **только read** (запись не по догадке модели);
- иначе (low/мусор/нет домена) → **∅/∅ = явный deny** (только `ask_human`).

Пустой `frozenset` = ЯВНЫЙ запрет; `None` НЕ возвращается (зарезервирован для OFF/legacy в графе).

### Применение в графе

Результат кладётся в state (`router_allowed_read_domains`, `router_allowed_write_domains`, `active_families`); `_apply_domain_policy(...)` фильтрует привязанные инструменты по разрешённым разделам. При выключенном роутере (`allowed = None`) — no-op, byte-identical.

## Прод-статус и откат

- `SREDA_REACT_PREFLIGHT_ENABLED` — ВКЛ на проде (2026-06-24).
- Доменный роутер #221 — execute-режим глобально (`SCOPE_MODE=execute`, `EXECUTE_TENANTS=*`). Откат: `shadow` + `safe_restart`.
- Детерминированный гейт времени `_text_mentions_time` (#180) сохранён — preflight его дополняет, не заменяет; на разрушающих/приватных путях сбой → уточнение/fail-closed, не no-op.

## Связанное

- Эпик: #191 (harness-обвязка ReAct).
- Интент: #197. Домены: #215 (карта «слово→раздел»), #221 (ontology-роутер + write-gate).
- Выбор рассуждающей модели: #173 (eval — честность + скорость), #224 (chat/fact → gemini-2.5-flash-lite).
- Наблюдаемость: #192 (трейс `classifier_raw`).
- Таксономия семей инструментов: [tool-family-taxonomy.md](./tool-family-taxonomy.md).
