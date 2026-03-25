"""add connect sessions and tenant eds accounts

Revision ID: 20260325_0005
Revises: 20260325_0004
Create Date: 2026-03-25 21:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260325_0005"
down_revision = "20260325_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_eds_accounts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("assistant_id", sa.String(length=64), sa.ForeignKey("assistants.id"), nullable=True),
        sa.Column("account_index", sa.String(length=32), nullable=False),
        sa.Column("account_role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_verification"),
        sa.Column("login_masked", sa.String(length=255), nullable=False),
        sa.Column("secure_record_id", sa.String(length=64), sa.ForeignKey("secure_records.id"), nullable=True),
        sa.Column("last_connect_session_id", sa.String(length=64), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_eds_accounts_tenant_id", "tenant_eds_accounts", ["tenant_id"])
    op.create_index("ix_tenant_eds_accounts_workspace_id", "tenant_eds_accounts", ["workspace_id"])
    op.create_index("ix_tenant_eds_accounts_assistant_id", "tenant_eds_accounts", ["assistant_id"])
    op.create_index("ix_tenant_eds_accounts_account_index", "tenant_eds_accounts", ["account_index"])
    op.create_index("ix_tenant_eds_accounts_account_role", "tenant_eds_accounts", ["account_role"])
    op.create_index("ix_tenant_eds_accounts_status", "tenant_eds_accounts", ["status"])
    op.create_index("ix_tenant_eds_accounts_secure_record_id", "tenant_eds_accounts", ["secure_record_id"])

    op.create_table(
        "connect_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("account_slot_type", sa.String(length=32), nullable=False),
        sa.Column("one_time_token_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="created"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secure_record_id", sa.String(length=64), sa.ForeignKey("secure_records.id"), nullable=True),
        sa.Column(
            "tenant_eds_account_id",
            sa.String(length=64),
            sa.ForeignKey("tenant_eds_accounts.id"),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_connect_sessions_tenant_id", "connect_sessions", ["tenant_id"])
    op.create_index("ix_connect_sessions_workspace_id", "connect_sessions", ["workspace_id"])
    op.create_index("ix_connect_sessions_user_id", "connect_sessions", ["user_id"])
    op.create_index("ix_connect_sessions_session_type", "connect_sessions", ["session_type"])
    op.create_index("ix_connect_sessions_account_slot_type", "connect_sessions", ["account_slot_type"])
    op.create_index("ix_connect_sessions_one_time_token_hash", "connect_sessions", ["one_time_token_hash"], unique=True)
    op.create_index("ix_connect_sessions_status", "connect_sessions", ["status"])
    op.create_index("ix_connect_sessions_expires_at", "connect_sessions", ["expires_at"])
    op.create_index("ix_connect_sessions_secure_record_id", "connect_sessions", ["secure_record_id"])
    op.create_index("ix_connect_sessions_tenant_eds_account_id", "connect_sessions", ["tenant_eds_account_id"])

    op.create_foreign_key(
        "fk_tenant_eds_accounts_last_connect_session_id",
        "tenant_eds_accounts",
        "connect_sessions",
        ["last_connect_session_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_tenant_eds_accounts_last_connect_session_id", "tenant_eds_accounts", type_="foreignkey")

    op.drop_index("ix_connect_sessions_tenant_eds_account_id", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_secure_record_id", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_expires_at", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_status", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_one_time_token_hash", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_account_slot_type", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_session_type", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_user_id", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_workspace_id", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_tenant_id", table_name="connect_sessions")
    op.drop_table("connect_sessions")

    op.drop_index("ix_tenant_eds_accounts_secure_record_id", table_name="tenant_eds_accounts")
    op.drop_index("ix_tenant_eds_accounts_status", table_name="tenant_eds_accounts")
    op.drop_index("ix_tenant_eds_accounts_account_role", table_name="tenant_eds_accounts")
    op.drop_index("ix_tenant_eds_accounts_account_index", table_name="tenant_eds_accounts")
    op.drop_index("ix_tenant_eds_accounts_assistant_id", table_name="tenant_eds_accounts")
    op.drop_index("ix_tenant_eds_accounts_workspace_id", table_name="tenant_eds_accounts")
    op.drop_index("ix_tenant_eds_accounts_tenant_id", table_name="tenant_eds_accounts")
    op.drop_table("tenant_eds_accounts")
