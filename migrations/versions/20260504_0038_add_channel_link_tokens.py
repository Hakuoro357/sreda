"""add channel_link_tokens table for cross-channel account linking

Phase 7 of MAX integration plan (recall-broadcast-fanout / max-integration plans).
Поддерживает Boris's deep-link + callback button flow для связывания
TG ↔ MAX каналов одного юзера в один tenant.

Schema:
- ``id`` — primary key (UUID-ish, ``link_<24>``).
- ``tenant_id`` — FK к existing tenant (source — где юзер уже зарегистрирован).
- ``source_channel`` / ``target_channel`` — `"telegram"` / `"max"`.
- ``token_hash`` — SHA-256 hex от raw token. Raw token живёт только в URL
  parameter и в response к mini-app frontend; в БД — only hash. Это
  защищает pending links при DB-leak'е.
- ``expires_at`` — TTL 5 минут (R5 hardening).
- ``used_at`` — single-use, atomic consume через ``UPDATE ... WHERE used_at
  IS NULL RETURNING``.

Composite index: ``(tenant_id, expires_at, used_at)`` — для rate-limit
counter (R5: 5 successful starts per 30 min) и для cleanup queries.
``token_hash`` unique → fast lookup at consume time.

Cleanup: 6-hourly job DELETE'ит rows with ``expires_at < now() - interval '1 day'``.

Revision ID: 20260504_0038
Revises: 20260504_0037
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260504_0038"
down_revision = "20260504_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_link_tokens",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_channel", sa.String(16), nullable=False),
        sa.Column("target_channel", sa.String(16), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_channel_link_tokens_active",
        "channel_link_tokens",
        ["tenant_id", "expires_at", "used_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_link_tokens_active",
        table_name="channel_link_tokens",
    )
    op.drop_table("channel_link_tokens")
