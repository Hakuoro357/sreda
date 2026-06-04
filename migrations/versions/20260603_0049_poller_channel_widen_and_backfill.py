"""poller_offsets / poller_heartbeats: widen channel PK to String(64)

Phase 3 of the second-Telegram-bot feature (second-tg-bot plan).

Problem: the ``channel`` primary-key column in both tables was String(16),
which cannot hold a per-bot key like ``"telegram:sreda_home"`` (19 chars).

Fix:
  1. Widen both channel PKs to String(64).
  2. Backfill the existing ``"telegram"`` offset/heartbeat row to
     ``"telegram:sreda"`` so the ``sreda`` poller resumes from the
     correct offset after cutover.

Why ``"telegram:sreda"`` instead of keeping ``"telegram"`` as-is:
  The new per-bot key scheme uses ``f"telegram:{bot_key}"`` so all bots
  are namespaced consistently.  If we left the legacy row under
  ``"telegram"`` and the sreda poller wrote to ``"telegram:sreda"``, the
  first run would not find the old row and would restart from offset 0,
  re-delivering every Telegram update since the last consumed one.
  Backfilling the row to ``"telegram:sreda"`` ensures a seamless resume.

  The downgrade re-backfills ``"telegram:sreda"`` → ``"telegram"`` and
  shrinks the column back to 16, which is safe as long as no
  ``"telegram:sreda_home"`` rows have been inserted yet.

Revision ID: 20260603_0049
Revises: 20260603_0048
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0049"
down_revision = "20260603_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # --- 1. Widen channel column in both tables (batch for SQLite compat) ---
    with op.batch_alter_table("poller_offsets") as batch_op:
        batch_op.alter_column(
            "channel",
            existing_type=sa.String(16),
            type_=sa.String(64),
            existing_nullable=False,
        )

    with op.batch_alter_table("poller_heartbeats") as batch_op:
        batch_op.alter_column(
            "channel",
            existing_type=sa.String(16),
            type_=sa.String(64),
            existing_nullable=False,
        )

    # --- 2. Backfill legacy "telegram" row to "telegram:sreda" -----------
    # This ensures the sreda poller resumes from the correct offset after
    # cutover to --bot-key sreda (Phase 8).  Safe to run on a DB that has
    # no poller_offsets row yet (no-op on 0 rows).
    bind.execute(sa.text(
        "UPDATE poller_offsets SET channel = 'telegram:sreda'"
        " WHERE channel = 'telegram'"
    ))
    bind.execute(sa.text(
        "UPDATE poller_heartbeats SET channel = 'telegram:sreda'"
        " WHERE channel = 'telegram'"
    ))


def downgrade() -> None:
    bind = op.get_bind()

    # Re-backfill "telegram:sreda" → "telegram" before shrinking the column.
    # This is only safe if "telegram:sreda_home" rows have NOT yet been
    # inserted.  If they have, the downgrade will leave orphaned rows that
    # will be silently truncated or fail the PK constraint.
    bind.execute(sa.text(
        "UPDATE poller_offsets SET channel = 'telegram'"
        " WHERE channel = 'telegram:sreda'"
    ))
    bind.execute(sa.text(
        "UPDATE poller_heartbeats SET channel = 'telegram'"
        " WHERE channel = 'telegram:sreda'"
    ))

    with op.batch_alter_table("poller_offsets") as batch_op:
        batch_op.alter_column(
            "channel",
            existing_type=sa.String(64),
            type_=sa.String(16),
            existing_nullable=False,
        )

    with op.batch_alter_table("poller_heartbeats") as batch_op:
        batch_op.alter_column(
            "channel",
            existing_type=sa.String(64),
            type_=sa.String(16),
            existing_nullable=False,
        )
