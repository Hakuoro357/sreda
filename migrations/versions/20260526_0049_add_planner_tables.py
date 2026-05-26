"""planner_executions + planner_gaps — Phase B storage

Sub-A7 of Plan-Execute Epic (Hakuoro357/vex-assistant#74, #76).

Adds the two planner-storage tables Phase B will populate as soon as
the planner LLM call lands:

  planner_executions   one row per planner invocation, tracks the full
                       lifecycle (planner → validation → turn transition
                       → execution → compose). Group 3.2 schema +
                       Group 6.3 timeouts + Group 6.5 composer race
                       guard + Group 3.4 recovery metadata.

  planner_gaps         one row per detected gap (unknown_outcome,
                       invalid_plan, contract_violation, step_timeout,
                       user_feedback). Group G runtime enrichment +
                       Phase F GEPA training corpus.

Tables are non-destructive additions — no existing data touched, no
running code references them yet. Safe to deploy ahead of Phase B
work (the tables sit empty until the planner_node lands).

Revision ID: 20260526_0049
Revises: 20260525_0048
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision = "20260526_0049"
down_revision = "20260525_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planner_executions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("agent_runs.id"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("feature_key", sa.String(64), nullable=False),
        # Stage 1: planner LLM call
        sa.Column("planner_prompt_version", sa.Integer, nullable=False),
        sa.Column("planner_provider", sa.String(64), nullable=False),
        sa.Column("planner_model", sa.String(128), nullable=False),
        sa.Column("planner_latency_ms", sa.Integer, nullable=True),
        sa.Column("raw_planner_response", sa.Text, nullable=True),
        sa.Column("planner_status", sa.String(16), nullable=False),
        # Stage 1.5: validation
        sa.Column(
            "plan_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("validation_errors", sa.Text, nullable=True),
        sa.Column(
            "execution_plan_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        # Stage 2: turn transition
        sa.Column("turn_id", sa.String(64), nullable=True),
        sa.Column("is_new_turn", sa.Boolean, nullable=True),
        sa.Column("turn_classification_reason", sa.String(500), nullable=True),
        # Stage 3: execution
        sa.Column("execution_status", sa.String(24), nullable=False),
        sa.Column(
            "execution_log_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_finished_at", sa.DateTime(timezone=True), nullable=True),
        # Compose stage
        sa.Column("composer_path", sa.String(128), nullable=True),
        sa.Column("composer_registry_snapshot_hash", sa.String(64), nullable=True),
        sa.Column("tool_registry_version", sa.String(64), nullable=True),
        sa.Column("final_reply_chars", sa.Integer, nullable=True),
        sa.Column("total_latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "planner_status IN ('pending','received','invalid','valid')",
            name="ck_planner_executions_planner_status",
        ),
        sa.CheckConstraint(
            "execution_status IN ("
            "'pending','in_progress','completed','partial_failure',"
            "'failed','aborted','aborted_partial'"
            ")",
            name="ck_planner_executions_execution_status",
        ),
    )
    op.create_index(
        "ix_planner_executions_run_id",
        "planner_executions",
        ["run_id"],
    )
    op.create_index(
        "ix_planner_executions_turn_id",
        "planner_executions",
        ["turn_id"],
    )
    op.create_index(
        "ix_planner_executions_recovery",
        "planner_executions",
        ["execution_status", "execution_started_at"],
    )
    op.create_index(
        "ix_planner_executions_tenant_created",
        "planner_executions",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "planner_gaps",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(64),
            sa.ForeignKey("planner_executions.id"),
            nullable=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("gap_type", sa.String(32), nullable=False),
        sa.Column("gap_source", sa.String(32), nullable=False),
        # Context (PII per privacy doc — encryption is on the
        # application-write side, not at-rest column type)
        sa.Column("user_message", sa.Text, nullable=True),
        sa.Column(
            "plan_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("step_id", sa.String(64), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column(
            "tool_args_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "actual_result_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("error_details", sa.Text, nullable=True),
        # Group 6.5 composer-side gap metadata
        sa.Column("template_id", sa.String(128), nullable=True),
        sa.Column(
            "closest_matches",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("planner_model", sa.String(128), nullable=True),
        sa.Column("prompt_version", sa.Integer, nullable=True),
        sa.Column("registry_version", sa.String(64), nullable=True),
        sa.Column("plan_trace_id", sa.String(64), nullable=True),
        # Lifecycle
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolved_in_version", sa.Integer, nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gap_type IN ("
            "'unknown_outcome','invalid_plan','contract_violation',"
            "'step_timeout','user_feedback'"
            ")",
            name="ck_planner_gaps_gap_type",
        ),
        sa.CheckConstraint(
            "gap_source IN ('admin','unknown_outcome','contract_violation','user_feedback','auto')",
            name="ck_planner_gaps_gap_source",
        ),
        sa.CheckConstraint(
            "status IN ('open','patched','wontfix')",
            name="ck_planner_gaps_status",
        ),
    )
    op.create_index(
        "ix_planner_gaps_execution_id",
        "planner_gaps",
        ["execution_id"],
    )
    op.create_index(
        "ix_planner_gaps_open",
        "planner_gaps",
        ["status", "created_at"],
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_planner_gaps_tenant_recent",
        "planner_gaps",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_planner_gaps_tenant_recent", table_name="planner_gaps")
    op.drop_index("ix_planner_gaps_open", table_name="planner_gaps")
    op.drop_index("ix_planner_gaps_execution_id", table_name="planner_gaps")
    op.drop_table("planner_gaps")
    op.drop_index(
        "ix_planner_executions_tenant_created", table_name="planner_executions"
    )
    op.drop_index("ix_planner_executions_recovery", table_name="planner_executions")
    op.drop_index("ix_planner_executions_turn_id", table_name="planner_executions")
    op.drop_index("ix_planner_executions_run_id", table_name="planner_executions")
    op.drop_table("planner_executions")
