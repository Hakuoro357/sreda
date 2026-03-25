# LangGraph EDS Monitor Architecture

## Цель

Построить LangGraph-версию `eds_monitor` так, чтобы:
- сохранить текущую логику по `accountId`;
- не потерять детерминированность парсинга;
- отделить polling, analysis и delivery;
- встроить это в общую платформу `Sreda`.

## Главный принцип

`LangGraph` здесь нужен не для:
- логина;
- браузерной автоматизации;
- HTTP scraping;
- Telegram API delivery.

`LangGraph` нужен для:
- анализа нормализованной заявки;
- оценки приоритета и рисков;
- решения о том, отправлять ли уведомление;
- последующего расширения до обработки changed claims и digest.

## Разделение слоев

### 1. Integration Layer

Что остается вне графа:
- config loader;
- site client;
- session restore/login;
- fetch list/detail/works;
- binary image fetch;
- Telegram send/edit;
- PostgreSQL repositories.

### 2. Job Orchestration Layer

Что делает platform layer:
- создает scheduled jobs;
- забирает due jobs;
- запускает account polling;
- сохраняет только технический state и metadata событий;
- вызывает graph для claim analysis;
- создает outbox event.

### 3. LangGraph Layer

Что делает graph:
- получает одну нормализованную заявку;
- получает account context;
- считает analysis;
- считает delivery decision;
- возвращает structured result.

## Целевой поток выполнения

### A. Scheduler Flow

1. найти `site_source_account` с `next_run_at <= now()`;
2. взять lock;
3. создать `poll_account_job`;
4. пересчитать `next_run_at`.

### B. Poll Account Flow

1. загрузить config и credentials для `accountId`;
2. восстановить session;
3. активировать operator role;
4. забрать список claims;
5. сравнить со state по `accountId`;
6. выделить `newClaims` и позже `changedClaims`;
7. по каждой новой заявке:
- получить detail;
- получить works при необходимости;
- нормализовать;
- обработать normalized item только во время текущего запуска;
- запустить graph анализа;
8. обновить account poll state.

### C. Claim Analysis Graph

Graph работает на одной заявке.

Состояние:
- `tenant_id`
- `workspace_id`
- `account_id`
- `source_run_id`
- `claim_id`
- `normalized_claim_transient`
- `analysis_prompt_version`
- `analysis_result`
- `delivery_decision`

Важно:
- `normalized_claim_transient` не должен попадать в долговременное хранилище;
- для этого flow нельзя сохранять полное содержимое заявки в persistent checkpoint.

Узлы:

#### `prepare_claim`

- проверка обязательных полей;
- формирование compact payload для модели;
- удаление лишних и опасных полей.

#### `analyze_claim`

- вызов модели по текущему промпту;
- получение JSON анализа;
- валидация структуры.

#### `apply_business_rules`

- детерминированные post-processing правила;
- например:
  - если `wasExpired = true`, убедиться, что риск отражен;
  - если `canAccept/canDecline = true`, отметить waiting state;
  - если safety-тема, не опускать priority слишком низко.

#### `decide_delivery`

- нужно ли отправлять;
- в какой шаблон;
- нужен ли immediate notify или можно агрегировать.

#### `build_delivery_payload`

- собрать финальный текст;
- подготовить список изображений;
- вернуть outbox payload.

## Почему граф один, а не много агентов

Для этого сценария много агентов не дают выигрыша.

Нужен не supervisor, а прозрачный pipeline:
- одна заявка;
- один анализ;
- одно решение о доставке.

Так проще:
- дебажить;
- тестировать;
- аудировать;
- объяснять клиенту.

## Как сохранить текущие сильные стороны

### Изоляция по `accountId`

Все таблицы и job payload должны содержать:
- `tenant_id`
- `workspace_id`
- `account_id`

`account_id` здесь остается фактической operational unit.

### Idempotent delivery

Сохранить модель из текущего `delivery-state.mjs`, но в БД:
- `claim_id + recipient_chat_id` -> delivery record;
- если сообщение уже было, редактируем;
- фото не шлем повторно без необходимости.

### Session reuse

Session storage нельзя переносить в graph state.

Нужно хранить отдельно:
- `account_sessions`
- encrypted storage blob или filesystem pointer

Graph должен только получать факт, что session уже готова.

### No-claim-storage policy

Полная карточка заявки:
- не хранится в `PostgreSQL`;
- не хранится в audit log;
- не хранится в delivery metadata;
- используется только во время текущей обработки.

## Что останется file-based даже после переноса

Возможный компромисс первой версии:
- Playwright storage state можно временно оставить в файловой системе на защищенном path;
- но metadata о session надо хранить в БД.

Это допустимо, если:
- runtime живет на одной машине;
- файловая зона защищена;
- есть явная привязка к `accountId`.

## Следующий уровень после MVP

После базового переноса в LangGraph можно добавлять:
- обработку `changedClaims`;
- повторные уведомления по значимым изменениям;
- digest;
- user preferences по фильтрации;
- approval flow для чувствительных автоматических действий.
