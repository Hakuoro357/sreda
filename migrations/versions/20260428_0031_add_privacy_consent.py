"""Add privacy_policy_accepted_at column to tenant_user_profiles.

152-ФЗ Часть 2 (2026-04-28). Без UX impact на текущем этапе:
- Колонка добавляется как nullable, default NULL
- Webhook gate'ов нет (юзер без согласия отвечает как обычно)
- Mini App кнопки нет
- Когда sredaspace.ru/privacy запустится — отдельной задачей
  добавим UI/gate

Поле живёт в `tenant_user_profiles` (user-level), не в `tenants`,
потому что согласие — это согласие конкретного владельца Telegram
аккаунта.

Revision ID: 20260428_0031
Revises: 20260428_0030
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260428_0031"
down_revision = "20260428_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_user_profiles",
        sa.Column(
            "privacy_policy_accepted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_user_profiles", "privacy_policy_accepted_at")
