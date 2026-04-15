"""add tenant_user_profile_proposals (Phase 2e)

Stores pending profile-change suggestions made by the agent. The user
confirms or rejects via an inline Telegram button; only on confirm do
we apply the change to ``tenant_user_profiles`` (with audit source
``agent_tool_confirmed``).

Revision ID: 20260415_0012
Revises: 20260415_0011
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0012"
down_revision = "20260415_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_user_profile_proposals",
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
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("proposed_value_json", sa.Text(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tu_profile_proposals_tenant_id",
        "tenant_user_profile_proposals",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tu_profile_proposals_user_id",
        "tenant_user_profile_proposals",
        ["user_id"],
    )
    op.create_index(
        "ix_tu_profile_proposals_status",
        "tenant_user_profile_proposals",
        ["status"],
    )
    op.create_index(
        "ix_tu_profile_proposals_expires",
        "tenant_user_profile_proposals",
        ["expires_at"],
    )

    with op.batch_alter_table("tenant_user_profile_proposals") as batch_op:
        batch_op.alter_column("status", server_default=None)
        batch_op.alter_column("created_at", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_tu_profile_proposals_expires",
        table_name="tenant_user_profile_proposals",
    )
    op.drop_index(
        "ix_tu_profile_proposals_status",
        table_name="tenant_user_profile_proposals",
    )
    op.drop_index(
        "ix_tu_profile_proposals_user_id",
        table_name="tenant_user_profile_proposals",
    )
    op.drop_index(
        "ix_tu_profile_proposals_tenant_id",
        table_name="tenant_user_profile_proposals",
    )
    op.drop_table("tenant_user_profile_proposals")
