"""add max_chat_id column to users

Phase 1 of MAX integration (после probe в Phase 0).

Probe показал что MAX отделяет ``user_id`` от ``chat_id``:
- Inbound update.user.user_id = 40921122 (Boris)
- Inbound update.chat_id = 320955459 (DM-чат с ботом)

Для send_message нужен ``recipient = {"chat_id": ...}``, не user_id.
Поэтому помимо существующей колонки ``users.max_account_id`` (= user_id)
нужна отдельная ``users.max_chat_id`` для outbound маршрутизации.

При получении incoming `bot_started` или `message_created` мы записываем
оба ID; outbox-delivery worker берёт ``max_chat_id`` для send.

Revision ID: 20260504_0039
Revises: 20260504_0038
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260504_0039"
down_revision = "20260504_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("max_chat_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_users_max_chat_id", "users", ["max_chat_id"])


def downgrade() -> None:
    op.drop_index("ix_users_max_chat_id", table_name="users")
    op.drop_column("users", "max_chat_id")
