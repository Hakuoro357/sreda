# Delivery Plan

## Цель

Довести Среду от уже готового infrastructure + `eds_monitor` slice до первого цельного MVP, где пользователь пишет боту, а система отвечает как ассистент и параллельно умеет мониторить EDS.

## Что уже завершено

### Базовый backend

Статус:
- `done`

Что уже сделано:
- структура Python-проекта;
- `FastAPI`;
- migrations;
- базовые health endpoints;
- settings/config;
- capability/plugin model.

### Secure Telegram ingress

Статус:
- `done`

Что уже сделано:
- `POST /webhooks/telegram/{bot_key}`;
- auto-onboarding базового пользователя;
- сохранение sanitized inbound;
- encrypted raw payload storage;
- onboarding message и callback button.

### EDS vertical slice

Статус:
- `done`

Что уже сделано:
- `eds_monitor` capability;
- EDS account binding;
- polling;
- change detection;
- outbox generation;
- Telegram delivery;
- фото;
- `LLM` summary.

## Этап 1. Assistant Runtime MVP

Приоритет:
- `highest`

Что сделать:
- довести `assistant_flow` от каркаса до рабочего graph;
- ввести thread/run сущности и связать их с inbound message;
- сделать первый routing слой для команд и простых интентов;
- формировать reply через outbox, а не прямой send из webhook;
- зафиксировать минимальный набор supported user scenarios.

Что уже начато:
- добавлены `agent_threads` и `agent_runs`;
- первый read-only runtime slice уже ведет через новый контур:
  - `help`
  - `status`
  - `subscriptions`
- mutation actions пока остаются на legacy path и будут переноситься следующей волной.

Результат:
- бот начинает не только принимать и онбордить, но и отвечать пользователю через runtime.

## Этап 2. Worker And Scheduling

Приоритет:
- `high`

Что сделать:
- реализовать `job_runner`;
- реализовать production sender loop;
- реализовать scheduler loop;
- убрать ручную зависимость от CLI для рабочих сценариев;
- сделать retry transitions и cleanup.

Текущая стратегия:
- пока Среда остается на собственном DB job-механизме;
- после стабилизации runtime планируется миграция execution backend на `Dramatiq + Redis`.

Результат:
- ключевые процессы переходят из ручного режима в системный.

## Этап 3. Approvals And Policy

Приоритет:
- `high`

Что сделать:
- таблица и API approvals;
- interrupt/resume path;
- policy guard для risky actions;
- базовая модель ручного approve/reject пользователем.

Результат:
- система может безопасно спрашивать разрешение перед чувствительными действиями.

## Этап 4. Stabilization

Приоритет:
- `medium`

Что сделать:
- структурные логи;
- correlation ids;
- устойчивые статусы jobs/outbox;
- smoke tests на end-to-end flow;
- простые recovery playbooks.

Результат:
- MVP можно уверенно гонять в pilot.

## Этап 5. Self-Service EDS Onboarding

Приоритет:
- `medium`

Что сделать:
- flow подключения `EDS` из чата;
- создание `EDSAccount` без ручной SQL-подготовки;
- безопасный сбор логина;
- дальнейшая ручная или полуавтоматическая привязка секрета;
- понятный статус подключения для пользователя.

Результат:
- подключение мониторинга становится управляемым product flow, а не только инженерной операцией.

## Минимальный релизный критерий

Можно считать MVP цельным, если:
- пользователь автоматически создается по первому сообщению;
- бот отвечает хотя бы на базовые команды;
- `eds_monitor` работает без ручного переписывания состояния;
- worker и sender переживают перезапуск;
- входящие сообщения и sensitive payload обрабатываются безопасно;
- есть базовый audit/trace по ключевым операциям.
