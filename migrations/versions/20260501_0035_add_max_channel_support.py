"""add max_account_id to users + preferred_channel to tenants

Phase 2 переезда на dual-channel (Telegram + VK MAX). Колонки добавляются
nullable — существующие 12 TG-юзеров не затрагиваются. Новые регистрации
через сайт sredaspace.ru будут заполнять preferred_channel='telegram'
или 'max' при выборе тарифа. max_account_id заполняется при первом
inbound от MAX-юзера в onboarding.

Revision ID: 20260501_0035
Revises: 20260430_0034
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260501_0035"
down_revision = "20260430_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MAX user identifier (parallel to telegram_account_id).
    # Nullable: legacy TG-only пользователи остаются с max_account_id IS NULL.
    op.add_column(
        "users",
        sa.Column("max_account_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_users_max_account_id",
        "users",
        ["max_account_id"],
    )

    # Per-tenant preferred channel ('telegram' | 'max'). Nullable:
    # legacy tenants остаются с NULL = TG-only (по умолчанию). Новые
    # tenants при регистрации через sredaspace.ru тариф-форму выбирают
    # явно. Если NULL — handler'ы трактуют как 'telegram' (back-compat).
    op.add_column(
        "tenants",
        sa.Column("preferred_channel", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "preferred_channel")
    op.drop_index("ix_users_max_account_id", table_name="users")
    op.drop_column("users", "max_account_id")
