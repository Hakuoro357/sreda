# Inbound Security и Secure Storage

Этот документ фиксирует первый реализованный слой безопасности для входящих сообщений `Telegram` в `Sreda`.

## Что реализовано

- входящий `Telegram webhook` теперь сохраняется не только как технический факт, но и как безопасно разделенные данные;
- raw payload сообщения больше не хранится в operational-таблицах в открытом виде;
- перед любым будущим вызовом `LLM` уже доступен `sanitized` текст, прошедший через `privacy guard`;
- для хранения чувствительных данных добавлен минимальный `secure storage` слой;
- webhook уже можно использовать как безопасную основу для onboarding нового пользователя.

## Новые сущности

### `secure_records`

Назначение:
- хранить raw payload входящих сообщений;
- хранить чувствительные данные отдельно от operational state;
- не давать LLM и обычным operational-таблицам доступ к исходному тексту.

Поля:
- `id`
- `tenant_id`
- `workspace_id`
- `record_type`
- `record_key`
- `encrypted_json`
- `created_at`

### `inbound_messages`

Назначение:
- хранить безопасную operational-проекцию входящего сообщения;
- связывать событие с `tenant / workspace / user`;
- хранить только `sanitized` текст и технические метаданные.

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

## Текущий flow webhook

1. `Telegram` присылает `webhook payload`
2. система извлекает `chat_id` и текст сообщения
3. raw payload сохраняется в `secure_records`
4. текст прогоняется через regex-based `privacy guard`
5. в `inbound_messages` сохраняются:
- `sanitized_text`
- флаг `contains_sensitive_data`
- ссылка на `secure_record_id`
6. raw текст не попадает в operational storage в открытом виде
7. onboarding flow может безопасно создать базовый tenant/workspace/user bundle без отправки raw текста в `LLM`

## Шифрование

Реализовано реальное симметричное шифрование:
- `AES-GCM`
- ключ берется из `SREDA_ENCRYPTION_KEY`

Поддержка ключа:
- `base64url` 32 байта
- `hex` 32 байта
- если формат произвольный, он нормализуется через `SHA-256`

Важно:
- это минимальный рабочий слой для MVP;
- полноценный `KMS / Vault / key rotation` пока не реализованы.

## Где это реализовано в коде

`sreda`:
- `src/sreda/services/privacy_guard.py`
- `src/sreda/services/encryption.py`
- `src/sreda/services/secure_storage.py`
- `src/sreda/services/inbound_messages.py`
- `src/sreda/api/routes/telegram_webhook.py`
- `src/sreda/db/models/core.py`
- `migrations/versions/20260325_0003_add_inbound_and_secure_storage.py`

## Что это уже защищает

- raw входящий текст пользователя не живет в открытом виде в operational-таблицах;
- будущий assistant flow сможет работать с `sanitized` текстом;
- чувствительные фрагменты вроде телефона, email, пароля и токенов вырезаются до вызова `LLM`;
- сохраняется трассировка входящего события без раскрытия полного raw текста в operational DB.

## Что пока не закрыто

- нет полноценного `secure access policy` на чтение `secure_records`;
- нет `Vault / KMS`;
- нет ротации ключей;
- нет redaction для всех логов;
- нет full inbound assistant flow;
- onboarding есть, но full assistant flow поверх inbound еще не реализован;
- нет автоматического удаления/TTL для secure payload;
- нет отдельной модели consent/approval для чтения secure raw данных.

## Практический вывод

На текущем этапе `Sreda` получила минимально рабочий безопасный inbound-layer:
- raw payload хранится отдельно и шифруется;
- operational слой получает только sanitized-представление;
- архитектура теперь готова к следующему шагу: запуску пользовательского assistant flow без отправки сырых сообщений в `LLM`.
