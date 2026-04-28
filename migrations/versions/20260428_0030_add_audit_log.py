"""Add audit_log table (152-ФЗ Часть 2 — compliance logging)

Создаёт таблицу `audit_log` для логирования важных действий системы:
admin actions (approve/reset), user actions (self-delete/consent),
проверки compliance.

Без backfill — таблица заполняется только новыми событиями. Старые
действия не реконструируем.

Revision ID: 20260428_0030
Revises: 20260428_0029
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260428_0030"
down_revision = "20260428_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            index=True,
        ),
    )
    op.create_index(
        "ix_audit_log_action_created",
        "audit_log",
        ["action", "created_at"],
    )
    op.create_index(
        "ix_audit_log_actor",
        "audit_log",
        ["actor_type", "actor_id"],
    )
    op.create_index(
        "ix_audit_log_resource",
        "audit_log",
        ["resource_type", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_resource", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_index("ix_audit_log_action_created", table_name="audit_log")
    op.drop_table("audit_log")
