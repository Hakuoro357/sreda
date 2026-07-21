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
# SR_TARGET — прогнать сьют против ДРУГОЙ копии скрипта (мутационное тестирование)
TARGET="${SR_TARGET:-$REPO_ROOT/scripts/safe_restart.sh}"

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
             sreda-telegram-poller@sreda.service sreda-telegram-poller@sreda_home.service              sreda-telegram-poller.service; do
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
      [ -f "$STATE/noinvafter_$kk" ] && touch "$STATE/noinv_$kk"
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
      InvocationID)
        # noinv_* — systemd «не отвечает» по юниту: фикстура fail-closed
        if [ -f "$STATE/noinv_$kk" ]; then echo ""
        elif [ -f "$STATE/inv_$kk" ]; then cat "$STATE/inv_$kk"
        else echo ""; fi;;
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
      sreda-telegram-poller.service) exit 0;;
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

    # tee-стаб: как настоящий, но по флагу падает ИМЕННО на строке DONE —
    # так проверяется порядок «сначала записать DONE, потом поднять флаг».
    cat > "$BIN/tee" <<'STUB'
#!/usr/bin/env bash
file=""
while [ $# -gt 0 ]; do
  case "$1" in -a) shift;; *) file="$1"; shift;; esac
done
data=$(cat)
if [ "${SR_TEE_FAIL_ON_DONE:-0}" = "1" ]; then
  case "$data" in *"DONE: safe_restart"*) exit 1;; esac
fi
printf '%s
' "$data"
[ -n "$file" ] && printf '%s
' "$data" >> "$file"
exit 0
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
    # фикстура A2: служба падает уже ПОСЛЕ пройденного гейта
    if [ -n "${SR_CRASH_DURING_SMOKE:-}" ]; then
        kk=$(printf '%s' "$SR_CRASH_DURING_SMOKE" | tr -c 'A-Za-z0-9' '_')
        echo "failed" > "$SR_TEST_STATE/state_$kk"
    fi
    [ -n "${SR_SMOKE_SLEEP:-}" ] && sleep "$SR_SMOKE_SLEEP"
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
# enabled, но ОСТАНОВЛЕН: InvocationID пуст, состояние inactive (как на живом боксе)
stopped()   { kk=$(printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'); : > "$STATE/inv_$kk"; echo "inactive" > "$STATE/state_$kk"; }

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
logged "служба НЕ работает" && ok "названа причина (служба не работает)" || bad "названа причина (служба не работает)"
# m-1: отказ обязан быть найден ГЕЙТОМ, до phase 6 — иначе запас обнаружения
# держится на одной лог-строке перепроверки после смоука.
! logged "phase 6" && ok "пойман ГЕЙТОМ, до смоука" || bad "пойман ГЕЙТОМ, до смоука" "дошло до phase 6"
! logged "DONE: safe_restart завершён успешно" && ok "DONE отсутствует" || bad "DONE отсутствует"
teardown_ws

# ---------------------------------------------------------------- T5
echo "T5: РЕГРЕССИЯ — дофиксовая версия на висящем curl + SIGHUP (обязательный тест)"
setup_ws
OLD="$WS/safe_restart_old.sh"
GOT_OLD=0
if [ -n "${SR_OLD_SCRIPT:-}" ] && [ -f "$SR_OLD_SCRIPT" ]; then
    cp "$SR_OLD_SCRIPT" "$OLD" && GOT_OLD=1
elif [ -f "$REPO_ROOT/tests/fixtures/safe_restart_prefix.sh" ]; then
    # Фикстура в репозитории — основной источник. Раньше базлайн добывался через
    # `git show`, и в реальной среде проекта (тест бежит под WSL, а рабочая копия —
    # git-worktree с указателем на windows-путь) git оттуда недоступен: ОБЯЗАТЕЛЬНЫЙ
    # тест-доказательство инцидента просто не запускался. Фикстура убирает зависимость
    # и от git, и от резолва путей. Защита от тавтологии ниже проверяет её так же.
    cp "$REPO_ROOT/tests/fixtures/safe_restart_prefix.sh" "$OLD" && GOT_OLD=1
elif git -C "$REPO_ROOT" show "${SR_OLD_SHA:-74adbfbff62f88edfc53f31f340336a98af70ae6}:scripts/safe_restart.sh" > "$OLD" 2>/dev/null; then
    GOT_OLD=1
fi
# ЗАЩИТА ОТ ТАВТОЛОГИИ (R2 C2 / R3 m-2): базлайн раньше брался из origin/main —
# после мержа это стал бы ИСПРАВЛЕННЫЙ скрипт, и тест «доказывал» бы инцидент на
# самом фиксе. Поэтому базлайн пиним на SHA дофиксового коммита и проверяем его
# содержимое ДВУМЯ независимыми способами: точный sha256 и отсутствие
# InvocationID (второе — читаемая подсказка, если первое разъедется).
# Этот же sha256 снят с ЖИВОГО /opt/sreda/scripts/safe_restart.sh на проде
# 2026-07-21 — то есть фикстура байт-в-байт равна версии, вызвавшей инцидент.
EXPECTED_OLD_SHA256=8921f5108f493dd1aa9d65535fc78057f23aee3eecf99b844f373598a4cd5082
if [ "$GOT_OLD" -eq 1 ]; then
    got_sha=$(sha256sum "$OLD" | cut -d" " -f1)
    if [ "$got_sha" != "$EXPECTED_OLD_SHA256" ]; then
        bad "базлайн T5 совпадает по sha256" "ожидали ${EXPECTED_OLD_SHA256:0:16}…, получили ${got_sha:0:16}…"
        GOT_OLD=0
    else
        ok "базлайн T5 совпадает по sha256 с дофиксовой версией (она же стояла на проде)"
    fi
fi
if [ "$GOT_OLD" -eq 1 ] && grep -q "InvocationID" "$OLD"; then
    bad "базлайн T5 действительно дофиксовый" "в базлайне есть InvocationID — взят ФИКС, тест стал бы тавтологией"
    GOT_OLD=0
fi
if [ "$GOT_OLD" -eq 1 ]; then
    sed -i \
        -e "s#^ENV_FILE=/etc/sreda/.env#ENV_FILE=$ENVF#" \
        -e "s#^LOG=/var/log/sreda/safe_restart.log#LOG=$LOGF#" \
        -e "s#/opt/sreda/.venv/bin/python#$WS/venv/bin/python#g" \
        "$OLD"
    # m-3: фикстура — рабочая копия ДОФИКСОВОГО деплой-скрипта. Без предохранителя
    # случайный `bash` по ней на боксе сделал бы НАСТОЯЩИЙ рестарт служб.
    sed -i "1a # ФИКСТУРА ТЕСТА #408 — НЕ ЗАПУСКАТЬ ВРУЧНУЮ (дофиксовый safe_restart)\n[ -n \"\${SR_FIXTURE_OK:-}\" ] || { echo \"это тестовая фикстура; запуск вне теста запрещён\" >&2; exit 1; }" "$OLD"
    # предохранитель обязан работать: без флага фикстура не должна стартовать
    if bash "$OLD" >/dev/null 2>&1; then
        bad "предохранитель фикстуры" "фикстура запустилась БЕЗ SR_FIXTURE_OK"
    else
        ok "предохранитель фикстуры: без SR_FIXTURE_OK не запускается"
    fi
    cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
case "$*" in *127.0.0.1*) printf '404'; exit 0;; esac
echo "$*" >> "$SR_TEST_STATE/curl_argv"
sleep 300
STUB
    chmod +x "$BIN/curl"
    setsid env PATH="$BIN:$PATH" SR_TEST_STATE="$STATE" SR_FIXTURE_OK=1 bash "$OLD" >"$WS/o" 2>&1 &
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
    # RC строго 129: иначе мгновенная смерть по ДРУГОЙ причине (например
    # нечитаемый env после подмены путей) проглатывалась бы как «инцидент».
    [ "$RC" -eq 129 ]         && ok "дофиксовая версия: умерла именно от SIGHUP (RC=129)"         || bad "дофиксовая версия: RC=129" "получили RC=$RC — базлайн умер не от сигнала"
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

# ---------------------------------------------------------------- T10
# Мутация M4 (sol MAJOR): если гейт и рестарт резолвят юнит РАЗНЫМИ путями,
# legacy-сценарий разъедется — рестартнём legacy, а проверим template.
echo "T10: legacy-сценарий — гейт и рестарт смотрят на ОДИН юнит"
setup_ws
touch "$STATE/absent_sreda_telegram_poller_sreda_service"   # template не установлен
echo "active" > "$STATE/state_sreda_telegram_poller_service" # legacy живой
run_script
[ "$RC" -eq 0 ] && ok "exit 0 (гейт согласован с рестартом)" || bad "exit 0" "RC=$RC; $(tail -3 "$LOGF")"
restarted "sreda-telegram-poller.service" && ok "перезапущен именно legacy-юнит" || bad "перезапущен именно legacy-юнит"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
teardown_ws

# ---------------------------------------------------------------- T11
# Мутация M2: fail-closed на пустом InvocationID. Пустой ответ systemd НЕ должен
# молча проходить — но и не должен выдаваться за доказанный недокат (код 8, не 7).
echo "T11: systemd не отвечает по юниту → «не смогли проверить» (8), а не тишина"
setup_ws
touch "$STATE/noinv_sreda_job_runner_service"
run_script
[ "$RC" -eq 8 ] && ok "пустой снимок ДО → exit 8" || bad "пустой снимок ДО → exit 8" "RC=$RC"
logged "InvocationID пуст" && ok "названо как непроверяемое (ID не читается)" || bad "названо как непроверяемое (ID не читается)"
logged "Откатывать вслепую ОПАСНО" && ok "явно предупреждает против отката" || bad "явно предупреждает против отката"
! logged "DONE: safe_restart завершён успешно" && ok "DONE отсутствует" || bad "DONE отсутствует"
teardown_ws

# Истинный путь M2: снимок ДО есть, а ПОСЛЕ systemd молчит.
setup_ws
touch "$STATE/noinvafter_sreda_job_runner_service"
run_script
[ "$RC" -eq 8 ] && ok "пропал ответ ПОСЛЕ рестарта → exit 8" || bad "пропал ответ ПОСЛЕ рестарта → exit 8" "RC=$RC"
logged "ПРОВЕРИТЬ НЕ СМОГЛИ" && ok "названо как непроверяемое (нет ответа ПОСЛЕ)" || bad "названо как непроверяемое (нет ответа ПОСЛЕ)"
! logged "DONE: safe_restart завершён успешно" && ok "DONE отсутствует" || bad "DONE отсутствует"
teardown_ws

# ---------------------------------------------------------------- T12
# Мутация M7 (terra MAJOR): флаг завершения обязан подниматься ПОСЛЕ записи DONE.
# Роняем tee ровно на строке DONE: durable-строки нет → прогон не вправе
# отрапортоваться успехом молча.
echo "T12: сбой записи DONE не выдаётся за успех"
setup_ws
run_script "$TARGET" env SR_TEE_FAIL_ON_DONE=1
[ "$RC" -ne 0 ] && ok "ненулевой код ($RC)" || bad "ненулевой код" "RC=0 — обрыв выдал себя за успех"
[ -s "$STATE/alerts" ] && ok "алерт отправлен (обрыв не проигнорирован)" || bad "алерт отправлен"
teardown_ws

# ---------------------------------------------------------------- T13
echo "T13: обрыв ПОСЛЕ гейта → «деплой доехал» (код 9), а не «не засчитан»"
setup_ws
run_script "$TARGET" env SR_TEE_FAIL_ON_DONE=1
[ "$RC" -eq 9 ] && ok "exit 9 (доехал, хвост не доиграл)" || bad "exit 9" "RC=$RC"
logged "Активация нового кода ДОКАЗАНА" && ok "сказано, что активация доказана" || bad "сказано, что активация доказана"
logged "откат по причине" && ok "явно сказано, что откат не нужен" || bad "явно сказано, что откат не нужен"
! logged "ДЕПЛОЙ НЕ ЗАСЧИТАН" && ok "НЕ рапортует «не засчитан»" || bad "НЕ рапортует «не засчитан»"
teardown_ws

# ---------------------------------------------------------------- T14
echo "T14: служба упала ПОСЛЕ гейта (во время смоука) → доказанный провал (7)"
setup_ws
run_script "$TARGET" env SR_CRASH_DURING_SMOKE=sreda-job-runner.service
[ "$RC" -eq 7 ] && ok "exit 7" || bad "exit 7" "RC=$RC"
logged "упал ПОСЛЕ пройденного гейта" && ok "названа причина" || bad "названа причина"
teardown_ws

# ---------------------------------------------------------------- T15
# КОНТРАКТ С ВЫЗЫВАЮЩЕЙ СТОРОНОЙ (B1). deploy_private_features.sh зовёт
# safe_restart голым вызовом под `set -e` и на ЛЮБОЙ ненулевой код делает
# `git reset --hard` + повторный рестарт. Значит коды обязаны различать
# «доказанно не доехало» (откат уместен) и «не смогли проверить / доехало»
# (откат ОПАСЕН: откатили бы ЗДОРОВЫЙ прод).
echo "T15: коды возврата различают «не доехало» и «не смогли проверить»"
setup_ws
freeze sreda-telegram-poller@sreda.service
run_script; rc_proven=$RC
teardown_ws
setup_ws
touch "$STATE/noinv_sreda_job_runner_service"
run_script; rc_unverified=$RC
teardown_ws
[ "$rc_proven" -eq 7 ] && ok "доказанный недокат → 7 (откат уместен)" || bad "доказанный недокат → 7" "получили $rc_proven"
[ "$rc_unverified" -eq 8 ] && ok "непроверяемое → 8 (откат запрещён)" || bad "непроверяемое → 8" "получили $rc_unverified"
[ "$rc_proven" -ne "$rc_unverified" ]     && ok "коды РАЗЛИЧАЮТСЯ (вызывающая сторона может не откатывать здоровый прод)"     || bad "коды различаются" "оба $rc_proven — вызывающая сторона откатит здоровый прод"
# Контракт задокументирован в самом скрипте — вызывающей стороне есть на что опереться
grep -q "КОНТРАКТ ДЛЯ ВЫЗЫВАЮЩЕЙ СТОРОНЫ" "$TARGET" && ok "контракт кодов записан в скрипте" || bad "контракт кодов записан в скрипте"

# ---------------------------------------------------------------- T16
echo "T16: токен бота не утекает в лог (stderr curl не логируется)"
setup_ws
run_script "$TARGET" env SR_TG_MODE=fail
! grep -q "tok-sreda" "$LOGF" && ok "токена нет в логе" || bad "токена нет в логе" "токен найден в $LOGF"
teardown_ws

# ---------------------------------------------------------------- T17
echo "T17: оба обращения к Telegram провалились → громкая строка в логе"
setup_ws
run_script "$TARGET" env SR_TG_MODE=fail
logged "ОБА обращения к Telegram провалились" && ok "предупреждение про оба сбоя" || bad "предупреждение про оба сбоя"
teardown_ws

# ---------------------------------------------------------------- T18
# R3 C-2 (CRITICAL): у enabled-но-ОСТАНОВЛЕННОГО юнита InvocationID ПУСТ
# (проверено на проде). Связка «был неактивен → стал active с новым ID» —
# ПОЛОЖИТЕЛЬНОЕ доказательство активации. Прошлая версия отдавала здесь exit 8,
# из-за чего вызывающий откатывал УДАВШИЙСЯ деплой.
echo "T18: поллер был остановлен (enable без --now) → успешный деплой, НЕ откат"
setup_ws
stopped sreda-telegram-poller@sreda.service
run_script
[ "$RC" -eq 0 ] && ok "exit 0 (деплой засчитан)" || bad "exit 0" "RC=$RC — здоровый деплой выдан за провал; $(tail -4 "$LOGF")"
logged "активация ДОКАЗАНА" && ok "названо доказанной активацией" || bad "названо доказанной активацией"
logged "DONE: safe_restart завершён успешно" && ok "есть DONE" || bad "есть DONE"
restarted "sreda-telegram-poller@sreda.service" && ok "поллер поднят" || bad "поллер поднят"
teardown_ws

echo "T18b: то же для uvicorn и job-runner"
for victim in sreda-uvicorn.service sreda-job-runner.service; do
    setup_ws
    stopped "$victim"
    run_script
    [ "$RC" -eq 0 ] && ok "${victim}: exit 0" || bad "${victim}: exit 0" "RC=$RC"
    teardown_ws
done

# ---------------------------------------------------------------- T19
# R3 M-1: бюджет смоука ОБЩИЙ на прогон, а не на каждого бота. Два бота,
# лимит 3с, смоук спит 5с → второй бот обязан быть пропущен по бюджету,
# а не получить свои полные 3с.
echo "T19: бюджет смоука общий на прогон, а не на каждого бота"
setup_ws
t0=$SECONDS
PATH="$BIN:$PATH" SR_TEST_STATE="$STATE" \
  SAFE_RESTART_ENV_FILE="$ENVF" SAFE_RESTART_LOG="$LOGF" \
  SAFE_RESTART_VENV_PYTHON="$WS/venv/bin/python" \
  SAFE_RESTART_TG_CONNECT_TIMEOUT=1 SAFE_RESTART_TG_MAX_TIME=1 \
  SAFE_RESTART_SMOKE_TIMEOUT=3 SR_SMOKE_SLEEP=5 \
  bash "$TARGET" >"$WS/stdout" 2>"$WS/stderr"
RC=$?
elapsed=$((SECONDS - t0))
[ "$RC" -eq 0 ] && ok "exit 0" || bad "exit 0" "RC=$RC"
# Точный оракул «бюджет ОБЩИЙ»: второй бот обязан получить ОСТАТОК, а не свежий
# полный лимит. Либо он вовсе пропущен (бюджет исчерпан) — тоже верно.
first_left=$(grep -o 'осталось [0-9]*s общего бюджета' "$LOGF" | sed -n 1p | grep -o '[0-9]*')
second_left=$(grep -o 'осталось [0-9]*s общего бюджета' "$LOGF" | sed -n 2p | grep -o '[0-9]*')
if logged "исчерпан — ПРОПУСКАЕМ"; then
    ok "второй бот пропущен: общий бюджет исчерпан первым"
elif [ -n "$second_left" ] && [ -n "$first_left" ] && [ "$second_left" -lt "$first_left" ]; then
    ok "бюджет ОБЩИЙ: второму боту достался остаток ${second_left}s из ${first_left}s"
else
    bad "бюджет общий, а не на каждого бота" "первому ${first_left:-?}s, второму ${second_left:-?}s — похоже на лимит на каждого"
fi
[ "$elapsed" -lt 20 ] && ok "прогон ограничен по времени (${elapsed}s)" \
    || bad "прогон ограничен по времени" "занял ${elapsed}s"
teardown_ws

# ---------------------------------------------------------------- T20
# R3 M-2: код 9 не должен обещать «новый код работает», если живучесть не
# подтверждена (обрыв до финальной перепроверки).
echo "T20: код 9 честен про живучесть"
setup_ws
run_script_bg "$TARGET" SR_TG_MODE=hang
sleep 6
hup_group
reap_group
teardown_ws
setup_ws
run_script "$TARGET" env SR_TEE_FAIL_ON_DONE=1
[ "$RC" -eq 9 ] && ok "exit 9 после пройденной перепроверки" || bad "exit 9" "RC=$RC"
logged "Живучесть служб подтверждена" && ok "живучесть названа подтверждённой (перепроверка прошла)" \
    || bad "живучесть названа подтверждённой"
teardown_ws

echo
echo "=== итог: успешно $PASS, провалено $FAIL ==="
[ "$FAIL" -eq 0 ] || exit 1
