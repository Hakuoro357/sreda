"""#262 срез A1: пользовательские категории памяти (аддитивно).

memory_categories + assistant_memories.category_id (NULLABLE) + composite FK + индексы
(parent UNIQUE(id,tenant,user); partial-unique «≤1 system на (tenant,user)»; unique name_normalized).

NOT NULL для category_id — ОТДЕЛЬНОЙ миграцией (A3) ПОСЛЕ backfill в Common и после рестарта кода,
который резолвит Common в save() (backwards-compatible deploy, план #262). Имена категорий — plaintext.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260629_0057"
down_revision = "20260612_0056"  # сверено `alembic heads` 2026-06-29 (g-062)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_categories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("slug", sa.String(32), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("name_normalized", sa.String(160), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # parent UNIQUE для composite-FK из assistant_memories
        sa.UniqueConstraint("id", "tenant_id", "user_id", name="uq_memory_categories_id_scope"),
        sa.UniqueConstraint("tenant_id", "user_id", "name_normalized", name="uq_memory_categories_name"),
    )
    op.create_index("ix_memory_categories_tenant_user", "memory_categories", ["tenant_id", "user_id"])
    # ровно одна system-категория (Common) на (tenant,user)
    op.create_index(
        "uq_memory_categories_one_system", "memory_categories", ["tenant_id", "user_id"], unique=True,
        postgresql_where=sa.text("is_system"), sqlite_where=sa.text("is_system"))
    # assistant_memories.category_id (NULLABLE в A1) + composite FK.
    # M1 (ревью R1): НЕ recreate на Postgres — там это переписало бы всю PII-таблицу под локом. Прямой ALTER:
    # ADD COLUMN (метаданные) + ADD CONSTRAINT FK. batch copy-and-move — ТОЛЬКО для SQLite (он не умеет ADD FK).
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("assistant_memories", schema=None, recreate="always") as batch:
            batch.add_column(sa.Column("category_id", sa.String(64), nullable=True))
            batch.create_foreign_key(
                "fk_assistant_memories_category", "memory_categories",
                ["category_id", "tenant_id", "user_id"], ["id", "tenant_id", "user_id"])
    else:
        op.add_column("assistant_memories", sa.Column("category_id", sa.String(64), nullable=True))
        op.create_foreign_key(
            "fk_assistant_memories_category", "assistant_memories", "memory_categories",
            ["category_id", "tenant_id", "user_id"], ["id", "tenant_id", "user_id"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("assistant_memories", schema=None, recreate="always") as batch:
            batch.drop_constraint("fk_assistant_memories_category", type_="foreignkey")
            batch.drop_column("category_id")
    else:
        op.drop_constraint("fk_assistant_memories_category", "assistant_memories", type_="foreignkey")
        op.drop_column("assistant_memories", "category_id")
    op.drop_index("uq_memory_categories_one_system", table_name="memory_categories")
    op.drop_index("ix_memory_categories_tenant_user", table_name="memory_categories")
    op.drop_table("memory_categories")
