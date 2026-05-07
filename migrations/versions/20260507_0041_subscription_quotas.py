"""subscription quotas: usage_ledger + signup_attempts + grandfather

Phase 1 of free-tier-subscription plan (`plans/free-tier-subscription-stalled-r7.md`).
Schema-only migration — никакого изменения runtime поведения. Manual
approval flow остаётся active. Phase 2 (отдельный sprint) переключает
behavior switch.

Что делает:
1. Таблица ``usage_ledger`` — generic per-(tenant, metric, period_type,
   period_key) счётчик с quota_snapshot для audit. Использовать через
   `services/usage_ledger.py` (Phase 2).
2. Таблица ``signup_attempts`` — abuse-control journal. PII (TG/MAX
   user_id) хранится как HMAC-SHA256, не raw — соблюдаем 152-ФЗ
   политику обезличивания.
3. Колонки на ``tenant_subscriptions``: ``feature_key`` (denormalized
   из plan для index), ``grandfathered_at``, ``grandfather_reason``,
   ``quota_overrides_json``.
4. Backfill ``feature_key`` из существующих rows + NOT NULL constraint
   (preceded by NULL-feature_key guard в plans table).
5. Insert plan rows ``sreda_free`` (sort_order=0) и
   ``housewife_grandfathered`` (sort_order=999, is_public=false).
6. Re-point existing approved tenants' housewife_assistant subscriptions
   к housewife_grandfathered (with grandfathered_at + reason).
7. Создать grandfathered subscription для approved tenants без
    active sub (cohort = ALL `tenants.approved_at IS NOT NULL`).

NB: Plan called for partial unique index `(tenant_id, feature_key)
WHERE status='active'` + duplicate-active preflight, но это ломает
existing eds_monitor multi-slot billing pattern (base + extra subs
share feature_key). Single-active invariant deferred к app-level
enforcement в Phase 2 (EntitlementGate + SignupAbuseGuard). Index +
preflight removed для совместимости с existing billing model.

ВАЖНО:
- ``downgrade()`` отказывается выполняться (one-way migration).
  Rollback procedure = restore from pg_dump backup.
- Pending tenants (approved_at IS NULL) НЕ trogаются миграцией.
  Boris ревьюит их вручную после deploy и применяет manual SQL для
  approval списка.

Revision ID: 20260507_0041
Revises: 20260506_0040
Create Date: 2026-05-07
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision = "20260507_0041"
down_revision = "20260506_0040"
branch_labels = None
depends_on = None


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _jsonb_type():
    """Возвращаем JSONB на PG, JSON elsewhere — кроссплатформенно для тестов."""
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = _is_postgres(bind)

    # NB: pgcrypto NOT required — IDs (sub_<uuid>) generated в Python через
    # uuid.uuid4().hex (см. step 10). Removed per Xiaomi review (R1 MINOR
    # — dead DDL noise).

    # 2. usage_ledger table
    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("period_type", sa.String(8), nullable=False),
        sa.Column("period_key", sa.String(10), nullable=False),
        sa.Column(
            "used",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("quota_snapshot", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "metric", "period_type", "period_key",
            name="uq_usage_ledger_period",
        ),
    )
    op.create_index(
        "ix_usage_ledger_tenant_metric",
        "usage_ledger",
        ["tenant_id", "metric"],
    )

    # 3. signup_attempts table — abuse control journal
    op.create_table(
        "signup_attempts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("source_id_hash", sa.String(64), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_signup_attempts_lookup",
        "signup_attempts",
        ["channel", "source_id_hash", "attempted_at"],
    )

    # 4. TenantSubscription columns
    op.add_column(
        "tenant_subscriptions",
        sa.Column("grandfathered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_subscriptions",
        sa.Column("grandfather_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenant_subscriptions",
        sa.Column("quota_overrides_json", _jsonb_type(), nullable=True),
    )
    op.add_column(
        "tenant_subscriptions",
        sa.Column("feature_key", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_tenant_subs_feature_key",
        "tenant_subscriptions",
        ["feature_key"],
    )

    # 5. Backfill feature_key из subscription_plans
    if is_pg:
        # PG syntax: UPDATE ... FROM ... WHERE
        op.execute(sa.text("""
            UPDATE tenant_subscriptions ts
            SET feature_key = sp.feature_key
            FROM subscription_plans sp
            WHERE ts.plan_id = sp.id
              AND ts.feature_key IS NULL
        """))
    else:
        # SQLite syntax: subquery in SET
        op.execute(sa.text("""
            UPDATE tenant_subscriptions
            SET feature_key = (
                SELECT sp.feature_key FROM subscription_plans sp
                WHERE sp.id = tenant_subscriptions.plan_id
            )
            WHERE feature_key IS NULL
        """))
    # Defensive guard before NOT NULL constraint: any subscription_plans row
    # с NULL feature_key would propagate NULL into tenant_subscriptions и
    # ALTER NOT NULL упал бы mid-migration. Detect explicitly с runbook'ом.
    null_feature_subs = bind.execute(sa.text("""
        SELECT COUNT(*) AS n FROM tenant_subscriptions WHERE feature_key IS NULL
    """)).scalar()
    if null_feature_subs:
        raise RuntimeError(
            f"Migration 0041: {null_feature_subs} tenant_subscriptions rows "
            f"have NULL feature_key after backfill. Это значит linked "
            f"subscription_plans row тоже has NULL feature_key (data corruption).\n"
            f"Cleanup runbook:\n"
            f"  -- Find offending plans:\n"
            f"  SELECT id, plan_key, feature_key FROM subscription_plans WHERE feature_key IS NULL;\n"
            f"  -- Set feature_key explicitly:\n"
            f"  UPDATE subscription_plans SET feature_key = '<correct_key>' WHERE id = '<plan_id>';\n"
            f"After cleanup, re-run alembic upgrade head."
        )

    # Now NOT NULL
    op.alter_column(
        "tenant_subscriptions", "feature_key",
        existing_type=sa.String(64), nullable=False,
    )

    # 6-7. Partial unique index DROPPED от плана — incompatible с existing
    # eds_monitor multi-slot billing (base + extra-account subs share
    # feature_key='eds_monitor' с несколькими active rows). Single-active-per-
    # (tenant, feature) invariant enforced на app-level в Phase 2 через
    # EntitlementGate + SignupAbuseGuard. Tracked as known TODO. Full
    # DB-level enforcement требует refactor eds_monitor billing model
    # (separate sprint, out of scope here).

    # 8. Insert plan rows (idempotent via plan_key UNIQUE)
    # NB: SubscriptionPlan.id is generated string — use Python uuid для consistency.
    sreda_free_id = "plan_sreda_free"
    grandfathered_id = "plan_housewife_grandfathered"

    op.execute(sa.text("""
        INSERT INTO subscription_plans (
            id, plan_key, feature_key, title, description, price_rub,
            billing_period_days, credits_monthly_quota,
            is_public, is_active, sort_order, created_at, updated_at
        )
        VALUES (
            :free_id, 'sreda_free', 'housewife_assistant', 'Free',
            'Бесплатный тариф с дневным и месячным лимитом на LLM-ходы и голосовые сообщения. Лимиты enforced через usage_ledger в Phase 2.',
            0, 30, NULL,
            TRUE, TRUE, 0, NOW(), NOW()
        )
        ON CONFLICT (plan_key) DO NOTHING
    """).bindparams(free_id=sreda_free_id))

    op.execute(sa.text("""
        INSERT INTO subscription_plans (
            id, plan_key, feature_key, title, description, price_rub,
            billing_period_days, credits_monthly_quota,
            is_public, is_active, sort_order, created_at, updated_at
        )
        VALUES (
            :gf_id, 'housewife_grandfathered', 'housewife_assistant',
            'Grandfathered',
            'Безлимитный grandfathered tier для tenants approved до миграции 0041. Не tradable, не billable.',
            0, 30, NULL,
            FALSE, TRUE, 999, NOW(), NOW()
        )
        ON CONFLICT (plan_key) DO NOTHING
    """).bindparams(gf_id=grandfathered_id))

    # 9. Re-point existing housewife_assistant active subscriptions
    # of approved tenants → housewife_grandfathered.
    if is_pg:
        op.execute(sa.text("""
            UPDATE tenant_subscriptions ts
            SET plan_id = (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_grandfathered'),
                grandfathered_at = NOW(),
                grandfather_reason = 'Migration 0041: existing approved tenants grandfathered before paid pricing introduction'
            FROM tenants t
            WHERE ts.tenant_id = t.id
              AND t.approved_at IS NOT NULL
              AND ts.status = 'active'
              AND ts.feature_key = 'housewife_assistant'
              AND ts.plan_id = (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_assistant')
        """))
    else:
        op.execute(sa.text("""
            UPDATE tenant_subscriptions
            SET plan_id = (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_grandfathered'),
                grandfathered_at = (SELECT CURRENT_TIMESTAMP),
                grandfather_reason = 'Migration 0041: existing approved tenants grandfathered before paid pricing introduction'
            WHERE tenant_id IN (SELECT id FROM tenants WHERE approved_at IS NOT NULL)
              AND status = 'active'
              AND feature_key = 'housewife_assistant'
              AND plan_id = (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_assistant')
        """))

    # 10. Approved tenants без active housewife_assistant sub — создать
    # grandfathered subscription. Cohort = ВСЕ approved_at IS NOT NULL
    # (Codex M4 round 5 fix — не filter по active sub state).
    # NB: NOT EXISTS обязательно scoped к feature_key='housewife_assistant'
    # (Codex round 7 R7 open TODO O3 — applied here proactively во избежание
    # carry-over в Phase 2).
    approved_without_sub = bind.execute(sa.text("""
        SELECT t.id AS tenant_id
        FROM tenants t
        WHERE t.approved_at IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM tenant_subscriptions ts
            WHERE ts.tenant_id = t.id
              AND ts.feature_key = 'housewife_assistant'
              AND ts.status = 'active'
          )
    """)).fetchall()

    if approved_without_sub:
        for row in approved_without_sub:
            sub_id = "sub_" + uuid.uuid4().hex[:24]
            op.execute(sa.text("""
                INSERT INTO tenant_subscriptions (
                    id, tenant_id, plan_id, feature_key, status,
                    starts_at, active_until,
                    grandfathered_at, grandfather_reason,
                    cancel_at_period_end, quantity, next_cycle_quantity,
                    created_at, updated_at
                )
                VALUES (
                    :sub_id, :tenant_id,
                    (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_grandfathered'),
                    'housewife_assistant',
                    'active',
                    NOW(), NULL,
                    NOW(),
                    'Migration 0041: approved tenant без active housewife_assistant sub',
                    FALSE, 1, 1,
                    NOW(), NOW()
                )
            """).bindparams(sub_id=sub_id, tenant_id=row.tenant_id))


def downgrade() -> None:
    """One-way migration. Rollback = restore from pg_dump backup.

    Re-pointing tenant_subscriptions.plan_id к housewife_grandfathered
    теряет original plan_id mapping — automatic restore невозможен.
    Auto-grants для approved-без-sub tenants too нельзя надёжно
    откатить без знания pre-migration состояния.
    """
    raise RuntimeError(
        "Migration 0041 is one-way. "
        "Rollback procedure: restore database from pg_dump backup made before deploy "
        "(see scripts/backup_postgres.sh)."
    )
