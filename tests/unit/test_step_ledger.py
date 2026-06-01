"""Unit tests for sreda.runtime.planner.step_ledger (Sub-A12 Phase E).

Uses an in-memory SQLite engine built from the ORM models directly —
no Alembic, no external DB.  Each test function gets its own session via
the module-level ``session`` fixture (rolled back on teardown).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Register ALL tables so FK targets (Tenant, Workspace, AgentThread, AgentRun,
# PlannerExecution, StepExecutionLedger) are present in Base.metadata.
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — registers core tables (Tenant, AgentRun …)
import sreda.db.models.planner  # noqa: F401 — registers planner tables

from sreda.db.models import (
    AgentRun,
    AgentThread,
    PlannerExecution,
    Tenant,
    Workspace,
)
from sreda.db.models.planner import StepExecutionLedger
from sreda.runtime.planner.step_ledger import mark_step_status, open_step


# ---------------------------------------------------------------------------
# Fixed timestamp shared across all tests
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Session-scoped engine (single :memory: connection, StaticPool)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Per-test session rolled back on teardown."""
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Seed helpers — mirror test_planner_models.py conventions
# ---------------------------------------------------------------------------


def _seed_execution(session: Session) -> str:
    """Insert minimal FK chain and return the PlannerExecution id."""
    tenant_id = f"tenant_{uuid4().hex[:8]}"
    workspace_id = f"ws_{uuid4().hex[:8]}"
    thread_id = f"thread_{uuid4().hex[:8]}"
    run_id = f"run_{uuid4().hex[:8]}"
    exec_id = f"pe_{uuid4().hex[:8]}"

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
    session.add(
        PlannerExecution(
            id=exec_id,
            run_id=run_id,
            tenant_id=tenant_id,
            feature_key="housewife_assistant",
            planner_prompt_version=1,
            planner_provider="mimo-v2.5-pro",
            planner_model="mimo-v2.5-pro",
            planner_status="pending",
            execution_status="pending",
            execution_log_json=[],
            created_at=NOW,
        )
    )
    session.flush()
    return exec_id


# ---------------------------------------------------------------------------
# open_step
# ---------------------------------------------------------------------------


class TestOpenStep:
    def test_inserts_row_with_started_status(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        row = open_step(
            session,
            execution_id=exec_id,
            step_id="step_1",
            tool="add_item",
            operation_id="op_abc123",
            now=NOW,
        )
        assert row.status == "started"
        assert row.execution_id == exec_id
        assert row.step_id == "step_1"
        assert row.tool == "add_item"
        assert row.operation_id == "op_abc123"
        assert row.created_at == NOW
        assert row.updated_at == NOW
        assert len(row.id) > 0

    def test_idempotent_returns_same_row(self, session: Session) -> None:
        """A crash-retry calling open_step twice must get the same row."""
        exec_id = _seed_execution(session)
        row1 = open_step(
            session,
            execution_id=exec_id,
            step_id="step_2",
            tool="some_tool",
            operation_id="op_xyz",
            now=NOW,
        )
        row2 = open_step(
            session,
            execution_id=exec_id,
            step_id="step_2",
            tool="some_tool",
            operation_id="op_xyz",
            now=LATER,  # caller passes different now — must be ignored
        )
        # Same row id and operation_id
        assert row1.id == row2.id
        assert row2.operation_id == "op_xyz"
        # created_at is NOT updated (not reset by retry)
        assert row2.created_at == NOW

    def test_no_duplicate_row_on_retry(self, session: Session) -> None:
        """Exactly one row for (execution_id, step_id) after two calls."""
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="step_3",
            tool="t",
            operation_id="op_1",
            now=NOW,
        )
        open_step(
            session,
            execution_id=exec_id,
            step_id="step_3",
            tool="t",
            operation_id="op_1",
            now=LATER,
        )
        rows = session.execute(
            select(StepExecutionLedger).where(
                StepExecutionLedger.execution_id == exec_id,
                StepExecutionLedger.step_id == "step_3",
            )
        ).scalars().all()
        assert len(rows) == 1

    def test_tool_none_allowed(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        row = open_step(
            session,
            execution_id=exec_id,
            step_id="step_notool",
            tool=None,
            operation_id="op_notool",
            now=NOW,
        )
        assert row.tool is None

    def test_existing_row_operation_id_mismatch_raises(self, session: Session) -> None:
        """A resume that re-derives a DIFFERENT operation_id is a bug (split
        ledger/audit idempotency) — must fail loud, not silently attach."""
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="mm",
            tool="t",
            operation_id="op_original",
            now=NOW,
        )
        with pytest.raises(ValueError, match="operation_id"):
            open_step(
                session,
                execution_id=exec_id,
                step_id="mm",
                tool="t",
                operation_id="op_DIFFERENT",
                now=LATER,
            )

    def test_existing_row_tool_mismatch_raises(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="mm2",
            tool="tool_a",
            operation_id="op_mm2",
            now=NOW,
        )
        with pytest.raises(ValueError, match="tool"):
            open_step(
                session,
                execution_id=exec_id,
                step_id="mm2",
                tool="tool_b",
                operation_id="op_mm2",
                now=LATER,
            )

    def test_existing_row_matching_identity_ok(self, session: Session) -> None:
        """Same operation_id + same tool on resume → returns the row, no raise."""
        exec_id = _seed_execution(session)
        r1 = open_step(
            session,
            execution_id=exec_id,
            step_id="ok",
            tool="t",
            operation_id="op_ok",
            now=NOW,
        )
        r2 = open_step(
            session,
            execution_id=exec_id,
            step_id="ok",
            tool="t",
            operation_id="op_ok",
            now=LATER,
        )
        assert r1.id == r2.id

    def test_open_step_does_not_commit(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caller-commits contract (mirrors emit_event): open_step flushes but
        never commits — the executor owns the ledger-session transaction."""
        exec_id = _seed_execution(session)
        commits: list[int] = []
        monkeypatch.setattr(session, "commit", lambda: commits.append(1))
        open_step(
            session,
            execution_id=exec_id,
            step_id="nocommit",
            tool="t",
            operation_id="op_nc",
            now=NOW,
        )
        assert commits == []


# ---------------------------------------------------------------------------
# mark_step_status
# ---------------------------------------------------------------------------


class TestMarkStepStatus:
    def test_mark_committed_updates_status_and_updated_at(
        self, session: Session
    ) -> None:
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="s1",
            tool="t",
            operation_id="op_c1",
            now=NOW,
        )
        row = mark_step_status(
            session,
            execution_id=exec_id,
            step_id="s1",
            status="committed",
            now=LATER,
        )
        assert row.status == "committed"
        assert row.updated_at == LATER

    def test_mark_unknown_pending_works(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="s2",
            tool="t",
            operation_id="op_c2",
            now=NOW,
        )
        row = mark_step_status(
            session,
            execution_id=exec_id,
            step_id="s2",
            status="unknown_pending",
            now=LATER,
        )
        assert row.status == "unknown_pending"

    def test_mark_unknown_works(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="s3",
            tool="t",
            operation_id="op_c3",
            now=NOW,
        )
        row = mark_step_status(
            session,
            execution_id=exec_id,
            step_id="s3",
            status="unknown",
            now=LATER,
        )
        assert row.status == "unknown"

    def test_invalid_status_raises_value_error(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="s4",
            tool="t",
            operation_id="op_c4",
            now=NOW,
        )
        with pytest.raises(ValueError, match="Invalid ledger status"):
            mark_step_status(
                session,
                execution_id=exec_id,
                step_id="s4",
                status="started",  # not allowed via mark_step_status
                now=LATER,
            )

    def test_bogus_status_raises_value_error(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        open_step(
            session,
            execution_id=exec_id,
            step_id="s5",
            tool="t",
            operation_id="op_c5",
            now=NOW,
        )
        with pytest.raises(ValueError, match="Invalid ledger status"):
            mark_step_status(
                session,
                execution_id=exec_id,
                step_id="s5",
                status="bogus",
                now=LATER,
            )

    def test_missing_row_raises_lookup_error(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        with pytest.raises(LookupError, match="No StepExecutionLedger row"):
            mark_step_status(
                session,
                execution_id=exec_id,
                step_id="nonexistent_step",
                status="committed",
                now=LATER,
            )
