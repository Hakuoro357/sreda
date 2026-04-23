"""add tasks_items table (Task scheduler MVP)

New planner surface («Расписание») — one table, voice-filled,
read-only in Mini App. Replaces the menu-week experience that was
shelved in the 2026-04-22 cleanup. Projects, priorities, labels,
delegation — all v1.2 additions on top.

Columns:
  * id / tenant_id / user_id — standard ownership scope
  * title / notes / delegated_to — EncryptedString (rendered to
    the client as plain strings through the column type decorator)
  * scheduled_date — nullable Date; NULL = inbox (без даты)
  * time_start / time_end — nullable Time (local, tenant TZ)
  * recurrence_rule — nullable RRULE string (RFC 5545)
  * reminder_id — nullable FK → family_reminders.id, ON DELETE SET NULL
  * reminder_offset_minutes — display helper (avoid join on list)
  * status — pending / completed / cancelled
  * completed_at — nullable audit timestamp
  * created_at / updated_at

Indexes:
  * ix_tasks_scheduled (tenant, user, scheduled_date) — covers
    today/tomorrow/by-date list queries
  * ix_tasks_status (tenant, user, status) — covers
    "pending only" / "all" filter shapes

Revision ID: 20260422_0022
Revises: 20260421_0021
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260422_0022"
down_revision = "20260421_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("time_start", sa.Time(), nullable=True),
        sa.Column("time_end", sa.Time(), nullable=True),
        sa.Column("recurrence_rule", sa.String(255), nullable=True),
        sa.Column(
            "reminder_id",
            sa.String(64),
            sa.ForeignKey("family_reminders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reminder_offset_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delegated_to", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_tasks_scheduled",
        "tasks_items",
        ["tenant_id", "user_id", "scheduled_date"],
    )
    op.create_index(
        "ix_tasks_status",
        "tasks_items",
        ["tenant_id", "user_id", "status"],
    )
    op.create_index(
        "ix_tasks_items_reminder_id",
        "tasks_items",
        ["reminder_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_items_reminder_id", table_name="tasks_items")
    op.drop_index("ix_tasks_status", table_name="tasks_items")
    op.drop_index("ix_tasks_scheduled", table_name="tasks_items")
    op.drop_table("tasks_items")
