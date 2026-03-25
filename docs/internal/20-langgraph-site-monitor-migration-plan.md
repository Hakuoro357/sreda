# LangGraph EDS Monitor Migration Plan

## Цель

Развивать `eds_monitor` внутри архитектуры `Sreda + LangGraph` без потери уже подтвержденного поведения.

## Принцип миграции

Не менять все слои одновременно.

Правильный порядок:
1. сначала перенести storage и orchestration;
2. потом встроить LangGraph analysis;
3. потом заменить script-driven polling на platform jobs;
4. только потом расширять сценарии.

Новый обязательный инвариант миграции:
- полные данные заявок не сохраняются в новой системе.

## Этап 1. Зафиксировать текущий контракт

Что сделать:
- сохранить sample config;
- сохранить sample normalized claim;
- сохранить sample expected analysis;
- сохранить sample notification text;
- зафиксировать список обязательных полей detail payload.

Зачем:
- чтобы перенос не сломал фактический рабочий контракт.

## Этап 2. Вынести смысловые модули

Из текущих скриптов выделить reusable логику:
- config loader;
- site client;
- claim normalizer;
- analysis client;
- notification formatter;
- delivery state semantics.

На этом этапе код должен оставаться в едином Python-стеке, но слой за слоем.

## Этап 3. Перенести state в PostgreSQL

Что сделать:
- создать таблицы `site_accounts`, `site_poll_runs`, `site_claim_state`, `site_change_events`, `site_delivery_records`;
- убрать file-based poll state;
- убрать file-based delivery state;
- session storage пока можно временно оставить file-based.

Это даст:
- платформенную наблюдаемость;
- multi-tenant compatibility;
- подготовку к scheduler и worker.

## Этап 4. Встроить jobs

Что сделать:
- scheduler loop;
- `poll_account_job`;
- `process_claim_job`;
- `deliver_message_job`.

На этом шаге `poll-claims.mjs` перестает быть главным orchestrator.

## Этап 5. Перенести analysis в LangGraph

Первый graph:
- input: `normalized claim`;
- output: `analysis + delivery decision`.

Что важно:
- не трогать deterministic integration steps;
- graph не должен сам логиниться в сайт;
- graph не должен сам дергать Telegram API.
- graph не должен сохранять полную заявку в persistent checkpoint.

## Этап 6. Сверить parity

Нужно проверить:
- для новой заявки формируется тот же normalized claim;
- analysis по смыслу совпадает;
- notification text совпадает по структуре;
- сообщение не дублируется;
- повторный запуск не ломает state.

## Этап 7. Только потом расширять функциональность

После parity можно добавлять:
- changed claims;
- digest;
- priority-based throttling;
- user-level filters;
- richer delivery policy.

## Практическая стратегия реализации

### Вариант A. Быстрый

- больше не рассматривается как целевой для MVP.

### Вариант B. Чистый

- переписать site client и normalizer сразу на Python;
- переносить всё в один стек.

Плюс:
- чище архитектура.

Минус:
- дольше до рабочего результата.

## Рекомендация

Для `Sreda` фиксируем единый путь:
- integration layer `eds_monitor` сразу переписывается на `Python`;
- детерминированные инварианты должны быть сохранены в новой реализации.

## Что нельзя сломать в процессе миграции

- изоляцию по `accountId`;
- обязательный вызов `operator role`;
- reuse session state;
- delivery idempotency;
- запрет на утечку секретов в LLM;
- запрет на смешивание заявок разных логинов.
- запрет на сохранение полных данных заявки в новой системе.
