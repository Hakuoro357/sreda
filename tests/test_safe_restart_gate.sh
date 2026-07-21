#!/usr/bin/env bash
# Тесты scripts/safe_restart.sh — инцидент vex-assistant#408 («молчаливый недокат»).
#
# ЧТО ПОКРЫТО:
#   T1  happy path: все службы перезапущены → exit 0 + DONE + гейт пройден
#   T2  deleteWebhook падает (curl rc≠0) → поллеры ВСЁ РАВНО перезапущены, exit 0
#   T3  deleteWebhook висит (curl упирается в --max-time) → то же самое
#   T4  ГЕЙТ ловит подделку: поллер не перезапустился, но active → exit 7, нет DONE
#   T5  РЕГРЕССИЯ: старая версия скрипта на висящем curl + SIGHUP НЕ доходит до
#       рестарта поллеров (воспроизводит инцидент), новая — доходит
#   T6  обрыв по SIGHUP → ненулевой код + FAILED в логе + попытка алерта
#   T7  getWebhookInfo недоступен (phase 5) → прогон не валится
#
# ЧЕМ ЭТО НЕ ЯВЛЯЕТСЯ: systemctl/curl/sudo здесь — заглушки. Тест проверяет
# ЛОГИКУ скрипта (порядок фаз, обработку сбоев, арифметику гейта), а НЕ реальное
# поведение systemd/сети. Живая проверка гейта против прод-юнитов — отдельный шаг
# приёмки, руками владельца.
#
# Запуск (нужен Linux; на Windows — через WSL):
#   bash tests/test_safe_restart_gate.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="$REPO_ROOT/scripts/safe_restart.sh"

PASS=0
FAIL=0
ok()  { echo "  [ok]   $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; [ -n "${2:-}" ] && echo "         $2"; FAIL=$((FAIL + 1)); }

[ -f "$TARGET" ] || { echo "не найден $TARGET"; exit 1; }
[ -r /proc/uptime ] || { echo "нужен Linux с /proc/uptime (на Windows запускай через WSL)"; exit 1; }

now_us() { awk '{printf "%d", $1 * 1000000}' /proc/uptime; }

# ---------------------------------------------------------------- workspace
# Создаёт песочницу: stub-бинарники в PATH, фейковый env-файл, свой лог.
setup_ws() {
    WS=$(mktemp -d)
    BIN="$WS/bin"; STATE="$WS/state"
    mkdir -p "$BIN" "$STATE"
    ENVF="$WS/sreda.env"; LOGF="$WS/safe_restart.log"
    printf 'SREDA_TELEGRAM_BOT_TOKEN=tok-sreda\nSREDA_HOME_BOT_TOKEN=tok-home\n' > "$ENVF"
    : > "$LOGF"
    : > "$STATE/restarts"

    # --- systemctl stub -------------------------------------------------
    # Модель: время старта юнита лежит в $STATE/start_<unit>. `restart`
    # обновляет его на «сейчас» — кроме юнитов, помеченных frozen_<unit>
    # (это и есть подделка «active, но процесс старый» из инцидента).
    cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
STATE="$SR_TEST_STATE"
# printf, НЕ echo: `tr -c` превратил бы завершающий перевод строки в '_'
# и ключ разъехался бы с именами файлов, которые готовит тест.
k() { printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'; }
nowus() { awk '{printf "%d", $1 * 1000000}' /proc/uptime; }
norm() { case "$1" in *.service) echo "$1";; *) echo "$1.service";; esac; }
cmd="${1:-}"; shift || true
case "$cmd" in
  restart)
    for u in "$@"; do
      u=$(norm "$u")
      [ -f "$STATE/frozen_$(k "$u")" ] || nowus > "$STATE/start_$(k "$u")"
      echo "$u" >> "$STATE/restarts"
    done
    ;;
  show)
    prop=""; unit=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -p) prop="$2"; shift 2;;
        --value) shift;;
        *) unit="$1"; shift;;
      esac
    done
    unit=$(norm "$unit")
    f="$STATE/start_$(k "$unit")"
    if [ ! -f "$f" ]; then echo ""; exit 0; fi
    case "$prop" in
      ActiveEnterTimestampMonotonic) cat "$f";;
      ActiveEnterTimestamp) echo "Tue 2026-07-21 00:00:00 UTC (mono=$(cat "$f"))";;
      *) echo "";;
    esac
    ;;
  cat)
    u=$(norm "${1:-}")
    [ -f "$STATE/absent_$(k "$u")" ] && exit 1
    case "$u" in
      sreda-uvicorn.service|sreda-job-runner.service|sreda-telegram-poller@*.service) exit 0;;
      *) exit 1;;
    esac
    ;;
  is-enabled|is-active)
    u=$(norm "${1:-}")
    [ -f "$STATE/absent_$(k "$u")" ] && exit 1
    [ -f "$STATE/inactive_$(k "$u")" ] && exit 1
    exit 0
    ;;
  reset-failed|daemon-reload) exit 0;;
  status) echo "(stub status ${1:-})"; exit 0;;
  *) exit 0;;
esac
STUB

    # --- curl stub ------------------------------------------------------
    # SR_TG_MODE: ok | fail | hang. Локальный health-probe (127.0.0.1) всегда
    # отвечает 404, иначе скрипт не пройдёт phase 2.
    cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *127.0.0.1*) printf '404'; exit 0;;
esac
# вытащить --max-time для правдоподобной эмуляции таймаута
maxt=15
prev=""
for a in $args; do [ "$prev" = "--max-time" ] && maxt="$a"; prev="$a"; done
case "$args" in
  *platform-api2.max.ru*|*platform-api.max.ru*)
      echo '{"code":"proto.payload","message":"Missing required parameter: url"}'; exit 0;;
esac
case "${SR_TG_MODE:-ok}" in
  fail) echo "curl: (6) Could not resolve host: api.telegram.org" >&2; exit 6;;
  hang) sleep "$maxt"; echo "curl: (28) Operation timed out" >&2; exit 28;;
esac
case "$args" in
  *getWebhookInfo*)
      [ "${SR_TG_INFO_MODE:-ok}" = "fail" ] && { echo "curl: (28) timed out" >&2; exit 28; }
      echo '{"ok":true,"result":{"url":"","pending_update_count":0}}'; exit 0;;
  *deleteWebhook*)
      echo '{"ok":true,"result":true,"description":"Webhook is already deleted"}'; exit 0;;
esac
echo '{"ok":true}'; exit 0
STUB

    # --- sudo stub: просто выполняет команду, отбрасывая -u <user> --------
    cat > "$BIN/sudo" <<'STUB'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in -u) shift 2;; -n) shift;; *) break;; esac
done
exec "$@"
STUB

    cat > "$BIN/hostname" <<'STUB'
#!/usr/bin/env bash
echo "test-host"
STUB

    # --- фейковый venv-python: изображает канал алертов, пишет факт вызова -
    mkdir -p "$WS/venv/bin"
    # Важно различать два вызова python: алерт идёт через `-c <код>`, а phase 6
    # запускает onboard_smoke.py. Без различения смоук засчитывался бы за алерт.
    cat > "$WS/venv/bin/python" <<'STUB'
#!/usr/bin/env bash
if [ "${1:-}" = "-c" ]; then
    cat > "$SR_TEST_STATE/alert_body"
    echo "alerted" >> "$SR_TEST_STATE/alerts"
else
    echo "[smoke stub] PASS"
fi
exit 0
STUB

    chmod +x "$BIN"/* "$WS/venv/bin/python"
}

teardown_ws() { [ -n "${WS:-}" ] && rm -rf "$WS"; }

# Запуск целевого скрипта в песочнице. Возвращает код в RC.
run_script() {
    local script="${1:-$TARGET}"; shift || true
    PATH="$BIN:$PATH" \
    SR_TEST_STATE="$STATE" \
    SAFE_RESTART_ENV_FILE="$ENVF" \
    SAFE_RESTART_LOG="$LOGF" \
    SAFE_RESTART_VENV_PYTHON="$WS/venv/bin/python" \
    SAFE_RESTART_TG_CONNECT_TIMEOUT=1 \
    SAFE_RESTART_TG_MAX_TIME=1 \
    "$@" bash "$script" >"$WS/stdout" 2>"$WS/stderr"
    RC=$?
    return 0
}

restarted() { grep -qx "$1" "$STATE/restarts" 2>/dev/null; }
logged()    { grep -q "$1" "$LOGF" 2>/dev/null; }

echo "=== safe_restart.sh — тесты #408 ==="

# ---------------------------------------------------------------- T1
echo "T1: happy path"
setup_ws
run_script
[ "$RC" -eq 0 ] && ok "exit 0" || bad "exit 0" "получили RC=$RC; stderr: $(tail -3 "$WS/stderr")"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
logged "гейт пройден" && ok "гейт пройден" || bad "гейт пройден"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер sreda перезапущен" || bad "поллер sreda перезапущен"
restarted "sreda-telegram-poller@sreda_home.service" && ok "поллер sreda_home перезапущен" || bad "поллер sreda_home перезапущен"
teardown_ws

# ---------------------------------------------------------------- T2
echo "T2: deleteWebhook падает — поллеры всё равно должны перезапуститься"
setup_ws
run_script "$TARGET" env SR_TG_MODE=fail
[ "$RC" -eq 0 ] && ok "exit 0 (сбой TG не валит деплой)" || bad "exit 0" "RC=$RC"
logged "ПРОДОЛЖАЕМ к рестарту поллеров" && ok "залогировано «продолжаем»" || bad "залогировано «продолжаем»"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер sreda перезапущен" || bad "поллер sreda перезапущен"
restarted "sreda-telegram-poller@sreda_home.service" && ok "поллер sreda_home перезапущен" || bad "поллер sreda_home перезапущен"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
teardown_ws

# ---------------------------------------------------------------- T3
echo "T3: deleteWebhook висит — таймаут спасает, поллеры перезапускаются"
setup_ws
run_script "$TARGET" env SR_TG_MODE=hang
[ "$RC" -eq 0 ] && ok "exit 0 (зависание TG не валит деплой)" || bad "exit 0" "RC=$RC"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер sreda перезапущен" || bad "поллер sreda перезапущен"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
teardown_ws

# ---------------------------------------------------------------- T4
echo "T4: ГЕЙТ ловит подделку (поллер active, но НЕ перезапущен)"
setup_ws
# поллер sreda «заморожен»: restart его не двигает, время старта — из прошлого
touch "$STATE/frozen_sreda_telegram_poller_sreda_service"
echo "1000" > "$STATE/start_sreda_telegram_poller_sreda_service"
run_script
[ "$RC" -eq 7 ] && ok "exit 7 (гейт не пройден)" || bad "exit 7" "RC=$RC"
logged "СТАРТОВАЛ РАНЬШЕ прогона" && ok "назван виновный юнит" || bad "назван виновный юнит"
logged "ГЕЙТ НЕ ПРОЙДЕН" && ok "явная ошибка в логе" || bad "явная ошибка в логе"
! logged "DONE: safe_restart завершён успешно" && ok "DONE отсутствует" || bad "DONE отсутствует"
[ -s "$STATE/alerts" ] && ok "алерт админу отправлен" || bad "алерт админу отправлен"
teardown_ws

# ---------------------------------------------------------------- T5
echo "T5: РЕГРЕССИЯ — старая версия на висящем curl + SIGHUP не доходит до поллеров"
setup_ws
OLD="$WS/safe_restart_old.sh"
# SR_OLD_SCRIPT — путь к ДОфиксовой версии скрипта. Нужен, когда git недоступен
# из среды запуска (типично для WSL: .git worktree ссылается на windows-путь).
# Извлечь заранее: git show origin/main:scripts/safe_restart.sh > /tmp/old.sh
if { [ -n "${SR_OLD_SCRIPT:-}" ] && [ -f "$SR_OLD_SCRIPT" ] && cp "$SR_OLD_SCRIPT" "$OLD"; } \
   || git -C "$REPO_ROOT" show origin/main:scripts/safe_restart.sh > "$OLD" 2>/dev/null; then
    # старая версия не знает про SAFE_RESTART_* — подменяем пути прямой заменой
    sed -i \
        -e "s#^ENV_FILE=/etc/sreda/.env#ENV_FILE=$ENVF#" \
        -e "s#^LOG=/var/log/sreda/safe_restart.log#LOG=$LOGF#" \
        -e "s#/opt/sreda/.venv/bin/python#$WS/venv/bin/python#g" \
        "$OLD"
    # curl «висит» неограниченно: старый вызов без --max-time
    cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
case "$*" in *127.0.0.1*) printf '404'; exit 0;; esac
sleep 300
STUB
    chmod +x "$BIN/curl"

    PATH="$BIN:$PATH" SR_TEST_STATE="$STATE" bash "$OLD" >"$WS/o" 2>&1 &
    OLDPID=$!
    sleep 6
    kill -HUP "$OLDPID" 2>/dev/null
    wait "$OLDPID" 2>/dev/null; OLDRC=$?

    ! restarted "sreda-telegram-poller@sreda.service" \
        && ok "старая версия: поллер НЕ перезапущен (инцидент воспроизведён)" \
        || bad "старая версия: поллер НЕ перезапущен" "поллер перезапустился — регрессия не воспроизвелась"
    ! logged "DONE: safe_restart завершён успешно" \
        && ok "старая версия: DONE отсутствует (молчаливый обрыв)" \
        || bad "старая версия: DONE отсутствует"
    echo "         (справка: старая версия оборвалась с кодом $OLDRC, без единой строки об ошибке)"
else
    echo "  [skip] origin/main недоступен — регрессионное сравнение пропущено"
fi
teardown_ws

# ---------------------------------------------------------------- T6
echo "T6: обрыв по SIGHUP → ненулевой код + FAILED + алерт"
setup_ws
# curl висит долго, чтобы успеть послать сигнал (max-time большой)
run_bg() {
    PATH="$BIN:$PATH" SR_TEST_STATE="$STATE" \
    SAFE_RESTART_ENV_FILE="$ENVF" SAFE_RESTART_LOG="$LOGF" \
    SAFE_RESTART_VENV_PYTHON="$WS/venv/bin/python" \
    SAFE_RESTART_TG_MAX_TIME=60 SAFE_RESTART_TG_CONNECT_TIMEOUT=60 \
    SR_TG_MODE=hang bash "$TARGET" >"$WS/stdout" 2>"$WS/stderr" &
    BGPID=$!
}
run_bg
sleep 6
kill -HUP "$BGPID" 2>/dev/null
wait "$BGPID" 2>/dev/null; RC=$?
[ "$RC" -ne 0 ] && ok "ненулевой код возврата ($RC)" || bad "ненулевой код возврата" "получили 0 — обрыв выдал бы себя за успех"
logged "FAILED: safe_restart ОБОРВАН" && ok "громкая строка FAILED в логе" || bad "громкая строка FAILED в логе"
[ -s "$STATE/alerts" ] && ok "алерт админу отправлен" || bad "алерт админу отправлен"
teardown_ws

# ---------------------------------------------------------------- T7
echo "T7: getWebhookInfo недоступен → прогон не валится"
setup_ws
run_script "$TARGET" env SR_TG_INFO_MODE=fail
[ "$RC" -eq 0 ] && ok "exit 0" || bad "exit 0" "RC=$RC"
logged "getWebhookInfo недоступен" && ok "предупреждение залогировано" || bad "предупреждение залогировано"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
teardown_ws

echo
echo "=== итог: успешно $PASS, провалено $FAIL ==="
[ "$FAIL" -eq 0 ] || exit 1
