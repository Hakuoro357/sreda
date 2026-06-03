"""inbound_messages: partial unique index on (channel_type, bot_key, external_update_id)

Phase 2 of the second-Telegram-bot feature (second-tg-bot plan).

Problem: Telegram update_id counters are per-bot, so bot-A's update 42
and bot-B's update 42 are different events.  The old dedup check filtered
only on ``external_update_id``, causing a false-duplicate when both bots
deliver the same numeric id.

Fix: widen the uniqueness key to (channel_type, bot_key, external_update_id).
Only non-NULL external_update_ids are constrained (NULL rows are synthetic
events that must remain freely insertable).

Also backfills existing inbound_messages rows that have a NULL bot_key to
'sreda' (the legacy Telegram bot) — the column was added with a NOT NULL
default in migration 0035, so NULL rows should not exist in practice, but
this guard runs anyway for safety.

Revision ID: 20260603_0048
Revises: 20260520_0047
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260603_0048"
down_revision = "20260520_0047"
branch_labels = None
depends_on = None

_INDEX_NAME = "ux_inbound_dedup_channel_bot_update"


def upgrade() -> None:
    bind = op.get_bind()

    # Safety backfill: any legacy row with NULL bot_key gets 'sreda'.
    # The column was added NOT NULL with server_default in 0035, so this
    # should be a no-op on a healthy DB, but it prevents the unique index
    # from being foiled by unexpected NULLs in the key columns.
    bind.execute(sa.text(
        "UPDATE inbound_messages SET bot_key = 'sreda' WHERE bot_key IS NULL"
    ))

    op.create_index(
        _INDEX_NAME,
        "inbound_messages",
        ["channel_type", "bot_key", "external_update_id"],
        unique=True,
        postgresql_where=sa.text("external_update_id IS NOT NULL"),
        sqlite_where=sa.text("external_update_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="inbound_messages")
