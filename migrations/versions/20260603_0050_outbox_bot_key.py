"""outbox_messages: add bot_key column (nullable) + backfill "sreda"

Phase 4a of the second-Telegram-bot feature (second-tg-bot plan).

Adds ``outbox_messages.bot_key VARCHAR(64) NULL`` so the delivery worker
can route each pending row through the correct bot's TelegramClient.

The column is intentionally NULL-able in this migration.  It will be
made NOT NULL only AFTER Phase 5 ensures every producer (including the
four proactive workers) sets bot_key on creation.  The delivery worker
uses ``row.bot_key or LEGACY_NULL_BOT_KEY`` during the migration window.

Backfill: existing rows (all produced by the single "sreda" bot before
this change) are set to "sreda" so they continue to route correctly
without relying on the NULL fallback.

Revision ID: 20260603_0050
Revises: 20260603_0049
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0050"
down_revision = "20260603_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. Add nullable bot_key column (batch for SQLite compat) ---------
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.add_column(
            sa.Column("bot_key", sa.String(64), nullable=True)
        )

    # --- 2. Backfill existing rows to "sreda" ------------------------------
    # All existing outbox rows were produced by the single legacy bot.
    # Setting bot_key="sreda" now ensures delivery routing works correctly
    # without falling back to LEGACY_NULL_BOT_KEY for these rows.
    bind.execute(sa.text(
        "UPDATE outbox_messages SET bot_key = 'sreda' WHERE bot_key IS NULL"
    ))


def downgrade() -> None:
    # Drop the column; the backfilled values are discarded with it.
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.drop_column("bot_key")
