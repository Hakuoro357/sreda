"""add address_form column to tenant_user_profiles (онбординг ты/вы)

После admin-approve бот спрашивает у юзера:
  1. «Как тебя зовут?» → пишется в `display_name`.
  2. «На ты или на вы?» → пишется в этом новом столбце.

Только после обоих ответов запускается housewife-welcome про семью.
NULL в `address_form` означает «ещё не выбрано» — LLM/ack-фразы
fallback'ат на нейтральную форму без жёстких глагольных форм.

Существующим тенантам ставим NULL (8 шт. на проде на момент 2026-04-27);
при следующем визите webhook поймает NULL и спросит ещё раз.

Revision ID: 20260425_0026
Revises: 20260425_0025
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260425_0026"
down_revision = "20260425_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_user_profiles",
        sa.Column("address_form", sa.String(8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_user_profiles", "address_form")
