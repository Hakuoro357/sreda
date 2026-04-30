"""add poller_offsets, poller_heartbeats, inbound_messages.processing_status

Phase 1 перехода с webhook на long-polling (см. plan
mellow-discovering-conway.md). Три изменения в одной миграции, все
backward-compatible:

1. ``poller_offsets`` — last update_id per channel. Poller сохраняет
   offset тут после durable ingest каждого update'а. Atomic: одна
   commit'а на update_id, другая на inbound_messages — корректность
   через idempotency по external_update_id (а не single-tx).

2. ``poller_heartbeats`` — liveness/health отдельно от offset. ``last_attempt_at``
   обновляется после КАЖДОГО getUpdates (включая 200 [] и сетевые ошибки) —
   probe видит что процесс жив. ``last_ok_at`` — только при successful API
   ответе → distinguish между «процесс мёртв» и «TG API down». ``last_error``
   обрезан до 1000 символов.

3. ``inbound_messages.processing_status`` — explicit lifecycle вместо
   inferring через outerjoin(agent_runs). Значения:
       ingested → processing_started → processed
   либо ``ignored`` (pending user / unsupported / service command).
   Duplicate update_id не создаёт новую row — existing остаётся в своём
   статусе.

   Backfill: все existing inbound_messages → ``processed``. Distinguishing
   duplicates задним числом ненадёжно (нужно сравнивать update_id попарно
   с возможными ложными срабатываниями); historical rows monitor поднимать
   не должен.

Revision ID: 20260502_0036
Revises: 20260501_0035
Create Date: 2026-05-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260502_0036"
down_revision = "20260501_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ============ poller_offsets ============
    op.create_table(
        "poller_offsets",
        sa.Column("channel", sa.String(length=16), primary_key=True),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ============ poller_heartbeats ============
    op.create_table(
        "poller_heartbeats",
        sa.Column("channel", sa.String(length=16), primary_key=True),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    # ============ inbound_messages.processing_status ============
    op.add_column(
        "inbound_messages",
        sa.Column(
            "processing_status",
            sa.String(length=32),
            nullable=False,
            server_default="ingested",
        ),
    )
    # Backfill existing rows → 'processed' (см. docstring).
    op.execute(
        "UPDATE inbound_messages SET processing_status = 'processed'"
    )
    op.create_index(
        "ix_inbound_messages_processing_status",
        "inbound_messages",
        ["processing_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_messages_processing_status",
        table_name="inbound_messages",
    )
    op.drop_column("inbound_messages", "processing_status")
    op.drop_table("poller_heartbeats")
    op.drop_table("poller_offsets")
