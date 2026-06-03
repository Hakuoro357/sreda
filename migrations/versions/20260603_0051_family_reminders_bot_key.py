"""family_reminders: add bot_key column (nullable) + backfill "sreda"

Phase 5 of the second-Telegram-bot feature (second-tg-bot plan).

Adds ``family_reminders.bot_key VARCHAR(64) NULL`` so new reminders store
the bot they were created in.  Backfills existing rows to "sreda"
(LEGACY_NULL_BOT_KEY) — those were all created via the single legacy bot.

The column is intentionally NULL-able here; read-fallback to
LEGACY_NULL_BOT_KEY is used in the delivery worker during the migration
window.

Revision ID: 20260603_0051
Revises: 20260603_0050
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0051"
down_revision = "20260603_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. Add nullable bot_key column (batch for SQLite compat) ----------
    with op.batch_alter_table("family_reminders") as batch_op:
        batch_op.add_column(
            sa.Column("bot_key", sa.String(64), nullable=True)
        )

    # --- 2. Backfill existing rows to "sreda" ------------------------------
    # All existing family_reminders were created via the single legacy bot.
    # Setting bot_key="sreda" ensures proactive workers route correctly
    # without falling back to LEGACY_NULL_BOT_KEY for these rows.
    bind.execute(sa.text(
        "UPDATE family_reminders SET bot_key = 'sreda' WHERE bot_key IS NULL"
    ))


def downgrade() -> None:
    with op.batch_alter_table("family_reminders") as batch_op:
        batch_op.drop_column("bot_key")
