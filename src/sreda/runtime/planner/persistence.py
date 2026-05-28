"""Persistence helpers for planner_executions lifecycle — Sub-A12 Phase B.4.

Thin DB-write layer around the ``PlannerExecution`` model. Orchestrator
(``runtime/planner/orchestrator.py``) drives the lifecycle:

    insert_pending → mark_received → mark_invalid OR mark_valid

Each call is small: explicit SQL with named parameters, no ORM
gymnastics. Caller passes a Session and commits at its own boundaries
(orchestrator owns per-stage transactions, not this module).

Out of scope for Phase B.4 (deferred to Phase B.4-followup migration):
* ``idempotency_key`` column for atomic claim-or-replay
* ``failure_kind`` column for retryable vs final classification
* ``attempts_log_json`` per-attempt metadata array

This module sticks to the existing Sub-A7 schema. The orchestrator
captures attempt metadata in memory (returned in PlannerResult) so
follow-up work can persist it without rewriting the lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from sreda.db.models.planner import PlannerExecution


def make_execution_id() -> str:
    """Stable id format ``plan_exec_<32hex>`` — matches Sub-A7 id shape."""
    return f"plan_exec_{uuid.uuid4().hex[:24]}"


def insert_pending(
    session: Session,
    *,
    execution_id: str,
    run_id: str,
    tenant_id: str,
    feature_key: str,
    planner_prompt_version: int,
    planner_provider: str,
    planner_model: str,
    tool_registry_version: str | None = None,
    composer_registry_snapshot_hash: str | None = None,
) -> None:
    """Stage 1: INSERT row with status='pending' before LLM call.

    Uses ORM (PlannerExecution) so JSON columns (execution_log_json)
    get proper type adapter handling on both Postgres and SQLite —
    raw SQL via text() bypassed the JSON adapter and broke SQLite tests."""
    row = PlannerExecution(
        id=execution_id,
        run_id=run_id,
        tenant_id=tenant_id,
        feature_key=feature_key,
        planner_prompt_version=planner_prompt_version,
        planner_provider=planner_provider,
        planner_model=planner_model,
        planner_status="pending",
        execution_status="pending",
        tool_registry_version=tool_registry_version,
        composer_registry_snapshot_hash=composer_registry_snapshot_hash,
        execution_log_json=[],
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()


def mark_received(
    session: Session,
    *,
    execution_id: str,
    raw_response: str,
    latency_ms: int,
    planner_provider: str | None = None,
    planner_model: str | None = None,
) -> None:
    """Stage 2: LLM returned. Save raw text + cumulative latency. Status
    moves pending → received. Validation/compile happen next.

    Codex Sub-A12 B.4 R1 MAJOR fix: persist effective provider/model
    from PlannerCallResult — insert_pending wrote placeholders because
    the actual resolved model wasn't known yet. Update here once we
    have the real values from the LLM call.

    If this is a retry (attempt_no=2), caller should accumulate latency
    BEFORE passing here — orchestrator owns the running sum."""
    row = session.get(PlannerExecution, execution_id)
    if row is None:
        raise LookupError(f"planner_execution {execution_id!r} not found")
    row.raw_planner_response = raw_response
    row.planner_latency_ms = latency_ms
    row.planner_status = "received"
    if planner_provider is not None:
        row.planner_provider = planner_provider
    if planner_model is not None:
        row.planner_model = planner_model
    session.flush()


def mark_invalid(
    session: Session,
    *,
    execution_id: str,
    validation_errors: str,
) -> None:
    """Terminal failure path: parsing/validation/compile failed and
    retry budget exhausted (or this is the only-attempt path).

    ``planner_status`` → 'invalid'. ``execution_status`` left as
    'pending' since we never reached executor. Caller maps this to
    PlannerResult(success=False)."""
    row = session.get(PlannerExecution, execution_id)
    if row is None:
        raise LookupError(f"planner_execution {execution_id!r} not found")
    row.planner_status = "invalid"
    row.validation_errors = validation_errors
    session.flush()


def mark_valid(
    session: Session,
    *,
    execution_id: str,
    plan_json: dict,
    execution_plan_json: dict,
    is_new_turn: bool | None = None,
    turn_classification_reason: str | None = None,
) -> None:
    """Success path: plan parsed + validated + compiled. Snapshot the
    JSON forms for executor consumption AND audit.

    ``planner_status`` → 'valid'. ``execution_status`` stays 'pending'
    until executor (Phase C) starts work."""
    row = session.get(PlannerExecution, execution_id)
    if row is None:
        raise LookupError(f"planner_execution {execution_id!r} not found")
    row.plan_json = plan_json
    row.execution_plan_json = execution_plan_json
    row.planner_status = "valid"
    row.is_new_turn = is_new_turn
    row.turn_classification_reason = turn_classification_reason
    session.flush()


__all__ = [
    "insert_pending",
    "make_execution_id",
    "mark_invalid",
    "mark_received",
    "mark_valid",
]
