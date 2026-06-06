"""users: add last_bot_key column (nullable) — current-bot routing (#109)

Revision ID: 20260606_0054
Revises: 20260603_0053
Create Date: 2026-06-06

Adds ``users.last_bot_key VARCHAR(64) NULL`` so async producers (reminder /
proactive / onboarding workers) can deliver notifications to the bot the
user is CURRENTLY messaging on, not the bot frozen at reminder-creation
time or the global system default.

Background (#109): users who migrated from the old ``@sreda01_bot``
(bot_key ``"sreda"``) to the new ``@sreda_home_bot`` (bot_key
``"sreda_home"``) kept receiving async notifications on the OLD bot —
lost if they abandoned it. The fix stamps ``users.last_bot_key`` on each
inbound (``ensure_telegram_user_bundle``); ``resolve_outbox_routings``
then channel-aware-populates ``OutboxRouting.bot_key`` from it.

The column is intentionally NULL-able and is NOT backfilled. NULL means
"current bot unknown" → producers fall back to their existing behaviour
(``reminder.bot_key`` / ``system_default_bot_key``), so this migration is
purely additive and backwards-compatible. No NOT-NULL follow-up is needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op


revision = "20260606_0054"
down_revision = "20260603_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent guard: skip if the column already exists (re-run safety).
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("users")}
    if "last_bot_key" in cols:
        return

    # Batch for SQLite compatibility (matches migration 0050 style).
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("last_bot_key", sa.String(64), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("users")}
    if "last_bot_key" not in cols:
        return

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("last_bot_key")
