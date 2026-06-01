"""Add encrypted _enc columns to planner_executions and planner_gaps (PR-2a).

EXPAND step of an expand/contract migration: adds 11 new nullable Text columns
alongside the existing plaintext PII columns. No existing columns are altered,
dropped, or backfilled. Dual-write logic is a later task.

  planner_executions (6):
    raw_planner_response_enc  — encrypted mirror of raw_planner_response
    plan_json_enc             — encrypted mirror of plan_json
    validation_errors_enc     — encrypted mirror of validation_errors
    execution_plan_json_enc   — encrypted mirror of execution_plan_json
    execution_log_json_enc    — encrypted mirror of execution_log_json
    turn_classification_reason_enc — encrypted mirror of turn_classification_reason

  planner_gaps (5):
    user_message_enc          — encrypted mirror of user_message
    plan_json_enc             — encrypted mirror of plan_json
    tool_args_json_enc        — encrypted mirror of tool_args_json
    actual_result_json_enc    — encrypted mirror of actual_result_json
    error_details_enc         — encrypted mirror of error_details

The underlying SQL type for both EncryptedString and JSONEncryptedString is
Text (the TypeDecorator stores ciphertext as a text envelope).

Revision ID: 20260601_0053
Revises: 20260526_0052
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


revision = "20260601_0053"
down_revision = "20260526_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # planner_executions — 6 encrypted mirrors
    op.add_column(
        "planner_executions",
        sa.Column("raw_planner_response_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_executions",
        sa.Column("plan_json_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_executions",
        sa.Column("execution_plan_json_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_executions",
        sa.Column("execution_log_json_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_executions",
        sa.Column("validation_errors_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_executions",
        sa.Column("turn_classification_reason_enc", sa.Text, nullable=True),
    )

    # planner_gaps — 5 encrypted mirrors
    op.add_column(
        "planner_gaps",
        sa.Column("user_message_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_gaps",
        sa.Column("plan_json_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_gaps",
        sa.Column("tool_args_json_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_gaps",
        sa.Column("actual_result_json_enc", sa.Text, nullable=True),
    )
    op.add_column(
        "planner_gaps",
        sa.Column("error_details_enc", sa.Text, nullable=True),
    )


def downgrade() -> None:
    # planner_gaps — drop in reverse order
    op.drop_column("planner_gaps", "error_details_enc")
    op.drop_column("planner_gaps", "actual_result_json_enc")
    op.drop_column("planner_gaps", "tool_args_json_enc")
    op.drop_column("planner_gaps", "plan_json_enc")
    op.drop_column("planner_gaps", "user_message_enc")

    # planner_executions — drop in reverse order
    op.drop_column("planner_executions", "turn_classification_reason_enc")
    op.drop_column("planner_executions", "validation_errors_enc")
    op.drop_column("planner_executions", "execution_log_json_enc")
    op.drop_column("planner_executions", "execution_plan_json_enc")
    op.drop_column("planner_executions", "plan_json_enc")
    op.drop_column("planner_executions", "raw_planner_response_enc")
