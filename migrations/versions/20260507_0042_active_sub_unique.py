"""partial unique index: one active subscription per (tenant_id, feature_key)

Phase 1 migration 0041 deferred this index из-за existing eds_monitor
multi-slot billing pattern (base + extra_account subs share feature_key
с несколькими active rows). EDS-monitor was scrubbed manually
2026-05-07 (2 active subs → cancelled, plans is_active=false), что
освободило путь к single-active-per-feature invariant на DB level.

Что делает:
- Preflight detect duplicate active subscriptions per (tenant_id,
  feature_key) — abort с runbook'ом если найдено.
- CREATE UNIQUE INDEX `ux_tenant_subs_active_per_feature` на
  (tenant_id, feature_key) WHERE status='active'.

Безопасный профиль:
- На момент deploy на проде: 0 duplicate active rows (verified
  manually). Index создаётся без data corruption.
- Phase 2 entitlement gate + signup abuse guard теперь могут
  безопасно полагаться на DB-level uniqueness.

Revision ID: 20260507_0042
Revises: 20260507_0041
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260507_0042"
down_revision = "20260507_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Preflight: detect duplicate active subscriptions per (tenant, feature_key).
    # Без этого CREATE UNIQUE INDEX упадёт mid-flight на real data.
    duplicates = bind.execute(sa.text("""
        SELECT tenant_id, feature_key, COUNT(*) AS n
        FROM tenant_subscriptions
        WHERE status = 'active'
        GROUP BY tenant_id, feature_key
        HAVING COUNT(*) > 1
    """)).fetchall()
    if duplicates:
        msg_lines = [
            "Migration 0042: duplicate active subscriptions detected — abort.",
            "Each (tenant_id, feature_key) must have at most one active row.",
            "Offending rows:",
        ]
        for row in duplicates:
            msg_lines.append(
                f"  tenant_id={row.tenant_id} feature_key={row.feature_key} count={row.n}"
            )
        msg_lines.extend([
            "",
            "Cleanup runbook (keep newest per group, mark older as cancelled):",
            "  UPDATE tenant_subscriptions SET status='cancelled', updated_at=NOW()",
            "  WHERE id IN (",
            "    SELECT id FROM (",
            "      SELECT id, ROW_NUMBER() OVER (",
            "        PARTITION BY tenant_id, feature_key",
            "        ORDER BY created_at DESC, id DESC",
            "      ) AS rn",
            "      FROM tenant_subscriptions WHERE status='active'",
            "    ) ranked WHERE rn > 1",
            "  );",
            "After cleanup, re-run alembic upgrade head.",
        ])
        raise RuntimeError("\n".join(msg_lines))

    op.create_index(
        "ux_tenant_subs_active_per_feature",
        "tenant_subscriptions",
        ["tenant_id", "feature_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_tenant_subs_active_per_feature",
        table_name="tenant_subscriptions",
    )
