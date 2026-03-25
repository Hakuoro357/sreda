# Public Docs Publication Plan

## Цель

Подготовить безопасный и понятный набор документации, который можно публиковать в публичном репозитории `Среды`.

Документ фиксирует:
- какие файлы можно публиковать;
- какие файлы лучше оставить внутренними;
- какие правки нужны перед публикацией.

## Принцип

В public должны попадать:
- продуктовые рамки;
- архитектурные решения высокого уровня;
- публично полезные схемы данных и flow;
- roadmap и backlog;
- общие принципы безопасности без внутренних operational details.

В public не должны попадать:
- внутренние эксплуатационные детали;
- модели администрирования конкретной инсталляции;
- deploy-процедуры конкретного хоста;
- детали по `SSH`, ключам, внутренним путям и служебным операциям;
- документы, из которых можно восстановить внутреннюю operational модель команды.

## Набор для public

Эти файлы можно готовить к публикации:

- `README.md`
- `01-product-vision.md`
- `02-why-langgraph.md`
- `03-mvp-architecture.md`
- `04-site-monitoring-pipeline.md`
- `05-data-security-and-isolation.md`
- `06-roadmap.md`
- `07-postgres-schema.md`
- `08-services-and-processes.md`
- `09-mvp-implementation-spec.md`
- `10-api-contracts.md`
- `11-langgraph-flows.md`
- `12-delivery-plan.md`
- `16-mvp-backlog.md`
- `23-open-core-plugin-model.md`
- `24-privacy-guard.md`
- `25-inbound-security-and-secure-storage.md`

## Набор для private/internal

Эти файлы лучше не публиковать в публичный репозиторий:

- `13-security-threat-model.md`
- `14-open-questions.md`
- `15-decisions.md`
- `17-current-site-monitor-analysis.md`
- `18-langgraph-site-monitor-architecture.md`
- `19-langgraph-site-monitor-data-model.md`
- `20-langgraph-site-monitor-migration-plan.md`
- `21-decisions-from-open-questions.md`
- `22-development-and-deploy-model.md`

Причины:
- содержат внутренние decision notes;
- содержат лишние operational details;
- содержат детали deploy/process модели, не нужные внешнему читателю;
- местами раскрывают внутреннюю организацию эксплуатации.

## Что нужно отредактировать перед публикацией

Даже в public-наборе перед публикацией нужно проверить:

- убрать прямые упоминания локальных путей Windows и macOS;
- убрать ссылки на внутренние runtime-папки;
- убрать operational language вида:
  - `на хосте Sreda`
  - `локально у команды`
  - `через SSH`
- заменить внутренние формулировки на product-neutral:
  - `deployment host`
  - `runtime environment`
  - `ignored local secrets storage`

## Правило по безопасности документации

Перед публикацией docs-set должен пройти финальный review на:
- секреты;
- токены;
- реальные логины и идентификаторы;
- конкретные `chat_id`;
- внутренние пути;
- детали по ключам и доступам;
- operational инструкции, которые не нужны внешнему читателю.

## Рекомендуемый формат публикации

Лучший вариант:

1. держать `internal docs` в отдельной непубличной папке или репозитории;
2. в публичный репозиторий положить только curated docs-set;
3. не публиковать весь каталог `docs` как есть.

## Следующий шаг

После одобрения этого плана:
- собрать отдельный public docs subset;
- при необходимости подготовить очищенные версии отдельных файлов;
- и только потом коммитить их в публичный репозиторий.
