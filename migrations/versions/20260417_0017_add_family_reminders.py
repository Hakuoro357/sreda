"""add family_reminders + housewife_assistant_base subscription plan

Phase 1 of the housewife skill core (spec 59-housewife-assistant-skill-spec).
Adds:

  * ``family_reminders`` — scheduled proactive triggers (one-shot or
    recurring via RRULE). Worker ``HousewifeReminderWorker`` polls this
    table each job-runner tick and fires due rows through the outbox.
  * ``subscription_plans`` seed row for ``housewife_assistant_base``
    (free, perpetual during beta). Feature_key = ``housewife_assistant``
    is gated by the skill manifest exposed from the private-features
    plugin.

Revision ID: 20260417_0017
Revises: 20260415_0016
Create Date: 2026-04-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260417_0017"
down_revision = "20260415_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "family_reminders",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence_rule", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("source_memo", sa.Text(), nullable=True),
        sa.Column("next_trigger_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_family_reminders_tenant_id", "family_reminders", ["tenant_id"]
    )
    op.create_index(
        "ix_family_reminders_user_id", "family_reminders", ["user_id"]
    )
    op.create_index(
        "ix_family_reminders_trigger_at", "family_reminders", ["trigger_at"]
    )
    op.create_index(
        "ix_family_reminders_status", "family_reminders", ["status"]
    )
    op.create_index(
        "ix_family_reminders_next_trigger_at",
        "family_reminders",
        ["next_trigger_at"],
    )
    op.create_index(
        "ix_family_reminders_tenant_next_trigger",
        "family_reminders",
        ["tenant_id", "next_trigger_at"],
    )

    # Drop SQLite-style server_defaults so the model's Python-side
    # defaults drive new inserts — consistent with other tables.
    with op.batch_alter_table("family_reminders") as batch_op:
        batch_op.alter_column("status", server_default=None)
        batch_op.alter_column("created_at", server_default=None)
        batch_op.alter_column("updated_at", server_default=None)

    # Seed the subscription plan. Beta-phase is free-perpetual: backend
    # special-cases ``price_rub == 0`` in ``start_*_subscription`` to set
    # ``active_until`` ~100 years out (see spec 27 addendum).
    op.execute(
        """
        INSERT INTO subscription_plans
            (id, plan_key, feature_key, title, description,
             price_rub, billing_period_days,
             is_public, is_active, sort_order,
             created_at, updated_at)
        VALUES
            ('plan_housewife_base',
             'housewife_assistant_base',
             'housewife_assistant',
             'Помощник домохозяйки',
             'Проактивный помощник по семейной рутине: память о семье, '
             'напоминания, меню и покупки. На бета-тесте — бесплатно.',
             0, 30,
             1, 1, 30,
             CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM subscription_plans WHERE plan_key = 'housewife_assistant_base'"
    )
    op.drop_index(
        "ix_family_reminders_tenant_next_trigger", table_name="family_reminders"
    )
    op.drop_index(
        "ix_family_reminders_next_trigger_at", table_name="family_reminders"
    )
    op.drop_index("ix_family_reminders_status", table_name="family_reminders")
    op.drop_index(
        "ix_family_reminders_trigger_at", table_name="family_reminders"
    )
    op.drop_index("ix_family_reminders_user_id", table_name="family_reminders")
    op.drop_index("ix_family_reminders_tenant_id", table_name="family_reminders")
    op.drop_table("family_reminders")
