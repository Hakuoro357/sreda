# Roadmap

## Текущее состояние

Среда уже вышла за пределы базового MVP-скелета.

На сегодня в продукте уже есть:
- core backend и data model;
- secure Telegram ingress;
- self-service onboarding;
- stub billing и подписки;
- self-service подключение кабинета `EDS`;
- verification flow;
- рабочий `eds_monitor`.

Поэтому roadmap надо считать не от нуля, а от уже работающего пользовательского контура.

## Этап 1. Foundation

Статус:
- `done`

Что уже есть:
- `FastAPI` приложение;
- базовые migrations;
- `tenants`, `workspaces`, `users`, `assistants`, `tenant_features`;
- `jobs`, `outbox_messages`, `secure_records`, `inbound_messages`;
- plugin/capability model;
- `Telegram` integration layer;
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

## Этап 4. Billing And Self-Service EDS Connect

Статус:
- `done`

Что уже есть:
- подписки на `EDS` через `stub` billing;
- статус и управление подписками в Telegram;
- безопасная connect-страница через `Mini App` / `web_app`;
- encrypted хранение логина и пароля `EDS`;
- verification worker;
- bridge из `tenant_eds_accounts` в runtime `eds_accounts`;
- защита от повторного подключения одного и того же личного кабинета `EDS`.

## Этап 5. Assistant Runtime MVP

Статус:
- `in_progress`

Что нужно сделать:
- полноценный assistant flow в `LangGraph`;
- routing пользовательских действий;
- `message -> job -> graph -> outbox reply`;
- thread/run model;
- нормальный worker loop;
- базовые команды:
  - помощь;
  - мой статус;
  - просмотр заявки по номеру.

Что уже начато:
- добавлены runtime-сущности `agent_threads` и `agent_runs`;
- появился первый runtime slice для read-only действий:
  - помощь;
  - мой статус;
  - подписки;
- transport слой начал переводиться на модель `update -> action -> job -> run`.

Примечание:
- свободный чат пока не является основной UX-моделью;
- основной интерфейс сейчас строится вокруг меню и inline-кнопок.

## Этап 6. Payments And Subscription Lifecycle

Статус:
- `next`

Что нужно сделать:
- интеграция с `ЮKassa`;
- переход со `stub` billing на реальную оплату;
- reminders перед продлением;
- lifecycle jobs для истечения и продления подписок;
- обработка неуспешной оплаты;
- финальная шлифовка UX подписок.

## Этап 7. Automation And Reliability

Статус:
- `next`

Что нужно сделать:
- scheduler loop для системных задач;
- production-ready `job_runner`;
- production-ready `outbox sender`;
- retry policy и cleanup jobs;
- структурные логи и correlation ids;
- smoke tests на полный цикл.

Примечание:
- на текущем этапе Среда остается на встроенном DB job-механизме;
- позже execution backend планируется перевести на `Dramatiq + Redis`, не меняя собственные runtime-сущности и предметную логику.

## Этап 8. Security And Control

Статус:
- `next`

Что нужно сделать:
- approval flow;
- policy guard для risky operations;
- лог-redaction;
- более строгие правила доступа к sensitive данным;
- отдельные encrypted сущности для пользовательской памяти.

## Этап 9. Early Production

Статус:
- `later`

Что входит:
- несколько `EDS`-логинов на одного клиента;
- базовая операторская админка;
- расширенные лимиты и антиспам;
- более богатый audit trail.

## Этап 10. Platform

Статус:
- `later`

Что входит:
- shared workspaces;
- richer memory layer;
- дополнительные capabilities кроме `eds_monitor`;
- billing hooks;
- analytics;
- второй message provider.

## Этап 11. On-Prem Box

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
- не усложнять billing раньше, чем стабилен текущий self-service flow;
- не смешивать pilot-ready продукт и enterprise-on-prem scope.
