# MVP Architecture

## Главная цель MVP

Доказать, что платформа умеет:
- принимать сообщения из `Telegram`;
- безопасно хранить контекст;
- запускать workflow в `LangGraph`;
- выполнять простые фоновые сценарии;
- отправлять пользователю полезный результат.

## Принцип упрощения

Для первого запуска нужно убрать все, что не влияет напрямую на ценность:
- без `MAX`;
- без `Redis`;
- без `pgvector`;
- без отдельного object storage, если нет файлов;
- без multi-agent supervisor;
- без сложной админки;
- без расширенного RBAC;
- без автономных агентов широкого профиля.

## Базовый стек

- `Python 3.12+`
- `FastAPI`
- `LangGraph`
- `PostgreSQL`
- `Telegram Bot API`

## Компоненты MVP

### 1. API / Control Plane

Отвечает за:
- tenants;
- tenant features;
- workspaces;
- users;
- assistants;
- настройки источников;
- approval endpoints;
- служебное администрирование.

### 2. Telegram Gateway

Отвечает за:
- прием webhook;
- нормализацию входящих событий;
- idempotency;
- запись входящих сообщений;
- передачу задач в worker.

### 3. Agent Runtime

Отвечает за:
- запуск `LangGraph` thread;
- работу с state;
- interrupts;
- decision making;
- генерацию ответа.

### 4. Worker

Отвечает за:
- выполнение фоновых jobs;
- запуск scheduled pipeline;
- повторные попытки;
- доставку сообщений через outbox.

### 5. PostgreSQL

Используется для:
- продуктовых сущностей;
- очереди задач;
- checkpoint state;
- audit log;
- profile data;
- расписаний;
- deduplication.

## Почему без Redis

На MVP `PostgreSQL` закрывает нужные функции:
- jobs queue через `FOR UPDATE SKIP LOCKED`;
- advisory locks;
- idempotency;
- retries;
- outbox pattern;
- ограниченный scheduler.

Это упрощает:
- инфраструктуру;
- доставку on-prem;
- резервное копирование;
- эксплуатацию.

## Минимальная операционная схема

1. Входящее сообщение или scheduled event попадает в БД.
2. Worker выбирает задачу.
3. Запускается `LangGraph`.
4. Результат сохраняется.
5. Сообщение отправляется через Telegram.
6. Все действия пишутся в аудит.

## Feature modes

На MVP сразу закладываются два режима:

### `core/free`

Доступно:
- чат с ассистентом;
- базовый пользовательский контекст;
- память по согласованию;
- без `eds_monitor`.

### `monitoring-enabled`

Доступно:
- все из `core/free`;
- `eds_monitor`;
- `eds_accounts`;
- scheduler и polling jobs;
- уведомления по изменениям заявок.

Включение и выключение идет на уровне `tenant feature flag`, а не через отдельный форк приложения.
