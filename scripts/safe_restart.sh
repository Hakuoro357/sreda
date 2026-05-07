#!/bin/bash
# Safe restart of Sreda services with webhook/long-poll reset.
#
# ВСЕГДА используй этот скрипт вместо `systemctl restart sreda-uvicorn`.
# Он:
#   1. Рестартует sreda-uvicorn + sreda-job-runner (+ sreda-telegram-poller
#      если активен в long-poll-режиме)
#   2. Ждёт пока сервис примет соединения (curl 127.0.0.1:8000)
#   3. Auto-detect TG режима:
#        - long-poll (sreda-telegram-poller active) → ТОЛЬКО deleteWebhook,
#          никогда не setWebhook (иначе getUpdates даст 409 Conflict).
#        - webhook → deleteWebhook + setWebhook (force TG переоткрыть TCP)
#   4. MAX webhook reset (когда подключим)
#   5. Smoke-test
#
# Без шага #3 юзеры на 30-60 сек получают «бот не отвечает» из-за stale
# keep-alive connections на стороне Telegram (HTTP/1.1 не сигналит graceful
# close). Прецедент 2026-04-30: 5+ минут timeout на проде после рестарта.
#
# Прецедент 2026-05-07: безусловный setWebhook сломал prod long-poll режим
# (sreda-telegram-poller exited code=3, "409 Conflict: an active webhook
# is still set"). Auto-detection ниже это предотвращает.
#
# Usage:
#   sudo ./scripts/safe_restart.sh

set -euo pipefail

ENV_FILE=/etc/sreda/.env
LOG=/var/log/sreda/safe_restart.log
SREDA_PORT=8000

ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

# Прочитать env (mode 0640, sreda group, root readable)
if [ ! -r "$ENV_FILE" ]; then
    log "FATAL: $ENV_FILE не читается. Запусти под sudo."
    exit 1
fi

TG_TOKEN=$(grep "^SREDA_TELEGRAM_BOT_TOKEN=" "$ENV_FILE" | cut -d= -f2-)
TG_SECRET=$(grep "^SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN=" "$ENV_FILE" | cut -d= -f2-)
MAX_TOKEN=$(grep "^SREDA_MAX_BOT_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
MAX_SECRET=$(grep "^SREDA_MAX_WEBHOOK_SECRET_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")

if [ -z "$TG_TOKEN" ]; then
    log "FATAL: SREDA_TELEGRAM_BOT_TOKEN не найден в $ENV_FILE"
    exit 1
fi

# ============ Detect TG mode: long-poll vs webhook ============
# Если sreda-telegram-poller enabled+active → long-poll режим. setWebhook
# в этом случае ломает getUpdates (409 Conflict). См. инцидент 2026-05-07.
TG_MODE="webhook"  # default
if systemctl list-unit-files sreda-telegram-poller.service >/dev/null 2>&1; then
    if systemctl is-enabled sreda-telegram-poller.service >/dev/null 2>&1 \
       || systemctl is-active sreda-telegram-poller.service >/dev/null 2>&1; then
        TG_MODE="long-poll"
    fi
fi
log "TG mode detected: ${TG_MODE}"

# ============ Phase 1: restart ============
if [ "$TG_MODE" = "long-poll" ]; then
    log "phase 1: restart sreda-uvicorn + sreda-job-runner + sreda-telegram-poller"
    systemctl restart sreda-uvicorn sreda-job-runner
    # poller рестартуем после deleteWebhook (Phase 3a) — иначе он стартует
    # на 409 Conflict и попадает в restart-loop. См. инцидент 2026-05-07.
else
    log "phase 1: restart sreda-uvicorn + sreda-job-runner"
    systemctl restart sreda-uvicorn sreda-job-runner
fi

# ============ Phase 2: wait for ready ============
log "phase 2: ждём пока uvicorn примет соединения (max 30s)"
for i in $(seq 1 30); do
    sleep 1
    # /webhooks/telegram/sreda без secret-token ОДОЛЖЕН вернуть 401 — это значит
    # uvicorn alive и authentication-pipeline активен. Любой другой ответ
    # (connection refused / 500 / 502) = ещё не готов.
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 \
                -X POST -H "Content-Type: application/json" -d "{}" \
                "http://127.0.0.1:${SREDA_PORT}/webhooks/telegram/sreda" 2>/dev/null || echo "000")
    if [ "$code" = "401" ]; then
        log "phase 2: ready после ${i}s (uvicorn вернул 401, как ожидалось)"
        break
    fi
    if [ $i -eq 30 ]; then
        log "FATAL: uvicorn не поднялся за 30s, последний код=${code}"
        systemctl status sreda-uvicorn --no-pager | tee -a "$LOG"
        exit 2
    fi
done

# ============ Phase 3: reset Telegram (mode-dependent) ============
if [ "$TG_MODE" = "long-poll" ]; then
    log "phase 3a: deleteWebhook (TG, long-poll mode) — обязательно для getUpdates"
    del_resp=$(curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/deleteWebhook" 2>&1 | head -c 200)
    log "  → $del_resp"

    sleep 2  # дать TG обработать deleteWebhook

    log "phase 3b: restart sreda-telegram-poller (после deleteWebhook, чтобы не словить 409)"
    systemctl reset-failed sreda-telegram-poller 2>/dev/null || true
    systemctl restart sreda-telegram-poller
    sleep 3
    if systemctl is-active sreda-telegram-poller >/dev/null 2>&1; then
        log "  → poller активен"
    else
        log "FATAL: sreda-telegram-poller не стартанул"
        systemctl status sreda-telegram-poller --no-pager | tee -a "$LOG"
        exit 3
    fi
else
    log "phase 3a: deleteWebhook (TG, webhook mode) — заставляет TG сбросить connection pool"
    del_resp=$(curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/deleteWebhook" 2>&1 | head -c 200)
    log "  → $del_resp"

    sleep 2  # дать TG обработать

    log "phase 3b: setWebhook (TG)"
    set_resp=$(curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/setWebhook" \
        -d "url=https://bot.sredaspace.ru/webhooks/telegram/sreda" \
        -d "secret_token=${TG_SECRET}" \
        -d "drop_pending_updates=false" \
        -d 'allowed_updates=["message","edited_message","callback_query"]' 2>&1 | head -c 200)
    log "  → $set_resp"
fi

# ============ Phase 4: reset MAX webhook (если настроен) ============
if [ -n "$MAX_TOKEN" ]; then
    log "phase 4a: deleteWebhook (MAX)"
    # MAX API: DELETE /subscriptions с auth header
    max_del=$(curl -sS -X DELETE "https://platform-api.max.ru/subscriptions" \
                  -H "Authorization: ${MAX_TOKEN}" 2>&1 | head -c 200 || echo "skip")
    log "  → $max_del"

    sleep 2

    log "phase 4b: setWebhook (MAX) — пропущен, добавится когда настроим webhook URL"
    # TODO: после настройки MAX webhook URL раскомментировать:
    # curl -sS -X POST "https://platform-api.max.ru/subscriptions" \
    #     -H "Authorization: ${MAX_TOKEN}" \
    #     -H "Content-Type: application/json" \
    #     -d "{\"url\":\"https://bot.sredaspace.ru/webhooks/max/sreda\",\"secret\":\"${MAX_SECRET}\",\"update_types\":[\"message_created\",\"message_callback\",\"bot_started\"]}"
else
    log "phase 4: MAX токен не настроен — skip"
fi

# ============ Phase 5: verify ============
log "phase 5: verify TG health"
sleep 3
info=$(curl -sS "https://api.telegram.org/bot${TG_TOKEN}/getWebhookInfo" 2>&1 | head -c 400)
log "  TG getWebhookInfo: $info"

if [ "$TG_MODE" = "long-poll" ]; then
    # В long-poll режиме URL должен быть ПУСТОЙ (иначе getUpdates даст 409).
    if echo "$info" | grep -q '"url":""'; then
        log "  ✓ webhook url пустой, long-poll функционирует"
    else
        log "FATAL: long-poll режим, но webhook URL не пустой — getUpdates будет 409"
        exit 4
    fi
    # Проверяем что poller всё ещё жив (не upal в restart-loop)
    if systemctl is-active sreda-telegram-poller >/dev/null 2>&1; then
        log "  ✓ sreda-telegram-poller active"
    else
        log "FATAL: sreda-telegram-poller died после рестарта"
        exit 5
    fi
else
    # В webhook режиме проверяем нет recent error
    if echo "$info" | grep -q "last_error_date"; then
        log "WARN: TG webhook сообщает recent error — это норма после рестарта,"
        log "      первый легитимный update должен пройти, и last_error_date не будет упоминаться больше"
    fi
fi

log "DONE: safe_restart завершён успешно"
echo
echo "Можно отправить тестовое сообщение боту — должно дойти в течение 1-2 секунд."
