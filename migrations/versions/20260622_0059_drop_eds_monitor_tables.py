"""#181 Фаза 4-A (под-A): дроп таблиц EDS-мониторинга (фича ретайрена).

EDS (eds_monitor) деактивирован (Фаза 1) и его реализация удалена (Фаза 2). Здесь — дроп 4 таблиц
EDS-МОНИТОРИНГА: eds_accounts, eds_claim_state, eds_change_events, eds_delivery_records.
На проде пусты (инвентаризация 2026-06-22: все 0 строк). ORM этих таблиц (eds_monitor.py)
использовался только удалёнными сервисами + admin-reset (вычищен в этой же фазе).

НЕ трогаем connect-слой (connect_sessions, tenant_eds_accounts) — его ORM (TenantEDSAccount)
использует billing (карантин/scope B, решение владельца). Его дроп — отдельно, вместе с B.
secure_records eds_connect_payload — тоже остаётся (привязан к connect-слою, retention им управляет).

FK: eds_claim_state/eds_change_events/eds_delivery_records -> eds_accounts (дети);
eds_accounts -> tenant_eds_accounts (исходящий FK уходит вместе с eds_accounts; tenant_eds_accounts
сохраняется). Внешних FK ИЗ сохраняемых таблиц на эти 4 нет (проверено) → ordered drop достаточен.

Перед прогоном на проде — БЭКАП БД (обязательно). Downgrade — one-way (фича ретайрена).

Revision ID: 20260622_0059
Revises: 20260619_0058
"""
from __future__ import annotations

from alembic import op

revision = "20260622_0059"
down_revision = "20260619_0058"
branch_labels = None
depends_on = None

# Дети -> родитель (без циклов: connect-слой не входит).
_EDS_MONITOR_TABLES = [
    "eds_delivery_records",
    "eds_change_events",
    "eds_claim_state",
    "eds_accounts",
]


def upgrade() -> None:
    for table in _EDS_MONITOR_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    # One-way: EDS — ретайренная фича; воссоздание дропнутых пустых таблиц бессмысленно.
    # Восстановление — из бэкапа БД, снятого перед прогоном (см. docstring).
    raise NotImplementedError(
        "#181 Phase 4-A: EDS-monitor tables dropped (feature retired). "
        "No downgrade — restore from the pre-migration DB backup if ever needed."
    )
