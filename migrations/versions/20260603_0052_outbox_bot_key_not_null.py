"""outbox_messages: make bot_key NOT NULL (Phase 5 final step)

Phase 5 of the second-Telegram-bot feature (second-tg-bot plan).

This is the LAST migration in the Phase 4/5 sequence.  It flips
``outbox_messages.bot_key`` from NULL-able to NOT NULL, enforcing
that every producer sets bot_key at insert time.

Prerequisites (must be deployed BEFORE running this migration):
  - 20260603_0050: outbox_messages.bot_key column exists + legacy backfill
  - 20260603_0051: family_reminders.bot_key column exists + legacy backfill
  - Phase 5 code: all four proactive workers set outbox.bot_key from
    reminder.bot_key (or system_default); enforcement test passes with
    no opt-out markers.

Any NULL outbox rows still present at upgrade time (should be zero after
the Phase 4 backfill) are back-filled to "sreda" here as a safety net
before the constraint is added.

Revision ID: 20260603_0052
Revises: 20260603_0051
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0052"
down_revision = "20260603_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Safety net: any rows still NULL get sreda before the constraint.
    bind.execute(sa.text(
        "UPDATE outbox_messages SET bot_key = 'sreda' WHERE bot_key IS NULL"
    ))

    # Flip to NOT NULL (batch for SQLite compat).
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.alter_column(
            "bot_key",
            existing_type=sa.String(64),
            nullable=False,
            server_default=sa.text("'sreda'"),
        )


def downgrade() -> None:
    # Revert to nullable so the previous migration state is valid.
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.alter_column(
            "bot_key",
            existing_type=sa.String(64),
            nullable=True,
        )
