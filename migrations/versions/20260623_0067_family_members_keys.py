"""operation_id + normalized_title_hash on family_members (#202 household keying)

Sub-A10 (миграция 0051) добавила эти колонки 5 таблицам (shopping/reminders/tasks/recipes/
checklists), но НЕ family_members. #202 оснащает семью household ключами идемпотентности на
ReAct-пути → нужна та же пара колонок + индексов (как у checklists).

  operation_id          text NULL — SHA-1 от (plan_id, step_id, action, entity_type=family_member,
                        logical_key=norm(name)). UNIQUE (tenant_id, user_id, operation_id) → повтор
                        tool_call дедупится. NULL (легаси-строки) не коллизируют (NULL distinct в UNIQUE).
  normalized_title_hash text NULL — HMAC-SHA256 от norm(name) (лемматизация имени). NON-unique partial
                        индекс (WHERE NOT NULL) для семантического pre-check (морфовариант мимо
                        whitespace-дедупа).

Безопасность миграции: family_members — МАЛЕНЬКАЯ таблица (единицы-десятки строк/юзер). ALTER + сборка
индекса мгновенны; конкуренции на блокировку нет. op_id у существующих строк = NULL → UNIQUE-индекс не
конфликтует (dedup-аудит не нужен, в отличие от 2b semantic-замка по живым данным). PG-lock_timeout
выставлен для подстраховки. batch_alter_table — для SQLite.

Revision ID: 20260623_0067
Revises: 20260623_0066
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260623_0067"
down_revision = "20260623_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Подстраховка: не зависнуть на блокировке (таблица крошечная — отработает мгновенно).
        op.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    with op.batch_alter_table("family_members") as batch_op:
        batch_op.add_column(sa.Column("operation_id", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("normalized_title_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_family_members_operation_id",
        "family_members",
        ["tenant_id", "user_id", "operation_id"],
        unique=True,
    )
    op.create_index(
        "ix_family_members_normalized_title",
        "family_members",
        ["tenant_id", "user_id", "normalized_title_hash"],
        postgresql_where=sa.text("normalized_title_hash IS NOT NULL"),
        sqlite_where=sa.text("normalized_title_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_family_members_normalized_title", table_name="family_members")
    op.drop_index("ix_family_members_operation_id", table_name="family_members")
    with op.batch_alter_table("family_members") as batch_op:
        batch_op.drop_column("normalized_title_hash")
        batch_op.drop_column("operation_id")
