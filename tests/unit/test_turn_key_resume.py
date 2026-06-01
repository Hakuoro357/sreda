"""Unit tests for create_or_resume_execution (Sub-A12 Phase E PR-2a #9b).

Uses an in-memory SQLite StaticPool engine built from ORM models directly —
no Alembic, no external DB.  Each test function gets its own session via
the per-test ``session`` fixture (rolled back on teardown), matching the
test_step_ledger.py pattern.

FK chain seeded per test: Tenant → Workspace → AgentThread → AgentRun.
create_or_resume_execution itself creates the PlannerExecution row, so
we seed only up to AgentRun and pass its run_id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Register ALL tables so FK targets are present in Base.metadata.
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — registers core tables (Tenant, AgentRun …)
import sreda.db.models.checklists  # noqa: F401 — tasks_items FKs checklists.id (not in __init__ yet)
import sreda.db.models.planner  # noqa: F401 — registers planner tables

from sreda.db.models import (
    AgentRun,
    AgentThread,
    Tenant,
    Workspace,
)
from sreda.db.models.planner import PlannerExecution
from sreda.runtime.planner.persistence import (
    ResumeResult,
    create_or_resume_execution,
    make_execution_id,
)


# ---------------------------------------------------------------------------
# Fixed timestamp
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Module-scoped engine, per-test session (same shape as test_step_ledger.py)
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
    """Per-test session, rolled back on teardown — no cross-test pollution."""
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
# Seed helper — mirrors _seed_execution in test_step_ledger.py but only
# seeds up to AgentRun (create_or_resume_execution creates the execution).
# ---------------------------------------------------------------------------


def _seed_run(session: Session) -> tuple[str, str]:
    """Insert minimal FK chain and return (tenant_id, run_id)."""
    tenant_id = f"tenant_{uuid4().hex[:8]}"
    workspace_id = f"ws_{uuid4().hex[:8]}"
    thread_id = f"thread_{uuid4().hex[:8]}"
    run_id = f"run_{uuid4().hex[:8]}"

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
    return tenant_id, run_id


def _call(
    session: Session,
    *,
    turn_key: str,
    tenant_id: str,
    run_id: str,
    execution_id: str | None = None,
) -> ResumeResult:
    """Thin wrapper that fills in boilerplate planner args."""
    return create_or_resume_execution(
        session,
        turn_key=turn_key,
        execution_id=execution_id or make_execution_id(),
        run_id=run_id,
        tenant_id=tenant_id,
        feature_key="housewife_assistant",
        planner_prompt_version=1,
        planner_provider="mimo-v2.5-pro",
        planner_model="mimo-v2.5-pro",
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestCreateOrResumeExecution:
    def test_first_call_creates_row(self, session: Session) -> None:
        """First call with a fresh turn_key → resumed=False, row exists."""
        tenant_id, run_id = _seed_run(session)
        tk = f"t:{tenant_id}:tg:upd_001"

        result = _call(session, turn_key=tk, tenant_id=tenant_id, run_id=run_id)

        assert result.resumed is False
        # Row is actually in the DB with the right turn_key.
        row = session.scalars(
            select(PlannerExecution).where(PlannerExecution.turn_key == tk)
        ).one_or_none()
        assert row is not None
        assert row.id == result.execution_id

    def test_second_call_same_turn_key_resumes(self, session: Session) -> None:
        """Second call with the same turn_key returns the FIRST row's id."""
        tenant_id, run_id = _seed_run(session)
        tk = f"t:{tenant_id}:tg:upd_002"

        first = _call(session, turn_key=tk, tenant_id=tenant_id, run_id=run_id)
        assert first.resumed is False

        # Simulate a retry: caller mints a NEW candidate execution_id.
        second = _call(
            session,
            turn_key=tk,
            tenant_id=tenant_id,
            run_id=run_id,
            execution_id=make_execution_id(),  # different candidate id
        )

        assert second.resumed is True
        # Must return the FIRST row's id, not the new candidate.
        assert second.execution_id == first.execution_id

        # Exactly one row for this turn_key.
        rows = session.scalars(
            select(PlannerExecution).where(PlannerExecution.turn_key == tk)
        ).all()
        assert len(rows) == 1

    def test_two_different_turn_keys_create_two_rows(
        self, session: Session
    ) -> None:
        """Two distinct turn_keys each produce a fresh, independent row."""
        tenant_id, run_id = _seed_run(session)
        tk_a = f"t:{tenant_id}:tg:upd_003a"
        tk_b = f"t:{tenant_id}:tg:upd_003b"

        r_a = _call(session, turn_key=tk_a, tenant_id=tenant_id, run_id=run_id)
        r_b = _call(session, turn_key=tk_b, tenant_id=tenant_id, run_id=run_id)

        assert r_a.resumed is False
        assert r_b.resumed is False
        assert r_a.execution_id != r_b.execution_id

        # Both rows exist.
        row_a = session.get(PlannerExecution, r_a.execution_id)
        row_b = session.get(PlannerExecution, r_b.execution_id)
        assert row_a is not None
        assert row_b is not None
        assert row_a.turn_key == tk_a
        assert row_b.turn_key == tk_b

    def test_empty_turn_key_raises_value_error(self, session: Session) -> None:
        """An empty turn_key must be rejected fail-closed."""
        tenant_id, run_id = _seed_run(session)
        with pytest.raises(ValueError, match="turn_key"):
            _call(session, turn_key="", tenant_id=tenant_id, run_id=run_id)

    def test_new_row_has_pending_defaults(self, session: Session) -> None:
        """Newly inserted row must carry the same pending defaults as insert_pending."""
        tenant_id, run_id = _seed_run(session)
        tk = f"t:{tenant_id}:tg:upd_defaults"

        result = _call(session, turn_key=tk, tenant_id=tenant_id, run_id=run_id)
        assert result.resumed is False

        row = session.get(PlannerExecution, result.execution_id)
        assert row is not None
        assert row.planner_status == "pending"
        assert row.execution_status == "pending"
        assert row.execution_log_json == []
        assert row.turn_key == tk
        assert row.created_at is not None

    def test_pre_existing_row_returns_resumed_true(self, session: Session) -> None:
        """Covers the found-path and the IntegrityError race proxy.

        We insert a PlannerExecution with the turn_key directly, then call
        create_or_resume_execution with that same turn_key.  The SELECT in
        step 2 finds the pre-existing row and returns resumed=True without
        inserting a duplicate.

        This also deterministically covers the race: the pre-insert simulates
        what a concurrent winner's INSERT would have left behind.  The full
        concurrent IntegrityError path (SELECT misses → INSERT → race →
        IntegrityError → re-SELECT) is guaranteed correct by code review +
        the savepoint pattern used; it cannot be triggered deterministically
        in a single-connection SQLite test.
        """
        tenant_id, run_id = _seed_run(session)
        tk = f"t:{tenant_id}:tg:upd_race"
        pre_id = make_execution_id()

        # Simulate a concurrent winner having already inserted.
        session.add(
            PlannerExecution(
                id=pre_id,
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
                turn_key=tk,
            )
        )
        session.flush()

        # Now call with a different candidate id — must re-attach, not insert.
        result = _call(
            session,
            turn_key=tk,
            tenant_id=tenant_id,
            run_id=run_id,
            execution_id=make_execution_id(),
        )

        assert result.resumed is True
        assert result.execution_id == pre_id

        # Still exactly one row for this turn_key.
        rows = session.scalars(
            select(PlannerExecution).where(PlannerExecution.turn_key == tk)
        ).all()
        assert len(rows) == 1

    def test_whitespace_turn_key_raises_value_error(self, session: Session) -> None:
        """Codex A/B #9b R1 MAJOR — whitespace-only turn_key is just as
        collapsing as empty; must fail closed."""
        tenant_id, run_id = _seed_run(session)
        with pytest.raises(ValueError, match="turn_key"):
            _call(session, turn_key="   ", tenant_id=tenant_id, run_id=run_id)

    def test_integrity_error_branch_re_selects(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deterministically exercise the REAL race path (Codex A/B #9b R1 MINOR):
        the initial SELECT misses, the INSERT collides on the unique turn_key,
        the savepoint rolls back, and the except branch re-SELECTs the winner —
        returning resumed=True with the parent transaction left healthy.

        We force the miss by monkeypatching session.scalars so ONLY the first
        call returns an empty result; a genuinely-present committed row then
        makes the INSERT raise IntegrityError, and the re-SELECT (call #2,
        delegated to the real scalars) finds it."""
        tenant_id, run_id = _seed_run(session)
        tk = f"t:{tenant_id}:tg:upd_real_race"
        pre_id = make_execution_id()
        # A real row with this turn_key exists + is flushed (unique index live).
        session.add(
            PlannerExecution(
                id=pre_id,
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
                turn_key=tk,
            )
        )
        session.flush()

        real_scalars = session.scalars
        calls = {"n": 0}

        class _EmptyResult:
            def one_or_none(self):
                return None

        def _fake_scalars(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate the race window: the initial locked SELECT misses.
                return _EmptyResult()
            return real_scalars(*args, **kwargs)

        monkeypatch.setattr(session, "scalars", _fake_scalars)

        result = _call(
            session,
            turn_key=tk,
            tenant_id=tenant_id,
            run_id=run_id,
            execution_id=make_execution_id(),
        )

        # INSERT hit the unique violation → savepoint rollback → re-SELECT winner.
        assert result.resumed is True
        assert result.execution_id == pre_id
        assert calls["n"] >= 2  # initial miss + re-select

        # Parent transaction is healthy: still queryable after the nested rollback.
        monkeypatch.undo()
        rows = session.scalars(
            select(PlannerExecution).where(PlannerExecution.turn_key == tk)
        ).all()
        assert len(rows) == 1
