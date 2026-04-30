"""add runtime_config table (legacy create_all gap)

Таблица существовала в моделях с 2026-04-22 (commit добавлен через
``Base.metadata.create_all()`` в dev), но миграции для неё не было.
На SQLite prod создалась автоматически из-за create_all() при первом
запуске. Перед переездом на PostgreSQL нужна явная миграция чтобы
``alembic upgrade head`` создавал её на чистой БД.

Revision ID: 20260430_0034
Revises: 20260429_0033
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260430_0034"
down_revision = "20260429_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Идемпотентность: на проде SQLite таблица УЖЕ существует
    # (legacy create_all). Skip create если есть.
    bind = op.get_bind()
    if "runtime_config" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "runtime_config",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=256), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_config")
