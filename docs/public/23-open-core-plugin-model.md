# Open-Core Plugin Model

## Цель

Разделить:
- публичное ядро платформы `Sreda`;
- приватные реализации отдельных feature modules.

Это нужно, чтобы:
- ядро можно было держать в открытом git-репозитории;
- конкретные реализации мониторинга и других платных функций не раскрывались публично;
- одна и та же платформа поддерживала open-source core и private extensions.

## Главный принцип

Публичный репозиторий содержит:
- core backend;
- capability system;
- feature contracts;
- plugin registry;
- базовые runtime interfaces;
- документацию;
- общую deploy/dev модель.

Приватные репозитории содержат:
- конкретные feature implementations;
- интеграции с внешними системами;
- vendor/customer-specific business logic;
- доменные prompt/heuristics;
- проприетарные парсеры, нормализаторы и workflow.

## Модель репозиториев

### Public

Пример:
- `sreda`

Содержит:
- `FastAPI`
- `LangGraph` core
- `PostgreSQL` core models
- `tenant_features`
- contracts/registry для feature modules

### Private

Примеры:
- `private-features-repo`
- `feature-package-x`
- `feature-package-y`

Содержат:
- реальную реализацию feature;
- регистрацию своих routes/jobs/graphs;
- свой integration layer.

## Два разных уровня

Важно разделять:

### 1. Установка feature в систему

Это значит:
- Python package физически установлен в окружение.

### 2. Включение feature клиенту

Это значит:
- в `tenant_features` для конкретного `tenant` стоит `enabled = true`.

То есть:
- feature package может быть установлено в систему;
- но клиент все равно не сможет им пользоваться без включения capability flag.

## Как ядро должно видеть фичу

Публичное ядро не должно знать внутренности private feature.

Ему нужен только контракт:
- какой у фичи `feature_key`;
- какие routes она регистрирует;
- какие jobs она добавляет;
- какие graph builders она добавляет;
- какие startup hooks ей нужны.

## Базовый контракт

Минимально feature module должен уметь:
- назвать свой `feature_key`;
- зарегистрировать API routes;
- зарегистрировать runtime hooks;
- зарегистрировать worker hooks.

В ядре это оформляется как:
- `FeatureModule` protocol/interface;
- `FeatureRegistry`.

## Механизм подключения

На MVP достаточно простого механизма:
- список module paths задается через env/config;
- ядро импортирует модули по именам;
- каждый модуль вызывает `register(registry)` или экспортирует `feature_module`.

Пример:

```text
SREDA_FEATURE_MODULES=private_feature_package.plugin
```

## Что должно жить в public core

- contracts
- registry
- loader
- capability checks
- базовые feature keys
- mock/demo feature modules при необходимости

## Что не должно жить в public core

- конкретный client под EDS
- реальные parsing rules
- private prompt logic
- vendor-specific нормализаторы
- платные feature workflow

## Рекомендуемая структура в public core

```text
src/sreda/features/
  contracts.py
  registry.py
  loader.py
```

## Рекомендуемая структура в private feature package

```text
src/private_feature_package/
  plugin.py
  routes.py
  jobs.py
  graphs.py
  integrations/
```

## Поток регистрации

1. ядро стартует;
2. читает список feature modules из config;
3. импортирует их;
4. каждый модуль регистрирует свои hooks в `FeatureRegistry`;
5. далее runtime уже проверяет `tenant_features`.

## Как это сочетается с billing

Billing не должен быть частью runtime feature logic.

Правильная модель:
- billing/ops решает, что feature надо включить;
- система меняет `tenant_features`;
- runtime читает только факт доступности feature.

## Что даёт эта схема

- open-core без раскрытия проприетарных фич;
- чистое разделение ответственности;
- легкое добавление новых feature packages;
- возможность держать один runtime, но разные installed capabilities;
- удобную enterprise-модель.

## Практический вывод

Для `Sreda` фиксируем такую стратегию:
- ядро платформы публичное;
- конкретные feature implementations приватные;
- фичи подключаются как отдельные Python packages;
- включение доступа клиенту идет через `tenant_features`.
