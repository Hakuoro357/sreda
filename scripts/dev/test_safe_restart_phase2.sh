#!/bin/bash
# Harness for safe_restart.sh Phase 2 (uvicorn readiness wait).
#
# Вырезает РЕАЛЬНЫЙ блок Phase 2 из scripts/safe_restart.sh (awk, без копипасты)
# и прогоняет его с mock-curl/log/systemctl по сценариям:
#   1) дефолт 120s когда UVICORN_READY_TIMEOUT не задан
#   2) override из env-файла (валидное целое)
#   3) невалидное значение → дефолт + WARN
#   4) готовность на N-й пробе → переход к Phase 3 (REACHED_PHASE3, rc=0)
#   5) недостижение готовности → FATAL + exit 2 (real sleep, дедлайн)
#   6) real-time elapsed через $SECONDS (real sleep)
#   7) МЕДЛЕННЫЕ пробы → wall-clock ограничен дедлайном (а не лимит×длительность)
#
# Мок systemctl возвращает non-zero на `status` (как упавший юнит) — проверяет,
# что FATAL-ветка под set -euo pipefail всё равно даёт ровно exit 2.
#
# Не требует прода: systemctl/curl/script-sleep замоканы. curl_delay использует
# `command sleep` (настоящий) для эмуляции медленного accept.
# Запуск: bash scripts/dev/test_safe_restart_phase2.sh
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/safe_restart.sh"
BLOCK=$(mktemp); COUNTER=$(mktemp); ENVF=$(mktemp)
trap 'rm -f "$BLOCK" "$COUNTER" "$ENVF"' EXIT

# Вырезаем блок [Phase 2 header .. перед Phase 3 header]
awk '/# ====+ Phase 2:/{f=1} /# ====+ Phase 3:/{f=0} f' "$SCRIPT" > "$BLOCK"
if ! grep -q 'phase 2: ждём' "$BLOCK"; then
    echo "FAIL: не удалось вырезать блок Phase 2"; exit 1
fi

PASS=0; FAIL=0
check() { # desc, expected_substr (НЕ пустой), actual_text
    if [ -z "$2" ]; then echo "  FAIL: $1 (ошибка теста: пустой ожидаемый substr)"; FAIL=$((FAIL+1)); return; fi
    if echo "$3" | grep -qF "$2"; then echo "  PASS: $1"; PASS=$((PASS+1));
    else echo "  FAIL: $1 (ждали '$2')"; echo "$3" | sed 's/^/      | /'; FAIL=$((FAIL+1)); fi
}
check_absent() { # desc, forbidden_substr (НЕ пустой), actual_text
    if [ -z "$2" ]; then echo "  FAIL: $1 (ошибка теста: пустой forbidden substr)"; FAIL=$((FAIL+1)); return; fi
    if echo "$3" | grep -qF "$2"; then echo "  FAIL: $1 (не должно быть '$2')"; echo "$3" | sed 's/^/      | /'; FAIL=$((FAIL+1));
    else echo "  PASS: $1"; PASS=$((PASS+1)); fi
}
check_rc() { if [ "$2" = "$3" ]; then echo "  PASS: $1 (rc=$3)"; PASS=$((PASS+1));
    else echo "  FAIL: $1 (ждали rc=$2, получили rc=$3)"; FAIL=$((FAIL+1)); fi }
check_le() { # desc, value, upper_bound
    if [ "$2" -le "$3" ]; then echo "  PASS: $1 ($2 <= $3)"; PASS=$((PASS+1));
    else echo "  FAIL: $1 ($2 > $3)"; FAIL=$((FAIL+1)); fi }

# Прогон блока в субшелле с моками.
#   run <envfile> <ready_at(-1=никогда)> <real_sleep(1=настоящий)> <shell_to> <curl_delay_s>
run() {
    local envfile="$1" ready_at="$2" real_sleep="${3:-0}" shell_to="${4:-}" curl_delay="${5:-0}"
    echo 0 > "$COUNTER"
    (
        set -euo pipefail
        LOG=/dev/null; SREDA_PORT=8000; ENV_FILE="$envfile"
        [ -n "$shell_to" ] && export UVICORN_READY_TIMEOUT="$shell_to"
        log() { echo "LOG: $*"; }
        # Упавший юнит: `status` отдаёт non-zero — стережёт `|| true` в FATAL-ветке.
        systemctl() { if [ "${1:-}" = "status" ]; then echo "MOCK systemctl status (failed unit)"; return 3; fi; echo "MOCK systemctl $*"; }
        if [ "$real_sleep" != "1" ]; then sleep() { :; }; fi
        curl() {
            [ "$curl_delay" -gt 0 ] && command sleep "$curl_delay"
            local n; n=$(cat "$COUNTER"); n=$((n+1)); echo "$n" > "$COUNTER"
            if [ "$ready_at" != "-1" ] && [ "$n" -ge "$ready_at" ]; then echo "401"; else echo "000"; fi
        }
        # shellcheck disable=SC1090
        source "$BLOCK"
        echo "REACHED_PHASE3"
    )
}

echo "=== Scenario 1: дефолт 120s (UVICORN_READY_TIMEOUT не задан) ==="
: > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "лог объявляет max 120s" "max 120s" "$out"
check "достиг Phase 3" "REACHED_PHASE3" "$out"
check_rc "успех" 0 "$rc"

echo "=== Scenario 2: override из env-файла (UVICORN_READY_TIMEOUT=7) ==="
printf 'UVICORN_READY_TIMEOUT=7\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "лог объявляет max 7s" "max 7s" "$out"
check "достиг Phase 3" "REACHED_PHASE3" "$out"

echo "=== Scenario 2b: CRLF + дубль в env (последняя строка, \\r вычищен) ==="
printf 'UVICORN_READY_TIMEOUT=99\r\nUVICORN_READY_TIMEOUT=8\r\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "взята последняя запись, max 8s" "max 8s" "$out"
check_absent "не ушёл в WARN/дефолт" "WARN" "$out"

echo "=== Scenario 2c: окружение (env-файл пуст, shell UVICORN_READY_TIMEOUT=8) → max 8s ==="
: > "$ENVF"
out=$(run "$ENVF" 1 0 "8" 0 2>&1); rc=$?
check "shell env применён, max 8s" "max 8s" "$out"

echo "=== Scenario 2d: приоритет env-файл > окружение (файл=7, shell=99) → max 7s ==="
printf 'UVICORN_READY_TIMEOUT=7\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "99" 0 2>&1); rc=$?
check "env-файл побеждает окружение, max 7s" "max 7s" "$out"

echo "=== Scenario 3: невалидное значение → дефолт + WARN ==="
printf 'UVICORN_READY_TIMEOUT=abc\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "WARN о невалидном значении" "WARN: UVICORN_READY_TIMEOUT='abc'" "$out"
check "откат на дефолт 120s" "max 120s" "$out"

echo "=== Scenario 3b: значение > MAX (9999) → дефолт + WARN ==="
printf 'UVICORN_READY_TIMEOUT=9999\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "WARN о превышении MAX" "WARN: UVICORN_READY_TIMEOUT='9999'" "$out"
check "откат на дефолт 120s" "max 120s" "$out"

echo "=== Scenario 3d: граница 3600 (валидно) → max 3600s, без WARN ==="
printf 'UVICORN_READY_TIMEOUT=3600\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "3600 принято, max 3600s" "max 3600s" "$out"
check_absent "без WARN на верхней границе" "WARN" "$out"

echo "=== Scenario 3e: 3601 (за границей) → дефолт + WARN ==="
printf 'UVICORN_READY_TIMEOUT=3601\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "3601 отвергнут" "WARN: UVICORN_READY_TIMEOUT='3601'" "$out"
check "откат на дефолт 120s" "max 120s" "$out"

echo "=== Scenario 3f: граница 1 (валидно) → max 1s, без WARN ==="
printf 'UVICORN_READY_TIMEOUT=1\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "1 принято, max 1s" "max 1s" "$out"
check_absent "без WARN на нижней границе" "WARN" "$out"

echo "=== Scenario 3c: '0' (и пустое после нормализации) → дефолт + WARN ==="
printf 'UVICORN_READY_TIMEOUT=0\n' > "$ENVF"
out=$(run "$ENVF" 1 0 "" 0 2>&1); rc=$?
check "WARN о нуле" "WARN: UVICORN_READY_TIMEOUT='0'" "$out"
check "откат на дефолт 120s" "max 120s" "$out"

echo "=== Scenario 4: готовность на 3-й пробе → real-time лог + Phase 3 ==="
: > "$ENVF"
out=$(run "$ENVF" 3 0 "" 0 2>&1); rc=$?
check "лог 'ready за' + проба 3" "проба 3" "$out"
check "достиг Phase 3" "REACHED_PHASE3" "$out"
check_rc "успех" 0 "$rc"

echo "=== Scenario 5: никогда не готов (timeout=3, real sleep) → FATAL + exit 2 ==="
printf 'UVICORN_READY_TIMEOUT=3\n' > "$ENVF"
set +e; out=$(run "$ENVF" -1 1 "" 0 2>&1); rc=$?; set -e
check "FATAL с лимитом 3s" "лимит 3s" "$out"
check_absent "НЕ достиг Phase 3 (рестарт поллеров не запущен)" "REACHED_PHASE3" "$out"
check "мок systemctl status отработал (non-zero, но не сорвал exit)" "MOCK systemctl status" "$out"
check_rc "exit 2 несмотря на non-zero systemctl status" 2 "$rc"

echo "=== Scenario 6: real elapsed через \$SECONDS (real sleep, ready на 2-й) ==="
: > "$ENVF"
out=$(run "$ENVF" 2 1 "" 0 2>&1); rc=$?
if echo "$out" | grep -qE 'ready за [1-9][0-9]*s'; then echo "  PASS: elapsed>=1s измерен ($(echo "$out" | grep -oE 'ready за [0-9]+s'))"; PASS=$((PASS+1));
else echo "  FAIL: elapsed не измерен"; echo "$out" | sed 's/^/      | /'; FAIL=$((FAIL+1)); fi

echo "=== Scenario 7: медленные пробы (curl ~2s) + timeout=4 → wall-clock ограничен дедлайном ==="
printf 'UVICORN_READY_TIMEOUT=4\n' > "$ENVF"
t0=$SECONDS
set +e; out=$(run "$ENVF" -1 1 "" 2 2>&1); rc=$?; set -e
el=$((SECONDS - t0))
check "FATAL по дедлайну (лимит 4s)" "лимит 4s" "$out"
check_rc "exit 2" 2 "$rc"
# Старый seq-цикл занял бы ~4проб × (sleep1+curl2)=~12s; дедлайн-цикл ≤ лимит+одна проба (~7s).
check_le "wall-clock ограничен дедлайном (не лимит×длительность)" "$el" 9

echo
echo "===== ИТОГ: PASS=$PASS FAIL=$FAIL ====="
[ "$FAIL" -eq 0 ]
