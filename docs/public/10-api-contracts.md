# API Contracts

## Цель

На MVP API должно закрыть только базовые сценарии:
- прием Telegram webhook;
- управление источниками мониторинга;
- просмотр approvals;
- подтверждение или отклонение approval;
- служебные health endpoints.

## Общие правила

- все внутренние API версии `v1`;
- JSON only;
- все ответы содержат `request_id`;
- все внутренние запросы должны идти в tenant/workspace контексте;
- публичный webhook endpoint отдельно от внутренних control-plane endpoint'ов.

## 1. Telegram Webhook

### `POST /webhooks/telegram/{bot_key}`

Назначение:
- принять update от Telegram;
- провалидировать bot binding;
- записать событие;
- создать job на обработку.

Ответ:

```json
{
  "ok": true,
  "request_id": "req_123"
}
```

Минимальный результат:
- не отвечать пользователю синхронно;
- только принять и поставить в обработку.

## 2. Sources

### `POST /api/v1/sources`

Создает источник мониторинга.

Пример запроса:

```json
{
  "tenant_id": "t_1",
  "workspace_id": "w_1",
  "assistant_id": "a_1",
  "name": "Новости компании",
  "source_type": "web_page",
  "url": "https://example.com/news",
  "schedule": {
    "type": "interval",
    "minutes": 15
  },
  "config": {
    "parser_type": "html_list"
  }
}
```

### `GET /api/v1/sources`

Возвращает список источников workspace.

Параметры:
- `tenant_id`
- `workspace_id`
- `assistant_id` опционально

### `GET /api/v1/sources/{source_id}`

Возвращает один источник и краткую сводку по последним run.

### `PATCH /api/v1/sources/{source_id}`

Обновляет:
- `status`
- `schedule`
- `config`
- `name`

### `POST /api/v1/sources/{source_id}/run`

Ручной запуск проверки источника.

## 3. Approvals

### `GET /api/v1/approvals`

Возвращает pending approvals.

Параметры:
- `tenant_id`
- `workspace_id`
- `status`

### `GET /api/v1/approvals/{approval_id}`

Возвращает детали approval:
- что запрашивается;
- какой run остановлен;
- какие последствия.

### `POST /api/v1/approvals/{approval_id}/approve`

Пример запроса:

```json
{
  "tenant_id": "t_1",
  "workspace_id": "w_1",
  "approved_by": "u_1",
  "comment": "Разрешаю"
}
```

### `POST /api/v1/approvals/{approval_id}/reject`

Пример запроса:

```json
{
  "tenant_id": "t_1",
  "workspace_id": "w_1",
  "rejected_by": "u_1",
  "comment": "Не отправлять"
}
```

## 4. Assistant Service Endpoints

### `GET /api/v1/threads`

Возвращает список thread по пользователю или ассистенту.

### `GET /api/v1/threads/{thread_id}`

Возвращает:
- thread metadata;
- последние messages;
- последние runs;
- pending approvals.

### `GET /api/v1/runs/{run_id}`

Возвращает:
- тип run;
- статус;
- trigger;
- input/output summary;
- ошибки;
- ссылки на approval или source run.

## 5. Health

### `GET /health/live`

Проверка, что процесс жив.

### `GET /health/ready`

Проверка готовности:
- доступна БД;
- доступен runtime config;
- процесс может брать jobs.

## Важные замечания

- на MVP можно обойтись без полноценной внешней публичной admin API;
- control-plane может быть внутренним API только для команды;
- tenant/workspace контекст нельзя вычислять только на клиенте, он должен валидироваться на сервере.
