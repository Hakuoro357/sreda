"""message_jobs: per-thread FIFO queue for plan-execute pipeline

Sub-A2 of Plan-Execute Epic (Hakuoro357/vex-assistant#74, #76).

Adds the ``message_jobs`` table that backs the new per-thread FIFO
queue. Each inbound message is enqueued here; a worker pool consumes
via ``FOR UPDATE SKIP LOCKED`` with lease fencing, removing the
stale-context risk we had when ``sreda-telegram-poller`` called
``handlers.chat()`` inline.

See ``src/sreda/db/models/message_jobs.py`` for the full schema rationale.

The migration is non-destructive: it only creates a new table + indexes,
no data backfill required, no impact on running code until the worker
loop and refactored poller land in this same sub-issue.

Revision ID: 20260525_0048
Revises: 20260520_0047
Create Date: 2026-05-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision = "20260525_0048"
down_revision = "20260520_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("external_update_id", sa.String(128), nullable=False),
        sa.Column(
            "message_payload",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "channel",
            "external_update_id",
            name="uq_message_jobs_channel_external_update_id",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','done','failed','dead_letter')",
            name="ck_message_jobs_status_enum",
        ),
        sa.CheckConstraint(
            "("
            " (status = 'pending'    AND started_at IS NULL  AND finished_at IS NULL)"
            " OR (status = 'processing' AND started_at IS NOT NULL AND finished_at IS NULL "
            "     AND lease_expires_at IS NOT NULL)"
            " OR (status IN ('done','failed','dead_letter') AND finished_at IS NOT NULL)"
            ")",
            name="ck_message_jobs_status_timestamps",
        ),
    )

    # Partial indexes — narrow on status to keep the hot index footprint
    # small (we usually have few pending/processing rows compared to
    # done/failed history).
    op.create_index(
        "ix_message_jobs_pending",
        "message_jobs",
        ["thread_id", "enqueued_at"],
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_message_jobs_processing",
        "message_jobs",
        ["thread_id"],
        postgresql_where=sa.text("status = 'processing'"),
        sqlite_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_message_jobs_expired_lease",
        "message_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
        sqlite_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_message_jobs_tenant_analytics",
        "message_jobs",
        ["tenant_id", "enqueued_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_jobs_tenant_analytics", table_name="message_jobs")
    op.drop_index("ix_message_jobs_expired_lease", table_name="message_jobs")
    op.drop_index("ix_message_jobs_processing", table_name="message_jobs")
    op.drop_index("ix_message_jobs_pending", table_name="message_jobs")
    op.drop_table("message_jobs")
