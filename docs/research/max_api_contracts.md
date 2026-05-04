# MAX Bot API — конкретные контракты (Phase 0 findings)

**Дата probe:** 2026-05-04 (commit'ом запишем после live testing).

Источники: docs-api, docs/chatbots/bots-coding/prepare, webapps/introduction +
эмпирический probe `scripts/dev/probe_max_api.py`.

## Установлено probe

### 1. Authentication

- **Header:** `Authorization: <token>` (БЕЗ `Bearer`)
- Подтверждено: `GET /me` → 200 OK с настоящим `SREDA_MAX_BOT_TOKEN`

### 2. `GET /me` response shape

```json
{
  "user_id": ...,
  "first_name": "Среда",
  "username": "id320700072280_bot",
  "is_bot": true,
  "last_activity_time": 1777906707976,
  "description": "Персональный ассистент, который ведёт семейный быт за вас...",
  "avatar_url": "https://i.oneme.ru/i?r=...",
  "full_avatar_url": "https://i.oneme.ru/i?r=...",
  "name": "Среда. Семейный AI-ассистент"
}
```

Поля: `user_id`, `first_name`, `username`, `is_bot`, `last_activity_time`
(unix-ms), `description`, `avatar_url`, `full_avatar_url`, `name`.

**Заметка:** username = `id<bot_id>_bot`, не arbitrary handle. В deep-link
используется именно `id<bot_id>_bot`.

### 3. `GET /updates?limit=N&timeout=T` response shape

```json
{
  "updates": [],
  "marker": 45118903
}
```

- `marker` — server-side cursor, аналог TG `update_id`. Передаётся обратно
  как `?marker=<value>` в следующий poll → server возвращает только updates
  после этого markera.
- `timeout` — long-poll seconds. `timeout=0` → non-blocking (immediate return).
  Default около 30s.
- Empty `updates` если новых событий нет.

### 4. `GET /subscriptions` shape

```json
{
  "subscriptions": []
}
```

Пусто потому что webhook не зарегистрирован (long-poll бот в dev до этого).
В Phase 5 при `set_webhook` row появится.

### 5. Network/transport

- **Endpoint:** `https://platform-api.max.ru/`
- **DNS:** RU (нужен в `NO_PROXY` чтобы не идти через SOCKS5 → egress 89.110.77.78
  → обратно в RU)
- **Latency:** ~120-180ms direct из VDS
- **HTTPS:** валидный сертификат, не self-signed для основного API
  (self-signed разрешён ТОЛЬКО для нашего webhook URL)

## Требует Boris-action для дополнительного probe

### A. Update / Message structure

Чтобы получить пример incoming Update, нужно:

1. Boris открывает MAX → находит бота `id320700072280_bot` (или `https://max.ru/id320700072280_bot`)
2. Пишет любое сообщение, например «привет»
3. Запускаем probe снова — увидим в `updates` array реальный объект

Что проверяем:
- Поля `from` (user_id, first_name, username?)
- Поле `chat_id` или эквивалент (для отправки ответа)
- Тип update (`message_created`?)
- Текст сообщения (`text` / `body.text`?)
- `phone` поле если есть (чтобы понять можно ли использовать для
  identity matching — пока не используем, но useful info)

### B. `start_param` delivery test

1. Boris открывает `https://max.ru/id320700072280_bot?startapp=test_first`
   из MAX (новый юзер для бота — если бот не знает Boris, fire `bot_started`)
2. Probe → видим update с `start_param=test_first`
3. Boris ещё раз открывает с `?startapp=test_second` (теперь existing user)
4. Probe → проверяем какой event приходит:
   - Опять `bot_started`?
   - `message_created` с какой-то особенностью?
   - Что-то третье?

Это критично для Phase 7 channel linking — мы должны ловить `start_param`
независимо от того, новый юзер или existing.

### C. POST /subscriptions с `secret_token`

Реальный test нужно делать одновременно с регистрацией webhook'а
(Phase 5). Сейчас ждём.

### D. POST /messages с разными форматами recipient

Тоже Phase 2 (когда напишем MaxClient) — нельзя без MAX user_id Boris'а.

### E. callback_data size limit

После `MaxClient` готов — отправить test message с inline-button и
варьировать callback_data длину. TG лимит: 64 bytes. MAX — uncertain.

## Decision (Phase 0 lock-in)

**Webhook auth:** ждём probe `secret_token` поддержки в Phase 5.
Если поддерживается → используем (стандарт TG-style header).
Если нет → HMAC-SHA256(body, key=bot_token), header `X-Sreda-MAX-Signature`.

**`recipient` формат:** ждём probe в Phase 2.
Гипотеза: `{"chat_id": <int>}` или `{"user_id": <int>}`. Может combo.

**`max_chat_id` миграция:** **скорее всего НЕ нужна**, потому что юзер_id
== chat_id для DM. Подтверждаем в Phase 2 при первом send.

**Mini-app SDK URL:** надо найти на business.max.ru → партнёр-портал.

## Status

- ✅ Auth, /me, /updates, /subscriptions structure
- ✅ Endpoint reachable direct из VDS (NO_PROXY required)
- ⏳ Update / Message shape — нужен Boris-message в МАКС
- ⏳ start_param delivery — нужны два Boris-tap
- ⏳ recipient format — Phase 2
- ⏳ secret_token support — Phase 5
- ⏳ callback_data limit — Phase 2 send-test
