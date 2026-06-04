"""family_reminders: make bot_key NOT NULL (Phase 5 final step)

Revision ID: 20260603_0053
Revises: 20260603_0052
Create Date: 2026-06-03

Flips ``family_reminders.bot_key`` from NULL-able to NOT NULL with a
``server_default`` of ``'sreda'`` (LEGACY_NULL_BOT_KEY).

Prerequisites:
  - 20260603_0051: column exists + legacy backfill to 'sreda'
  - 20260603_0052: outbox_messages.bot_key NOT NULL (sibling migration)
  - Phase 5 code: all reminder creation paths supply bot_key

Any remaining NULL rows (should be zero after migration 0051 backfill)
are back-filled to 'sreda' before the constraint is added, matching the
pattern used in migration 0052 for outbox_messages.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0053"
down_revision = "20260603_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Safety net: any remaining NULL rows get sreda before the constraint.
    bind.execute(sa.text(
        "UPDATE family_reminders SET bot_key = 'sreda' WHERE bot_key IS NULL"
    ))

    # Flip to NOT NULL with server_default (batch for SQLite compat).
    with op.batch_alter_table("family_reminders") as batch_op:
        batch_op.alter_column(
            "bot_key",
            existing_type=sa.String(64),
            nullable=False,
            server_default=sa.text("'sreda'"),
        )


def downgrade() -> None:
    # Revert to nullable so the previous migration state is valid.
    with op.batch_alter_table("family_reminders") as batch_op:
        batch_op.alter_column(
            "bot_key",
            existing_type=sa.String(64),
            nullable=True,
            server_default=None,
        )
