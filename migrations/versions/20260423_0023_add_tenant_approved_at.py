"""add tenants.approved_at (manual approval gate, MVP-костыль)

Adds a nullable ``approved_at`` timestamp to the ``tenants`` table.
New tenants created via the Telegram /start flow start with NULL =
«заявка принята, ожидает одобрения модератором» — the webhook handler
silent-drops every subsequent message until an admin clicks "Одобрить"
in /admin/users.

To avoid breaking existing live users on deploy, the upgrade UPDATEs
every row currently in ``tenants`` to ``NOW()`` (auto-approve). Only
brand-new tenants created AFTER the migration land NULL by default.

Revision ID: 20260423_0023
Revises: 20260422_0022
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260423_0023"
down_revision = "20260422_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Auto-approve всех существующих тенантов. Без этого живые
    # пользователи на проде перестанут получать ответы после деплоя.
    op.execute(
        "UPDATE tenants SET approved_at = CURRENT_TIMESTAMP "
        "WHERE approved_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("tenants", "approved_at")
