# Services And Processes

## Цель

На MVP нужно минимальное количество процессов, которые:
- легко поднять;
- легко дебажить;
- легко перенести в on-prem.

## Базовый набор

### 1. `api`

Что делает:
- принимает webhook от `Telegram`;
- отдает control-plane API;
- создает записи в `inbound_messages`;
- создает jobs;
- принимает approval/resume запросы;
- отдает служебные endpoints.
- проверяет tenant feature flags для feature-gated сценариев.

Технология:
- `FastAPI`

## 2. `worker`

Что делает:
- забирает jobs из `PostgreSQL`;
- обрабатывает входящие сообщения;
- запускает `LangGraph` flow;
- создает `outbox_messages`;
- выполняет `eds_monitor` pipeline;
- делает retries.

Важно:
- worker не должен запускать monitoring jobs, если у tenant не включена feature `eds_monitor`.

На MVP лучше держать один тип worker-процесса.

## 3. `sender`

Что делает:
- выбирает `outbox_messages` со статусом `pending`;
- отправляет сообщения в `Telegram`;
- обновляет статус доставки;
- повторяет отправку при временных ошибках.

Сейчас в реальной `Среде` это пока может быть совмещено с `worker`, но логически слой доставки все равно остается отдельным.

## 4. `scheduler`

Что делает:
- проверяет `sources.next_run_at`;
- создает jobs на запуск мониторинга;
- следит, чтобы задачи не создавались повторно.

Важно:
- scheduler должен учитывать `tenant_features` и игнорировать tenants без `eds_monitor`.

На первом этапе `scheduler` можно встроить в `worker`, если запускать у него periodic loop.

## Минимальная конфигурация процессов

Для самого первого запуска можно поднять всего 2 процесса:

1. `api`
2. `worker`

При этом:
- `worker` совмещает обычную обработку jobs;
- `worker` же отправляет `outbox_messages`;
- `worker` же запускает scheduled checks.

Это не идеально, но сильно упрощает старт.

## Рекомендуемая схема первого production-приближения

1. `api`
2. `worker`
3. `sender`

Так проще:
- ограничивать rate;
- расследовать сбои доставки;
- не держать сетевые отправки в одном execution loop с тяжелыми jobs.

## Deployment форматы

### MVP local / early cloud

- `docker compose`
- `api`
- `worker`
- `postgres`

### Early production

- `docker compose` или небольшой `k8s`
- отдельный volume для Postgres backup
- отдельные секреты для бота и шифрования

### On-prem

- тот же набор сервисов;
- single-tenant конфигурация;
- отдельная БД на инсталляцию;
- локальная секретная среда клиента.

## Что не нужно на первом запуске

- отдельный event bus;
- Redis;
- отдельный orchestration cluster;
- отдельный realtime gateway;
- отдельный vector DB;
- отдельный scraping fleet.

## Важные operational правила

- все внешние side effects только через outbox;
- каждый worker должен иметь `worker_id`;
- все retries должны быть ограничены;
- все jobs должны быть идемпотентны;
- все approval паузы должны быть возобновляемыми.
