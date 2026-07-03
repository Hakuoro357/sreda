"""skill_ai_executions: +index (task_type, created_at) (#304)

Индекс под фильтр task_type='react_turn' + окно created_at, который гоняют:
- #304 DAU/WAU/MAU (COUNT DISTINCT tenant_id по окнам 24ч/7д/30д);
- #303 отчёт надёжности (turns_total / runs_failed).
Сейчас таблица мелкая (≈1.4к строк → PG выбирает seq scan, он мгновенный),
индекс — про рост: skill_ai_executions пополняется каждым AI-вызовом.
Backwards-compatible: обычный CREATE INDEX берёт короткий lock на запись, но на
текущем объёме он почти мгновенный, поэтому CONCURRENTLY не нужен; старый код
индекс не замечает.

Revision ID: 20260703_0076
Revises: 20260703_0075
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op


revision = "20260703_0076"
down_revision = "20260703_0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_skill_ai_tasktype_created",
        "skill_ai_executions",
        ["task_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_ai_tasktype_created", table_name="skill_ai_executions")
