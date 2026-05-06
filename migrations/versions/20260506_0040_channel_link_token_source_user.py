"""add source_user_id to channel link tokens

Revision ID: 20260506_0040
Revises: 20260504_0039
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260506_0040"
down_revision = "20260504_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_link_tokens",
        sa.Column("source_user_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_channel_link_tokens_source_user_id_users",
        "channel_link_tokens",
        "users",
        ["source_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_clt_source_user_id",
        "channel_link_tokens",
        ["source_user_id"],
    )
    # Invalidate existing pending tokens (none have source_user_id yet —
    # the column was just added). Forces fresh tokens with proper user
    # scope. `IS NULL` literal — parametrized `IS :param` doesn't work
    # in Postgres (IS requires literal NULL keyword, not bind param).
    op.execute(
        sa.text(
            "UPDATE channel_link_tokens "
            "SET used_at = now() "
            "WHERE source_user_id IS NULL AND used_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_clt_source_user_id", table_name="channel_link_tokens")
    op.drop_constraint(
        "fk_channel_link_tokens_source_user_id_users",
        "channel_link_tokens",
        type_="foreignkey",
    )
    op.drop_column("channel_link_tokens", "source_user_id")
