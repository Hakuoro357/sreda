"""add skill platform tables + created_at on jobs/outbox (Phase 0)

Introduces the six platform tables from spec 48:
  * ``tenant_skill_states``
  * ``tenant_skill_configs``
  * ``skill_runs``
  * ``skill_run_attempts``
  * ``skill_events``
  * ``skill_ai_executions``

Also backfills ``created_at`` on ``jobs`` and ``outbox_messages`` so the
retention cleanup job (spec 41) has a time column to filter on.

Revision ID: 20260415_0009
Revises: 20260410_0008
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260415_0009"
down_revision = "20260410_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- jobs.created_at & outbox_messages.created_at ---------------------
    # ``server_default=now()`` keeps existing rows valid; we strip the
    # default afterwards so new inserts use the application-level default
    # from the SQLAlchemy model (UTC-aware ``datetime.now(UTC)``).
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.create_index("ix_jobs_created_at", ["created_at"])
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column("created_at", server_default=None)

    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.create_index("ix_outbox_messages_created_at", ["created_at"])
    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.alter_column("created_at", server_default=None)

    # -- tenant_skill_states ---------------------------------------------
    op.create_table(
        "tenant_skill_states",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("health_status", sa.String(length=32), nullable=False, server_default="healthy"),
        sa.Column("status_reason_code", sa.String(length=128), nullable=True),
        sa.Column("status_reason_message_sanitized", sa.Text(), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_successful_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_skill_states_tenant_feature"),
    )
    op.create_index("ix_tenant_skill_states_tenant_id", "tenant_skill_states", ["tenant_id"])
    op.create_index("ix_tenant_skill_states_feature_key", "tenant_skill_states", ["feature_key"])
    op.create_index(
        "ix_tenant_skill_states_lifecycle_status", "tenant_skill_states", ["lifecycle_status"]
    )
    op.create_index(
        "ix_tenant_skill_states_health_status", "tenant_skill_states", ["health_status"]
    )
    op.create_index("ix_tenant_skill_states_next_run_at", "tenant_skill_states", ["next_run_at"])

    # -- tenant_skill_configs --------------------------------------------
    op.create_table(
        "tenant_skill_configs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("config_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "secure_record_id",
            sa.String(length=64),
            sa.ForeignKey("secure_records.id"),
            nullable=True,
        ),
        sa.Column("updated_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_skill_configs_tenant_feature"),
    )
    op.create_index("ix_tenant_skill_configs_tenant_id", "tenant_skill_configs", ["tenant_id"])
    op.create_index("ix_tenant_skill_configs_feature_key", "tenant_skill_configs", ["feature_key"])
    op.create_index(
        "ix_tenant_skill_configs_schema_version", "tenant_skill_configs", ["config_schema_version"]
    )
    op.create_index(
        "ix_tenant_skill_configs_secure_record_id", "tenant_skill_configs", ["secure_record_id"]
    )

    # -- skill_runs -------------------------------------------------------
    op.create_table(
        "skill_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=True
        ),
        sa.Column(
            "assistant_id", sa.String(length=64), sa.ForeignKey("assistants.id"), nullable=True
        ),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("trigger_ref", sa.String(length=256), nullable=True),
        sa.Column("run_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("input_json", sa.Text(), nullable=True),
        sa.Column(
            "input_secure_record_id",
            sa.String(length=64),
            sa.ForeignKey("secure_records.id"),
            nullable=True,
        ),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column(
            "output_secure_record_id",
            sa.String(length=64),
            sa.ForeignKey("secure_records.id"),
            nullable=True,
        ),
        sa.Column("current_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "feature_key", "run_key", name="uq_skill_runs_tenant_feature_runkey"
        ),
    )
    op.create_index("ix_skill_runs_tenant_id", "skill_runs", ["tenant_id"])
    op.create_index("ix_skill_runs_workspace_id", "skill_runs", ["workspace_id"])
    op.create_index("ix_skill_runs_assistant_id", "skill_runs", ["assistant_id"])
    op.create_index("ix_skill_runs_feature_key", "skill_runs", ["feature_key"])
    op.create_index("ix_skill_runs_run_key", "skill_runs", ["run_key"])
    op.create_index("ix_skill_runs_status", "skill_runs", ["status"])
    op.create_index("ix_skill_runs_created_at", "skill_runs", ["created_at"])
    op.create_index("ix_skill_runs_tenant_created", "skill_runs", ["tenant_id", "created_at"])
    op.create_index("ix_skill_runs_feature_created", "skill_runs", ["feature_key", "created_at"])
    op.create_index("ix_skill_runs_status_created", "skill_runs", ["status", "created_at"])

    # -- skill_run_attempts ----------------------------------------------
    op.create_table(
        "skill_run_attempts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("skill_runs.id"), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=True
        ),
        sa.Column(
            "assistant_id", sa.String(length=64), sa.ForeignKey("assistants.id"), nullable=True
        ),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("retry_decision", sa.String(length=64), nullable=True),
        sa.Column("retry_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "run_id", "attempt_number", name="uq_skill_run_attempts_run_attempt"
        ),
    )
    op.create_index("ix_skill_run_attempts_run_id", "skill_run_attempts", ["run_id"])
    op.create_index("ix_skill_run_attempts_tenant_id", "skill_run_attempts", ["tenant_id"])
    op.create_index("ix_skill_run_attempts_feature_key", "skill_run_attempts", ["feature_key"])
    op.create_index("ix_skill_run_attempts_status", "skill_run_attempts", ["status"])
    op.create_index("ix_skill_run_attempts_job_id", "skill_run_attempts", ["job_id"])
    op.create_index(
        "ix_skill_run_attempts_tenant_created", "skill_run_attempts", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_skill_run_attempts_feature_created",
        "skill_run_attempts",
        ["feature_key", "created_at"],
    )
    op.create_index(
        "ix_skill_run_attempts_status_created", "skill_run_attempts", ["status", "created_at"]
    )

    # -- skill_events -----------------------------------------------------
    op.create_table(
        "skill_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.String(length=64), sa.ForeignKey("workspaces.id"), nullable=True
        ),
        sa.Column(
            "assistant_id", sa.String(length=64), sa.ForeignKey("assistants.id"), nullable=True
        ),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("issue_class", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skill_events_tenant_id", "skill_events", ["tenant_id"])
    op.create_index("ix_skill_events_feature_key", "skill_events", ["feature_key"])
    op.create_index("ix_skill_events_severity", "skill_events", ["severity"])
    op.create_index("ix_skill_events_issue_class", "skill_events", ["issue_class"])
    op.create_index("ix_skill_events_event_type", "skill_events", ["event_type"])
    op.create_index("ix_skill_events_created_at", "skill_events", ["created_at"])
    op.create_index("ix_skill_events_tenant_created", "skill_events", ["tenant_id", "created_at"])
    op.create_index(
        "ix_skill_events_feature_created", "skill_events", ["feature_key", "created_at"]
    )
    op.create_index("ix_skill_events_run_created", "skill_events", ["run_id", "created_at"])
    op.create_index(
        "ix_skill_events_attempt_created", "skill_events", ["attempt_id", "created_at"]
    )
    op.create_index(
        "ix_skill_events_severity_created", "skill_events", ["severity", "created_at"]
    )
    op.create_index(
        "ix_skill_events_issue_class_created", "skill_events", ["issue_class", "created_at"]
    )

    # -- skill_ai_executions ---------------------------------------------
    op.create_table(
        "skill_ai_executions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=True),
        sa.Column("provider_key", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("ai_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="started"),
        sa.Column("finish_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "validation_status", sa.String(length=32), nullable=False, server_default="not_checked"
        ),
        sa.Column("safety_flags_json", sa.Text(), nullable=True),
        sa.Column("structured_output_json", sa.Text(), nullable=True),
        sa.Column(
            "raw_artifact_secure_record_id",
            sa.String(length=64),
            sa.ForeignKey("secure_records.id"),
            nullable=True,
        ),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_rub_micro", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_sanitized", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_skill_ai_run_id", "skill_ai_executions", ["run_id"])
    op.create_index("ix_skill_ai_attempt_id", "skill_ai_executions", ["attempt_id"])
    op.create_index("ix_skill_ai_tenant_id", "skill_ai_executions", ["tenant_id"])
    op.create_index("ix_skill_ai_feature_key", "skill_ai_executions", ["feature_key"])
    op.create_index("ix_skill_ai_status", "skill_ai_executions", ["status"])
    op.create_index("ix_skill_ai_created_at", "skill_ai_executions", ["created_at"])
    op.create_index("ix_skill_ai_run_created", "skill_ai_executions", ["run_id", "created_at"])
    op.create_index(
        "ix_skill_ai_attempt_created", "skill_ai_executions", ["attempt_id", "created_at"]
    )
    op.create_index(
        "ix_skill_ai_tenant_created", "skill_ai_executions", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_skill_ai_feature_created", "skill_ai_executions", ["feature_key", "created_at"]
    )
    op.create_index(
        "ix_skill_ai_provider_model_created",
        "skill_ai_executions",
        ["provider_key", "model", "created_at"],
    )
    op.create_index(
        "ix_skill_ai_status_created", "skill_ai_executions", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("skill_ai_executions")
    op.drop_table("skill_events")
    op.drop_table("skill_run_attempts")
    op.drop_table("skill_runs")
    op.drop_table("tenant_skill_configs")
    op.drop_table("tenant_skill_states")

    with op.batch_alter_table("outbox_messages") as batch_op:
        batch_op.drop_index("ix_outbox_messages_created_at")
        batch_op.drop_column("created_at")

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index("ix_jobs_created_at")
        batch_op.drop_column("created_at")
