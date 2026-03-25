# PostgreSQL Schema

## Цель

На MVP `PostgreSQL` должен закрыть сразу несколько задач:
- продуктовые данные;
- runtime state;
- jobs queue;
- audit trail;
- хранение расписаний;
- outbox delivery;
- простую память профиля.

Для первого запуска лучше держать все в одной БД, но логически разделить таблицы по группам.

## 1. Tenant And Identity

### `tenants`

Поля:
- `id`
- `name`
- `slug`
- `status`
- `created_at`
- `updated_at`

### `workspaces`

Поля:
- `id`
- `tenant_id`
- `name`
- `slug`
- `status`
- `created_at`
- `updated_at`

### `tenant_features`

Поля:
- `id`
- `tenant_id`
- `feature_key`
- `enabled`
- `enabled_at`
- `disabled_at`
- `granted_by`
- `notes`

Первые `feature_key`:
- `core_assistant`
- `eds_monitor`

### `users`

Поля:
- `id`
- `tenant_id`
- `external_user_id`
- `display_name`
- `timezone`
- `locale`
- `status`
- `created_at`
- `updated_at`

### `workspace_memberships`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `user_id`
- `role`
- `created_at`

## 2. Assistants And Channels

### `assistants`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `name`
- `assistant_type`
- `status`
- `config_json`
- `created_at`
- `updated_at`

### `channel_bindings`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `channel_type`
- `external_chat_id`
- `external_bot_id`
- `status`
- `created_at`
- `updated_at`

`channel_type` на MVP:
- `telegram_dm`

## 3. Inbound / Outbound Messaging

### `secure_records`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `record_type`
- `record_key`
- `encrypted_json`
- `created_at`

### `inbound_messages`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `user_id`
- `channel_type`
- `channel_account_id`
- `bot_key`
- `external_update_id`
- `sender_chat_id`
- `message_text_sanitized`
- `contains_sensitive_data`
- `secure_record_id`
- `status`
- `created_at`

Индексы:
- unique по `bot_key + external_update_id`
- индекс по `created_at`

### `outbox_messages`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `channel_binding_id`
- `message_type`
- `payload_json`
- `status`
- `attempt_count`
- `next_attempt_at`
- `last_error`
- `created_at`
- `sent_at`

`status`:
- `pending`
- `sending`
- `sent`
- `failed`

## 4. LangGraph Runtime

### `agent_threads`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `user_id`
- `thread_key`
- `status`
- `last_message_at`
- `created_at`
- `updated_at`

`thread_key` лучше сделать детерминированным для личного чата.

### `agent_runs`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `thread_id`
- `run_type`
- `trigger_type`
- `status`
- `input_json`
- `output_json`
- `started_at`
- `finished_at`
- `last_error`

`trigger_type`:
- `user_message`
- `scheduled_source`
- `manual`
- `approval_resume`

### `agent_checkpoints`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `thread_id`
- `run_id`
- `checkpoint_ns`
- `checkpoint_data`
- `created_at`

Если использовать готовый checkpointer LangGraph, таблица может быть другой по структуре, но логика останется такой же.

## 5. Profile And Sensitive Data

### `user_profiles`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `user_id`
- `profile_summary`
- `preferences_json`
- `updated_at`

На MVP тут должны лежать только безопасные summary-данные.

### `pii_blobs`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `user_id`
- `blob_type`
- `ciphertext`
- `iv`
- `key_version`
- `created_at`
- `updated_at`

Это отдельный контур для чувствительных данных.

## 6. EDS Monitoring

### `eds_accounts`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `account_key`
- `label`
- `login`
- `status`
- `created_at`
- `updated_at`

### `eds_poll_runs`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `eds_account_id`
- `status`
- `started_at`
- `finished_at`
- `claims_seen`
- `new_claims_count`
- `changed_claims_count`
- `unchanged_claims_count`
- `last_error`

### `eds_claim_state`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `eds_account_id`
- `claim_id`
- `first_seen_at`
- `last_seen_at`
- `status`
- `status_name`
- `deadline_at`
- `fingerprint_hash`
- `last_seen_changed`
- `last_history_order`
- `last_history_code`
- `last_history_date`
- `last_notified_event_key`
- `updated_at`

### `eds_change_events`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `eds_account_id`
- `poll_run_id`
- `claim_id`
- `change_type`
- `message_text`
- `created_at`

### `eds_delivery_records`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `eds_account_id`
- `claim_id`
- `channel_type`
- `channel_account_id`
- `telegram_message_id`
- `photos_sent_hash`
- `created_at`
- `updated_at`

## 7. Jobs And Scheduling

### `jobs`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `job_type`
- `payload_json`
- `status`
- `priority`
- `attempt_count`
- `max_attempts`
- `run_at`
- `locked_at`
- `locked_by`
- `last_error`
- `created_at`
- `updated_at`

`status`:
- `pending`
- `running`
- `done`
- `failed`
- `dead`

Выбор задач:
- `FOR UPDATE SKIP LOCKED`

### `job_attempts`

Поля:
- `id`
- `job_id`
- `worker_id`
- `started_at`
- `finished_at`
- `status`
- `error_text`

## 8. Approval And Audit

### `approvals`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `thread_id`
- `run_id`
- `approval_type`
- `status`
- `request_payload`
- `response_payload`
- `requested_at`
- `resolved_at`

### `audit_log`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `actor_type`
- `actor_id`
- `action_type`
- `resource_type`
- `resource_id`
- `details_json`
- `created_at`

## Минимальные правила проектирования

- везде, где возможно, держать `tenant_id` и `workspace_id`;
- использовать `RLS` с самого начала;
- не хранить PII в runtime-таблицах;
- не складывать большие бинарные файлы в БД;
- называть статусы явно и конечным набором;
- все внешние отправки делать через `outbox_messages`.
- capability checks делать через `tenant_features`, а не через billing-логику.
