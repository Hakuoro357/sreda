# LangGraph EDS Monitor Data Model

## Цель

Зафиксировать модель данных `eds_monitor` в `PostgreSQL`, сохранив текущую operational semantics.

## Главная сущность

Для `eds_monitor` отдельная operational единица:
- `eds_account`

Это не заменяет `tenant` и `workspace`, а живет внутри них.

## 1. Site Accounts

### `site_accounts`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `assistant_id`
- `site_key`
- `account_key`
- `label`
- `owner_user_id`
- `owner_chat_id`
- `login`
- `status`
- `created_at`
- `updated_at`

`account_key` это аналог текущего `accountId`.

## 2. Site Credentials

### `site_account_credentials`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `credential_type`
- `ciphertext`
- `iv`
- `key_version`
- `updated_at`

Пароль и чувствительные credential значения должны быть отдельно от основной account metadata.

## 3. Site Sessions

### `site_account_sessions`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `storage_state_path`
- `status`
- `last_authenticated_at`
- `last_role_activation_at`
- `updated_at`

Если позже понадобится хранить session blob в БД, можно заменить `storage_state_path` на `encrypted_blob_id`.

## 4. Poll Runs

### `site_poll_runs`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `status`
- `started_at`
- `finished_at`
- `claims_seen`
- `new_claims_count`
- `changed_claims_count`
- `unchanged_claims_count`
- `last_error`

## 5. Claim State Snapshot

### `site_claim_state`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `claim_id`
- `first_seen_at`
- `last_seen_at`
- `status`
- `status_name`
- `changed_at`
- `deadline_at`
- `ext_id`
- `fingerprint_hash`
- `updated_at`

Это перенос текущего `state-store` в БД.

## 6. Claim Change Events

### `site_change_events`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `poll_run_id`
- `claim_id`
- `change_type`
- `previous_fingerprint_hash`
- `current_fingerprint_hash`
- `status`
- `status_name`
- `has_new_response`
- `requires_user_action`
- `processed_at`
- `created_at`

`change_type`:
- `new_claim`
- `status_changed`
- `new_response`
- `user_action_changed`
- `other_significant_change`

Важно:
- полные payload заявки здесь не хранятся.

## 7. Delivery Decisions

### `site_delivery_decisions`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `poll_run_id`
- `claim_id`
- `should_send`
- `reason_code`
- `message_hash`
- `photo_count`
- `created_at`

## 8. Delivery Records

### `site_delivery_records`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `claim_id`
- `recipient_chat_id`
- `text_message_id`
- `photo_message_ids_json`
- `last_message_hash`
- `updated_at`

Уникальность:
- unique по `site_account_id + claim_id + recipient_chat_id`

Это прямой перенос смысла текущего `delivery-state.mjs`.

## 11. Scheduling

### `site_poll_schedules`

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `site_account_id`
- `enabled`
- `interval_minutes`
- `next_run_at`
- `last_run_at`
- `updated_at`

## Что важно не потерять при переносе

### 1. State hash не равен full normalized claim hash

Текущий `state-store` сравнивает только значимые operational поля.
Это правильно, потому что:
- не каждое cosmetic изменение должно считаться новым событием;
- хеш должен отражать то, что реально влияет на уведомления.

### 2. Delivery record хранится отдельно от анализа

Это тоже правильно:
- анализ можно пересчитать;
- delivery надо хранить как отдельный факт отправки.

### 3. Session слой не должен течь в LLM

Никакие storage state, cookies и session identifiers не должны попадать:
- ни в graph state;
- ни в analysis payload;
- ни в audit logs в открытом виде.

### 4. Полная заявка не хранится

Нормализованная полная карточка:
- может существовать только transiently в момент обработки;
- не должна сохраняться в таблицах платформы;
- не должна попадать в долговременные checkpoints monitoring flow.
