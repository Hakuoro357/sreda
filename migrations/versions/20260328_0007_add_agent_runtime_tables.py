"""add agent runtime tables

Revision ID: 20260328_0007
Revises: 20260325_0006
Create Date: 2026-03-28 23:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260328_0007"
down_revision = "20260325_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_threads",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=64), nullable=True),
        sa.Column("channel_type", sa.String(length=32), nullable=False),
        sa.Column("external_chat_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_threads_tenant_id", "agent_threads", ["tenant_id"], unique=False)
    op.create_index("ix_agent_threads_workspace_id", "agent_threads", ["workspace_id"], unique=False)
    op.create_index("ix_agent_threads_assistant_id", "agent_threads", ["assistant_id"], unique=False)
    op.create_index("ix_agent_threads_channel_type", "agent_threads", ["channel_type"], unique=False)
    op.create_index("ix_agent_threads_external_chat_id", "agent_threads", ["external_chat_id"], unique=False)
    op.create_index("ix_agent_threads_status", "agent_threads", ["status"], unique=False)
    op.create_index(
        "ux_agent_threads_tenant_channel_chat",
        "agent_threads",
        ["tenant_id", "channel_type", "external_chat_id"],
        unique=True,
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("inbound_message_id", sa.String(length=64), nullable=True),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"]),
        sa.ForeignKeyConstraint(["inbound_message_id"], ["inbound_messages.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["thread_id"], ["agent_threads.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_thread_id", "agent_runs", ["thread_id"], unique=False)
    op.create_index("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"], unique=False)
    op.create_index("ix_agent_runs_workspace_id", "agent_runs", ["workspace_id"], unique=False)
    op.create_index("ix_agent_runs_assistant_id", "agent_runs", ["assistant_id"], unique=False)
    op.create_index("ix_agent_runs_job_id", "agent_runs", ["job_id"], unique=False)
    op.create_index("ix_agent_runs_inbound_message_id", "agent_runs", ["inbound_message_id"], unique=False)
    op.create_index("ix_agent_runs_action_type", "agent_runs", ["action_type"], unique=False)
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"], unique=False)
    op.create_index("ix_agent_runs_error_code", "agent_runs", ["error_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_error_code", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_action_type", table_name="agent_runs")
    op.drop_index("ix_agent_runs_inbound_message_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_job_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_assistant_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_workspace_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_tenant_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_thread_id", table_name="agent_runs")
    op.drop_table("agent_runs")

    op.drop_index("ux_agent_threads_tenant_channel_chat", table_name="agent_threads")
    op.drop_index("ix_agent_threads_status", table_name="agent_threads")
    op.drop_index("ix_agent_threads_external_chat_id", table_name="agent_threads")
    op.drop_index("ix_agent_threads_channel_type", table_name="agent_threads")
    op.drop_index("ix_agent_threads_assistant_id", table_name="agent_threads")
    op.drop_index("ix_agent_threads_workspace_id", table_name="agent_threads")
    op.drop_index("ix_agent_threads_tenant_id", table_name="agent_threads")
    op.drop_table("agent_threads")
