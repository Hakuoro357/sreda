"""add inbound messages and secure storage

Revision ID: 20260325_0003
Revises: 20260324_0002
Create Date: 2026-03-25 13:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260325_0003"
down_revision = "20260324_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "secure_records",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("record_type", sa.String(length=64), nullable=False),
        sa.Column("record_key", sa.String(length=128), nullable=False),
        sa.Column("encrypted_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_secure_records_tenant_id", "secure_records", ["tenant_id"])
    op.create_index("ix_secure_records_workspace_id", "secure_records", ["workspace_id"])
    op.create_index("ix_secure_records_record_type", "secure_records", ["record_type"])
    op.create_index("ix_secure_records_record_key", "secure_records", ["record_key"])

    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("channel_type", sa.String(length=32), nullable=False),
        sa.Column("channel_account_id", sa.String(length=64), nullable=True),
        sa.Column("bot_key", sa.String(length=64), nullable=False),
        sa.Column("external_update_id", sa.String(length=128), nullable=True),
        sa.Column("sender_chat_id", sa.String(length=128), nullable=True),
        sa.Column("message_text_sanitized", sa.Text(), nullable=True),
        sa.Column("contains_sensitive_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("secure_record_id", sa.String(length=64), sa.ForeignKey("secure_records.id"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="accepted"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inbound_messages_tenant_id", "inbound_messages", ["tenant_id"])
    op.create_index("ix_inbound_messages_workspace_id", "inbound_messages", ["workspace_id"])
    op.create_index("ix_inbound_messages_user_id", "inbound_messages", ["user_id"])
    op.create_index("ix_inbound_messages_channel_type", "inbound_messages", ["channel_type"])
    op.create_index("ix_inbound_messages_bot_key", "inbound_messages", ["bot_key"])
    op.create_index("ix_inbound_messages_external_update_id", "inbound_messages", ["external_update_id"])
    op.create_index("ix_inbound_messages_sender_chat_id", "inbound_messages", ["sender_chat_id"])
    op.create_index("ix_inbound_messages_secure_record_id", "inbound_messages", ["secure_record_id"])
    op.create_index("ix_inbound_messages_status", "inbound_messages", ["status"])


def downgrade() -> None:
    op.drop_index("ix_inbound_messages_status", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_secure_record_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_sender_chat_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_external_update_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_bot_key", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_channel_type", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_user_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_workspace_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_tenant_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")

    op.drop_index("ix_secure_records_record_key", table_name="secure_records")
    op.drop_index("ix_secure_records_record_type", table_name="secure_records")
    op.drop_index("ix_secure_records_workspace_id", table_name="secure_records")
    op.drop_index("ix_secure_records_tenant_id", table_name="secure_records")
    op.drop_table("secure_records")
