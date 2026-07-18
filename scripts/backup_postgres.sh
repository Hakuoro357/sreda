#!/bin/bash
# Sreda PostgreSQL daily backup with AES-256 encryption + 14-day retention.
# Cron: 03:00 UTC daily. Logs to /var/log/sreda/backup.log.
#
# Pipeline: pg_dump (custom format) → integrity-check → gzip → openssl AES-256.
# Output: /var/backups/sreda/sreda-YYYYMMDD-HHMMSS.dump.gz.enc
# Offsite: SREDA_BACKUP_OFFSITE_CMD (env) — см. блок в конце; без неё копии
# живут только на этом хосте (WARN в лог каждый прогон).
#
# Restore (DR):
#   openssl enc -d -aes-256-cbc -pbkdf2 -in sreda-DATE.dump.gz.enc \
#       -out /tmp/sreda-DATE.dump.gz -pass file:/etc/sreda/.backup_key
#   gunzip /tmp/sreda-DATE.dump.gz
#   pg_restore -d sreda_restore --clean --if-exists /tmp/sreda-DATE.dump

set -euo pipefail

# Move to a CWD readable by sreda — иначе `find` в конце ругается
# "Failed to restore initial working directory" если запущено из
# домашней директории root/boris.
cd /tmp

DEST=/var/backups/sreda
LOG=/var/log/sreda/backup.log
RETENTION_DAYS=14
KEY_FILE=/etc/sreda/.backup_key
PGPASSFILE_PATH=/var/lib/sreda/.pgpass
DATE=$(date -u +%Y%m%d-%H%M%S)
DUMP="$DEST/sreda-$DATE.dump"

# Explicit pgpass — sreda user's HOME is not always /var/lib/sreda when
# called from cron, so HOME/.pgpass discovery is unreliable.
export PGPASSFILE="$PGPASSFILE_PATH"

mkdir -p "$DEST"
ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts) $*" >> "$LOG"; }

log "backup start"

# Verify key file exists
if [ ! -r "$KEY_FILE" ]; then
    log "FAIL: encryption key file $KEY_FILE not readable"
    exit 1
fi

# Audit 2026-07-18: на ЛЮБОМ failure-пути ниже (pg_dump/gzip/openssl:
# диск/ключ/сеть) не оставляем plaintext-дампы и partial-артефакты в $DEST.
DUMP_GZ="$DUMP.gz"
DUMP_ENC="$DUMP_GZ.enc"
cleanup_on_error() { rm -f "$DUMP" "$DUMP_GZ" "$DUMP_ENC"; }
trap cleanup_on_error ERR

# pg_dump custom format. -Z 0 disables internal compression — gzip after.
# .pgpass provides credentials. --no-owner/--no-acl makes restore portable.
pg_dump -F c -Z 0 -d sreda \
    --host=127.0.0.1 --port=5432 --username=sreda \
    --no-password --no-owner --no-acl \
    --file="$DUMP"

# Integrity check via pg_restore --list (reads custom-format header)
if ! pg_restore --list "$DUMP" > /dev/null 2>&1; then
    # Audit 2026-07-18: plaintext-дамп НЕ оставляем (legacy-поведение —
    # переименование в «corrupt»-файл — держало plaintext вечно) —
    # удаляем артефакт, алертим через лог.
    log "INTEGRITY FAIL: pg_restore --list rejected dump — removing plaintext artefact"
    rm -f "$DUMP"
    exit 1
fi

# Compress + encrypt в один pipe (no temp file with plaintext)
gzip -9 "$DUMP"

openssl enc -aes-256-cbc -pbkdf2 -salt \
    -in "$DUMP_GZ" \
    -out "$DUMP_ENC" \
    -pass "file:$KEY_FILE"

trap - ERR

# Удаляем plain gzip — оставляем только encrypted
rm -f "$DUMP_GZ"

# Retention cleanup
find "$DEST" -name 'sreda-*.dump.gz.enc' -mtime +$RETENTION_DAYS -delete
# Defense-in-depth (audit 2026-07-18): plaintext-остатки failed-прогонов
# (.dump / .dump.gz, включая легаси-артефакты) не живут дольше суток.
find "$DEST" \( -name 'sreda-*.dump' -o -name 'sreda-*.dump.gz' \) -mtime +1 -delete

SIZE=$(stat -c '%s' "$DUMP_ENC")
COUNT=$(ls -1 "$DEST"/sreda-*.dump.gz.enc 2>/dev/null | wc -l)
log "backup ok: sreda-$DATE.dump.gz.enc size=${SIZE}b retained=$COUNT files"

# ============================================================================
# Offsite-копия (audit 2026-07-18: раньше ВСЕ бэкапы жили на этом же VDS —
# отказ диска/взлом = потеря БД и бэкапов одновременно).
#
# Настройка: SREDA_BACKUP_OFFSITE_CMD в /etc/sreda/.env — shell-команда,
# выполняется через `sh -c` с экспортированным $DUMP_ENC. Примеры:
#   rsync -az --timeout=120 "$DUMP_ENC" backup-host:/srv/sreda-backups/
#   rclone copyto "$DUMP_ENC" remote:sreda-backups/
# Безопасный дефолт: переменная НЕ задана → копия пропускается с WARN в лог
# (локальный бэкап уже сделан; молчать об отсутствии offsite нельзя).
# Провал offsite-копии НЕ валит прогон: локальный зашифрованный бэкап валиден,
# но WARN в логе обязателен.
# ============================================================================
OFFSITE_CMD="${SREDA_BACKUP_OFFSITE_CMD:-}"
if [ -n "$OFFSITE_CMD" ]; then
    if DUMP_ENC="$DUMP_ENC" sh -c "$OFFSITE_CMD"; then
        log "offsite copy ok"
    else
        log "WARN: offsite copy FAILED (local encrypted backup intact) — check SREDA_BACKUP_OFFSITE_CMD"
    fi
else
    log "WARN: no offsite copy configured (set SREDA_BACKUP_OFFSITE_CMD) — backups exist on this host only"
fi

# ============================================================================
# Issue #68 — backup enforcement: llm-trace PII НЕ должен попадать в backup.
# /var/lib/sreda/private/llm-traces/ содержит decrypted memories, user text,
# tool args. NEVER include в pg_dump / tar / rsync backup artefacts.
# Plan: plans/mellow-discovering-conway-final.md, Section 9.
# ============================================================================
# pg_dump is database-only — фундаментально не touches /var/lib/sreda/private.
# Check is defensive против future drift (e.g. кто-то добавит tar bundle).
LEAK_ROOTS=("$DEST")
for root in "${LEAK_ROOTS[@]}"; do
    [ -d "$root" ] || continue
    while IFS= read -r artefact; do
        if tar -tf "$artefact" 2>/dev/null | grep -qE '(^|/)llm-traces/'; then
            log "CRITICAL: llm-traces leaked into $artefact"
            python3 -c "
import sys
sys.path.insert(0, '/opt/sreda/src')
try:
    from sreda.services.admin_alerts import send_admin_alert
    send_admin_alert(
        severity='P0',
        title='Backup leak: llm-traces in $artefact',
        body='Backup artefact contains /var/lib/sreda/private/llm-traces — CHECK IMMEDIATELY',
        dedupe_key='backup_llm_traces_leak',
    )
except Exception as e:
    print(f'alert failed: {e}', file=sys.stderr)
" 2>&1 | head -3
            exit 1
        fi
    done < <(find "$root" -mindepth 1 -maxdepth 2 -mtime -1 -name '*.tar*' 2>/dev/null)
done
