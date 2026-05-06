# Channel-linking MVP — smoke test matrix

## Pre-flight

- [ ] All unit tests pass: `pytest tests/unit/test_*.py tests/integration/test_channel_linking_mvp.py`
- [ ] Migration 0040 applied: `alembic current` shows 20260506_0040
- [ ] Settings on prod: `SREDA_TELEGRAM_BOT_USERNAME=sreda01_bot`,
  `SREDA_TELEGRAM_MINIAPP_SHORTNAME=sreda_app`
- [ ] Existing channel_link_tokens с source_user_id=NULL invalidated
  (migration runs UPDATE)

## Smoke cases

### Case 1: TG → MAX happy path (no prior MAX tenant)

Boris's tenant_tg_352612382 (user has telegram=40921122, no max).

1. Open Sreda mini-app в TG via Menu Button
2. Dashboard renders. Look for «🅼 Привязать MAX» card
3. Tap card → backend POST /start → returns deep_link
4. Mini-app opens MAX app via bridge.openLink
5. MAX mini-app loads, detects start_param=lnk_X
6. Confirm screen: «Привязать MAX к Sreda юзеру?»
7. Tap Подтвердить → backend POST /consume
8. Success view: «✅ Аккаунты связаны»
9. Tap Продолжить → dashboard re-renders
10. **DB verify**: `SELECT max_account_id, max_chat_id FROM users WHERE id='user_tg_352612382'` → both populated с правильными значениями
11. **AuditLog verify**: `SELECT * FROM audit_log WHERE action='channel_link.attached' ORDER BY created_at DESC LIMIT 1` → metadata имеет target_channel=max
12. **Reverse smoke**: open TG mini-app снова → «Привязать MAX» card НЕ показывается (already linked)
13. Send «привет» в MAX bot → bot отвечает в context tenant_tg_352612382 (его TG-история доступна)

Pass criteria: все 13 шагов ✅

### Case 2: MAX → TG happy path (no prior TG tenant)

Symmetric к Case 1, но source = MAX, target = TG. Use тестовый MAX-only
аккаунт (если есть, иначе skip).

### Case 3: Collision (cross-tenant блок)

Setup: создать тестовый tenant_max_test с user_max_test (max=99999).
Then with Boris's tenant_tg_X try to link target=max account=99999.

1. TG mini-app → Привязать MAX → MAX confirm screen
2. Tap Подтвердить → /consume returns error
   `account_already_registered_separately`
3. UI показывает: «У тебя уже есть отдельный Sreda-аккаунт в MAX.
   Напиши в @sreda_support»
4. **DB verify**: оба tenant'а UNTOUCHED (no destructive merge)

Cleanup: DELETE tenant_max_test после теста.

### Case 4: Token expired (5 min TTL)

1. POST /start → get raw_token
2. Wait 6 minutes (or manually update row.expires_at в DB to past)
3. Open MAX mini-app с deep-link
4. Confirm tap → error «Ссылка истекла или уже использована»

### Case 5: Token replay

1. Successful link (Case 1)
2. Wait — через 1 минуту попробовать снова открыть SAME deep-link
3. Tap Подтвердить → error «Ссылка уже использована»

### Case 6: Cancel button

1. POST /start → opens MAX mini-app с confirm screen
2. Tap Отмена
3. **DB verify**: row.used_at populated (cancelled = invalidated)
4. Try to consume same token → error not_found_or_expired

### Case 7: Cross-tenant /status access (security)

1. Юзер A starts link → token row with tenant_id=A
2. Юзер B опен'ит /status?id=<A's token id> from B's mini-app
3. Backend returns 404 (NO info leak про token's existence)

Verify via curl from prod:
```bash
curl -H "Authorization: tma <B_initData>" \
  "https://miniapp.sredaspace.ru/miniapp/api/v1/channel-link/status?id=<A_token_id>"
```
→ 404

### Case 8: Already-linked /start blocked

1. Successful link (Case 1)
2. Boris в TG mini-app опять нажимает «Привязать MAX» (если card
   re-renders) ИЛИ прямой POST /start
3. Backend returns 409 already_linked
4. UI: card hidden via /account-status

## Post-deploy verification

- [ ] No errors в `journalctl -u sreda-uvicorn --since '5 minutes ago' | grep -i error`
- [ ] No regression on existing TG flow (Boris пишет привычное сообщение в TG bot, получает ответ)
- [ ] No regression on existing MAX flow (если есть тестовый MAX-аккаунт)
- [ ] AuditLog backfill для existing tenants — N/A (нет существующих linked, fresh feature)