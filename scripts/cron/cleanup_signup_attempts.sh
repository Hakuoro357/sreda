#!/usr/bin/env bash
# Daily cleanup signup_attempts >30d retention.
#
# Phase 2 of free-tier-subscription plan. signup_attempts хранит
# HMAC'd source_id + timestamps для rate-limit (3/24h per source).
# Старше 30 дней не нужны — purge per 152-ФЗ retention minimum.
#
# Cron: daily at 03:30 UTC (after backup_postgres.sh at 03:00).
# Logs: /var/log/sreda/cleanup-signup-attempts.log
#
# Deploy:
#   sudo cp deploy/systemd/sreda-cleanup-signup-attempts.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now sreda-cleanup-signup-attempts.timer

set -euo pipefail

LOG_DIR="/var/log/sreda"
LOG_FILE="${LOG_DIR}/cleanup-signup-attempts.log"

# Xiaomi m3 fix: ensure log dir exists (fresh deploy без log rotation
# config — script silently fail-loud'ит без понятного сообщения).
mkdir -p "$LOG_DIR"

ts() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }

{
    echo "[$(ts)] cleanup_signup_attempts: starting"
    deleted=$(sudo -u postgres psql sreda -t -A -c "
        WITH del AS (
            DELETE FROM signup_attempts
            WHERE attempted_at < NOW() - INTERVAL '30 days'
            RETURNING id
        )
        SELECT COUNT(*) FROM del;
    ")
    echo "[$(ts)] cleanup_signup_attempts: deleted ${deleted} rows"
} | tee -a "$LOG_FILE"
