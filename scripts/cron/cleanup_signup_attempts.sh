#!/usr/bin/env bash
# Daily cleanup signup_attempts >30d retention.
#
# Phase 2 of free-tier-subscription plan. signup_attempts хранит
# HMAC'd source_id + timestamps для rate-limit (3/24h per source).
# Старше 30 дней не нужны — purge per 152-ФЗ retention minimum.
#
# Codex MAJOR-7 fix 2026-05-07: раньше скрипт делал `sudo -u postgres
# psql` под `User=sreda` systemd-юнитом — silently failed (sreda не
# может passwordless sudo). Сейчас читает SREDA_DATABASE_URL из
# /etc/sreda/.env и подключается напрямую под app-user'ом, у которого
# DELETE permission на signup_attempts.
#
# Cron: daily at 03:30 UTC (after backup_postgres.sh at 03:00).
# Output: journald (StandardOutput=journal в systemd unit).
#
# Deploy:
#   sudo cp deploy/systemd/sreda-cleanup-signup-attempts.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now sreda-cleanup-signup-attempts.timer

set -euo pipefail

ENV_FILE=/etc/sreda/.env

ts() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }

if [ ! -r "$ENV_FILE" ]; then
    echo "[$(ts)] FATAL: $ENV_FILE не читается" >&2
    exit 1
fi

# Извлекаем SREDA_DATABASE_URL без source — там есть строки с
# не-bash значениями (см. SREDA_ADMIN_LOG_FILES с пробелами/скобками).
RAW_URL=$(grep -E "^SREDA_DATABASE_URL=" "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ -z "$RAW_URL" ]; then
    echo "[$(ts)] FATAL: SREDA_DATABASE_URL не найден в $ENV_FILE" >&2
    exit 1
fi

# psql не понимает `postgresql+psycopg://` (SQLAlchemy dialect suffix).
# Strip к стандартному `postgresql://`. Удаляем кавычки если есть.
PG_URL=$(echo "$RAW_URL" | sed -e 's|postgresql+psycopg|postgresql|' -e 's|^"||' -e 's|"$||')

echo "[$(ts)] cleanup_signup_attempts: starting"
deleted=$(psql "$PG_URL" -t -A -v ON_ERROR_STOP=1 -c "
    WITH del AS (
        DELETE FROM signup_attempts
        WHERE attempted_at < NOW() - INTERVAL '30 days'
        RETURNING id
    )
    SELECT COUNT(*) FROM del;
")
echo "[$(ts)] cleanup_signup_attempts: deleted ${deleted} rows"
