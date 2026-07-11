#!/bin/bash
# Safe restart of Sreda services with long-poll reset.
#
# ВСЕГДА используй этот скрипт вместо `systemctl restart sreda-uvicorn`.
# Он:
#   1. Рестартует sreda-uvicorn + sreda-job-runner
#   2. Ждёт пока сервис примет соединения (curl 127.0.0.1:8000)
#   3. Long-poll режим (единственный прод-режим с 2026-04-30):
#        - deleteWebhook для каждого настроенного бота
#        - рестарт всех активных sreda-telegram-poller@<bot_key>.service
#        - проверка heartbeat по каждому каналу telegram:<bot_key>
#        - НИКОГДА не setWebhook (setWebhook блокирует getUpdates 409 Conflict)
#   4. MAX webhook reset (когда подключим)
#   5. Smoke-test
#
# Webhook-режим УДАЛЁН. После инцидента 2026-04-30 прод работает ТОЛЬКО
# через long-poll. Функция restore_webhook.py требует явного
# --force-webhook-mode и откажет если поллер активен.
#
# Прецеденты:
#   2026-04-30: 5+ мин timeout после рестарта из-за stale TG keep-alive.
#   2026-05-07: безусловный setWebhook сломал prod long-poll (409 Conflict).
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

# Основной бот — всегда присутствует
TG_TOKEN_SREDA=$(grep "^SREDA_TELEGRAM_BOT_TOKEN=" "$ENV_FILE" | cut -d= -f2-)
# Второй бот — опциональный
TG_TOKEN_HOME=$(grep "^SREDA_HOME_BOT_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")

MAX_TOKEN=$(grep "^SREDA_MAX_BOT_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
MAX_SECRET=$(grep "^SREDA_MAX_WEBHOOK_SECRET_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")

if [ -z "$TG_TOKEN_SREDA" ]; then
    log "FATAL: SREDA_TELEGRAM_BOT_TOKEN не найден в $ENV_FILE"
    exit 1
fi

# ============ Build per-bot lists ============
# BOT_KEYS: пробел-разделённый список bot_key'ов для которых есть токен.
# BOT_TOKENS_<key>: токен бота (не логируем).
BOT_KEYS="sreda"
BOT_TOKEN_sreda="$TG_TOKEN_SREDA"

if [ -n "$TG_TOKEN_HOME" ]; then
    BOT_KEYS="sreda sreda_home"
    BOT_TOKEN_sreda_home="$TG_TOKEN_HOME"
    log "Second bot sreda_home токен настроен — будет включён в restart."
else
    log "SREDA_HOME_BOT_TOKEN не задан — только бот sreda."
fi

# ============ Phase 1: restart uvicorn + job-runner ============
log "phase 1: restart sreda-uvicorn + sreda-job-runner"
systemctl restart sreda-uvicorn sreda-job-runner
# Поллеры рестартуем в Phase 3 после deleteWebhook чтобы не словить 409.

# ============ Phase 2: wait for ready ============
# Таймаут готовности uvicorn — РЕАЛЬНОЕ wall-clock время (дедлайн по $SECONDS),
# а не число проб. На ХОЛОДНОМ старте (после деплоя) lifespan-init грузит модель
# bge-m3 / llm-trace writer и блокирует accept >30s — наблюдали 34s при деплое
# #187 (2026-06-22); старый лимит 30s давал ложный FATAL и обрывал скрипт до
# Phase 3 (deleteWebhook + рестарт поллеров). Тёплый рестарт — 3–6s, проба выходит
# сразу при готовности, потолок штатных рестартов не замедляет.
#
# Конфигурируется через UVICORN_READY_TIMEOUT в $ENV_FILE (как токены) или из
# окружения (если sudo пробрасывает); приоритет: env-файл → окружение → дефолт.
# Берём ПОСЛЕДНЮЮ запись из env-файла и чистим пробелы/CR. Невалидное значение
# (не целое 1..READY_TIMEOUT_MAX) → дефолт + WARN.
READY_TIMEOUT_DEFAULT=120
READY_TIMEOUT_MAX=3600
_rt_envfile=$(grep "^UVICORN_READY_TIMEOUT=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '[:space:]' || echo "")
READY_TIMEOUT="${_rt_envfile:-${UVICORN_READY_TIMEOUT:-$READY_TIMEOUT_DEFAULT}}"
# regex ограничивает 1..9999 (≤4 цифр — нет переполнения), затем -gt отсекает >MAX.
if ! printf '%s' "$READY_TIMEOUT" | grep -Eq '^[1-9][0-9]{0,3}$' || [ "$READY_TIMEOUT" -gt "$READY_TIMEOUT_MAX" ]; then
    log "phase 2: WARN: UVICORN_READY_TIMEOUT='${READY_TIMEOUT}' невалиден (ожидается целое 1..${READY_TIMEOUT_MAX}) — использую дефолт ${READY_TIMEOUT_DEFAULT}s"
    READY_TIMEOUT=$READY_TIMEOUT_DEFAULT
fi

log "phase 2: ждём пока uvicorn примет соединения (max ${READY_TIMEOUT}s)"
phase2_start=$SECONDS
phase2_deadline=$((phase2_start + READY_TIMEOUT))
ready=false
probe=0
code="000"
while [ "$SECONDS" -lt "$phase2_deadline" ]; do
    probe=$((probe + 1))
    # /webhooks/telegram/sreda без secret-token должен вернуть 401 (auth pipeline активен)
    # или 404 если secret не настроен и bot_key неизвестен реестру — любое значение
    # кроме 000/500/502 означает что uvicorn жив.
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 \
                -X POST -H "Content-Type: application/json" -d "{}" \
                "http://127.0.0.1:${SREDA_PORT}/webhooks/telegram/sreda" 2>/dev/null || echo "000")
    if [ "$code" = "401" ] || [ "$code" = "404" ] || [ "$code" = "202" ] || [ "$code" = "422" ]; then
        log "phase 2: ready за $((SECONDS - phase2_start))s (uvicorn вернул ${code}, проба ${probe})"
        ready=true
        break
    fi
    sleep 1
done
if [ "$ready" = "false" ]; then
    log "FATAL: uvicorn не поднялся за $((SECONDS - phase2_start))s (лимит ${READY_TIMEOUT}s, проб: ${probe}), последний код=${code}"
    # `|| true`: под set -euo pipefail `systemctl status` на упавшем юните вернёт
    # non-zero и pipefail увёл бы exit-код не в 2 (harness это ловит).
    systemctl status sreda-uvicorn --no-pager 2>&1 | tee -a "$LOG" || true
    exit 2
fi

# ============ Phase 3: deleteWebhook per bot + restart pollers ============
log "phase 3: long-poll reset для всех ботов"

for bot_key in $BOT_KEYS; do
    # Получить токен по bot_key (indirect variable reference, POSIX-safe через eval)
    eval "bot_token=\$BOT_TOKEN_${bot_key}"
    if [ -z "$bot_token" ]; then
        log "  WARN: токен для ${bot_key} пустой — пропускаем deleteWebhook"
        continue
    fi

    log "  phase 3a [${bot_key}]: deleteWebhook (long-poll mode)"
    del_resp=$(curl -sS -X POST "https://api.telegram.org/bot${bot_token}/deleteWebhook" 2>&1 | head -c 200)
    log "    → $del_resp"
done

sleep 2  # дать TG обработать deleteWebhook для всех ботов

for bot_key in $BOT_KEYS; do
    unit="sreda-telegram-poller@${bot_key}.service"

    # Проверяем что юнит существует и был enabled/active до рестарта.
    # ВАЖНО: `systemctl cat` резолвит TEMPLATE-инстанс (например
    # sreda-telegram-poller@sreda.service) через его @.service-файл.
    # `list-unit-files <instance>` НЕ матчит имя инстанса (только сам
    # template-файл) → раньше установленные инстансы ошибочно считались
    # «не установлен» и поллеры НЕ рестартовались при каждом прогоне.
    if ! systemctl cat "$unit" >/dev/null 2>&1; then
        log "  phase 3b [${bot_key}]: юнит ${unit} не установлен — пропускаем"
        continue
    fi

    # Рестартуем только если юнит enabled ИЛИ уже был active
    if systemctl is-enabled "$unit" >/dev/null 2>&1 \
       || systemctl is-active "$unit" >/dev/null 2>&1; then
        log "  phase 3b [${bot_key}]: restart ${unit}"
        systemctl reset-failed "$unit" 2>/dev/null || true
        systemctl restart "$unit"
        sleep 2
        if systemctl is-active "$unit" >/dev/null 2>&1; then
            log "    → ${unit} активен"
        else
            log "FATAL: ${unit} не стартанул"
            systemctl status "$unit" --no-pager | tee -a "$LOG"
            exit 3
        fi
    else
        log "  phase 3b [${bot_key}]: ${unit} не enabled и не active — пропускаем"

        # Fallback: если есть только старый non-template юнит (pre-cutover),
        # рестарт его для совместимости.
        if [ "$bot_key" = "sreda" ]; then
            if systemctl is-enabled sreda-telegram-poller.service >/dev/null 2>&1 \
               || systemctl is-active sreda-telegram-poller.service >/dev/null 2>&1; then
                log "  phase 3b [legacy]: restart sreda-telegram-poller.service (pre-cutover fallback)"
                systemctl reset-failed sreda-telegram-poller 2>/dev/null || true
                systemctl restart sreda-telegram-poller
                sleep 2
                if systemctl is-active sreda-telegram-poller >/dev/null 2>&1; then
                    log "    → sreda-telegram-poller (legacy) активен"
                else
                    log "FATAL: sreda-telegram-poller (legacy) не стартанул"
                    systemctl status sreda-telegram-poller --no-pager | tee -a "$LOG"
                    exit 3
                fi
            fi
        fi
    fi
done

# ============ Phase 4: reset MAX webhook (если настроен) ============
if [ -n "$MAX_TOKEN" ]; then
    # #214: адрес MAX API — из env (дефолт platform-api2.max.ru).
    MAX_BASE="${SREDA_MAX_API_BASE_URL:-https://platform-api2.max.ru}"
    MAX_BASE="${MAX_BASE%/}"  # снять хвостовой слэш (как max_base_url() в коде)
    # #214 (Codex R3): зеркалим Python-allowlist (Settings._validate_max_api_base_url) —
    # токен (Authorization) шлём ТОЛЬКО на известные хосты MAX. Плохой/опечатанный
    # env → fail-closed: пропускаем MAX webhook, токен НЕ уходит на чужой хост.
    case "$MAX_BASE" in
        https://platform-api2.max.ru|https://platform-api.max.ru)
            # TLS-доверие Минцифры — бандл из репо (тот же, что max_ssl_context в
            # коде): platform-api2 выпущен Минцифры (нет в системном хранилище) →
            # без --cacert curl упадёт на verify.
            _MAX_CERTS="$(cd "$(dirname "$0")/.." && pwd)/src/sreda/integrations/max/certs"
            MAX_CA="/tmp/sreda_max_ca_$$.pem"
            if cat "$_MAX_CERTS/russian_trusted_root_ca.pem" \
                   "$_MAX_CERTS/russian_trusted_sub_ca_ssl_rsa2024.pem" > "$MAX_CA" 2>/dev/null; then
                CA_OPT="--cacert $MAX_CA"
            else
                CA_OPT=""
                log "  ВНИМАНИЕ: бандл Минцифры не собран ($_MAX_CERTS) — verify к platform-api2 не пройдёт"
            fi

            log "phase 4a: deleteWebhook (MAX @ ${MAX_BASE})"
            max_del=$(curl -sS $CA_OPT -X DELETE "${MAX_BASE}/subscriptions" \
                          -H "Authorization: ${MAX_TOKEN}" 2>&1 | head -c 200 || echo "skip")
            log "  → $max_del"

            sleep 2

            log "phase 4b: setWebhook (MAX) — пропущен, добавится когда настроим webhook URL"
            # TODO: после настройки MAX webhook URL раскомментировать (использует ${MAX_BASE} + $CA_OPT).
            # #341 (F1, CRITICAL): НЕ регистрировать MAX webhook без секрета — иначе
            # роут принимал бы неаутентифицированный inbound (fail-open класс). При
            # раскомментировании ОБЯЗАТЕЛЬНО сохранить guard [ -n "$MAX_SECRET" ] ниже:
            # if [ -z "$MAX_SECRET" ]; then
            #     log "  ОТКАЗ: SREDA_MAX_WEBHOOK_SECRET_TOKEN пуст — setWebhook пропущен (fail-open guard #341)"
            # else
            #     curl -sS $CA_OPT -X POST "${MAX_BASE}/subscriptions" \
            #         -H "Authorization: ${MAX_TOKEN}" \
            #         -H "Content-Type: application/json" \
            #         -d "{\"url\":\"https://bot.sredaspace.ru/webhooks/max/sreda\",\"secret\":\"${MAX_SECRET}\",\"update_types\":[\"message_created\",\"message_callback\",\"bot_started\"]}"
            # fi

            rm -f "$MAX_CA"
            ;;
        *)
            log "phase 4: SREDA_MAX_API_BASE_URL='${MAX_BASE}' вне allowlist (platform-api2.max.ru / platform-api.max.ru) — MAX webhook ПРОПУЩЕН (fail-closed, токен не шлём)"
            ;;
    esac
else
    log "phase 4: MAX токен не настроен — skip"
fi

# ============ Phase 5: verify ============
log "phase 5: verify TG health per bot"
sleep 3

all_ok=true
for bot_key in $BOT_KEYS; do
    eval "bot_token=\$BOT_TOKEN_${bot_key}"
    if [ -z "$bot_token" ]; then
        continue
    fi
    info=$(curl -sS "https://api.telegram.org/bot${bot_token}/getWebhookInfo" 2>&1 | head -c 400)
    log "  [${bot_key}] getWebhookInfo: $info"

    # В long-poll режиме webhook URL ДОЛЖЕН быть пустым
    if echo "$info" | grep -q '"url":""'; then
        log "  ✓ [${bot_key}] webhook url пустой, long-poll функционирует"
    else
        log "FATAL: [${bot_key}] long-poll режим, но webhook URL не пустой — getUpdates будет 409"
        all_ok=false
    fi
done

if [ "$all_ok" = "false" ]; then
    exit 4
fi

# Проверяем что поллеры живы (per-bot).
# Токен присутствует → поллер ОБЯЗАН быть активен.
# «Не установлен» для бота с токеном — FATAL: inbound мёртв.
for bot_key in $BOT_KEYS; do
    unit="sreda-telegram-poller@${bot_key}.service"
    legacy_unit="sreda-telegram-poller.service"

    # Проверяем templated юнит
    if systemctl is-active "$unit" >/dev/null 2>&1; then
        log "  ✓ ${unit} active"
        continue
    fi

    # Fallback: проверяем legacy юнит для sreda
    if [ "$bot_key" = "sreda" ] && systemctl is-active "$legacy_unit" >/dev/null 2>&1; then
        log "  ✓ ${legacy_unit} (legacy) active"
        continue
    fi

    # Токен настроен → поллер обязан быть активен.
    # Отсутствие или неактивность юнита = FATAL (inbound мёртв для этого бота).
    log "FATAL: поллер для ${bot_key} не активен после рестарта (токен настроен, поллер обязателен)"
    exit 5
done

# ============ Phase 6: onboarding smoke (отчёт, НЕ гейт) ============
# Мера B пост-мортема vex-assistant#331 (#334): после рестарта гоняем сквозной
# онбординг-smoke по каждому тиру (крэш free-тира был невидим канарейке основного
# бота). ТОЛЬКО ОТЧЁТ — exit-код safe_restart НЕ меняет: деплой ручной, решение
# принимает оператор по PASS/FAIL ниже. Скрипт сам чистит свой синтетический
# тенант и не трогает реальные данные (ownership-пруф, см. шапку onboard_smoke.py).
SMOKE="$(cd "$(dirname "$0")" && pwd)/onboard_smoke.py"
if [ -f "$SMOKE" ]; then
    for bot_key in $BOT_KEYS; do
        log "phase 6 [${bot_key}]: онбординг-smoke"
        smoke_rc=0
        sudo -u sreda /opt/sreda/.venv/bin/python "$SMOKE" --bot-key "$bot_key" 2>&1 | tee -a "$LOG" || smoke_rc=$?
        case "$smoke_rc" in
            0) log "  ✓ [${bot_key}] онбординг-smoke PASS" ;;
            1) log "  ✗ [${bot_key}] онбординг-smoke FAIL — онбординг СЛОМАН, разберись прежде чем считать деплой успешным" ;;
            2) log "  ⚠ [${bot_key}] онбординг-smoke ABORT/остаток — нужно ручное внимание (см. вывод выше)" ;;
            *) log "  ⚠ [${bot_key}] онбординг-smoke неожиданный код ${smoke_rc}" ;;
        esac
    done
else
    log "phase 6: ${SMOKE} не найден — онбординг-smoke пропущен"
fi

log "DONE: safe_restart завершён успешно"
echo
echo "Можно отправить тестовое сообщение боту — должно дойти в течение 1-2 секунд."

# ============ Cutover sequence (оператор, при деплое фазы 8) ============
#
# Один раз при переходе со старого sreda-telegram-poller.service
# на templated sreda-telegram-poller@<bot_key>.service:
#
#   # 1. Установить шаблонный unit (уже есть в deploy/systemd/)
#   sudo cp deploy/systemd/sreda-telegram-poller@.service /etc/systemd/system/
#   sudo systemctl daemon-reload
#
#   # 2. Проверить конфиг до старта (getMe + token uniqueness)
#   sudo -u sreda /opt/sreda/.venv/bin/python \
#       -m sreda.workers.telegram_long_poll --bot-key sreda --check-config
#
#   # 3. Остановить и замаскировать старый non-template unit
#   sudo systemctl disable --now sreda-telegram-poller.service
#   sudo systemctl mask sreda-telegram-poller.service
#
#   # 4. Включить templated units
#   sudo systemctl enable --now sreda-telegram-poller@sreda.service
#
#   # 5. (Опционально) Второй бот — только если SREDA_HOME_BOT_TOKEN задан
#   #    и --check-config прошёл:
#   sudo -u sreda /opt/sreda/.venv/bin/python \
#       -m sreda.workers.telegram_long_poll --bot-key sreda_home --check-config
#   sudo systemctl enable --now sreda-telegram-poller@sreda_home.service
#
#   # 6. Проверить что старый юнит неактивен
#   systemctl is-active sreda-telegram-poller.service && echo "FAIL — должен быть inactive" || echo "OK"
#
#   # 7. Убедиться что нет getUpdates 409 (оба поллера отвечают разными токенами)
#   journalctl -u sreda-telegram-poller@sreda -n 20
#   journalctl -u sreda-telegram-poller@sreda_home -n 20
#
# После cutover'а этот скрипт автоматически обнаружит templated units и
# рестартует их вместо legacy unit.
