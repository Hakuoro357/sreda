# LangGraph Flows

## Цель

На MVP нужны два отдельных графа:
- граф личного ассистента;
- граф мониторинга источника.

Не нужно делать много агентов. Нужны два понятных workflow.

## 1. Assistant Flow

### Назначение

Обрабатывать входящее сообщение пользователя и формировать ответ.

### State

Минимальное состояние:

- `tenant_id`
- `workspace_id`
- `assistant_id`
- `user_id`
- `thread_id`
- `incoming_message`
- `profile_summary`
- `conversation_summary`
- `tool_context`
- `policy_flags`
- `approval_required`
- `final_response`

### Узлы

#### `load_context`

Задачи:
- загрузить thread context;
- загрузить profile summary;
- загрузить policy flags.

#### `policy_guard`

Задачи:
- определить, есть ли ограничения;
- отметить, нужны ли sensitive tools;
- запретить опасные действия без approval.

#### `generate_response`

Задачи:
- сформировать основной ответ;
- подготовить tool plan, если нужен.

#### `maybe_interrupt`

Задачи:
- если требуется approval, вызвать `interrupt`;
- сохранить request payload;
- остановить run до внешнего решения.

#### `persist_and_enqueue_reply`

Задачи:
- сохранить результат run;
- записать outbox message;
- обновить thread metadata.

### Условия переходов

- если approval не нужен, идти сразу в `persist_and_enqueue_reply`;
- если approval нужен, идти в `maybe_interrupt`;
- после resume продолжать с подтвержденным контекстом.

## 2. Source Monitoring Flow

### Назначение

Обрабатывать новые записи, найденные при парсинге сайта или другого источника.

### State

- `tenant_id`
- `workspace_id`
- `assistant_id`
- `source_id`
- `source_run_id`
- `normalized_item_id`
- `item_type`
- `item_payload_transient`
- `summary`
- `priority`
- `should_send`
- `final_message`

Важно:
- `item_payload_transient` используется только во время текущего выполнения;
- полное содержимое заявки не должно сохраняться в постоянном storage или в долговременном checkpoint этого flow.

### Узлы

#### `prepare_item`

Задачи:
- очистить текст;
- убрать мусор;
- подготовить компактный input.
- держать полный payload только в памяти текущего запуска.

#### `classify_item`

Задачи:
- определить тип записи;
- отделить полезное от шума.

#### `summarize_item`

Задачи:
- сделать короткое summary;
- выделить главное.

#### `score_item`

Задачи:
- оценить важность;
- оценить полезность для канала;
- определить срочность.

#### `decide_delivery`

Задачи:
- отправлять или нет;
- сразу или через агрегированную сводку;
- какой формат нужен.

#### `enqueue_message`

Задачи:
- сохранить decision;
- создать запись в outbox, если доставка нужна.

## 3. Approval Resume Flow

Это не отдельный сложный граф, а путь возобновления.

После approval нужно:
- загрузить paused run;
- передать `Command(resume=...)`;
- завершить оставшиеся шаги;
- записать результат в аудит.

## Общие принципы проектирования графов

- deterministic шаги не отдавать LLM;
- side effects выносить за пределы генерации;
- все risky actions отмечать policy layer;
- состояние делать компактным;
- большие blobs не держать внутри graph state;
- формировать idempotent execution path.
- для `eds_monitor` flow не сохранять полные тексты заявок в постоянный state.

## Что не нужно на MVP

- supervisor graph;
- subagents;
- planner-executor из нескольких моделей;
- циклические research-графы;
- автономные self-triggering graph без расписания.
