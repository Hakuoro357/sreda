"""user_data_change_feed + audit_outbox (Sub-A11, Category I / Group 6.4)

Adds the two-table outbox pattern for the data-change audit feed.

  audit_outbox            buffer — app writes events here in same TX
                          as the underlying data mutation (SAVEPOINT).
  user_data_change_feed   durable destination — relay worker drains
                          outbox here once committed.

Both tables share the same event shape (operation_id, tenant_id,
user_id, source, entity_type, entity_id, action, payload, payload_hash,
caused_by, occurred_at). Outbox additionally carries retry bookkeeping
(enqueued_at, attempts, last_attempt_at, last_error).

UNIQUE on operation_id in both tables makes the relay's INSERT
idempotent under retry.

Revision ID: 20260526_0052
Revises: 20260526_0051
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision = "20260526_0052"
down_revision = "20260526_0051"
branch_labels = None
depends_on = None


_SOURCE_CHECK = "source IN ('среда','mini-app','api','system')"
_ACTION_CHECK = "action IN ('created','updated','deleted','skipped')"


def _common_columns():
    """Columns shared between outbox and feed."""
    return [
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column(
            "caused_by",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "user_data_change_feed",
        *_common_columns(),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_user_data_change_feed_operation_id",
        ),
        sa.CheckConstraint(_SOURCE_CHECK, name="ck_user_data_change_feed_source"),
        sa.CheckConstraint(_ACTION_CHECK, name="ck_user_data_change_feed_action"),
    )
    op.create_index(
        "ix_user_data_change_feed_tenant_recent",
        "user_data_change_feed",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_user_data_change_feed_entity",
        "user_data_change_feed",
        ["tenant_id", "entity_type", "entity_id", "occurred_at"],
    )

    op.create_table(
        "audit_outbox",
        *_common_columns(),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_audit_outbox_operation_id",
        ),
        sa.CheckConstraint(_SOURCE_CHECK, name="ck_audit_outbox_source"),
        sa.CheckConstraint(_ACTION_CHECK, name="ck_audit_outbox_action"),
    )
    op.create_index(
        "ix_audit_outbox_tenant_recent",
        "audit_outbox",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_outbox_pending",
        "audit_outbox",
        ["enqueued_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_outbox_pending", table_name="audit_outbox")
    op.drop_index("ix_audit_outbox_tenant_recent", table_name="audit_outbox")
    op.drop_table("audit_outbox")
    op.drop_index(
        "ix_user_data_change_feed_entity",
        table_name="user_data_change_feed",
    )
    op.drop_index(
        "ix_user_data_change_feed_tenant_recent",
        table_name="user_data_change_feed",
    )
    op.drop_table("user_data_change_feed")
