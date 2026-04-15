"""add assistant_memories (Phase 3)

Per-user memory store. Two tiers for MVP:
  * ``core``     — stable facts about the user
  * ``episodic`` — conversation / event summaries

Embeddings are JSON-encoded float arrays in ``embedding_json``. Good
enough for a few hundred rows per user; swap to pgvector later by
editing ``MemoryRepository.recall`` and this migration.

Revision ID: 20260415_0013
Revises: 20260415_0012
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0013"
down_revision = "20260415_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_memories",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="agent_inferred",
        ),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_assistant_memories_tenant_id", "assistant_memories", ["tenant_id"]
    )
    op.create_index(
        "ix_assistant_memories_user_id", "assistant_memories", ["user_id"]
    )
    op.create_index(
        "ix_assistant_memories_tenant_user_tier",
        "assistant_memories",
        ["tenant_id", "user_id", "tier"],
    )
    op.create_index(
        "ix_assistant_memories_created_at",
        "assistant_memories",
        ["created_at"],
    )

    with op.batch_alter_table("assistant_memories") as batch_op:
        batch_op.alter_column("embedding_dim", server_default=None)
        batch_op.alter_column("source", server_default=None)
        batch_op.alter_column("access_count", server_default=None)
        batch_op.alter_column("created_at", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_memories_created_at", table_name="assistant_memories"
    )
    op.drop_index(
        "ix_assistant_memories_tenant_user_tier", table_name="assistant_memories"
    )
    op.drop_index("ix_assistant_memories_user_id", table_name="assistant_memories")
    op.drop_index("ix_assistant_memories_tenant_id", table_name="assistant_memories")
    op.drop_table("assistant_memories")
