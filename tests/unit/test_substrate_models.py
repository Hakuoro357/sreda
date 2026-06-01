"""Tests for Sub-A12 Phase E substrate models.

Covers:
- StepExecutionLedger: round-trip, UNIQUE (execution_id, step_id),
  UNIQUE operation_id, CHECK status rejects bad values.
- PlannerLlmReservation: round-trip, UNIQUE llm_call_id,
  CHECK state rejects bad values.
- PlannerExecution new columns (recovery lease + turn_key + diagnostics):
  round-trip, turn_key UNIQUE constraint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models import (
    AgentRun,
    AgentThread,
    PlannerExecution,
    PlannerLlmReservation,
    StepExecutionLedger,
    Tenant,
    Workspace,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_minimal_run(session: Session) -> str:
    """Insert one Tenant + Workspace + AgentThread + AgentRun.
    Returns the AgentRun id."""
    tenant_id = f"tenant_{uuid4().hex[:12]}"
    workspace_id = f"ws_{uuid4().hex[:12]}"
    thread_id = f"thread_{uuid4().hex[:12]}"
    run_id = f"run_{uuid4().hex[:12]}"

    session.add(Tenant(id=tenant_id, name="t"))
    session.add(Workspace(id=workspace_id, tenant_id=tenant_id, name="w"))
    session.add(
        AgentThread(
            id=thread_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_type="telegram",
            external_chat_id="42",
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action_type="chat",
        )
    )
    session.flush()
    return run_id


def _exec_kwargs(run_id: str, **overrides) -> dict:
    base = dict(
        id=f"pe_{uuid4().hex[:12]}",
        run_id=run_id,
        tenant_id="tenant_1",
        feature_key="housewife_assistant",
        planner_prompt_version=1,
        planner_provider="mimo-v2.5-pro",
        planner_model="mimo-v2.5-pro",
        planner_status="pending",
        execution_status="pending",
        execution_log_json=[],
        created_at=_now(),
    )
    base.update(overrides)
    return base


def _make_execution(session: Session, **overrides) -> PlannerExecution:
    run_id = _seed_minimal_run(session)
    pe = PlannerExecution(**_exec_kwargs(run_id, **overrides))
    session.add(pe)
    session.flush()
    return pe


def _ledger_kwargs(execution_id: str, **overrides) -> dict:
    base = dict(
        id=f"sel_{uuid4().hex[:12]}",
        execution_id=execution_id,
        step_id="s1",
        tool="add_shopping_items",
        operation_id=f"op_{uuid4().hex[:16]}",
        status="started",
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(overrides)
    return base


def _reservation_kwargs(execution_id: str | None = None, **overrides) -> dict:
    base = dict(
        id=f"res_{uuid4().hex[:12]}",
        llm_call_id=f"call_{uuid4().hex[:16]}",
        execution_id=execution_id,
        tenant_id="tenant_1",
        feature_key="housewife_assistant",
        model="mimo-v2.5-pro",
        state="reserved",
        reserved_tokens=1000,
        created_at=_now(),
        updated_at=_now(),
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# StepExecutionLedger — round-trip
# ---------------------------------------------------------------------------


def test_step_execution_ledger_round_trip(db_session: Session) -> None:
    pe = _make_execution(db_session)

    ledger = StepExecutionLedger(**_ledger_kwargs(pe.id))
    db_session.add(ledger)
    db_session.flush()
    db_session.expire(ledger)

    loaded = db_session.query(StepExecutionLedger).one()
    assert loaded.execution_id == pe.id
    assert loaded.step_id == "s1"
    assert loaded.tool == "add_shopping_items"
    assert loaded.status == "started"
    assert loaded.operation_id is not None


def test_step_execution_ledger_all_valid_statuses(db_session: Session) -> None:
    pe = _make_execution(db_session)
    for status in ("started", "committed", "unknown", "unknown_pending"):
        entry = StepExecutionLedger(
            **_ledger_kwargs(
                pe.id,
                step_id=f"s_{status}",
                operation_id=f"op_{uuid4().hex[:16]}",
                status=status,
            )
        )
        db_session.add(entry)
    db_session.flush()
    assert db_session.query(StepExecutionLedger).count() == 4


# ---------------------------------------------------------------------------
# StepExecutionLedger — UNIQUE (execution_id, step_id)
# ---------------------------------------------------------------------------


def test_step_execution_ledger_unique_execution_step(db_session: Session) -> None:
    pe = _make_execution(db_session)

    db_session.add(StepExecutionLedger(**_ledger_kwargs(pe.id, step_id="s1")))
    db_session.flush()

    # second insert with same (execution_id, step_id) must fail
    db_session.add(
        StepExecutionLedger(
            **_ledger_kwargs(
                pe.id,
                step_id="s1",
                operation_id=f"op_{uuid4().hex[:16]}",  # different op_id
            )
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# StepExecutionLedger — UNIQUE operation_id (global)
# ---------------------------------------------------------------------------


def test_step_execution_ledger_unique_operation_id(db_session: Session) -> None:
    pe = _make_execution(db_session)
    shared_op_id = f"op_{uuid4().hex[:16]}"

    db_session.add(
        StepExecutionLedger(**_ledger_kwargs(pe.id, step_id="s1", operation_id=shared_op_id))
    )
    db_session.flush()

    # second insert with the SAME operation_id on a different step must fail
    db_session.add(
        StepExecutionLedger(
            **_ledger_kwargs(pe.id, step_id="s2", operation_id=shared_op_id)
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# StepExecutionLedger — CHECK status rejects invalid values
# ---------------------------------------------------------------------------


def test_step_execution_ledger_bad_status_rejected(db_session: Session) -> None:
    pe = _make_execution(db_session)
    db_session.add(StepExecutionLedger(**_ledger_kwargs(pe.id, status="zombie")))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# PlannerLlmReservation — round-trip
# ---------------------------------------------------------------------------


def test_planner_llm_reservation_round_trip(db_session: Session) -> None:
    pe = _make_execution(db_session)

    res = PlannerLlmReservation(**_reservation_kwargs(pe.id))
    db_session.add(res)
    db_session.flush()
    db_session.expire(res)

    loaded = db_session.query(PlannerLlmReservation).one()
    assert loaded.execution_id == pe.id
    assert loaded.state == "reserved"
    assert loaded.reserved_tokens == 1000
    assert loaded.actual_tokens is None
    assert loaded.expires_at is None


def test_planner_llm_reservation_all_valid_states(db_session: Session) -> None:
    pe = _make_execution(db_session)
    for state in ("reserved", "charged", "released", "expired", "unknown_provider_outcome"):
        db_session.add(
            PlannerLlmReservation(
                **_reservation_kwargs(
                    pe.id,
                    llm_call_id=f"call_{uuid4().hex[:16]}",
                    state=state,
                )
            )
        )
    db_session.flush()
    assert db_session.query(PlannerLlmReservation).count() == 5


def test_planner_llm_reservation_nullable_execution_id(db_session: Session) -> None:
    """execution_id is nullable — reservation can exist without a linked execution."""
    res = PlannerLlmReservation(**_reservation_kwargs(None))
    db_session.add(res)
    db_session.flush()
    loaded = db_session.query(PlannerLlmReservation).one()
    assert loaded.execution_id is None


def test_planner_llm_reservation_with_actual_tokens_and_expiry(
    db_session: Session,
) -> None:
    pe = _make_execution(db_session)
    res = PlannerLlmReservation(
        **_reservation_kwargs(
            pe.id,
            state="charged",
            reserved_tokens=2000,
            actual_tokens=1834,
            expires_at=_now(),
        )
    )
    db_session.add(res)
    db_session.flush()
    db_session.expire(res)
    loaded = db_session.query(PlannerLlmReservation).one()
    assert loaded.state == "charged"
    assert loaded.actual_tokens == 1834
    assert loaded.expires_at is not None


# ---------------------------------------------------------------------------
# PlannerLlmReservation — UNIQUE llm_call_id
# ---------------------------------------------------------------------------


def test_planner_llm_reservation_unique_llm_call_id(db_session: Session) -> None:
    shared_call_id = f"call_{uuid4().hex[:16]}"

    db_session.add(PlannerLlmReservation(**_reservation_kwargs(None, llm_call_id=shared_call_id)))
    db_session.flush()

    db_session.add(PlannerLlmReservation(**_reservation_kwargs(None, llm_call_id=shared_call_id)))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# PlannerLlmReservation — CHECK state rejects invalid values
# ---------------------------------------------------------------------------


def test_planner_llm_reservation_bad_state_rejected(db_session: Session) -> None:
    db_session.add(PlannerLlmReservation(**_reservation_kwargs(None, state="limbo")))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# PlannerExecution — new Sub-A12 Phase E columns
# ---------------------------------------------------------------------------


def test_planner_execution_recovery_lease_columns(db_session: Session) -> None:
    """recovery_worker_id / recovery_attempt / recovery_lease_until round-trip."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            recovery_worker_id="worker_42",
            recovery_attempt=2,
            recovery_lease_until=_now(),
        )
    )
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)

    loaded = db_session.query(PlannerExecution).one()
    assert loaded.recovery_worker_id == "worker_42"
    assert loaded.recovery_attempt == 2
    assert loaded.recovery_lease_until is not None


def test_planner_execution_recovery_attempt_defaults_to_zero(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id))
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.recovery_attempt == 0


def test_planner_execution_turn_key_round_trip(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    key = "tenant_abc:telegram:update_9999"
    pe = PlannerExecution(**_exec_kwargs(run_id, turn_key=key))
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.turn_key == key


def test_planner_execution_turn_key_unique(db_session: Session) -> None:
    shared_key = f"tenant_1:telegram:update_{uuid4().hex[:8]}"

    run_id1 = _seed_minimal_run(db_session)
    db_session.add(PlannerExecution(**_exec_kwargs(run_id1, turn_key=shared_key)))
    db_session.flush()

    run_id2 = _seed_minimal_run(db_session)
    db_session.add(PlannerExecution(**_exec_kwargs(run_id2, turn_key=shared_key)))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_planner_execution_turn_key_nullable(db_session: Session) -> None:
    """turn_key=None is allowed (not every invocation has a resume key)."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id, turn_key=None))
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.turn_key is None


def test_planner_execution_diagnostic_columns(db_session: Session) -> None:
    """failure_kind / failure_stage / retryable / step_count /
    unknown_outcome_count / write_committed all persist correctly."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            execution_status="failed",
            failure_kind="tool_timeout",
            failure_stage="execution",
            retryable=True,
            step_count=4,
            unknown_outcome_count=1,
            write_committed=False,
        )
    )
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)

    loaded = db_session.query(PlannerExecution).one()
    assert loaded.failure_kind == "tool_timeout"
    assert loaded.failure_stage == "execution"
    assert loaded.retryable is True
    assert loaded.step_count == 4
    assert loaded.unknown_outcome_count == 1
    assert loaded.write_committed is False


def test_planner_execution_diagnostic_columns_nullable(db_session: Session) -> None:
    """All diagnostic columns default to NULL."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id))
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)

    loaded = db_session.query(PlannerExecution).one()
    assert loaded.failure_kind is None
    assert loaded.failure_stage is None
    assert loaded.retryable is None
    assert loaded.step_count is None
    assert loaded.unknown_outcome_count is None
    assert loaded.write_committed is None


def test_planner_execution_new_terminal_statuses(db_session: Session) -> None:
    """failed_needs_manual and committed_unserved_needs_manual are valid
    execution_status values (model-level CHECK extended in Phase E)."""
    for status in ("failed_needs_manual", "committed_unserved_needs_manual"):
        run_id = _seed_minimal_run(db_session)
        pe = PlannerExecution(**_exec_kwargs(run_id, execution_status=status))
        db_session.add(pe)
        db_session.flush()
        db_session.expire(pe)
        loaded = db_session.query(PlannerExecution).filter_by(id=pe.id).one()
        assert loaded.execution_status == status


# ---------------------------------------------------------------------------
# Schema sanity — indexes on new tables exist
# ---------------------------------------------------------------------------


def test_new_tables_have_expected_indexes(db_session: Session) -> None:
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(db_session.bind)

    sel_idx_names = {ix["name"] for ix in inspector.get_indexes("step_execution_ledger")}
    assert "ix_step_execution_ledger_execution_id" in sel_idx_names, (
        f"missing ix on step_execution_ledger; got {sel_idx_names}"
    )

    res_idx_names = {ix["name"] for ix in inspector.get_indexes("planner_llm_reservations")}
    assert "ix_planner_llm_reservations_execution_id" in res_idx_names, (
        f"missing ix on planner_llm_reservations; got {res_idx_names}"
    )
    assert "ix_planner_llm_reservations_state_expires" in res_idx_names, (
        f"missing composite (state, expires_at) index on planner_llm_reservations; "
        f"got {res_idx_names}"
    )

    exec_idx_names = {ix["name"] for ix in inspector.get_indexes("planner_executions")}
    assert "ix_planner_executions_recovery_lease" in exec_idx_names, (
        f"missing recovery_lease index on planner_executions; got {exec_idx_names}"
    )


# ---------------------------------------------------------------------------
# StepExecutionLedger — step_id and operation_id are NOT NULL
# ---------------------------------------------------------------------------


def test_step_execution_ledger_step_id_not_null_rejected(db_session: Session) -> None:
    """step_id=None must be rejected (NOT NULL constraint)."""
    pe = _make_execution(db_session)
    db_session.add(StepExecutionLedger(**_ledger_kwargs(pe.id, step_id=None)))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_step_execution_ledger_operation_id_not_null_rejected(db_session: Session) -> None:
    """operation_id=None must be rejected (NOT NULL constraint)."""
    pe = _make_execution(db_session)
    db_session.add(StepExecutionLedger(**_ledger_kwargs(pe.id, operation_id=None)))
    with pytest.raises(IntegrityError):
        db_session.flush()
