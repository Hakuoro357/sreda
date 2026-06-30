"""#193 — durable персистентность диалога ReAct (react_checkpoint + react_checkpoint_write).

Две таблицы под кастомный EncryptedSqlCheckpointSaver (вариант B): один checkpoint = одна строка
(ВЕСЬ checkpoint incl channel_values сериализован штатным EncryptedSerializer → blob BYTEA +
checkpoint_type; channel_values НЕ выносим — 1-blob inline); writes — отдельная таблица, idx ЗНАКОВЫЙ
(спец-writes < 0: INTERRUPT=-3/RESUME=-4/ERROR=-1/SCHEDULED=-2). blob/metadata = ciphertext (шифрует
serde, не колонка) → LargeBinary. Аддитивно, обычные индексы (без CONCURRENTLY); legacy не тронут.

Revision ID: 20260623_0066
Revises: 20260622_0065
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260623_0066"
down_revision = "20260622_0065"
branch_labels = None
depends_on = None

_CP = "react_checkpoint"
_W = "react_checkpoint_write"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))

    op.create_table(
        _CP,
        sa.Column("thread_id", sa.String(160), primary_key=True),
        sa.Column("checkpoint_ns", sa.String(64), primary_key=True),
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("parent_checkpoint_id", sa.String(64), nullable=True),
        # type = "{serde_type}+{key_id}" → ширина с запасом под длинный key_id (CR R1 MINOR)
        sa.Column("checkpoint_type", sa.String(255), nullable=False),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.Column("metadata_type", sa.String(255), nullable=False),
        sa.Column("metadata", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_react_checkpoint_thread_created", _CP, ["thread_id", "checkpoint_ns", "created_at"],
    )

    op.create_table(
        _W,
        sa.Column("thread_id", sa.String(160), primary_key=True),
        sa.Column("checkpoint_ns", sa.String(64), primary_key=True),
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),  # ЗНАКОВЫЙ (спец-writes < 0)
        sa.Column("channel", sa.String(128), nullable=False),
        sa.Column("write_type", sa.String(255), nullable=False),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_react_cp_write_lookup", _W, ["thread_id", "checkpoint_ns", "checkpoint_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_react_cp_write_lookup", table_name=_W)
    op.drop_table(_W)
    op.drop_index("ix_react_checkpoint_thread_created", table_name=_CP)
    op.drop_table(_CP)
