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
    ForeignKeyConstraint,
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
from sreda.db.types import EncryptedString, JSONEncryptedString


_JSONB = JSON().with_variant(JSONB(), "postgresql")


class PlannerExecution(Base):
    """One planner LLM invocation tracked through its full lifecycle.

    See module docstring for the field-by-field rationale + which group
    of the architecture plan motivated each column.
    """

    __tablename__ = "planner_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ``run_id`` keeps its simple FK to agent_runs.id — checks even when
    # turn_id is NULL (composite FK alone is inert with NULL columns).
    # The composite (run_id, turn_id) FK in __table_args__ adds the
    # *consistency* invariant on top: when both are set, they must agree
    # with the agent_runs row's own turn_id (Codex Sub-A9 R2 MAJOR #1).
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
    # Codex Sub-A9 R1 MAJOR #4 + R2 MAJOR #1 — turn_id is constrained
    # via the composite FK (run_id, turn_id) → agent_runs(id, turn_id)
    # declared in __table_args__. That FK simultaneously:
    #   - rejects orphan turn_id (must match some agent_runs row)
    #   - rejects run/turn mismatch (planner_executions.turn_id MUST
    #     equal agent_runs.turn_id for the same run_id).
    # No separate single-column FK to conversation_turns.id is needed —
    # agent_runs has its own composite FK to conversation_turns, so the
    # transitive constraint chain is: planner_executions → agent_runs
    # → conversation_turns.
    turn_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
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
    execution_log_json: Mapped[list] = mapped_column(
        _JSONB,
        nullable=False,
        default=list,
        comment=(
            "List of per-step results; appended-to incrementally per "
            "Group 3.2 (one entry per executor visit, ordered by visit "
            "time, each entry is a dict with node_id + status + "
            "outcome). Codex review 2026-05-26 MEDIUM fix: was dict."
        ),
    )
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Encrypted mirrors (PR-2a expand step — additive only) ---------
    raw_planner_response_enc: Mapped[str | None] = mapped_column(
        EncryptedString(),
        nullable=True,
        comment="Encrypted mirror of raw_planner_response (PR-2a)",
    )
    plan_json_enc: Mapped[dict | list | None] = mapped_column(
        JSONEncryptedString(),
        nullable=True,
        comment="Encrypted mirror of plan_json (PR-2a)",
    )
    execution_plan_json_enc: Mapped[dict | list | None] = mapped_column(
        JSONEncryptedString(),
        nullable=True,
        comment="Encrypted mirror of execution_plan_json (PR-2a)",
    )
    execution_log_json_enc: Mapped[list | None] = mapped_column(
        JSONEncryptedString(),
        nullable=True,
        comment="Encrypted mirror of execution_log_json (PR-2a; nullable unlike original)",
    )
    validation_errors_enc: Mapped[str | None] = mapped_column(
        EncryptedString(),
        nullable=True,
        comment="Encrypted mirror of validation_errors (PR-2a; ValidationError text can embed rejected payload snippets)",
    )
    turn_classification_reason_enc: Mapped[str | None] = mapped_column(
        EncryptedString(),
        nullable=True,
        comment="Encrypted mirror of turn_classification_reason (PR-2a; LLM free-text rationale, PII-capable — conservative)",
    )
    # PR-2a PII audit (2026-06-01): the 6 columns mirrored above are the FULL
    # PII set on planner_executions — raw_planner_response, plan_json,
    # validation_errors, execution_plan_json, execution_log_json,
    # turn_classification_reason. Every other column (ids / enums / timestamps /
    # hashes / counters: planner_status, execution_status, composer_path,
    # *_snapshot_hash, tool_registry_version, *_latency_ms, final_reply_chars,
    # turn_id/is_new_turn, *_at) is non-PII and intentionally NOT mirrored.

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
        # Codex Sub-A9 R2 MAJOR #1 — composite FK ensures that for any
        # planner_executions row, ``turn_id`` matches the ``turn_id`` of
        # the agent_runs row referenced by ``run_id``. Either both NULL,
        # or both pointing at the same conversation_turn.
        ForeignKeyConstraint(
            ["run_id", "turn_id"],
            ["agent_runs.id", "agent_runs.turn_id"],
            name="fk_planner_executions_run_turn",
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
    # PR-2a PII audit (Codex R3): step_id mirrors a ``Plan.actions`` KEY,
    # which the LLM currently generates freely. Treated as non-PII (not
    # mirrored) ONLY because task #8 enforces an action-id format
    # ``^s[1-9]\d*$`` in the planner schema — keys become structural
    # s1/s2/s3, never user content. Until #8 lands there is ZERO exposure:
    # planner_gaps has no writer yet. Decision: Boris, 2026-06-01.
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_args_json: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    actual_result_json: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Encrypted mirrors (PR-2a expand step — additive only) ---------
    user_message_enc: Mapped[str | None] = mapped_column(
        EncryptedString(),
        nullable=True,
        comment="Encrypted mirror of user_message (PR-2a)",
    )
    plan_json_enc: Mapped[dict | list | None] = mapped_column(
        JSONEncryptedString(),
        nullable=True,
        comment="Encrypted mirror of plan_json (PR-2a)",
    )
    tool_args_json_enc: Mapped[dict | list | None] = mapped_column(
        JSONEncryptedString(),
        nullable=True,
        comment="Encrypted mirror of tool_args_json (PR-2a)",
    )
    actual_result_json_enc: Mapped[dict | list | None] = mapped_column(
        JSONEncryptedString(),
        nullable=True,
        comment="Encrypted mirror of actual_result_json (PR-2a)",
    )
    error_details_enc: Mapped[str | None] = mapped_column(
        EncryptedString(),
        nullable=True,
        comment="Encrypted mirror of error_details (PR-2a)",
    )

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
