"""Tests for ``PlannerExecution`` + ``PlannerGap`` models (Sub-A7, Epic #74).

Phase A only creates the schema — these tables don't have writer code
yet (that lands in Phase B planner_node). The tests here lock the
contract:

- Required columns can be inserted
- CHECK constraints reject invalid enum values
- Indexes are present
- FK execution_id → planner_executions cascades sensibly
- Status transitions persist (lifecycle moves through pending → received
  → invalid/valid; execution_status through pending → in_progress →
  terminal)

Production behaviour (incremental persist of execution_log_json,
turn_id FK, snapshot_hash race detection) lives in Phase B / B6 tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models import (
    AgentRun,
    AgentThread,
    PlannerExecution,
    PlannerGap,
    Tenant,
    Workspace,
)


# ---------------------------------------------------------------------------
# Test helpers — seed minimum FK chain so PlannerExecution.run_id resolves
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_minimal_run(session: Session) -> str:
    """Insert one Tenant + Workspace + AgentThread + AgentRun so a
    PlannerExecution.run_id FK has somewhere to point.

    Returns the AgentRun id.
    """
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


# ---------------------------------------------------------------------------
# PlannerExecution — happy path + lifecycle
# ---------------------------------------------------------------------------


def test_planner_execution_pending_row_persists(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id))
    db_session.add(pe)
    db_session.flush()
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.planner_status == "pending"
    assert loaded.execution_status == "pending"
    assert loaded.execution_log_json == []


def test_planner_execution_validation_path(db_session: Session) -> None:
    """received → invalid lifecycle for a malformed planner response."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            planner_status="invalid",
            raw_planner_response="oh no garbage",
            validation_errors="JSON parse error at line 3",
            planner_latency_ms=150,
        )
    )
    db_session.add(pe)
    db_session.flush()
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.planner_status == "invalid"
    assert loaded.validation_errors is not None
    assert loaded.plan_json is None  # never reached the valid stage


def test_planner_execution_full_lifecycle(db_session: Session) -> None:
    """planner=valid + execution=completed with composer path."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            planner_status="valid",
            plan_json={"actions": {}, "compose": {"kind": "template", "template_id": "x"}},
            execution_plan_json={"layers": []},
            execution_status="completed",
            execution_log_json=[{"node_id": "s1", "status": "added"}],
            execution_started_at=_now(),
            execution_finished_at=_now(),
            composer_path="template:add_items_ok",
            composer_registry_snapshot_hash="abc123",
            tool_registry_version="v1",
            final_reply_chars=42,
            total_latency_ms=2500,
        )
    )
    db_session.add(pe)
    db_session.flush()
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.execution_status == "completed"
    assert loaded.execution_log_json == [{"node_id": "s1", "status": "added"}]
    assert loaded.composer_path == "template:add_items_ok"


def test_planner_execution_partial_failure_state(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            planner_status="valid",
            execution_status="partial_failure",
            execution_started_at=_now(),
            execution_finished_at=_now(),
        )
    )
    db_session.add(pe)
    db_session.flush()
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.execution_status == "partial_failure"


def test_planner_execution_aborted_partial_state(db_session: Session) -> None:
    """Group 3.4 — chain timeout after side effects."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            execution_status="aborted_partial",
            execution_started_at=_now(),
            execution_finished_at=_now(),
        )
    )
    db_session.add(pe)
    db_session.flush()
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.execution_status == "aborted_partial"


# ---------------------------------------------------------------------------
# PlannerExecution — CHECK constraints
# ---------------------------------------------------------------------------


def test_planner_execution_invalid_planner_status_rejected(
    db_session: Session,
) -> None:
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id, planner_status="weird"))
    db_session.add(pe)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_planner_execution_invalid_execution_status_rejected(
    db_session: Session,
) -> None:
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id, execution_status="zombie"))
    db_session.add(pe)
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# PlannerGap — basics + CHECK constraints
# ---------------------------------------------------------------------------


def _gap_kwargs(execution_id: str | None = None, **overrides) -> dict:
    base = dict(
        id=f"gap_{uuid4().hex[:12]}",
        execution_id=execution_id,
        tenant_id="tenant_1",
        gap_type="unknown_outcome",
        gap_source="auto",
        status="open",
        created_at=_now(),
    )
    base.update(overrides)
    return base


def test_planner_gap_minimal_row_persists(db_session: Session) -> None:
    gap = PlannerGap(**_gap_kwargs())
    db_session.add(gap)
    db_session.flush()
    loaded = db_session.query(PlannerGap).one()
    assert loaded.gap_type == "unknown_outcome"
    assert loaded.status == "open"


def test_planner_gap_links_to_execution(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id))
    db_session.add(pe)
    db_session.flush()
    gap = PlannerGap(
        **_gap_kwargs(
            execution_id=pe.id,
            user_message="купи молоко и хлеб",
            tool_name="add_shopping_items",
            error_details="status=weird_unknown",
        )
    )
    db_session.add(gap)
    db_session.flush()
    loaded = db_session.query(PlannerGap).filter_by(id=gap.id).one()
    assert loaded.execution_id == pe.id
    assert loaded.tool_name == "add_shopping_items"


def test_planner_gap_invalid_gap_type_rejected(db_session: Session) -> None:
    gap = PlannerGap(**_gap_kwargs(gap_type="something_unknown"))
    db_session.add(gap)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_planner_gap_invalid_status_rejected(db_session: Session) -> None:
    gap = PlannerGap(**_gap_kwargs(status="zombie"))
    db_session.add(gap)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_planner_gap_with_template_metadata(db_session: Session) -> None:
    """Group 6.5 composer-gap metadata fields populate as expected."""
    gap = PlannerGap(
        **_gap_kwargs(
            gap_type="invalid_plan",
            gap_source="contract_violation",
            template_id="add_items_succes",
            closest_matches=["add_items_success", "add_items_ok"],
            planner_model="mimo-v2.5-pro",
            prompt_version=3,
            registry_version="snap_abc123",
            plan_trace_id="trace_xyz",
        )
    )
    db_session.add(gap)
    db_session.flush()
    loaded = db_session.query(PlannerGap).one()
    assert loaded.template_id == "add_items_succes"
    assert loaded.closest_matches == ["add_items_success", "add_items_ok"]


def test_planner_gap_resolved_lifecycle(db_session: Session) -> None:
    gap = PlannerGap(
        **_gap_kwargs(
            status="patched",
            resolved_in_version=5,
            resolved_at=_now(),
        )
    )
    db_session.add(gap)
    db_session.flush()
    loaded = db_session.query(PlannerGap).one()
    assert loaded.status == "patched"
    assert loaded.resolved_in_version == 5


# ---------------------------------------------------------------------------
# PR-2a — encrypted _enc column round-trips (additive expand step)
# ---------------------------------------------------------------------------


def test_planner_execution_enc_columns_roundtrip(db_session: Session) -> None:
    """The 6 new _enc columns on PlannerExecution accept write + read back
    the same value through the EncryptedString / JSONEncryptedString
    TypeDecorators (SQLite in-memory, same path as production Text storage)."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(
        **_exec_kwargs(
            run_id,
            raw_planner_response_enc="raw LLM output secret",
            plan_json_enc={"actions": {}, "compose": {"kind": "template"}},
            validation_errors_enc="plan_schema_error: rejected '<user payload>'",
            execution_plan_json_enc={"layers": [{"nodes": ["s1"]}]},
            execution_log_json_enc=[{"node_id": "s1", "status": "ok"}],
            turn_classification_reason_enc="continuation of the cooking thread",
        )
    )
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.raw_planner_response_enc == "raw LLM output secret"
    assert loaded.plan_json_enc == {"actions": {}, "compose": {"kind": "template"}}
    assert loaded.validation_errors_enc == "plan_schema_error: rejected '<user payload>'"
    assert loaded.execution_plan_json_enc == {"layers": [{"nodes": ["s1"]}]}
    assert loaded.execution_log_json_enc == [{"node_id": "s1", "status": "ok"}]
    assert loaded.turn_classification_reason_enc == "continuation of the cooking thread"


def test_planner_execution_enc_columns_nullable(db_session: Session) -> None:
    """All 6 _enc columns default to NULL when not supplied."""
    run_id = _seed_minimal_run(db_session)
    pe = PlannerExecution(**_exec_kwargs(run_id))
    db_session.add(pe)
    db_session.flush()
    db_session.expire(pe)
    loaded = db_session.query(PlannerExecution).one()
    assert loaded.raw_planner_response_enc is None
    assert loaded.plan_json_enc is None
    assert loaded.validation_errors_enc is None
    assert loaded.execution_plan_json_enc is None
    assert loaded.execution_log_json_enc is None
    assert loaded.turn_classification_reason_enc is None


def test_planner_gap_enc_columns_roundtrip(db_session: Session) -> None:
    """All 5 new _enc columns on PlannerGap accept write + read back."""
    gap = PlannerGap(
        **_gap_kwargs(
            user_message_enc="купи молоко",
            plan_json_enc={"actions": {}, "compose": {"kind": "template"}},
            tool_args_json_enc={"item": "молоко", "qty": 2},
            actual_result_json_enc={"status": "ok", "ids": [1, 2]},
            error_details_enc="timeout on step s3",
        )
    )
    db_session.add(gap)
    db_session.flush()
    db_session.expire(gap)
    loaded = db_session.query(PlannerGap).one()
    assert loaded.user_message_enc == "купи молоко"
    assert loaded.plan_json_enc == {"actions": {}, "compose": {"kind": "template"}}
    assert loaded.tool_args_json_enc == {"item": "молоко", "qty": 2}
    assert loaded.actual_result_json_enc == {"status": "ok", "ids": [1, 2]}
    assert loaded.error_details_enc == "timeout on step s3"


def test_planner_gap_enc_columns_nullable(db_session: Session) -> None:
    """All 5 _enc columns default to NULL when not supplied."""
    gap = PlannerGap(**_gap_kwargs())
    db_session.add(gap)
    db_session.flush()
    db_session.expire(gap)
    loaded = db_session.query(PlannerGap).one()
    assert loaded.user_message_enc is None
    assert loaded.plan_json_enc is None
    assert loaded.tool_args_json_enc is None
    assert loaded.actual_result_json_enc is None
    assert loaded.error_details_enc is None


# ---------------------------------------------------------------------------
# PR-2a — ciphertext-at-rest: raw SQL must never show plaintext
# ---------------------------------------------------------------------------


def test_planner_gap_user_message_enc_stored_as_ciphertext(
    db_session: Session,
) -> None:
    """Bypass the ORM TypeDecorator and confirm the on-disk value for
    user_message_enc (EncryptedString) is a v2: envelope, not plaintext.

    Mirrors test_encrypted_string.py::test_stored_value_is_encrypted_envelope.
    """
    gap = PlannerGap(**_gap_kwargs(user_message_enc="секрет пользователя"))
    db_session.add(gap)
    db_session.flush()
    raw = db_session.execute(
        text(
            "SELECT user_message_enc FROM planner_gaps WHERE id = :id"
        ),
        {"id": gap.id},
    ).scalar()
    assert raw is not None
    assert raw.startswith("v2:")
    assert "секрет" not in raw
    assert "пользователя" not in raw


def test_planner_gap_plan_json_enc_stored_as_ciphertext(
    db_session: Session,
) -> None:
    """Bypass the ORM TypeDecorator and confirm the on-disk value for
    plan_json_enc (JSONEncryptedString) is a v2: envelope, not JSON text.

    Mirrors test_json_encrypted_string.py::test_stored_dict_is_encrypted_not_plaintext.
    """
    payload = {"actions": {}, "compose": {"kind": "template"}}
    gap = PlannerGap(**_gap_kwargs(plan_json_enc=payload))
    db_session.add(gap)
    db_session.flush()
    raw = db_session.execute(
        text(
            "SELECT plan_json_enc FROM planner_gaps WHERE id = :id"
        ),
        {"id": gap.id},
    ).scalar()
    assert raw is not None
    assert raw.startswith("v2:")
    assert "template" not in raw
    assert "{" not in raw


# ---------------------------------------------------------------------------
# Schema sanity — indexes & FK presence (cross-dialect)
# ---------------------------------------------------------------------------


def test_planner_executions_has_expected_indexes(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    names = {ix["name"] for ix in inspector.get_indexes("planner_executions")}
    expected = {
        "ix_planner_executions_recovery",
        "ix_planner_executions_tenant_created",
    }
    assert expected <= names, f"missing indexes: {expected - names}"


def test_planner_gaps_has_expected_indexes(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    names = {ix["name"] for ix in inspector.get_indexes("planner_gaps")}
    expected = {"ix_planner_gaps_open", "ix_planner_gaps_tenant_recent"}
    assert expected <= names, f"missing indexes: {expected - names}"
