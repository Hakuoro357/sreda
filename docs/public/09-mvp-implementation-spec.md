# MVP Implementation Spec

## Цель MVP

Собрать работающий первый контур, в котором:
- пользователь общается с ассистентом через `Telegram`;
- ассистент использует `LangGraph`;
- система хранит профиль и контекст в `PostgreSQL`;
- есть scheduled `eds_monitor` одного источника;
- результаты могут отправляться в Telegram автоматически;
- есть базовая изоляция по tenant/workspace.

## Scope In

- `Telegram` webhook bot
- `FastAPI`
- `LangGraph` runtime
- `PostgreSQL`
- один основной assistant flow
- один scheduled `eds_monitor` flow
- один канал доставки
- audit log
- field-level encryption для чувствительных полей
- manual approval для рискованных действий

## Scope Out

- `MAX`
- voice
- files
- multimodal
- vector search
- multi-agent orchestration
- полноценная admin panel
- billing
- сложный RBAC
- on-prem installer как продукт

## Главные use cases

### 1. Личный диалог

Пользователь пишет боту в Telegram.

Система должна:
- найти или создать `tenant/workspace/user/thread`;
- записать входящее сообщение;
- создать job;
- выполнить LangGraph flow;
- записать результат;
- отправить ответ в Telegram.

### 2. Мониторинг сайта

По расписанию запускается source job.

Система должна:
- забрать страницу;
- выделить новые записи;
- сделать dedup;
- прогнать каждую новую запись через LangGraph analysis flow;
- для релевантных событий сформировать сообщение;
- отправить сообщение в Telegram.

Важно:
- полные данные заявки используются только во время обработки;
- в постоянное хранилище попадают только признаки изменений и технический state.
- сценарий доступен только tenant, у которого включена feature `eds_monitor`.

### 3. Approval

Если flow пытается сделать рискованное действие, система должна:
- создать approval record;
- остановить run;
- дождаться подтверждения;
- после подтверждения возобновить run.

## Первый набор модулей

### `app/api`

- webhook endpoint
- assistant endpoints
- source endpoints
- approval endpoints
- health endpoints

### `app/domain`

- tenants
- tenant_features
- users
- assistants
- threads
- sources
- jobs
- approvals

### `app/runtime`

- LangGraph graph definitions
- state schemas
- runtime services
- policy checks

### `app/integrations`

- telegram client
- source fetchers
- html parsing utilities
- encryption helpers

### `app/storage`

- SQLAlchemy models
- repositories
- migrations

### `app/workers`

- job poller
- outbox sender
- scheduler loop

## Первый граф ассистента

Упростить до 4-5 шагов:

1. `load_context`
2. `policy_guard`
3. `generate_response`
4. `maybe_interrupt`
5. `persist_and_enqueue_reply`

## Первый граф мониторинга

1. `prepare_item`
2. `classify_item`
3. `summarize_item`
4. `decide_delivery`
5. `enqueue_message`

## Definition Of Done

MVP можно считать собранным, если:
- Telegram бот принимает и отвечает на сообщения;
- один пользователь изолирован от другого;
- профиль пользователя хранится отдельно от runtime state;
- scheduler запускает проверку сайта по расписанию;
- новые элементы не шлются повторно при дубле;
- LangGraph flow умеет делать summary;
- отправка в Telegram идет через outbox;
- approval flow можно остановить и продолжить;
- все ключевые действия попадают в audit log.
- `eds_monitor` можно явно включить или выключить на уровне tenant.

## Следующий шаг после MVP

После сборки MVP логично идти в таком порядке:

1. стабилизация и наблюдаемость;
2. выделение sender в отдельный процесс;
3. поддержка нескольких источников;
4. tenant-level keys;
5. `MAX`;
6. on-prem deployment profile.
