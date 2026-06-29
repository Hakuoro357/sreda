"""react_summaries — durable-выжимка истории ReAct (#232 способ Б)

Отдельная таблица (не канал чекпойнта): выжимка — пересказ уже существующего, не новое событие →
не должна числиться «самым свежим» состоянием (иначе гонка-затирание, код-ревью R1 CRITICAL).
Одна актуальная выжимка на тред (PK = durable thread_id, как react_checkpoint). summary_text — шифр
(EncryptedString → Text). Привязка к прошлому: covered_message_count + covered_hash + covered_through_ts.
GC 30д — по updated_at (ветка в retention).

Безопасность миграции: CREATE TABLE новой таблицы — без блокировок существующих данных, мгновенно.

Revision ID: 20260629_0068
Revises: 20260623_0067
Create Date: 2026-06-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260629_0068"
down_revision = "20260623_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "react_summaries",
        sa.Column("thread_id", sa.String(160), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=False),  # EncryptedString impl=Text (ciphertext)
        sa.Column("covered_message_count", sa.Integer(), nullable=False),
        sa.Column("covered_hash", sa.String(64), nullable=False),
        sa.Column("covered_through_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_react_summaries_tenant_id", "react_summaries", ["tenant_id"])
    op.create_index("ix_react_summaries_updated_at", "react_summaries", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_react_summaries_updated_at", table_name="react_summaries")
    op.drop_index("ix_react_summaries_tenant_id", table_name="react_summaries")
    op.drop_table("react_summaries")
