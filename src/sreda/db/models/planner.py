"""Planner execution + gaps tables (Sub-A7 of Plan-Execute Epic #74).

``planner_executions``  one row per planner invocation; tracks the full
                        lifecycle from raw LLM response through
                        validation, turn transition, executor run, and
                        compose. Phase B will populate; Phase A only
                        creates the schema so the table exists before
                        the planner code wants to write.

``planner_gaps``        one row per detected gap — unknown tool outcome,
                        invalid plan, contract violation. Drives the
                        runtime planner-prompt enrichment (Group G) and
                        the offline GEPA training corpus (Phase F).
                        References planner_executions.id for trace
                        linkage.

Schema highlights (per Group 3.2 lifecycle, Group 6.3 timeouts, Group
6.5 composer race, Group G gap registry):

  planner_executions
    raw_planner_response  string  Sub-A2 hot debug + reproducibility
    plan_json             jsonb   parsed Plan after validation (NULL until valid)
    execution_plan_json   jsonb   validator output (topological layers, fail_modes)
    validation_errors     text    detail on planner_status='invalid'
    planner_status        check   pending/received/invalid/valid
    execution_status      check   pending/in_progress/completed/partial_failure/
                                  failed/aborted/aborted_partial
    composer_registry_snapshot_hash  text  Group 6.5 race guard
    tool_registry_version  text   Group 3.4 recovery — schema-mismatch guard
    turn_id, is_new_turn, turn_classification_reason  Category B Stage 2

  planner_gaps
    gap_type              check   unknown_outcome/invalid_plan/contract_violation/
                                  step_timeout/user_feedback
    template_id, closest_matches, planner_model, prompt_version,
    registry_version, plan_trace_id   Group 6.5 GEPA metadata
    status                check   open/patched/wontfix
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from sreda.db.base import Base


_JSONB = JSON().with_variant(JSONB(), "postgresql")


class PlannerExecution(Base):
    """One planner LLM invocation tracked through its full lifecycle.

    See module docstring for the field-by-field rationale + which group
    of the architecture plan motivated each column.
    """

    __tablename__ = "planner_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Stage 1: planner LLM call --------------------------------------
    planner_prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_model: Mapped[str] = mapped_column(String(128), nullable=False)
    planner_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_planner_response: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Raw LLM response — kept for debug + GEPA replay (PII per privacy doc)",
    )
    planner_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
    )

    # --- Stage 1.5: validation -----------------------------------------
    plan_json: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    validation_errors: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_plan_json: Mapped[dict | None] = mapped_column(
        _JSONB,
        nullable=True,
        comment="Validator output: topological layers + per-join fail_modes",
    )

    # --- Stage 2: turn transition ---------------------------------------
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_new_turn: Mapped[bool | None] = mapped_column(nullable=True)
    turn_classification_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )

    # --- Stage 3: execution --------------------------------------------
    execution_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending",
    )
    execution_log_json: Mapped[dict] = mapped_column(
        _JSONB,
        nullable=False,
        default=dict,
        comment="List of per-step results; incrementally persisted (Group 3.2)",
    )
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Compose stage --------------------------------------------------
    composer_path: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment='"template:X" or "llm:Y" — what compose actually chose',
    )
    composer_registry_snapshot_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Group 6.5 race guard — registry hash at validation time",
    )
    tool_registry_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Group 3.4 recovery — schema-mismatch guard",
    )
    final_reply_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "planner_status IN ('pending','received','invalid','valid')",
            name="ck_planner_executions_planner_status",
        ),
        CheckConstraint(
            "execution_status IN ("
            "'pending','in_progress','completed','partial_failure',"
            "'failed','aborted','aborted_partial'"
            ")",
            name="ck_planner_executions_execution_status",
        ),
        Index(
            "ix_planner_executions_recovery",
            "execution_status",
            "execution_started_at",
        ),
        Index(
            "ix_planner_executions_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )


class PlannerGap(Base):
    """One detected gap — surfaces for runtime planner enrichment + GEPA.

    Group G in the plan: planner sees the top-K most-relevant recent
    gaps in its system prompt so it doesn't repeat the same mistake
    before the offline GEPA cycle generates a patch.
    """

    __tablename__ = "planner_gaps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("planner_executions.id"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gap_type: Mapped[str] = mapped_column(String(32), nullable=False)
    gap_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="auto",
        comment="admin / unknown_outcome / contract_violation / user_feedback",
    )

    # --- Per-message context (PII per privacy doc) ---------------------
    user_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_args_json: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    actual_result_json: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Group 6.5: composer-side gap metadata -------------------------
    template_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closest_matches: Mapped[list | None] = mapped_column(_JSONB, nullable=True)
    planner_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registry_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_trace_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Link to #68 LLM trace file for full envelope",
    )

    # --- Lifecycle ------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
    )
    resolved_in_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "gap_type IN ("
            "'unknown_outcome','invalid_plan','contract_violation',"
            "'step_timeout','user_feedback'"
            ")",
            name="ck_planner_gaps_gap_type",
        ),
        CheckConstraint(
            "gap_source IN ('admin','unknown_outcome','contract_violation','user_feedback','auto')",
            name="ck_planner_gaps_gap_source",
        ),
        CheckConstraint(
            "status IN ('open','patched','wontfix')",
            name="ck_planner_gaps_status",
        ),
        Index(
            "ix_planner_gaps_open",
            "status",
            "created_at",
            postgresql_where=sql_text("status = 'open'"),
            sqlite_where=sql_text("status = 'open'"),
        ),
        Index(
            "ix_planner_gaps_tenant_recent",
            "tenant_id",
            "created_at",
        ),
    )
