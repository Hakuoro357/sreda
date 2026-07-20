"""Unit tests for sreda.runtime.planner.recovery_scanner (Sub-A12 Phase E #9c).

Uses an in-memory SQLite engine.  Each test class gets a clean DB
(module-scoped engine + per-test rolled-back sessions).

FK chain required by PlannerExecution:
    Tenant → Workspace → AgentThread → AgentRun → PlannerExecution
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Register ALL FK-target tables before create_all.
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — registers all ORM tables incl. checklists (#86)
import sreda.db.models.planner  # noqa: F401 — PlannerExecution, StepExecutionLedger
import sreda.db.models.audit_feed  # noqa: F401 — AuditOutboxEvent

from sreda.db.models import (
    AgentRun,
    AgentThread,
    PlannerExecution,
    Tenant,
    Workspace,
)
from sreda.db.models.audit_feed import AuditOutboxEvent
from sreda.db.models.planner import StepExecutionLedger
from sreda.runtime.planner.recovery import (
    TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL,
    TERMINAL_FAILED_NEEDS_MANUAL,
)
from sreda.runtime.planner.recovery_scanner import (
    _M6_HARD_SETTLE_FACTOR,
    claim_stale_executions,
    recover_execution,
    run_recovery_scan,
)
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
PAST = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)   # 2 h ago — lease expired
FAR_PAST = datetime(2026, 6, 1,  0, 0, tzinfo=timezone.utc)  # 12 h ago — well past settle

LEASE_SECONDS = 300        # mirrors message_queue.LEASE_DURATION_SEC
SETTLE_SECONDS = 240       # > chain-timeout (120) + worst-step (90)
WORKER = "scanner-test-01"

# Real registry built from MIGRATED_TOOL_SPECS.
REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}

# Spot-check the test registry contains the tools we rely on.
assert "add_shopping_items" in REGISTRY, "add_shopping_items missing from MIGRATED_TOOL_SPECS"
assert "list_shopping" in REGISTRY, "list_shopping missing from MIGRATED_TOOL_SPECS"

# Confirm expected durable/read classification.
assert REGISTRY["add_shopping_items"].is_durable_write, "add_shopping_items must be durable"
assert not REGISTRY["list_shopping"].is_durable_write, "list_shopping must be non-durable"


# ---------------------------------------------------------------------------
# Module-scoped engine + per-test session fixture
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
    """Per-test session, rolled back on teardown."""
    conn = engine.connect()
    trans = conn.begin()
    sess = sessionmaker(bind=conn)()
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def make_session(engine):
    """Factory that returns a new independent session (for run_recovery_scan)."""
    factory = sessionmaker(bind=engine)
    sessions: list[Session] = []

    def _factory() -> Session:
        s = factory()
        sessions.append(s)
        return s

    yield _factory

    for s in sessions:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_execution(
    session: Session,
    *,
    execution_status: str = "in_progress",
    recovery_lease_until: datetime | None = None,
    recovery_worker_id: str | None = None,
    recovery_attempt: int = 0,
    created_at: datetime = NOW,
) -> str:
    """Insert minimal FK chain and return the PlannerExecution.id."""
    tenant_id = f"t_{uuid4().hex[:8]}"
    ws_id = f"ws_{uuid4().hex[:8]}"
    thread_id = f"th_{uuid4().hex[:8]}"
    run_id = f"run_{uuid4().hex[:8]}"
    exec_id = f"pe_{uuid4().hex[:8]}"

    session.add(Tenant(id=tenant_id, name="test-tenant"))
    session.add(Workspace(id=ws_id, tenant_id=tenant_id, name="test-ws"))
    session.add(
        AgentThread(
            id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
            channel_type="telegram",
            external_chat_id="99",
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
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
            planner_status="valid",
            execution_status=execution_status,
            execution_log_json=[],
            created_at=created_at,
            recovery_lease_until=recovery_lease_until,
            recovery_worker_id=recovery_worker_id,
            recovery_attempt=recovery_attempt,
        )
    )
    session.flush()
    return exec_id


def _seed_ledger_row(
    session: Session,
    *,
    execution_id: str,
    step_id: str = "s1",
    tool: str = "add_shopping_items",
    operation_id: str | None = None,
    status: str = "started",
    updated_at: datetime = NOW,
) -> str:
    """Insert a StepExecutionLedger row and return its operation_id."""
    op_id = operation_id or uuid4().hex
    row = StepExecutionLedger(
        id=uuid4().hex,
        execution_id=execution_id,
        step_id=step_id,
        tool=tool,
        operation_id=op_id,
        status=status,
        created_at=updated_at,
        updated_at=updated_at,
    )
    session.add(row)
    session.flush()
    return op_id


def _seed_audit_outbox(
    session: Session,
    *,
    operation_id: str,
    tenant_id: str = "t_test",
) -> None:
    """Insert an AuditOutboxEvent to simulate a committed durable write."""
    session.add(
        AuditOutboxEvent(
            operation_id=operation_id,
            tenant_id=tenant_id,
            source="среда",
            entity_type="shopping_list_item",
            action="created",
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Tests: claim_stale_executions
# ---------------------------------------------------------------------------


class TestClaimStaleExecutions:
    def test_claims_execution_with_null_lease(self, session: Session) -> None:
        """in_progress + recovery_lease_until=None → should be claimed."""
        exec_id = _seed_execution(session, recovery_lease_until=None)

        claimed = claim_stale_executions(
            session, worker_id=WORKER, now=NOW, lease_seconds=LEASE_SECONDS
        )

        assert exec_id in claimed
        row = session.get(PlannerExecution, exec_id)
        assert row is not None
        assert row.recovery_worker_id == WORKER
        assert row.recovery_attempt == 1
        assert row.recovery_lease_until is not None
        assert row.recovery_lease_until > NOW

    def test_claims_execution_with_expired_lease(self, session: Session) -> None:
        """in_progress + recovery_lease_until in the past → should be claimed."""
        expired_lease = NOW - timedelta(seconds=1)
        exec_id = _seed_execution(
            session,
            recovery_lease_until=expired_lease,
            recovery_worker_id="old-worker",
            recovery_attempt=1,
        )

        claimed = claim_stale_executions(
            session, worker_id=WORKER, now=NOW, lease_seconds=LEASE_SECONDS
        )

        assert exec_id in claimed
        row = session.get(PlannerExecution, exec_id)
        assert row is not None
        assert row.recovery_worker_id == WORKER
        assert row.recovery_attempt == 2  # incremented from 1

    def test_does_not_claim_active_lease(self, session: Session) -> None:
        """in_progress + recovery_lease_until in the future → must NOT be claimed."""
        future_lease = NOW + timedelta(seconds=100)
        exec_id = _seed_execution(
            session,
            recovery_lease_until=future_lease,
            recovery_worker_id="other-worker",
        )

        claimed = claim_stale_executions(
            session, worker_id=WORKER, now=NOW, lease_seconds=LEASE_SECONDS
        )

        assert exec_id not in claimed

    def test_does_not_claim_completed_execution(self, session: Session) -> None:
        """Terminal execution_status='completed' → must NOT be claimed."""
        exec_id = _seed_execution(session, execution_status="completed")

        claimed = claim_stale_executions(
            session, worker_id=WORKER, now=NOW, lease_seconds=LEASE_SECONDS
        )

        assert exec_id not in claimed

    def test_does_not_claim_failed_needs_manual(self, session: Session) -> None:
        """Terminal execution_status='failed_needs_manual' → must NOT be claimed."""
        exec_id = _seed_execution(
            session, execution_status="failed_needs_manual"
        )

        claimed = claim_stale_executions(
            session, worker_id=WORKER, now=NOW, lease_seconds=LEASE_SECONDS
        )

        assert exec_id not in claimed

    def test_limit_respected(self, session: Session) -> None:
        """Only up to `limit` executions are claimed per call."""
        ids = [_seed_execution(session) for _ in range(5)]

        claimed = claim_stale_executions(
            session, worker_id=WORKER, now=NOW, lease_seconds=LEASE_SECONDS, limit=3
        )

        assert len(claimed) == 3
        # All claimed ids must come from the seeded set.
        for cid in claimed:
            assert cid in ids


# ---------------------------------------------------------------------------
# Tests: recover_execution
# ---------------------------------------------------------------------------


class TestRecoverExecution:
    def test_durable_step_landed_yields_committed_unserved(self, session: Session) -> None:
        """Durable step with audit row → ledger 'committed', but the execution
        is committed_unserved_needs_manual (NOT 'completed'): the scanner cannot
        prove the user was served. Codex A/B #9c R1, both MAJOR."""
        exec_id = _seed_execution(session)
        op_id = _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="add_shopping_items",
            status="started",
        )
        _seed_audit_outbox(session, operation_id=op_id)

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        assert outcome.status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
        assert execution.recovery_lease_until is None
        # The durable step's write is recorded committed in the ledger.
        ledger_row = session.execute(
            select(StepExecutionLedger).where(
                StepExecutionLedger.execution_id == exec_id
            )
        ).scalar_one()
        assert ledger_row.status == "committed"
        # An alert payload is returned for the unserved-but-committed turn
        # (run_recovery_scan fires it post-commit; recover_execution never does).
        assert outcome.alert is not None
        assert exec_id in outcome.alert.body
        assert "unserved" in outcome.alert.dedupe_key

    def test_durable_step_not_landed_yields_failed_needs_manual(
        self, session: Session
    ) -> None:
        """Durable step, no audit row, settle elapsed → failed_needs_manual."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="add_shopping_items",
            status="started",
            # No audit outbox row seeded → probe returns False.
            updated_at=FAR_PAST,  # settle window definitely elapsed
        )

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        assert outcome.status == TERMINAL_FAILED_NEEDS_MANUAL
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == TERMINAL_FAILED_NEEDS_MANUAL
        assert execution.recovery_lease_until is None  # lease cleared
        # Alert payload returned (fired post-commit by run_recovery_scan).
        assert outcome.alert is not None
        assert exec_id in outcome.alert.body
        assert "failed" in outcome.alert.dedupe_key

    def test_unknown_pending_not_settled_yields_re_probe(
        self, session: Session
    ) -> None:
        """unknown_pending step, settle window NOT elapsed → re_probe_pending."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="add_shopping_items",
            status="unknown_pending",
            updated_at=NOW,  # just now → settle window not elapsed
            # No audit row → probe returns False
        )

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        assert outcome.status == "re_probe_pending"
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == "in_progress"  # not changed to terminal
        # BACKOFF lease (NOT None) = step.updated_at + HARD settle window (M6 R1:
        # settle_window × _M6_HARD_SETTLE_FACTOR) — grace для позднего to_thread
        # commit, backoff выровнен на то же окно (без tight-loop). Codex A/B #9c.
        assert execution.recovery_lease_until is not None
        # SQLite reads DateTime back as naive; normalise to UTC before compare.
        lease = execution.recovery_lease_until
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=timezone.utc)
        assert lease == NOW + timedelta(
            seconds=SETTLE_SECONDS * _M6_HARD_SETTLE_FACTOR
        )

    def test_unknown_pending_settled_not_landed_yields_failed_needs_manual(
        self, session: Session
    ) -> None:
        """unknown_pending step, settle window elapsed, no audit → failed_needs_manual."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="add_shopping_items",
            status="unknown_pending",
            updated_at=FAR_PAST,  # well past settle window
            # No audit row → probe returns False
        )

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        assert outcome.status == TERMINAL_FAILED_NEEDS_MANUAL
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == TERMINAL_FAILED_NEEDS_MANUAL

    def test_unknown_tool_treated_as_durable_fail_closed(
        self, session: Session
    ) -> None:
        """Unknown tool (not in registry) is treated as durable → probe → failed_needs_manual."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="completely_unknown_tool_xyz",  # not in registry
            status="started",
            updated_at=FAR_PAST,
            # No audit row → probe returns False
        )

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        # Fail-closed: unknown tool → treat as durable → no audit row → manual
        assert outcome.status == TERMINAL_FAILED_NEEDS_MANUAL

    def test_read_only_step_yields_failed_no_data_impact(self, session: Session) -> None:
        """Non-durable step (list_shopping) started → skip → NO durable commit →
        execution 'failed' (interrupted, no data impact). NEVER 'completed' —
        the scanner cannot prove the user was served. Codex A/B #9c R1."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="list_shopping",  # is_durable_write=False
            status="started",
            updated_at=FAR_PAST,
        )

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        assert outcome.status == "failed"
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == "failed"
        assert execution.recovery_lease_until is None

    def test_empty_ledger_yields_failed(self, session: Session) -> None:
        """An in_progress execution with NO ledger rows (crash before any step
        started) → 'failed', NOT 'completed'. Codex A/B #9c R1 MAJOR — the
        empty-ledger case must not be a false success."""
        exec_id = _seed_execution(session)  # no ledger rows seeded

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )

        assert outcome.status == "failed"
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == "failed"

    def test_re_probe_backoff_lease_prevents_immediate_reclaim(
        self, session: Session
    ) -> None:
        """After re_probe sets a backoff lease, a claim BEFORE the deadline does
        NOT re-claim; a claim AFTER does. Codex A/B #9c R1 MAJOR (no tight loop)."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session,
            execution_id=exec_id,
            tool="add_shopping_items",
            status="unknown_pending",
            updated_at=NOW,  # settle window not yet elapsed
        )

        outcome = recover_execution(
            session,
            execution_id=exec_id,
            registry=REGISTRY,
            now=NOW,
            settle_window_seconds=SETTLE_SECONDS,
        )
        assert outcome.status == "re_probe_pending"
        # M6 (R1): backoff = updated_at + HARD settle window (settle × factor).
        deadline = NOW + timedelta(seconds=SETTLE_SECONDS * _M6_HARD_SETTLE_FACTOR)

        # A claim BEFORE the backoff deadline must NOT re-claim (lease active).
        before = claim_stale_executions(
            session, worker_id=WORKER, now=deadline - timedelta(seconds=5),
            lease_seconds=LEASE_SECONDS,
        )
        assert exec_id not in before

        # A claim AFTER the deadline re-claims (lease expired → re-probe due).
        after = claim_stale_executions(
            session, worker_id=WORKER, now=deadline + timedelta(seconds=5),
            lease_seconds=LEASE_SECONDS,
        )
        assert exec_id in after

    # --- Mixed-outcome priority (Codex A/B #9c R2, both MAJOR) -------------
    # Priority: any_committed_durable > re_probe_pending > needs_manual > failed.

    def test_committed_plus_failed_yields_committed_unserved(
        self, session: Session
    ) -> None:
        """One durable step committed + another durable step NOT landed →
        committed_unserved_needs_manual (committed data DOMINATES needs_manual:
        the dangerous 'state changed' signal must win)."""
        exec_id = _seed_execution(session)
        op_ok = _seed_ledger_row(
            session, execution_id=exec_id, step_id="s1",
            tool="add_shopping_items", status="started", updated_at=FAR_PAST,
        )
        _seed_audit_outbox(session, operation_id=op_ok)  # s1 landed
        _seed_ledger_row(
            session, execution_id=exec_id, step_id="s2",
            tool="add_shopping_items", status="started", updated_at=FAR_PAST,
        )  # s2 no audit → would be failed_needs_manual on its own

        outcome = recover_execution(
            session, execution_id=exec_id, registry=REGISTRY,
            now=NOW, settle_window_seconds=SETTLE_SECONDS,
        )
        assert outcome.status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
        assert outcome.alert is not None

    def test_committed_plus_re_probe_yields_committed_unserved(
        self, session: Session
    ) -> None:
        """One durable step committed + another durable step still unsettled
        (unknown_pending, window not elapsed) → committed_unserved_needs_manual
        immediately (committed DOMINATES re_probe; the pending step's
        idempotency is independently protected, so no need to keep probing)."""
        exec_id = _seed_execution(session)
        op_ok = _seed_ledger_row(
            session, execution_id=exec_id, step_id="s1",
            tool="add_shopping_items", status="started", updated_at=FAR_PAST,
        )
        _seed_audit_outbox(session, operation_id=op_ok)  # s1 landed
        _seed_ledger_row(
            session, execution_id=exec_id, step_id="s2",
            tool="add_shopping_items", status="unknown_pending", updated_at=NOW,
        )  # s2 unsettled → would be re_probe on its own

        outcome = recover_execution(
            session, execution_id=exec_id, registry=REGISTRY,
            now=NOW, settle_window_seconds=SETTLE_SECONDS,
        )
        assert outcome.status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.recovery_lease_until is None  # terminal, not re-probed

    def test_failed_plus_re_probe_yields_re_probe(self, session: Session) -> None:
        """No durable commit; one durable step failed (settled, not landed) +
        another still unsettled → re_probe_pending (keep probing the unsettled
        one before terminalizing). re_probe DOMINATES needs_manual when nothing
        committed."""
        exec_id = _seed_execution(session)
        _seed_ledger_row(
            session, execution_id=exec_id, step_id="s1",
            tool="add_shopping_items", status="started", updated_at=FAR_PAST,
        )  # settled, no audit → needs_manual on its own
        _seed_ledger_row(
            session, execution_id=exec_id, step_id="s2",
            tool="add_shopping_items", status="unknown_pending", updated_at=NOW,
        )  # unsettled → re_probe

        outcome = recover_execution(
            session, execution_id=exec_id, registry=REGISTRY,
            now=NOW, settle_window_seconds=SETTLE_SECONDS,
        )
        assert outcome.status == "re_probe_pending"
        execution = session.get(PlannerExecution, exec_id)
        assert execution is not None
        assert execution.execution_status == "in_progress"
        assert execution.recovery_lease_until is not None  # backoff lease set


# ---------------------------------------------------------------------------
# Tests: run_recovery_scan — end-to-end
# ---------------------------------------------------------------------------


class TestRunRecoveryScan:
    def test_end_to_end_summary_two_executions(self, engine: Any) -> None:
        """Two executions: one committed-unserved (audit landed), one
        failed_needs_manual (no audit). Summary dict correct + executions
        isolated. The scanner never reports 'completed'."""
        factory = sessionmaker(bind=engine)

        # Seed both executions in a shared session, commit so run_recovery_scan
        # can see the rows in its own independent sessions.
        setup_session = factory()
        try:
            # Execution 1: durable step with audit → committed_unserved_needs_manual.
            exec_id_1 = _seed_execution(setup_session)
            op_id_1 = _seed_ledger_row(
                setup_session,
                execution_id=exec_id_1,
                tool="add_shopping_items",
                status="started",
                updated_at=FAR_PAST,
            )
            _seed_audit_outbox(setup_session, operation_id=op_id_1)

            # Execution 2: durable step, no audit, settle elapsed → failed_needs_manual.
            exec_id_2 = _seed_execution(setup_session)
            _seed_ledger_row(
                setup_session,
                execution_id=exec_id_2,
                tool="add_shopping_items",
                status="started",
                updated_at=FAR_PAST,
            )

            setup_session.commit()
        finally:
            setup_session.close()

        alert_calls: list[dict[str, Any]] = []

        def capture_alert(**kwargs: Any) -> None:
            alert_calls.append(kwargs)

        summary = run_recovery_scan(
            factory,
            worker_id=WORKER,
            now_fn=lambda: NOW,
            lease_seconds=LEASE_SECONDS,
            settle_window_seconds=SETTLE_SECONDS,
            registry=REGISTRY,
            alert_fn=capture_alert,
            limit=10,
        )

        assert summary["claimed"] == 2
        # exec1 (durable write landed, serving unproven) → committed_unserved;
        # exec2 (durable write not landed, settled) → failed_needs_manual.
        # The scanner never reports 'completed'.
        assert summary["committed_unserved_needs_manual"] == 1
        assert summary["failed_needs_manual"] == 1
        assert summary["failed"] == 0
        assert summary["re_probe_pending"] == 0
        assert summary["errored"] == 0

        # Both terminal outcomes fire a P1 alert (committed_unserved + failed_needs_manual).
        assert len(alert_calls) == 2

        # Verify DB state via a fresh read session.
        verify_session = factory()
        try:
            e1 = verify_session.get(PlannerExecution, exec_id_1)
            e2 = verify_session.get(PlannerExecution, exec_id_2)
            assert e1 is not None
            assert e1.execution_status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
            assert e2 is not None and e2.execution_status == TERMINAL_FAILED_NEEDS_MANUAL
        finally:
            verify_session.close()

    def test_per_execution_isolation_on_error(self, engine: Any) -> None:
        """Codex A/B #9c R1 MINOR — TWO claimable executions; recovery raises
        for ONE specific id; the OTHER must still commit its terminal status,
        and the crash is counted as errored (not propagated)."""
        from unittest.mock import patch

        factory = sessionmaker(bind=engine)

        # exec_good: durable write landed → committed_unserved_needs_manual.
        # exec_bad: recovery will be patched to raise for this id only.
        setup_session = factory()
        try:
            exec_id_good = _seed_execution(setup_session)
            op_id = _seed_ledger_row(
                setup_session,
                execution_id=exec_id_good,
                tool="add_shopping_items",
                status="started",
                updated_at=FAR_PAST,
            )
            _seed_audit_outbox(setup_session, operation_id=op_id)

            exec_id_bad = _seed_execution(setup_session)
            _seed_ledger_row(
                setup_session,
                execution_id=exec_id_bad,
                tool="add_shopping_items",
                status="started",
                updated_at=FAR_PAST,
            )
            setup_session.commit()
        finally:
            setup_session.close()

        original_recover = recover_execution

        def flaky_recover(session, *, execution_id, **kwargs):
            if execution_id == exec_id_bad:
                raise RuntimeError("simulated recover crash for the bad execution")
            return original_recover(session, execution_id=execution_id, **kwargs)

        with patch(
            "sreda.runtime.planner.recovery_scanner.recover_execution",
            side_effect=flaky_recover,
        ):
            summary = run_recovery_scan(
                factory,
                worker_id=WORKER,
                now_fn=lambda: NOW,
                lease_seconds=LEASE_SECONDS,
                settle_window_seconds=SETTLE_SECONDS,
                registry=REGISTRY,
                limit=10,
            )

        assert summary["claimed"] == 2
        assert summary["errored"] == 1  # exec_bad crashed
        assert summary["committed_unserved_needs_manual"] == 1  # exec_good resolved

        # The good execution committed its terminal despite the other's crash.
        verify_session = factory()
        try:
            good = verify_session.get(PlannerExecution, exec_id_good)
            bad = verify_session.get(PlannerExecution, exec_id_bad)
            assert good is not None
            assert good.execution_status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
            # exec_bad's recovery rolled back; the CLAIM committed, so it stays
            # in_progress with the claim's lease (re-claimable after expiry).
            assert bad is not None
            assert bad.execution_status == "in_progress"
        finally:
            verify_session.close()

    def test_no_claimable_executions_returns_zero_claimed(self, engine: Any) -> None:
        """When no stalled executions exist, summary claimed=0 and no errors."""
        factory = sessionmaker(bind=engine)

        # Seed a completed execution — must not be claimed.
        setup_session = factory()
        try:
            _seed_execution(setup_session, execution_status="completed")
            setup_session.commit()
        finally:
            setup_session.close()

        summary = run_recovery_scan(
            factory,
            worker_id=WORKER,
            now_fn=lambda: NOW,
            lease_seconds=LEASE_SECONDS,
            settle_window_seconds=SETTLE_SECONDS,
            registry=REGISTRY,
            limit=10,
        )

        assert summary["claimed"] == 0
        assert summary["errored"] == 0
        assert summary["failed"] == 0
        assert summary["committed_unserved_needs_manual"] == 0
