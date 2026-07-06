"""admin auth via Telegram (#305): admin_sessions + admin_login_challenges

Две аддитивные таблицы (Фаза 1). Схема деплоится ПЕРВОЙ — auth-код Фазы 2
читает admin_sessions, поэтому таблицы должны существовать до него.
Backwards-compatible: новые таблицы, старый код их не замечает; fallback-токен
работает как раньше (он НЕ использует admin_sessions, у него свой HMAC-cookie).

Revision ID: 20260704_0077
Revises: 20260703_0076
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260704_0077"
down_revision = "20260703_0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("tg_id", sa.String(64), nullable=False),
        sa.Column("auth_method", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_admin_sessions_expires", "admin_sessions", ["expires_at"])

    op.create_table(
        "admin_login_challenges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("challenge_id", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_bind_hash", sa.String(64), nullable=False),
        sa.Column("human_code", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("button_send_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tg_id", sa.String(64), nullable=True),
        sa.Column("bot_key", sa.String(32), nullable=True),
        sa.Column("chat_id", sa.String(64), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_admin_login_challenges_client_ip_created",
        "admin_login_challenges", ["client_ip", "created_at"],
    )
    op.create_index(
        "ix_admin_login_challenges_expires", "admin_login_challenges", ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_login_challenges_expires", table_name="admin_login_challenges")
    op.drop_index("ix_admin_login_challenges_client_ip_created", table_name="admin_login_challenges")
    op.drop_table("admin_login_challenges")
    op.drop_index("ix_admin_sessions_expires", table_name="admin_sessions")
    op.drop_table("admin_sessions")
