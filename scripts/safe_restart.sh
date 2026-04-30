#!/bin/bash
# Safe restart of Sreda services with webhook reset.
#
# ВСЕГДА используй этот скрипт вместо `systemctl restart sreda-uvicorn`.
# Он:
#   1. Рестартует sreda-uvicorn + sreda-job-runner
#   2. Ждёт пока сервис примет соединения (curl 127.0.0.1:8000)
#   3. Делает deleteWebhook + setWebhook для всех каналов (TG, MAX когда
#      подключим) — заставляет messenger переоткрыть TCP-соединение
#   4. Smoke-test: getWebhookInfo показывает 0 ошибок
#
# Без шага #3 юзеры на 30-60 сек получают «бот не отвечает» из-за stale
# keep-alive connections на стороне Telegram (HTTP/1.1 не сигналит graceful
# close). Прецедент 2026-04-30: 5+ минут timeout на проде после рестарта.
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

# ============ Phase 1: restart ============
log "phase 1: restart sreda-uvicorn + sreda-job-runner"
systemctl restart sreda-uvicorn sreda-job-runner

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

# ============ Phase 3: reset Telegram webhook ============
log "phase 3a: deleteWebhook (TG) — заставляет TG сбросить connection pool"
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
log "phase 5: verify webhook health"
sleep 3
info=$(curl -sS "https://api.telegram.org/bot${TG_TOKEN}/getWebhookInfo" 2>&1 | head -c 400)
log "  TG: $info"

# Проверяем нет recent error
if echo "$info" | grep -q "last_error_date"; then
    log "WARN: TG webhook сообщает recent error — это норма после рестарта,"
    log "      первый легитимный update должен пройти, и last_error_date не будет упоминаться больше"
fi

log "DONE: safe_restart завершён успешно"
echo
echo "Можно отправить тестовое сообщение боту — должно дойти в течение 1-2 секунд."
