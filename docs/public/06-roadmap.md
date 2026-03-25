# Roadmap

## Текущее состояние

Среда уже прошла базовый infrastructure stage и сейчас находится между:
- завершенным vertical slice по `eds_monitor`;
- и незавершенным общим assistant runtime.

То есть roadmap надо считать не от нуля, а от уже готового фундамента.

## Этап 1. Foundation

Статус:
- `done`

Что уже есть:
- `FastAPI` приложение;
- базовые migrations;
- `tenants`, `workspaces`, `users`, `assistants`, `tenant_features`;
- `jobs`, `outbox_messages`, `secure_records`, `inbound_messages`;
- plugin/capability model;
- `Telegram` webhook каркас;
- local secrets model через `.secrets/`.

## Этап 2. Secure Ingress

Статус:
- `done`

Что уже есть:
- прием входящих `Telegram` updates;
- автоматическое создание базового tenant bundle по первому сообщению;
- onboarding message;
- `privacy_guard` на regex-правилах;
- encrypted raw payload storage;
- sanitized inbound event storage.

## Этап 3. EDS Vertical Slice

Статус:
- `done`

Что уже есть:
- feature-gated `eds_monitor`;
- `EDSAccount`, `EDSClaimState`, `EDSChangeEvent`, `EDSDeliveryRecord`;
- ручной и API-triggered polling;
- автологин и refresh сессии;
- change detection по `claimHistory`;
- `LLM` summary для уведомлений;
- Telegram delivery с фотографиями;
- transient processing без хранения полной заявки.

## Этап 4. Assistant Runtime MVP

Статус:
- `next`

Что нужно сделать:
- полноценный assistant flow в `LangGraph`;
- routing пользовательских сообщений;
- `message -> job -> graph -> outbox reply`;
- thread/run model;
- нормальный worker loop;
- базовые команды:
  - помощь;
  - мой статус;
  - последние изменения;
  - просмотр заявки по номеру.

## Этап 5. Automation And Reliability

Статус:
- `next`

Что нужно сделать:
- scheduler loop для системных задач;
- production-ready `job_runner`;
- production-ready `outbox sender`;
- retry policy и cleanup jobs;
- структурные логи и correlation ids;
- smoke tests на полный цикл.

## Этап 6. Security And Control

Статус:
- `next`

Что нужно сделать:
- approval flow;
- policy guard для risky operations;
- лог-redaction;
- более строгие правила доступа к sensitive данным;
- отдельные encrypted сущности для пользовательской памяти.

## Этап 7. Early Production

Статус:
- `later`

Что входит:
- несколько `EDS`-логинов на одного клиента;
- self-service onboarding для мониторинга;
- базовая операторская админка;
- расширенные лимиты и антиспам;
- более богатый audit trail.

## Этап 8. Platform

Статус:
- `later`

Что входит:
- shared workspaces;
- richer memory layer;
- дополнительные capabilities кроме `eds_monitor`;
- billing hooks;
- analytics;
- второй message provider.

## Этап 9. On-Prem Box

Статус:
- `later`

Что входит:
- single-tenant deployment profile;
- локальные secrets и ключи;
- локальный model gateway;
- install/update workflow;
- документация по установке и обновлению.

## Что важно не делать слишком рано

- не строить full multi-agent platform до завершения assistant runtime;
- не добавлять новые каналы до стабилизации `Telegram`;
- не делать approval/policy слой наполовину;
- не тащить billing и operator UI раньше, чем стабилен core flow;
- не смешивать pilot-ready продукт и enterprise-on-prem scope.
