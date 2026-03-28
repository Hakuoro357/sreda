# MVP Backlog

## Цель

Разложить текущий MVP не от нуля, а от уже готового состояния Среды.

## Epic 1. Foundation

Статус:
- `done`

Что уже сделано:
- структура Python-проекта;
- `FastAPI`, `SQLAlchemy`, migrations, settings;
- health endpoints;
- core entities:
  - `tenants`
  - `workspaces`
  - `users`
  - `assistants`
  - `tenant_features`
  - `jobs`
  - `outbox_messages`

## Epic 2. Secure Telegram Ingress

Статус:
- `done`

Что уже сделано:
- `POST /webhooks/telegram/{bot_key}`;
- auto-create базового tenant bundle;
- onboarding response;
- `inbound_messages`;
- `secure_records`;
- regex-based `privacy_guard`.

## Epic 3. EDS Monitor Vertical Slice

Статус:
- `done`

Что уже сделано:
- `eds_accounts`;
- `eds_claim_state`;
- `eds_change_events`;
- `eds_delivery_records`;
- API:
  - `POST /api/v1/eds-monitor/accounts`
  - `GET /api/v1/eds-monitor/accounts`
  - `POST /api/v1/eds-monitor/accounts/{eds_account_id}/poll`
- manual and API-triggered poll;
- outbox generation;
- Telegram delivery;
- photo delivery;
- `LLM` summary;
- transient claim processing;
- feature gate для `eds_monitor`.

## Epic 4. Assistant Runtime MVP

Статус:
- `in_progress`

Что уже сделано:
- добавлены runtime-сущности:
  - `agent_threads`
  - `agent_runs`
- добавлен `action dispatcher` для read-only Telegram действий;
- первый runtime slice уже обрабатывает:
  - помощь;
  - мой статус;
  - подписки;
- эти сценарии уже проходят через `job -> run -> runtime`.

### Task 4.1

Перенести mutation actions в новый runtime:
- добавить подписку на `EDS`;
- подключить личный кабинет `EDS`;
- убрать слот / вернуть слот;
- убрать кабинет / вернуть кабинет.

### Task 4.2

Довести runtime graph:
- `load_context`
- `route_action`
- `policy_guard`
- `execute_action`
- `persist_and_enqueue_reply`

### Task 4.3

Добавить `claim.lookup` как первый assistant-сценарий поверх нового runtime.

### Task 4.4

Связать inbound event, job, run и outbox в один стабильный execution path.

## Epic 5. Worker And Outbox Runtime

Статус:
- `in_progress`

Что уже зафиксировано:
- пока Среда остается на текущем DB job-механизме;
- позже execution backend будет вынесен на `Dramatiq + Redis`.

### Task 5.1

Реализовать реальный `job_runner`.

### Task 5.2

Реализовать production sender loop.

### Task 5.3

Добавить:
- retry policy;
- status transitions;
- last error storage;
- cleanup.

### Task 5.4

Убрать зависимость от ручных CLI-прогонов для основных flow.

## Epic 6. Approvals And Policy

Статус:
- `planned`

### Task 6.1

Создать таблицу `approvals`.

### Task 6.2

Реализовать `interrupt` path.

### Task 6.3

Сделать API:
- `GET /api/v1/approvals`
- `GET /api/v1/approvals/{id}`
- `POST /api/v1/approvals/{id}/approve`
- `POST /api/v1/approvals/{id}/reject`

### Task 6.4

Реализовать resume execution после approval.

## Epic 7. Security Hardening

Статус:
- `in_progress`

### Task 7.1

Довести encryption/storage layer для sensitive data.

### Task 7.2

Добавить redaction policy для логов и debug output.

### Task 7.3

Проверить, что полные данные заявок не попадают:
- в persistent checkpoints;
- в audit log;
- в технические логи.

### Task 7.4

Подготовить основу для secure user memory.

## Epic 8. Observability And Reliability

Статус:
- `planned`

### Task 8.1

Добавить структурные логи.

### Task 8.2

Добавить correlation/request ids.

### Task 8.3

Логировать:
- inbound events;
- job lifecycle;
- outbox delivery;
- approvals;
- `eds_monitor` runs.

### Task 8.4

Сделать smoke tests на end-to-end flow.

## Epic 9. Self-Service EDS Onboarding

Статус:
- `done`

Что уже сделано:
- self-service flow подключения `EDS`;
- защищенная connect-страница;
- verification worker;
- статус подключения и ошибок в Telegram;
- bridge в runtime `eds_monitor`.

## Ближайший приоритет

1. `Epic 4`
2. `Epic 5`
3. `Epic 6`
4. `Epic 8`
5. `Epic 9`
