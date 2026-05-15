"""R-33: Task ↔ Checklist 1-to-1 optional link

R-33 (vex-assistant#42): user feedback tg_755682022 «связать расписание
с чеклистами — в расписании короткое название, детали в чеклисте».

Boris constraint: «у итема в расписании может быть только один список
дел. И у списка дел может быть только один итем в расписании, если он
есть» = **1-to-1 optional both sides**.

Schema changes:
- Add column ``tasks_items.checklist_id`` (nullable FK → checklists.id,
  ON DELETE SET NULL). При hard-delete checklist → task.checklist_id
  становится NULL (task survives). Archive checklist (soft delete) тоже
  unlink'ает task explicitly (R-31 endpoint update в R-33 phase).
- UNIQUE constraint ``uq_tasks_checklist_id`` enforces 1-to-1: каждый
  checklist linked максимум с 1 task. Postgres UNIQUE на nullable column
  allows multiple NULLs — preserves «1-to-1 only when linked» semantic.
- Index ``ix_tasks_checklist_id`` для query performance (наследуется от
  UNIQUE constraint automatically в Postgres — отдельный index не нужен).

Backward compat: existing tasks без checklist_id (default NULL) работают
как раньше. Mini-app schedule render для них показывает full task title
без 📋 link button.

Revision ID: 20260515_0044
Revises: 20260515_0043
Create Date: 2026-05-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260515_0044"
down_revision = "20260515_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable FK column to tasks_items
    op.add_column(
        "tasks_items",
        sa.Column(
            "checklist_id",
            sa.String(64),
            nullable=True,
        ),
    )

    # FK constraint: ON DELETE SET NULL (task survives checklist delete)
    op.create_foreign_key(
        "fk_tasks_checklist_id",
        "tasks_items",
        "checklists",
        ["checklist_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # UNIQUE constraint enforcing 1-to-1 (Postgres allows multiple NULLs)
    op.create_unique_constraint(
        "uq_tasks_checklist_id",
        "tasks_items",
        ["checklist_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_tasks_checklist_id", "tasks_items", type_="unique"
    )
    op.drop_constraint(
        "fk_tasks_checklist_id", "tasks_items", type_="foreignkey"
    )
    op.drop_column("tasks_items", "checklist_id")
