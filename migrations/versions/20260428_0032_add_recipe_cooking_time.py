"""Add cooking_time_minutes column to recipes.

Контекст: при ревью pending-онбординга (2026-04-28) юзер заметил, что
текст ветки `_RECIPES` обещает «время приготовления и КБЖУ», но в
модели Recipe нет поля времени. Добавляем поле — оставляем текст как есть.

Семантика: общее время от начала до подачи на стол. Single int.
Если станет нужен prep+cook раздельно — расширим отдельной миграцией.

Backfill: NULL для всех существующих рецептов. LLM/юзер дозаполнят
при следующем редактировании.

Revision ID: 20260428_0032
Revises: 20260428_0031
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260428_0032"
down_revision = "20260428_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recipes",
        sa.Column("cooking_time_minutes", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recipes", "cooking_time_minutes")
