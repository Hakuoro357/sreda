"""initial schema

Revision ID: 20260323_0001
Revises:
Create Date: 2026-03-23 13:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260323_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
    )
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])

    op.create_table(
        "tenant_features",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_tenant_features_tenant_id", "tenant_features", ["tenant_id"])
    op.create_index("ix_tenant_features_feature_key", "tenant_features", ["feature_key"])

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("telegram_account_id", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "assistants",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
    )
    op.create_index("ix_assistants_tenant_id", "assistants", ["tenant_id"])
    op.create_index("ix_assistants_workspace_id", "assistants", ["workspace_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_workspace_id", "jobs", ["workspace_id"])
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("channel_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_outbox_messages_tenant_id", "outbox_messages", ["tenant_id"])
    op.create_index("ix_outbox_messages_workspace_id", "outbox_messages", ["workspace_id"])
    op.create_index("ix_outbox_messages_status", "outbox_messages", ["status"])

    op.create_table(
        "eds_accounts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("assistant_id", sa.String(length=64), sa.ForeignKey("assistants.id"), nullable=False),
        sa.Column("site_key", sa.String(length=64), nullable=False),
        sa.Column("account_key", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("login", sa.String(length=255), nullable=False),
    )
    op.create_index("ix_eds_accounts_tenant_id", "eds_accounts", ["tenant_id"])
    op.create_index("ix_eds_accounts_workspace_id", "eds_accounts", ["workspace_id"])
    op.create_index("ix_eds_accounts_assistant_id", "eds_accounts", ["assistant_id"])
    op.create_unique_constraint("uq_eds_accounts_account_key", "eds_accounts", ["account_key"])

    op.create_table(
        "eds_claim_state",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("eds_account_id", sa.String(length=64), sa.ForeignKey("eds_accounts.id"), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("status_name", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eds_claim_state_eds_account_id", "eds_claim_state", ["eds_account_id"])
    op.create_index("ix_eds_claim_state_claim_id", "eds_claim_state", ["claim_id"])
    op.create_index("ix_eds_claim_state_fingerprint_hash", "eds_claim_state", ["fingerprint_hash"])

    op.create_table(
        "eds_change_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("eds_account_id", sa.String(length=64), sa.ForeignKey("eds_accounts.id"), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("change_type", sa.String(length=64), nullable=False),
        sa.Column("has_new_response", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requires_user_action", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eds_change_events_eds_account_id", "eds_change_events", ["eds_account_id"])
    op.create_index("ix_eds_change_events_claim_id", "eds_change_events", ["claim_id"])
    op.create_index("ix_eds_change_events_change_type", "eds_change_events", ["change_type"])

    op.create_table(
        "eds_delivery_records",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("eds_account_id", sa.String(length=64), sa.ForeignKey("eds_accounts.id"), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_chat_id", sa.String(length=64), nullable=False),
        sa.Column("text_message_id", sa.String(length=64), nullable=True),
        sa.Column("last_message_hash", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_eds_delivery_records_eds_account_id", "eds_delivery_records", ["eds_account_id"])
    op.create_index("ix_eds_delivery_records_claim_id", "eds_delivery_records", ["claim_id"])
    op.create_index("ix_eds_delivery_records_recipient_chat_id", "eds_delivery_records", ["recipient_chat_id"])


def downgrade() -> None:
    op.drop_index("ix_eds_delivery_records_recipient_chat_id", table_name="eds_delivery_records")
    op.drop_index("ix_eds_delivery_records_claim_id", table_name="eds_delivery_records")
    op.drop_index("ix_eds_delivery_records_eds_account_id", table_name="eds_delivery_records")
    op.drop_table("eds_delivery_records")

    op.drop_index("ix_eds_change_events_change_type", table_name="eds_change_events")
    op.drop_index("ix_eds_change_events_claim_id", table_name="eds_change_events")
    op.drop_index("ix_eds_change_events_eds_account_id", table_name="eds_change_events")
    op.drop_table("eds_change_events")

    op.drop_index("ix_eds_claim_state_fingerprint_hash", table_name="eds_claim_state")
    op.drop_index("ix_eds_claim_state_claim_id", table_name="eds_claim_state")
    op.drop_index("ix_eds_claim_state_eds_account_id", table_name="eds_claim_state")
    op.drop_table("eds_claim_state")

    op.drop_constraint("uq_eds_accounts_account_key", "eds_accounts", type_="unique")
    op.drop_index("ix_eds_accounts_assistant_id", table_name="eds_accounts")
    op.drop_index("ix_eds_accounts_workspace_id", table_name="eds_accounts")
    op.drop_index("ix_eds_accounts_tenant_id", table_name="eds_accounts")
    op.drop_table("eds_accounts")

    op.drop_index("ix_outbox_messages_status", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_workspace_id", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_tenant_id", table_name="outbox_messages")
    op.drop_table("outbox_messages")

    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_job_type", table_name="jobs")
    op.drop_index("ix_jobs_workspace_id", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_assistants_workspace_id", table_name="assistants")
    op.drop_index("ix_assistants_tenant_id", table_name="assistants")
    op.drop_table("assistants")

    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_tenant_features_feature_key", table_name="tenant_features")
    op.drop_index("ix_tenant_features_tenant_id", table_name="tenant_features")
    op.drop_table("tenant_features")

    op.drop_index("ix_workspaces_tenant_id", table_name="workspaces")
    op.drop_table("workspaces")

    op.drop_table("tenants")
