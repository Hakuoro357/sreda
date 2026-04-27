"""add checklists + checklist_items tables (Часть A плана)

«Чек-листы» — третий тип «списков» в продукте, поверх tasks_items
(события с датой) и shopping_list_items (продукты). Закрывает кейс
из недели 2026-04-21..25 (швея надиктовывает «план кроя» из 7 пунктов
без дат — раньше LLM мапила в add_task на разные даты, юзер видел
бессмысленное расписание из 7 ужинов).

Таблицы:
  * ``checklists`` — родительский именованный список («План кроя на
    эту неделю», «Дела на дачу»). Пара (tenant_id, user_id) +
    title + status (active/archived).
  * ``checklist_items`` — пункты внутри списка с галочкой done.
    title/notes — EncryptedString (контент = PII). status
    pending/done/cancelled. position для сортировки внутри.

CASCADE на удалении checklist → items уходят за ним.

Revision ID: 20260425_0025
Revises: 20260425_0024
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260425_0025"
down_revision = "20260425_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checklists",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
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
        "ix_checklists_active",
        "checklists",
        ["tenant_id", "user_id", "status"],
    )

    op.create_table(
        "checklist_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "checklist_id",
            sa.String(64),
            sa.ForeignKey("checklists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Сортировка внутри списка. Авто-инкремент в сервисе при
        # add_items (max(position)+1, чтобы новые шли в конец).
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        # title/notes хранятся как Text (EncryptedString сериализует в
        # зашифрованную строку — Text вмещает любой размер).
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
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
        "ix_checklist_items_status",
        "checklist_items",
        ["checklist_id", "status", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_checklist_items_status", table_name="checklist_items")
    op.drop_table("checklist_items")
    op.drop_index("ix_checklists_active", table_name="checklists")
    op.drop_table("checklists")
