"""tenant_user_profiles.timezone default UTC → Europe/Moscow

Среда — RU-bot, ~80%+ пользователей в Московском часовом поясе. Прежний
default "UTC" приводил к сдвигу времени в напоминаниях (R-39 shadow
показал: «на 7 вечера» парсилось как 19:00 UTC = 22:00 MSK).

Изменения:
- ALTER COLUMN tenant_user_profiles.timezone SET DEFAULT 'Europe/Moscow'
- Существующие строки с UTC уже переведены на MSK через SQL UPDATE
  (out-of-migration, see R-39 deploy log 2026-05-18).
- Новые tenant_user_profiles теперь будут с MSK по умолчанию.

Пользователи в других часовых поясах могут changing через /timezone
команду (TBD R-40) или прямой SQL UPDATE их profile.

Revision ID: 20260518_0046
Revises: 20260518_0045
Create Date: 2026-05-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260518_0046"
down_revision = "20260518_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tenant_user_profiles",
        "timezone",
        existing_type=sa.String(64),
        server_default="Europe/Moscow",
    )


def downgrade() -> None:
    op.alter_column(
        "tenant_user_profiles",
        "timezone",
        existing_type=sa.String(64),
        server_default="UTC",
    )
