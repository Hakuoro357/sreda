"""Crash-safety / billing / recovery substrate (Sub-A12 Phase E PR-2a).

Additive-only migration (new tables + new nullable columns + model-level
CHECK extension).  No existing data is touched.

New tables
----------
step_execution_ledger
    Crash-safety core.  One row per tool-step execution attempt.
    Tracks started / committed / unknown / unknown_pending status.
    UNIQUE (execution_id, step_id) — one ledger entry per step.
    UNIQUE operation_id — global idempotency key, PII-free opaque.

planner_llm_reservations
    Billing reservation ledger.  One row per LLM-call budget hold.
    Lifecycle: reserved → charged | released | expired |
    unknown_provider_outcome.
    UNIQUE llm_call_id prevents double-reservation under retry.

New columns on planner_executions
----------------------------------
Recovery lease (mirrors message_jobs lease-fencing pattern):
    recovery_worker_id      String(64)   nullable
    recovery_attempt        Integer      NOT NULL  server_default='0'
    recovery_lease_until    DateTime     nullable

Whole-turn resume key:
    turn_key                String(256)  nullable  UNIQUE

PII-free derived diagnostics:
    failure_kind            String(32)   nullable
    failure_stage           String(32)   nullable
    retryable               Boolean      nullable
    step_count              Integer      nullable
    unknown_outcome_count   Integer      nullable
    write_committed         Boolean      nullable

CHECK constraint on execution_status
-------------------------------------
The ORM model now includes ``failed_needs_manual`` and
``committed_unserved_needs_manual`` in the allowed set.
The ``upgrade()`` function replaces the existing
``ck_planner_executions_execution_status`` constraint in-migration via
``batch_alter_table`` (drop_constraint + create_check_constraint).
This copy-and-move strategy works on both SQLite and PostgreSQL — no
manual DDL step is needed after running this migration.

Revision ID: 20260601_0054
Revises: 20260601_0053
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision = "20260601_0054"
down_revision = "20260601_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. New table: step_execution_ledger
    # ------------------------------------------------------------------
    op.create_table(
        "step_execution_ledger",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(64),
            sa.ForeignKey("planner_executions.id"),
            nullable=False,
        ),
        sa.Column("step_id", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(128), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "execution_id",
            "step_id",
            name="uq_step_execution_ledger_execution_step",
        ),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_step_execution_ledger_operation_id",
        ),
        sa.CheckConstraint(
            "status IN ('started','committed','unknown','unknown_pending')",
            name="ck_step_execution_ledger_status",
        ),
    )
    op.create_index(
        "ix_step_execution_ledger_execution_id",
        "step_execution_ledger",
        ["execution_id"],
    )

    # ------------------------------------------------------------------
    # 2. New table: planner_llm_reservations
    # ------------------------------------------------------------------
    op.create_table(
        "planner_llm_reservations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("llm_call_id", sa.String(128), nullable=False),
        sa.Column(
            "execution_id",
            sa.String(64),
            sa.ForeignKey("planner_executions.id"),
            nullable=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("feature_key", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reserved_tokens", sa.Integer, nullable=False),
        sa.Column("actual_tokens", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "llm_call_id",
            name="uq_planner_llm_reservations_llm_call_id",
        ),
        sa.CheckConstraint(
            "state IN ("
            "'reserved','charged','released','expired',"
            "'unknown_provider_outcome'"
            ")",
            name="ck_planner_llm_reservations_state",
        ),
    )
    op.create_index(
        "ix_planner_llm_reservations_execution_id",
        "planner_llm_reservations",
        ["execution_id"],
    )
    op.create_index(
        "ix_planner_llm_reservations_state_expires",
        "planner_llm_reservations",
        ["state", "expires_at"],
    )

    # ------------------------------------------------------------------
    # 3. New columns on planner_executions
    # ------------------------------------------------------------------

    # Recovery lease (mirrors message_jobs fencing pattern)
    op.add_column(
        "planner_executions",
        sa.Column("recovery_worker_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "planner_executions",
        sa.Column(
            "recovery_attempt",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "planner_executions",
        sa.Column("recovery_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_planner_executions_recovery_lease",
        "planner_executions",
        ["recovery_lease_until"],
    )

    # Remaining new columns + turn_key UNIQUE + widen execution_status +
    # replace CHECK constraint — use batch_alter_table so SQLite's
    # copy-and-move strategy can handle all DDL that SQLite cannot do via
    # plain ALTER TABLE (ADD CONSTRAINT, ALTER COLUMN type, DROP CONSTRAINT).
    with op.batch_alter_table("planner_executions") as batch_op:
        # Widen execution_status from String(24) → String(32) to accommodate
        # 'committed_unserved_needs_manual' (31 chars).
        batch_op.alter_column(
            "execution_status",
            type_=sa.String(32),
            existing_nullable=False,
        )
        # Replace the execution_status CHECK to include the two new terminal
        # values (failed_needs_manual, committed_unserved_needs_manual).
        # batch_alter_table + drop/create works on both SQLite and PostgreSQL.
        batch_op.drop_constraint(
            "ck_planner_executions_execution_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_planner_executions_execution_status",
            "execution_status IN ("
            "'pending','in_progress','completed','partial_failure',"
            "'failed','aborted','aborted_partial',"
            "'failed_needs_manual','committed_unserved_needs_manual'"
            ")",
        )
        # Widen turn_key from String(160) → String(256); format
        # {tenant_id}:{channel}:{external_update_id} can reach ~226 chars.
        batch_op.add_column(sa.Column("turn_key", sa.String(256), nullable=True))
        batch_op.create_unique_constraint(
            "uq_planner_executions_turn_key",
            ["turn_key"],
        )
        batch_op.add_column(sa.Column("failure_kind", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("failure_stage", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("retryable", sa.Boolean, nullable=True))
        batch_op.add_column(sa.Column("step_count", sa.Integer, nullable=True))
        batch_op.add_column(
            sa.Column("unknown_outcome_count", sa.Integer, nullable=True)
        )
        batch_op.add_column(sa.Column("write_committed", sa.Boolean, nullable=True))


def downgrade() -> None:
    # planner_executions new columns — use batch_alter_table so the
    # SQLite copy-and-move strategy handles DROP COLUMN, DROP UNIQUE
    # CONSTRAINT, DROP/CREATE CHECK, and ALTER COLUMN, which SQLite
    # cannot do via plain ALTER TABLE.
    with op.batch_alter_table("planner_executions") as batch_op:
        batch_op.drop_column("write_committed")
        batch_op.drop_column("unknown_outcome_count")
        batch_op.drop_column("step_count")
        batch_op.drop_column("retryable")
        batch_op.drop_column("failure_stage")
        batch_op.drop_column("failure_kind")
        batch_op.drop_constraint("uq_planner_executions_turn_key", type_="unique")
        batch_op.drop_column("turn_key")
        # Restore execution_status CHECK to original values (no new terminal
        # states) and narrow column back to String(24).
        batch_op.drop_constraint(
            "ck_planner_executions_execution_status",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_planner_executions_execution_status",
            "execution_status IN ("
            "'pending','in_progress','completed','partial_failure',"
            "'failed','aborted','aborted_partial'"
            ")",
        )
        batch_op.alter_column(
            "execution_status",
            type_=sa.String(24),
            existing_nullable=False,
        )
        batch_op.drop_index("ix_planner_executions_recovery_lease")
        batch_op.drop_column("recovery_lease_until")
        batch_op.drop_column("recovery_attempt")
        batch_op.drop_column("recovery_worker_id")

    # planner_llm_reservations
    op.drop_index(
        "ix_planner_llm_reservations_state_expires",
        table_name="planner_llm_reservations",
    )
    op.drop_index(
        "ix_planner_llm_reservations_execution_id",
        table_name="planner_llm_reservations",
    )
    op.drop_table("planner_llm_reservations")

    # step_execution_ledger
    op.drop_index(
        "ix_step_execution_ledger_execution_id",
        table_name="step_execution_ledger",
    )
    op.drop_table("step_execution_ledger")
