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
#   5. Verify + ГЕЙТ по времени старта служб (#408)
#   6. Smoke-test
#
# ГАРАНТИЯ #408: «успех» = дошли до строки `DONE: safe_restart завершён успешно`
# И все службы стартовали позже начала прогона. Любой обрыв (в т.ч. SIGHUP при
# разрыве SSH-сессии деплоя) = ненулевой код возврата + алерт админу.
# Вызывающая сторона ОБЯЗАНА проверять код возврата, а не `systemctl is-active`.
#
# Коды возврата:
#   0 — успех (дошли до DONE, гейт пройден)
#   1 — env-файл не читается / нет токена основного бота
#   2 — uvicorn не поднялся
#   3 — поллер не стартанул
#   4 — webhook URL не пуст (будет 409 Conflict)
#   5 — поллер не активен после рестарта
#   7 — ГЕЙТ не пройден: служба работает на СТАРОМ коде (деплой не доехал)
#  90 — прогон оборван до DONE (сеть/сигнал/убитая сессия)
# 129/143/130 — оборван сигналом HUP/TERM/INT
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

# Пути прод-дефолтами. Переопределяются только через окружение — нужно тестам
# (tests/test_safe_restart_gate.sh гоняет скрипт со stub-ами systemctl/curl).
# Прод-безопасно: скрипт и так запускается под root, а sudo по умолчанию чистит
# окружение (env_reset) — снаружи эти переменные не подставить.
ENV_FILE="${SAFE_RESTART_ENV_FILE:-/etc/sreda/.env}"
LOG="${SAFE_RESTART_LOG:-/var/log/sreda/safe_restart.log}"
VENV_PYTHON="${SAFE_RESTART_VENV_PYTHON:-/opt/sreda/.venv/bin/python}"
SREDA_PORT=8000

ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

# ============ #408: анти-«молчаливый недокат» ============
# Инцидент 2026-07-20 (vex-assistant#408). Phase 3a (deleteWebhook) вызывала curl
# БЕЗ таймаута, а api.telegram.org с этого бокса периодически залипает на connect
# (в логах поллера — ConnectTimeout/ReadTimeout). Скрипт блокировался на curl;
# SSH-сессию деплоя убивал клиентский таймаут (~140s) → SIGHUP → скрипт умирал
# ДО phase 3b, и поллеры оставались на СТАРОМ коде. При этом `systemctl is-active`
# = active, а `git rev-parse HEAD` на диске верный — обе привычные проверки
# ЗЕЛЁНЫЕ. Итог: фиксы #401/#405 не жили у пользователей ~15 часов.
#
# Три меры (по порядку важности):
#   1. ГЕЙТ ПО ВРЕМЕНИ СТАРТА (phase 5b) — главный. Каждый юнит, который прогон
#      обязан был перезапустить, ОБЯЗАН иметь время старта позже старта прогона.
#      Ловит ровно этот класс: «служба active, но процесс старый».
#   2. Внешние curl — с таймаутом и НЕ фатальные. Long-poll reset вспомогателен:
#      сеть до TG не должна блокировать рестарт поллеров.
#   3. Нет финального DONE = ненулевой код + громкий алерт админу (trap на EXIT),
#      включая обрыв по SIGHUP/SIGTERM.

# Таймауты внешних вызовов. Сумма держится заметно ниже клиентского таймаута
# деплой-сессии (~120s), чтобы скрипт успевал доработать и отчитаться САМ.
TG_CONNECT_TIMEOUT="${SAFE_RESTART_TG_CONNECT_TIMEOUT:-5}"
TG_MAX_TIME="${SAFE_RESTART_TG_MAX_TIME:-15}"

# Точка отсчёта гейта. Монотоника (мкс от загрузки) — тот же клок, что у
# systemd *TimestampMonotonic: сравнение целых, без парсинга локале-зависимых
# строк и без чувствительности к прыжкам NTP. Усечение (не округление) — чтобы
# отсечка не оказалась «позже» реального старта прогона.
GATE_START_MONOTONIC_US=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
GATE_START_HUMAN=$(ts)

SAFE_RESTART_COMPLETED=0

# Алерт админу через СУЩЕСТВУЮЩИЙ дуал-канал (#395: Telegram основной + MAX дубль).
# Best-effort: сам никогда не роняет скрипт (мы уже на аварийном пути) и ограничен
# по времени. Текст идёт через stdin, не через argv (argv виден в process list).
alert_admin() {
    local text="$1"
    local py="$VENV_PYTHON"
    if [ ! -x "$py" ]; then
        log "  (алерт пропущен: $py не найден)"
        return 0
    fi
    if printf '%s' "$text" | timeout 30 sudo -u sreda "$py" -c '
import sys, os, asyncio
from pathlib import Path
text = sys.stdin.read()
env_file = "/etc/sreda/.env"
if Path(env_file).exists() and not os.environ.get("SREDA_DATABASE_URL"):
    from dotenv import load_dotenv
    load_dotenv(env_file)
from sreda.services.admin_alerts import alert_admin_async
sys.exit(0 if asyncio.run(alert_admin_async(text)) else 1)
' >/dev/null 2>&1; then
        log "  → алерт админу доставлен"
    else
        log "  → алерт админу НЕ доставлен (best-effort, см. канал вручную)"
    fi
    return 0
}

# Единый выход: всё, что НЕ дошло до финального DONE, — провал деплоя.
# Ловит set -e, явный exit, а также SIGHUP (обрыв SSH-сессии деплоя — механизм
# инцидента #408) и SIGTERM. SIGKILL не перехватывается — там спасает только
# внешняя проверка гейта.
on_exit() {
    local rc=$?
    set +e
    trap - EXIT
    if [ "$SAFE_RESTART_COMPLETED" = "1" ]; then
        exit "$rc"
    fi
    # Оборвались молча и с нулевым кодом — это НЕ успех.
    [ "$rc" -eq 0 ] && rc=90
    log "FAILED: safe_restart ОБОРВАН до DONE (код ${rc}) — ДЕПЛОЙ НЕ ЗАСЧИТАН."
    log "        Службы могли остаться на СТАРОМ коде (uvicorn/job-runner/поллеры)."
    log "        Проверь время старта служб и перезапусти вручную."
    alert_admin "🔴 P0 Среда: safe_restart ОБОРВАН — деплой не доехал

Прогон стартовал: ${GATE_START_HUMAN}
Код возврата: ${rc}
Хост: $(hostname)

Скрипт не дошёл до финального DONE: часть служб могла остаться на СТАРОМ коде,
при этом systemctl is-active показывает active. Проверь:
  systemctl show -p ActiveEnterTimestamp --value sreda-telegram-poller@sreda
Лог: ${LOG}"
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 143' TERM
trap 'exit 130' INT

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
    # #408: таймаут + НЕ фатально. Этот шаг вспомогательный (сброс long-poll
    # состояния на стороне TG) — его сбой НЕ должен мешать phase 3b поднять
    # поллеры на новом коде. Без `|| del_rc=$?` ненулевой curl под
    # `set -euo pipefail` убил бы скрипт МОЛЧА: код подстановки наследуется
    # присваиванием (проверено). Обрезку вынесли из пайпа с curl — иначе
    # SIGPIPE от `head` на длинном ответе даёт 141 и тот же молчаливый выход.
    del_rc=0
    del_resp=$(curl -sS --connect-timeout "$TG_CONNECT_TIMEOUT" --max-time "$TG_MAX_TIME" \
                    -X POST "https://api.telegram.org/bot${bot_token}/deleteWebhook" 2>&1) || del_rc=$?
    del_resp=$(printf '%s' "$del_resp" | head -c 200) || true
    if [ "$del_rc" -eq 0 ]; then
        log "    → $del_resp"
    else
        log "    ⚠ deleteWebhook не удался (curl rc=${del_rc}) — ПРОДОЛЖАЕМ к рестарту поллеров"
        log "      (детали: ${del_resp})"
    fi
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
    # #408: таймаут + не фатально по СЕТИ (прогон 2026-07-20 13:26 умер молча
    # именно здесь — тот же untimed curl).
    info_rc=0
    info=$(curl -sS --connect-timeout "$TG_CONNECT_TIMEOUT" --max-time "$TG_MAX_TIME" \
                "https://api.telegram.org/bot${bot_token}/getWebhookInfo" 2>&1) || info_rc=$?
    info=$(printf '%s' "$info" | head -c 400) || true

    if [ "$info_rc" -ne 0 ]; then
        # Сеть до TG недоступна — проверить нечего, но это НЕ повод валить деплой:
        # реальную гарантию дают проверка активности поллеров ниже и гейт 5b.
        # Непустой webhook всё равно был бы пойман: поллер словил бы 409 и выпал
        # из active (exit 3), а это ловит проверка ниже.
        log "  ⚠ [${bot_key}] getWebhookInfo недоступен (curl rc=${info_rc}) — проверку webhook пропускаем"
        log "    (детали: ${info})"
        continue
    fi

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

# ============ Phase 5b: ГЕЙТ по времени старта служб (#408) ============
# ГЛАВНАЯ проверка недоката, ради неё вся правка. `systemctl is-active` = active
# и верный `git rev-parse HEAD` на диске НЕ доказывают, что процесс перезапустился:
# 2026-07-20 поллеры остались на коде от 18:02, а деплои 19:17 и 21:32 были
# отрапортованы успешными именно по этим двум зелёным признакам.
#
# Инвариант: КАЖДЫЙ юнит, который прогон обязан был перезапустить, стартовал
# ПОЗЖЕ старта прогона. Старт раньше = работает старый процесс = деплой провален.
#
# Гейт стоит ДО phase 6: на старом коде смоук бы прошёл (PASS) и добавил ложной
# уверенности — сначала доказываем, что код вообще активирован.
log "phase 5b: гейт — все службы стартовали в ЭТОМ прогоне?"

# Ожидаемый набор строим из КОНФИГУРАЦИИ (а не из того, что успели рестартнуть),
# чтобы «phase 3b молча пропустил поллер» тоже ловилось.
GATE_UNITS="sreda-uvicorn sreda-job-runner"
for bot_key in $BOT_KEYS; do
    unit="sreda-telegram-poller@${bot_key}.service"
    if systemctl cat "$unit" >/dev/null 2>&1; then
        GATE_UNITS="$GATE_UNITS $unit"
    elif [ "$bot_key" = "sreda" ] && systemctl cat sreda-telegram-poller.service >/dev/null 2>&1; then
        GATE_UNITS="$GATE_UNITS sreda-telegram-poller.service"
    else
        log "FATAL: [${bot_key}] токен настроен, но юнит поллера не установлен — inbound мёртв"
        exit 7
    fi
done

gate_ok=true
for unit in $GATE_UNITS; do
    enter_us=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$unit" 2>/dev/null || echo "")
    enter_human=$(systemctl show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null || echo "?")
    if ! printf '%s' "$enter_us" | grep -Eq '^[0-9]+$' || [ "$enter_us" -eq 0 ]; then
        log "  ✗ ${unit}: времени старта нет (юнит не активен) — деплой не доехал"
        gate_ok=false
        continue
    fi
    if [ "$enter_us" -lt "$GATE_START_MONOTONIC_US" ]; then
        log "  ✗ ${unit}: СТАРТОВАЛ РАНЬШЕ прогона (${enter_human}) — процесс СТАРЫЙ, новый код НЕ активирован"
        gate_ok=false
    else
        log "  ✓ ${unit}: перезапущен в этом прогоне (${enter_human})"
    fi
done

if [ "$gate_ok" = "false" ]; then
    log "FATAL: ГЕЙТ НЕ ПРОЙДЕН — часть служб работает на СТАРОМ коде. ДЕПЛОЙ НЕ ЗАСЧИТАН."
    log "       Это тот самый класс отказа, из-за которого #401/#405 не жили ~15 часов."
    alert_admin "🔴 P0 Среда: деплой НЕ доехал — службы на старом коде

Прогон стартовал: ${GATE_START_HUMAN}
Гейт по времени старта не пройден: часть служб не перезапустилась,
хотя systemctl is-active показывает active.

Юниты прогона: ${GATE_UNITS}
Лог (детали по каждому юниту): ${LOG}"
    exit 7
fi
log "  ✓ гейт пройден: все службы перезапущены этим прогоном"

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
        sudo -u sreda "$VENV_PYTHON" "$SMOKE" --bot-key "$bot_key" 2>&1 | tee -a "$LOG" || smoke_rc=$?
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

# #408: единственная точка, где прогон признаётся успешным. Всё, что не дошло
# сюда, ловит trap on_exit → ненулевой код + алерт админу.
SAFE_RESTART_COMPLETED=1
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
