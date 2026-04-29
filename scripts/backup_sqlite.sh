#!/bin/bash
# Sreda SQLite WAL-safe backup with gzip + 14-day local retention.
# Cron: 03:00 UTC daily. Logs to /var/log/sreda/backup.log.
set -euo pipefail

DB=/var/lib/sreda/sreda.db
DEST=/var/backups/sreda
LOG=/var/log/sreda/backup.log
RETENTION_DAYS=14
DATE=$(date -u +%Y%m%d-%H%M)

mkdir -p "$DEST"

ts() { date -u +'%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts) $*" >> "$LOG"; }

log "backup start: $DB"

# WAL-safe online backup (does NOT block writers)
TMP="$DEST/.sreda-$DATE.db.tmp"
sqlite3 "$DB" ".backup '$TMP'"

# Verify integrity before compressing
INT=$(sqlite3 "$TMP" 'PRAGMA integrity_check;')
if [ "$INT" != "ok" ]; then
    log "INTEGRITY FAIL: $INT — keeping uncompressed for inspection"
    mv "$TMP" "$DEST/sreda-$DATE.CORRUPT.db"
    exit 1
fi

gzip -9 "$TMP"
mv "$TMP.gz" "$DEST/sreda-$DATE.db.gz"

# 14-day retention
find "$DEST" -name 'sreda-*.db.gz' -mtime +$RETENTION_DAYS -delete -print | while read -r f; do
    log "pruned: $f"
done

SIZE=$(stat -c '%s' "$DEST/sreda-$DATE.db.gz")
COUNT=$(ls -1 "$DEST"/sreda-*.db.gz 2>/dev/null | wc -l)
log "backup ok: sreda-$DATE.db.gz size=${SIZE}b retained=$COUNT files"
