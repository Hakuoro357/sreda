"""add free_tier_usage + reply_button_cache

Две таблицы из плана v2 «новый клиентский путь housewife-скила»:

1. ``free_tier_usage`` — счётчик LLM-вызовов в день для бесплатных
   пользователей. Лимит 20 turn'ов/день, после — отлуп с предложением
   подписки. Уникальный ключ (tenant_id, user_id, day).

2. ``reply_button_cache`` — кэш inline-кнопок, которые LLM генерирует
   в ответах через ``reply_with_buttons`` tool. Хранит label для
   короткого token (8 hex), чтобы влезало в Telegram callback_data
   (64 байта). TTL 1 час — старые токены игнорируются.

Revision ID: 20260425_0024
Revises: 20260423_0023
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260425_0024"
down_revision = "20260423_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # free_tier_usage: per-(tenant,user,day) счётчик LLM turn'ов.
    # UNIQUE для UPSERT-pattern (INSERT ... ON CONFLICT ... UPDATE).
    op.create_table(
        "free_tier_usage",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_free_tier_usage_unique",
        "free_tier_usage",
        ["tenant_id", "user_id", "day"],
        unique=True,
    )

    # reply_button_cache: token → label. Инлайн-кнопки LLM-ответа.
    # token — 8 hex-символов (32 бита, коллизии пренебрежимо редки на
    # TTL 1 час). Через callback_data "btn_reply:<token>" влезает в
    # лимит Telegram 64 байта с большим запасом.
    op.create_table(
        "reply_button_cache",
        sa.Column("token", sa.String(16), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reply_button_cache_created_at",
        "reply_button_cache",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_reply_button_cache_created_at", table_name="reply_button_cache")
    op.drop_table("reply_button_cache")
    op.drop_index("ix_free_tier_usage_unique", table_name="free_tier_usage")
    op.drop_table("free_tier_usage")
