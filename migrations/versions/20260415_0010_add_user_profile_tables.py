"""add user profile + user skill config tables (Phase 2)

Introduces per-user settings that were previously absent:

  * ``tenant_user_profiles``       — cross-skill identity, timezone,
    quiet hours, communication style, interest tags.
  * ``tenant_user_skill_configs``  — per-skill notification priority,
    token budget, free-form skill params.

Also extends ``outbox_messages`` with two columns the delivery worker
needs to enforce quiet-hours:

  * ``scheduled_at``  — deferred delivery timestamp; NULL means "send now"
  * ``feature_key``   — which skill produced this reply, so the worker
    can look up the correct ``notification_priority`` for per-skill gating

Revision ID: 20260415_0010
Revises: 20260415_0009
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0010"
down_revision = "20260415_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- tenant_user_profiles --------------------------------------------
    op.create_table(
        "tenant_user_profiles",
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
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column(
            "timezone", sa.String(length=64), nullable=False, server_default="UTC"
        ),
        sa.Column(
            "quiet_hours_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "communication_style",
            sa.String(length=16),
            nullable=False,
            server_default="casual",
        ),
        sa.Column(
            "interest_tags_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "updated_by_source",
            sa.String(length=32),
            nullable=False,
            server_default="user_command",
        ),
        sa.Column("updated_by_user_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", name="uq_tenant_user_profiles_tenant_user"
        ),
    )
    op.create_index(
        "ix_tenant_user_profiles_tenant_id", "tenant_user_profiles", ["tenant_id"]
    )
    op.create_index(
        "ix_tenant_user_profiles_user_id", "tenant_user_profiles", ["user_id"]
    )
    op.create_index(
        "ix_tenant_user_profiles_timezone", "tenant_user_profiles", ["timezone"]
    )

    # Strip server defaults so future INSERTs rely on the SQLAlchemy model.
    with op.batch_alter_table("tenant_user_profiles") as batch_op:
        batch_op.alter_column("timezone", server_default=None)
        batch_op.alter_column("quiet_hours_json", server_default=None)
        batch_op.alter_column("communication_style", server_default=None)
        batch_op.alter_column("interest_tags_json", server_default=None)
        batch_op.alter_column("updated_by_source", server_default=None)
        batch_op.alter_column("created_at", server_default=None)
        batch_op.alter_column("updated_at", server_default=None)

    # -- tenant_user_skill_configs ---------------------------------------
    op.create_table(
        "tenant_user_skill_configs",
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
            nullable=False,
        ),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column(
            "notification_priority",
            sa.String(length=16),
            nullable=False,
            server_default="normal",
        ),
        sa.Column(
            "token_budget_daily",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "skill_params_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "updated_by_source",
            sa.String(length=32),
            nullable=False,
            server_default="user_command",
        ),
        sa.Column("updated_by_user_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "feature_key",
            name="uq_tenant_user_skill_configs_tuf",
        ),
    )
    op.create_index(
        "ix_tenant_user_skill_configs_tenant_id",
        "tenant_user_skill_configs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tenant_user_skill_configs_user_id",
        "tenant_user_skill_configs",
        ["user_id"],
    )
    op.create_index(
        "ix_tenant_user_skill_configs_feature",
        "tenant_user_skill_configs",
        ["feature_key"],
    )
    op.create_index(
        "ix_tenant_user_skill_configs_priority",
        "tenant_user_skill_configs",
        ["notification_priority"],
    )

    with op.batch_alter_table("tenant_user_skill_configs") as batch_op:
        batch_op.alter_column("notification_priority", server_default=None)
        batch_op.alter_column("token_budget_daily", server_default=None)
        batch_op.alter_column("skill_params_json", server_default=None)
        batch_op.alter_column("updated_by_source", server_default=None)
        batch_op.alter_column("created_at", server_default=None)
        batch_op.alter_column("updated_at", server_default=None)

    # -- outbox_messages.scheduled_at + feature_key ----------------------
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.add_column(
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("feature_key", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_outbox_messages_scheduled_at", ["scheduled_at"]
        )
        batch_op.create_index("ix_outbox_messages_feature_key", ["feature_key"])


def downgrade() -> None:
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.drop_index("ix_outbox_messages_feature_key")
        batch_op.drop_index("ix_outbox_messages_scheduled_at")
        batch_op.drop_column("feature_key")
        batch_op.drop_column("scheduled_at")

    op.drop_index(
        "ix_tenant_user_skill_configs_priority", table_name="tenant_user_skill_configs"
    )
    op.drop_index(
        "ix_tenant_user_skill_configs_feature", table_name="tenant_user_skill_configs"
    )
    op.drop_index(
        "ix_tenant_user_skill_configs_user_id", table_name="tenant_user_skill_configs"
    )
    op.drop_index(
        "ix_tenant_user_skill_configs_tenant_id", table_name="tenant_user_skill_configs"
    )
    op.drop_table("tenant_user_skill_configs")

    op.drop_index("ix_tenant_user_profiles_timezone", table_name="tenant_user_profiles")
    op.drop_index("ix_tenant_user_profiles_user_id", table_name="tenant_user_profiles")
    op.drop_index(
        "ix_tenant_user_profiles_tenant_id", table_name="tenant_user_profiles"
    )
    op.drop_table("tenant_user_profiles")
