#!/usr/bin/env bash
# Тесты scripts/safe_restart.sh — инцидент vex-assistant#408 («молчаливый недокат»).
#
# ЧТО ПОКРЫТО:
#   T1  happy path + НЕЗАВИСИМАЯ проверка, что у КАЖДОГО внешнего curl реально
#       стоят --connect-timeout и --max-time (иначе «таймаут» — только на словах)
#   T2  deleteWebhook падает (curl rc≠0) → поллеры ВСЁ РАВНО перезапущены
#   T3  deleteWebhook висит → прогон укладывается в бюджет, поллеры перезапущены
#   T4  ГЕЙТ ловит подделку для КАЖДОГО юнита (поллер / uvicorn / job-runner),
#       а также «стартовал и сразу упал» (новый InvocationID, но не active)
#   T5  РЕГРЕССИЯ (обязательная, не skip): дофиксовая версия на висящем curl +
#       SIGHUP НЕ доходит до рестарта поллеров — воспроизведение инцидента
#   T6  обрыв по SIGHUP → ненулевой код + FAILED + алерт
#   T7  getWebhookInfo недоступен → прогон не валится
#   T8  при провале гейта алерт уходит РОВНО один раз
#   T9  невалидный таймаут (в т.ч. 0 = «без таймаута» у curl) отвергается
#
# ЧЕМ ЭТО НЕ ЯВЛЯЕТСЯ: systemctl/curl/sudo — заглушки. Тест проверяет ЛОГИКУ
# скрипта, а НЕ поведение systemd/сети. Живая проверка гейта против прод-юнитов —
# отдельный шаг приёмки на проде.
#
# Запуск (нужен Linux; на Windows — через WSL). НЕ под root: скрипт намеренно
# запрещает SAFE_RESTART_*-override'ы под root.
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
[ "$(id -u)" -ne 0 ] || { echo "не запускай тесты под root — скрипт запрещает override'ы под root"; exit 1; }

# ---------------------------------------------------------------- workspace
setup_ws() {
    WS=$(mktemp -d)
    BIN="$WS/bin"; STATE="$WS/state"
    mkdir -p "$BIN" "$STATE"
    ENVF="$WS/sreda.env"; LOGF="$WS/safe_restart.log"
    # MAX-токен задан намеренно: иначе phase 4 (и её curl) не выполнялась бы
    # и осталась бы непокрытой.
    printf 'SREDA_TELEGRAM_BOT_TOKEN=tok-sreda\nSREDA_HOME_BOT_TOKEN=tok-home\nSREDA_MAX_BOT_TOKEN=tok-max\n' > "$ENVF"
    : > "$LOGF"; : > "$STATE/restarts"; : > "$STATE/curl_argv"; : > "$STATE/alerts"
    echo 0 > "$STATE/invseq"

    # Стартовое состояние: все юниты уже active со СВОИМ InvocationID
    # (как на живом боксе до деплоя).
    for u in sreda-uvicorn.service sreda-job-runner.service \
             sreda-telegram-poller@sreda.service sreda-telegram-poller@sreda_home.service; do
        kk=$(printf '%s' "$u" | tr -c 'A-Za-z0-9' '_')
        echo "pre-$kk" > "$STATE/inv_$kk"
        echo "active"  > "$STATE/state_$kk"
    done

    # --- systemctl stub -------------------------------------------------
    cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
STATE="$SR_TEST_STATE"
# printf, НЕ echo: `tr -c` превратил бы завершающий \n в '_' и ключ разъехался
# бы с именами файлов, которые готовит тест.
k() { printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'; }
norm() { case "$1" in *.service) printf '%s' "$1";; *) printf '%s.service' "$1";; esac; }
cmd="${1:-}"; shift || true
case "$cmd" in
  restart)
    for u in "$@"; do
      u=$(norm "$u"); kk=$(k "$u")
      echo "$u" >> "$STATE/restarts"
      # frozen = юнит НЕ переактивируется (та самая подделка из инцидента:
      # systemctl отрапортовал успех, но процесс остался прежний)
      [ -f "$STATE/frozen_$kk" ] && continue
      n=$(( $(cat "$STATE/invseq") + 1 )); echo "$n" > "$STATE/invseq"
      echo "inv-$n-$kk" > "$STATE/inv_$kk"
      # crashafter = активация состоялась (новый ID), но служба тут же упала
      if [ -f "$STATE/crashafter_$kk" ]; then echo "failed" > "$STATE/state_$kk"
      else echo "active" > "$STATE/state_$kk"; fi
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
    kk=$(k "$(norm "$unit")")
    case "$prop" in
      InvocationID)         [ -f "$STATE/inv_$kk" ]   && cat "$STATE/inv_$kk"   || echo "";;
      ActiveState)          [ -f "$STATE/state_$kk" ] && cat "$STATE/state_$kk" || echo "inactive";;
      ActiveEnterTimestamp) echo "Tue 2026-07-21 00:00:00 UTC";;
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
  is-enabled)
    u=$(norm "${1:-}"); [ -f "$STATE/absent_$(k "$u")" ] && exit 1; exit 0;;
  is-active)
    u=$(norm "${1:-}"); kk=$(k "$u")
    [ -f "$STATE/absent_$kk" ] && exit 1
    [ "$(cat "$STATE/state_$kk" 2>/dev/null)" = "active" ] && exit 0 || exit 1;;
  reset-failed|daemon-reload) exit 0;;
  status) echo "(stub status ${1:-})"; exit 0;;
  *) exit 0;;
esac
STUB

    # --- curl stub ------------------------------------------------------
    # Записывает КАЖДЫЙ вызов в $STATE/curl_argv — тест потом независимо
    # проверяет наличие таймаут-флагов (без этого «таймаут» не доказан).
    cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *127.0.0.1*) printf '404'; exit 0;;
esac
echo "$args" >> "$SR_TEST_STATE/curl_argv"
maxt=15; prev=""
for a in $args; do [ "$prev" = "--max-time" ] && maxt="$a"; prev="$a"; done
case "$args" in
  *max.ru*)
      case "${SR_MAX_MODE:-ok}" in
        hang) sleep "$maxt"; echo "curl: (28) Operation timed out" >&2; exit 28;;
      esac
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

    cat > "$BIN/sudo" <<'STUB'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in -u) shift 2;; -n|-H) shift;; *) break;; esac
done
exec "$@"
STUB

    cat > "$BIN/hostname" <<'STUB'
#!/usr/bin/env bash
echo "test-host"
STUB

    mkdir -p "$WS/venv/bin"
    # Алерт идёт как `python -c <код>`; phase 6 запускает onboard_smoke.py.
    # Без различения смоук засчитывался бы за алерт.
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

# Запуск в СВОЕЙ группе процессов, чтобы сигнал бил по группе (как SSH шлёт
# HUP всей сессии), а хвосты не оставались висеть.
run_script_bg() {
    setsid env PATH="$BIN:$PATH" \
        SR_TEST_STATE="$STATE" \
        SAFE_RESTART_ENV_FILE="$ENVF" \
        SAFE_RESTART_LOG="$LOGF" \
        SAFE_RESTART_VENV_PYTHON="$WS/venv/bin/python" \
        SAFE_RESTART_TG_CONNECT_TIMEOUT=60 \
        SAFE_RESTART_TG_MAX_TIME=60 \
        "$@" bash "${1:-$TARGET}" >"$WS/stdout" 2>"$WS/stderr" &
    BGPID=$!
}
hup_group() { kill -HUP -"$BGPID" 2>/dev/null || kill -HUP "$BGPID" 2>/dev/null; }
reap_group() { wait "$BGPID" 2>/dev/null; RC=$?; kill -KILL -"$BGPID" 2>/dev/null; return 0; }

restarted() { grep -qx "$1" "$STATE/restarts" 2>/dev/null; }
logged()    { grep -q "$1" "$LOGF" 2>/dev/null; }
freeze()    { touch "$STATE/frozen_$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')"; }
crash()     { touch "$STATE/crashafter_$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_')"; }

# Независимый оракул: КАЖДЫЙ внешний вызов обязан нести оба таймаут-флага.
assert_all_curls_bounded() {
    local missing=0 n=0
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        n=$((n + 1))
        case "$line" in
            *--connect-timeout*--max-time*|*--max-time*--connect-timeout*) ;;
            *) missing=$((missing + 1)); echo "         без таймаута: $line";;
        esac
    done < "$STATE/curl_argv"
    if [ "$n" -eq 0 ]; then bad "$1 (внешних curl не было — проверять нечего)"; return; fi
    [ "$missing" -eq 0 ] && ok "$1 (проверено вызовов: $n)" || bad "$1" "без таймаута: $missing из $n"
}

echo "=== safe_restart.sh — тесты #408 ==="

# ---------------------------------------------------------------- T1
echo "T1: happy path + все curl ограничены таймаутом"
setup_ws
run_script
[ "$RC" -eq 0 ] && ok "exit 0" || bad "exit 0" "RC=$RC; stderr: $(tail -3 "$WS/stderr")"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
logged "гейт пройден" && ok "гейт пройден" || bad "гейт пройден"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер sreda перезапущен" || bad "поллер sreda перезапущен"
restarted "sreda-telegram-poller@sreda_home.service" && ok "поллер sreda_home перезапущен" || bad "поллер sreda_home перезапущен"
assert_all_curls_bounded "у всех внешних curl есть --connect-timeout и --max-time"
teardown_ws

# ---------------------------------------------------------------- T2
echo "T2: deleteWebhook падает — поллеры всё равно перезапускаются"
setup_ws
run_script "$TARGET" env SR_TG_MODE=fail
[ "$RC" -eq 0 ] && ok "exit 0 (сбой TG не валит деплой)" || bad "exit 0" "RC=$RC"
logged "ПРОДОЛЖАЕМ к рестарту поллеров" && ok "залогировано «продолжаем»" || bad "залогировано «продолжаем»"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер sreda перезапущен" || bad "поллер sreda перезапущен"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
teardown_ws

# ---------------------------------------------------------------- T3
echo "T3: deleteWebhook висит — таймаут спасает, укладываемся в бюджет"
setup_ws
t0=$SECONDS
run_script "$TARGET" env SR_TG_MODE=hang SR_MAX_MODE=hang
elapsed=$((SECONDS - t0))
[ "$RC" -eq 0 ] && ok "exit 0 (зависание TG/MAX не валит деплой)" || bad "exit 0" "RC=$RC"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер sreda перезапущен" || bad "поллер sreda перезапущен"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
[ "$elapsed" -lt 60 ] && ok "прогон ограничен по времени (${elapsed}s)" || bad "прогон ограничен по времени" "занял ${elapsed}s"
teardown_ws

# ---------------------------------------------------------------- T4
for victim in sreda-telegram-poller@sreda.service sreda-uvicorn.service sreda-job-runner.service; do
    echo "T4: ГЕЙТ ловит подделку — ${victim} не переактивирован"
    setup_ws
    freeze "$victim"
    run_script
    [ "$RC" -eq 7 ] && ok "exit 7" || bad "exit 7" "RC=$RC"
    logged "InvocationID НЕ изменился" && ok "названа причина (ID не изменился)" || bad "названа причина"
    logged "$victim" && ok "назван виновный юнит" || bad "назван виновный юнит"
    ! logged "DONE: safe_restart завершён успешно" && ok "DONE отсутствует" || bad "DONE отсутствует"
    teardown_ws
done

echo "T4d: ГЕЙТ ловит «стартовал и сразу упал» (новый ID, но не active)"
setup_ws
crash sreda-job-runner.service
run_script
[ "$RC" -eq 7 ] && ok "exit 7" || bad "exit 7" "RC=$RC"
logged "служба упала после старта" && ok "названа причина (упала после старта)" || bad "названа причина"
! logged "DONE: safe_restart завершён успешно" && ok "DONE отсутствует" || bad "DONE отсутствует"
teardown_ws

# ---------------------------------------------------------------- T5
echo "T5: РЕГРЕССИЯ — дофиксовая версия на висящем curl + SIGHUP (обязательный тест)"
setup_ws
OLD="$WS/safe_restart_old.sh"
GOT_OLD=0
if [ -n "${SR_OLD_SCRIPT:-}" ] && [ -f "$SR_OLD_SCRIPT" ]; then
    cp "$SR_OLD_SCRIPT" "$OLD" && GOT_OLD=1
elif git -C "$REPO_ROOT" show origin/main:scripts/safe_restart.sh > "$OLD" 2>/dev/null; then
    GOT_OLD=1
fi
if [ "$GOT_OLD" -eq 1 ]; then
    sed -i \
        -e "s#^ENV_FILE=/etc/sreda/.env#ENV_FILE=$ENVF#" \
        -e "s#^LOG=/var/log/sreda/safe_restart.log#LOG=$LOGF#" \
        -e "s#/opt/sreda/.venv/bin/python#$WS/venv/bin/python#g" \
        "$OLD"
    cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
case "$*" in *127.0.0.1*) printf '404'; exit 0;; esac
echo "$*" >> "$SR_TEST_STATE/curl_argv"
sleep 300
STUB
    chmod +x "$BIN/curl"
    setsid env PATH="$BIN:$PATH" SR_TEST_STATE="$STATE" bash "$OLD" >"$WS/o" 2>&1 &
    BGPID=$!
    sleep 6
    hup_group
    reap_group
    ! restarted "sreda-telegram-poller@sreda.service" \
        && ok "дофиксовая версия: поллер НЕ перезапущен (инцидент воспроизведён)" \
        || bad "дофиксовая версия: поллер НЕ перезапущен" "регрессия не воспроизвелась"
    ! logged "DONE: safe_restart завершён успешно" \
        && ok "дофиксовая версия: DONE отсутствует (молчаливый обрыв)" \
        || bad "дофиксовая версия: DONE отсутствует"
    echo "         (справка: оборвалась с кодом $RC, без единой строки об ошибке)"
else
    bad "дофиксовая версия недоступна" "передай SR_OLD_SCRIPT=<путь> (git show origin/main:scripts/safe_restart.sh)"
fi
teardown_ws

# ---------------------------------------------------------------- T6
echo "T6: обрыв по SIGHUP → ненулевой код + FAILED + алерт"
setup_ws
run_script_bg "$TARGET" SR_TG_MODE=hang
sleep 6
hup_group
reap_group
[ "$RC" -ne 0 ] && ok "ненулевой код возврата ($RC)" || bad "ненулевой код возврата" "0 — обрыв выдал бы себя за успех"
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

# ---------------------------------------------------------------- T8
echo "T8: при провале гейта алерт уходит ровно один раз"
setup_ws
freeze sreda-telegram-poller@sreda.service
run_script
n=$(wc -l < "$STATE/alerts" 2>/dev/null | tr -d ' ')
[ "$n" = "1" ] && ok "ровно один алерт" || bad "ровно один алерт" "отправлено: ${n}"
teardown_ws

# ---------------------------------------------------------------- T9
echo "T9: невалидный таймаут отвергается (0 = «без таймаута» у curl)"
setup_ws
for badval in 0 abc 9999; do
    PATH="$BIN:$PATH" SR_TEST_STATE="$STATE" \
    SAFE_RESTART_ENV_FILE="$ENVF" SAFE_RESTART_LOG="$LOGF" \
    SAFE_RESTART_VENV_PYTHON="$WS/venv/bin/python" \
    SAFE_RESTART_TG_MAX_TIME="$badval" \
    bash "$TARGET" >/dev/null 2>"$WS/e9"
    rc=$?
    [ "$rc" -eq 1 ] && ok "TG_MAX_TIME='${badval}' отвергнут" || bad "TG_MAX_TIME='${badval}' отвергнут" "rc=$rc"
done
teardown_ws

echo
echo "=== итог: успешно $PASS, провалено $FAIL ==="
[ "$FAIL" -eq 0 ] || exit 1
