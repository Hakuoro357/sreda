"""add credits tracking to plans + executions (Phase 4.5)

Two additive columns for per-skill LLM-budget enforcement:

  * ``subscription_plans.credits_monthly_quota`` — how many credits the
    plan grants per billing cycle; NULL means "unmetered" (legacy plans
    without a quota continue to work).
  * ``skill_ai_executions.credits_consumed`` — computed via the rate
    formula (see ``sreda.services.credit_formula``) at write time.
    Existing rows get 0 which is a conservative default: the usage
    reporter shows a slight under-count for historical rows until they
    age out of the retention window.

Revision ID: 20260415_0014
Revises: 20260415_0013
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0014"
down_revision = "20260415_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subscription_plans") as batch_op:
        batch_op.add_column(
            sa.Column("credits_monthly_quota", sa.Integer(), nullable=True)
        )

    with op.batch_alter_table("skill_ai_executions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "credits_consumed",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_index(
            "ix_skill_ai_feature_created_credits",
            ["feature_key", "created_at", "credits_consumed"],
        )

    with op.batch_alter_table("skill_ai_executions") as batch_op:
        batch_op.alter_column("credits_consumed", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("skill_ai_executions") as batch_op:
        batch_op.drop_index("ix_skill_ai_feature_created_credits")
        batch_op.drop_column("credits_consumed")
    with op.batch_alter_table("subscription_plans") as batch_op:
        batch_op.drop_column("credits_monthly_quota")
