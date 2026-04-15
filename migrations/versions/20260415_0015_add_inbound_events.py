"""add inbound_events (Phase 4)

Unified contract "skill → platform event". Ingestors write rows here;
the proactive worker consumes them and fans out to skill handlers.

Revision ID: 20260415_0015
Revises: 20260415_0014
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0015"
down_revision = "20260415_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_events",
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
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("external_event_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "relevance_score", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column("relevance_reason", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="new"
        ),
        sa.Column("status_reason", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "feature_key",
            "external_event_key",
            name="uq_inbound_events_feature_external",
        ),
    )
    op.create_index(
        "ix_inbound_events_tenant_id", "inbound_events", ["tenant_id"]
    )
    op.create_index("ix_inbound_events_user_id", "inbound_events", ["user_id"])
    op.create_index(
        "ix_inbound_events_feature_key", "inbound_events", ["feature_key"]
    )
    op.create_index(
        "ix_inbound_events_event_type", "inbound_events", ["event_type"]
    )
    op.create_index(
        "ix_inbound_events_status", "inbound_events", ["status"]
    )
    op.create_index(
        "ix_inbound_events_status_created",
        "inbound_events",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_inbound_events_tenant_feature",
        "inbound_events",
        ["tenant_id", "feature_key"],
    )

    with op.batch_alter_table("inbound_events") as batch_op:
        batch_op.alter_column("payload_json", server_default=None)
        batch_op.alter_column("relevance_score", server_default=None)
        batch_op.alter_column("status", server_default=None)
        batch_op.alter_column("created_at", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_events_tenant_feature", table_name="inbound_events"
    )
    op.drop_index(
        "ix_inbound_events_status_created", table_name="inbound_events"
    )
    op.drop_index("ix_inbound_events_status", table_name="inbound_events")
    op.drop_index("ix_inbound_events_event_type", table_name="inbound_events")
    op.drop_index("ix_inbound_events_feature_key", table_name="inbound_events")
    op.drop_index("ix_inbound_events_user_id", table_name="inbound_events")
    op.drop_index("ix_inbound_events_tenant_id", table_name="inbound_events")
    op.drop_table("inbound_events")
