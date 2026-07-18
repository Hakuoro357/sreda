#!/usr/bin/env bash
# wipe_tenant.sh — Hard-delete a tenant + all FK children in one transaction.
#
# Usage:
#   scripts/wipe_tenant.sh <tenant_id>           — interactive confirm
#   scripts/wipe_tenant.sh <tenant_id> --force   — skip confirm (CI / loops)
#
# Env overrides:
#   SREDA_VDS_HOST  — REQUIRED (user@host боевого VDS; дефолт удалён из репо —
#                     audit 2026-07-18: адрес сервера не публикуем)
#   SREDA_VDS_KEY   — default ~/.ssh/timeweb_openclaw
#
# Use case: orphan-tenant cleanup during channel-linking smoke testing,
# wipe-and-retry on registration flows, debugging onboarding for a single
# test tenant. Runs in a single transaction — rolls back on any error.
#
# Safety:
# - tenant_id MUST match ^tenant_[a-z]+_[0-9]+$ (defends against
#   shell-injection through filename-like values; also prevents wiping
#   the wrong tenant by typo).
# - Inventory shown before delete; interactive confirmation by default.
# - Single-row DELETE on every table — no `WHERE 1=1` patterns.

set -euo pipefail

TENANT_ID="${1:-}"
FORCE="${2:-}"

if [[ -z "$TENANT_ID" ]]; then
  echo "Usage: $0 <tenant_id> [--force]" >&2
  exit 1
fi

if [[ ! "$TENANT_ID" =~ ^tenant_[a-z]+_[0-9]+$ ]]; then
  echo "Refusing to wipe: tenant_id '$TENANT_ID' doesn't match" >&2
  echo "expected pattern (tenant_<channel>_<id>)." >&2
  exit 1
fi

if [[ -z "${SREDA_VDS_HOST:-}" ]]; then
  echo "Refusing to run: SREDA_VDS_HOST is not set (user@host of the target VDS)." >&2
  echo "The hardcoded default was removed (audit 2026-07-18: server address is not public)." >&2
  exit 1
fi
SSH_HOST="$SREDA_VDS_HOST"
SSH_KEY="${SREDA_VDS_KEY:-$HOME/.ssh/timeweb_openclaw}"

# ----- Inventory before delete
echo "=== Inventory of $TENANT_ID ==="
ssh -i "$SSH_KEY" "$SSH_HOST" "sudo -u postgres psql sreda" <<SQL
SELECT 'tenant'        AS table, (SELECT COUNT(*) FROM tenants                  WHERE id='$TENANT_ID')::int AS rows
UNION ALL SELECT 'users',         (SELECT COUNT(*) FROM users                   WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'inbound',       (SELECT COUNT(*) FROM inbound_messages        WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'outbox',        (SELECT COUNT(*) FROM outbox_messages         WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'agent_runs',    (SELECT COUNT(*) FROM agent_runs              WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'memories',      (SELECT COUNT(*) FROM assistant_memories      WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'reminders',     (SELECT COUNT(*) FROM family_reminders        WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'subscriptions', (SELECT COUNT(*) FROM tenant_subscriptions    WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'secure',        (SELECT COUNT(*) FROM secure_records          WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'react_trace',   (SELECT COUNT(*) FROM react_turn_trace        WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'react_ckpt',    (SELECT COUNT(*) FROM react_checkpoint        WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'conv_turns',    (SELECT COUNT(*) FROM conversation_turns      WHERE tenant_id='$TENANT_ID')::int
UNION ALL SELECT 'heartbeats',    (SELECT COUNT(*) FROM poller_heartbeats       WHERE tenant_id='$TENANT_ID')::int;
SQL

# ----- Confirm
if [[ "$FORCE" != "--force" ]]; then
  echo
  read -r -p "Delete tenant $TENANT_ID and ALL related rows? [y/N] " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted." >&2
    exit 0
  fi
fi

# ----- Cascade delete in single transaction
# Audit 2026-07-18 (#6): список дополнен react_turn_trace / react_checkpoint /
# conversation_turns / poller_heartbeats (no-FK таблицы с текстами переписки —
# раньше «wipe» оставлял их PII в БД). При добавлении новых tenant-таблиц в
# схему этот список надо держать в синхроне с rls_registry.TENANT_TABLES
# (динамический эталон — onboard_smoke.py).
ssh -i "$SSH_KEY" "$SSH_HOST" "sudo -u postgres psql sreda -v ON_ERROR_STOP=1" <<SQL
BEGIN;
DELETE FROM agent_runs                    WHERE tenant_id='$TENANT_ID';
DELETE FROM agent_threads                 WHERE tenant_id='$TENANT_ID';
DELETE FROM assistant_memories            WHERE tenant_id='$TENANT_ID';
DELETE FROM conversation_turns            WHERE tenant_id='$TENANT_ID';
DELETE FROM jobs                          WHERE tenant_id='$TENANT_ID';
DELETE FROM assistants                    WHERE tenant_id='$TENANT_ID';
DELETE FROM inbound_messages              WHERE tenant_id='$TENANT_ID';
DELETE FROM inbound_events                WHERE tenant_id='$TENANT_ID';
DELETE FROM outbox_messages               WHERE tenant_id='$TENANT_ID';
DELETE FROM poller_heartbeats             WHERE tenant_id='$TENANT_ID';
DELETE FROM react_checkpoint              WHERE tenant_id='$TENANT_ID';
DELETE FROM react_turn_trace              WHERE tenant_id='$TENANT_ID';
DELETE FROM secure_records                WHERE tenant_id='$TENANT_ID';
DELETE FROM connect_sessions              WHERE tenant_id='$TENANT_ID';
DELETE FROM channel_link_tokens           WHERE tenant_id='$TENANT_ID';
DELETE FROM family_members                WHERE tenant_id='$TENANT_ID';
DELETE FROM family_reminders              WHERE tenant_id='$TENANT_ID';
DELETE FROM eds_accounts                  WHERE tenant_id='$TENANT_ID';
DELETE FROM checklists                    WHERE tenant_id='$TENANT_ID';
DELETE FROM menu_plans                    WHERE tenant_id='$TENANT_ID';
DELETE FROM payment_orders                WHERE tenant_id='$TENANT_ID';
DELETE FROM recipes                       WHERE tenant_id='$TENANT_ID';
DELETE FROM shopping_list_items           WHERE tenant_id='$TENANT_ID';
DELETE FROM skill_ai_executions           WHERE tenant_id='$TENANT_ID';
DELETE FROM skill_run_attempts            WHERE tenant_id='$TENANT_ID';
DELETE FROM skill_runs                    WHERE tenant_id='$TENANT_ID';
DELETE FROM skill_events                  WHERE tenant_id='$TENANT_ID';
DELETE FROM tasks_items                   WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_billing_cycles         WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_eds_accounts           WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_features               WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_skill_configs          WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_skill_states           WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_subscriptions          WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_user_profile_proposals WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_user_profiles          WHERE tenant_id='$TENANT_ID';
DELETE FROM tenant_user_skill_configs     WHERE tenant_id='$TENANT_ID';
DELETE FROM workspaces                    WHERE tenant_id='$TENANT_ID';
DELETE FROM users                         WHERE tenant_id='$TENANT_ID';
DELETE FROM tenants                       WHERE id='$TENANT_ID';
COMMIT;
SQL

echo "✓ Deleted $TENANT_ID"
