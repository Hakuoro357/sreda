"""Unit tests for sreda.runtime.planner.billing (Sub-A12 Phase E PR-2a #10c).

Uses an in-memory SQLite engine built from the ORM models — no Alembic,
no external DB.  Each test gets its own session rolled back on teardown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Register ALL tables so FK targets are present in Base.metadata.
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — registers Tenant, Workspace, AgentThread, AgentRun
import sreda.db.models.planner  # noqa: F401 — registers PlannerExecution, PlannerLlmReservation

from sreda.db.models import (
    AgentRun,
    AgentThread,
    Tenant,
    Workspace,
)
from sreda.db.models.planner import PlannerExecution, PlannerLlmReservation
from sreda.runtime.planner.billing import (
    charge,
    expire,
    make_llm_call_id,
    mark_unknown_provider_outcome,
    release,
    reserve,
)

# ---------------------------------------------------------------------------
# Fixed timestamps
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)
# EVEN_LATER is used when expiring a row with ttl_seconds=3600 (expires_at = NOW+1h = LATER).
# expire() requires expires_at < now (strict), so now must be > LATER.
EVEN_LATER = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)

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
# Seed helpers — minimal FK chain
# ---------------------------------------------------------------------------


def _seed_execution(session: Session) -> str:
    """Insert Tenant→Workspace→AgentThread→AgentRun→PlannerExecution.

    Returns the PlannerExecution.id so tests can attach reservations to it.
    """
    tenant_id = f"tenant_{uuid4().hex[:8]}"
    ws_id = f"ws_{uuid4().hex[:8]}"
    thread_id = f"thr_{uuid4().hex[:8]}"
    run_id = f"run_{uuid4().hex[:8]}"
    exec_id = f"pe_{uuid4().hex[:8]}"

    session.add(Tenant(id=tenant_id, name="t"))
    session.add(Workspace(id=ws_id, tenant_id=tenant_id, name="w"))
    session.add(
        AgentThread(
            id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
            channel_type="telegram",
            external_chat_id="42",
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
            feature_key="test_skill",
            planner_prompt_version=1,
            planner_provider="mimo",
            planner_model="mimo-v2.5-pro",
            planner_status="pending",
            execution_status="pending",
            execution_log_json=[],
            created_at=NOW,
        )
    )
    session.flush()
    return exec_id


def _make_reservation(
    session: Session,
    *,
    llm_call_id: str | None = None,
    execution_id: str | None = None,
    reserved_tokens: int = 500,
    ttl_seconds: int | None = None,
) -> PlannerLlmReservation:
    """Convenience: insert a 'reserved' row and return it."""
    if llm_call_id is None:
        llm_call_id = f"exec123:planner:0:mimo-v2.5-pro_{uuid4().hex[:6]}"
    return reserve(
        session,
        llm_call_id=llm_call_id,
        execution_id=execution_id,
        tenant_id="tenant_test",
        feature_key="skill_a",
        model="mimo-v2.5-pro",
        reserved_tokens=reserved_tokens,
        now=NOW,
        ttl_seconds=ttl_seconds,
    )


# ---------------------------------------------------------------------------
# make_llm_call_id
# ---------------------------------------------------------------------------


class TestMakeLlmCallId:
    def test_deterministic(self) -> None:
        k1 = make_llm_call_id(
            execution_id="pe_abc", phase="planner", call_seq=0, model="mimo-v2.5-pro"
        )
        k2 = make_llm_call_id(
            execution_id="pe_abc", phase="planner", call_seq=0, model="mimo-v2.5-pro"
        )
        assert k1 == k2

    def test_format(self) -> None:
        key = make_llm_call_id(
            execution_id="pe_abc", phase="composer", call_seq=1, model="gpt-4o"
        )
        assert key == "pe_abc:composer:1:gpt-4o"

    def test_different_call_seq_gives_different_id(self) -> None:
        k0 = make_llm_call_id(
            execution_id="pe_x", phase="planner", call_seq=0, model="m"
        )
        k1 = make_llm_call_id(
            execution_id="pe_x", phase="planner", call_seq=1, model="m"
        )
        assert k0 != k1

    def test_different_phase_gives_different_id(self) -> None:
        k_plan = make_llm_call_id(
            execution_id="pe_x", phase="planner", call_seq=0, model="m"
        )
        k_comp = make_llm_call_id(
            execution_id="pe_x", phase="composer", call_seq=0, model="m"
        )
        assert k_plan != k_comp

    def test_different_model_gives_different_id(self) -> None:
        k1 = make_llm_call_id(
            execution_id="pe_x", phase="planner", call_seq=0, model="model-a"
        )
        k2 = make_llm_call_id(
            execution_id="pe_x", phase="planner", call_seq=0, model="model-b"
        )
        assert k1 != k2

    def test_empty_execution_id_raises(self) -> None:
        with pytest.raises(ValueError, match="execution_id"):
            make_llm_call_id(
                execution_id="", phase="planner", call_seq=0, model="m"
            )

    def test_empty_model_raises(self) -> None:
        with pytest.raises(ValueError, match="model"):
            make_llm_call_id(
                execution_id="pe_x", phase="planner", call_seq=0, model=""
            )

    def test_negative_call_seq_raises(self) -> None:
        with pytest.raises(ValueError, match="call_seq"):
            make_llm_call_id(
                execution_id="pe_x", phase="planner", call_seq=-1, model="m"
            )

    def test_over_128_char_model_raises(self) -> None:
        long_model = "m" * 130  # will produce key > 128 chars
        with pytest.raises(ValueError, match="128"):
            make_llm_call_id(
                execution_id="pe_x", phase="planner", call_seq=0, model=long_model
            )

    def test_zero_call_seq_allowed(self) -> None:
        key = make_llm_call_id(
            execution_id="pe_x", phase="planner", call_seq=0, model="m"
        )
        assert ":0:" in key

    def test_finalize_rescue_phase_valid(self) -> None:
        key = make_llm_call_id(
            execution_id="pe_x",
            phase="finalize_rescue",
            call_seq=0,
            model="m",
        )
        assert "finalize_rescue" in key

    def test_invalid_phase_raises(self) -> None:
        with pytest.raises(ValueError, match="phase"):
            make_llm_call_id(
                execution_id="pe_x",
                phase="unknown_phase",  # type: ignore[arg-type]
                call_seq=0,
                model="m",
            )


# ---------------------------------------------------------------------------
# reserve
# ---------------------------------------------------------------------------


class TestReserve:
    def test_inserts_reserved_row_with_correct_fields(
        self, session: Session
    ) -> None:
        call_id = "pe_abc:planner:0:mimo-v2.5-pro"
        row = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="tenant_1",
            feature_key="skill_a",
            model="mimo-v2.5-pro",
            reserved_tokens=1000,
            now=NOW,
        )
        assert row.state == "reserved"
        assert row.reserved_tokens == 1000
        assert row.actual_tokens is None
        assert row.tenant_id == "tenant_1"
        assert row.llm_call_id == call_id
        assert row.expires_at is None
        assert row.created_at == NOW
        assert row.updated_at == NOW
        assert len(row.id) > 0

    def test_expires_at_set_when_ttl_given(self, session: Session) -> None:
        call_id = f"pe_ttl:planner:0:m_{uuid4().hex[:6]}"
        row = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=NOW,
            ttl_seconds=3600,
        )
        assert row.expires_at == NOW + timedelta(seconds=3600)

    def test_reserved_tokens_zero_raises(self, session: Session) -> None:
        with pytest.raises(ValueError, match="reserved_tokens"):
            reserve(
                session,
                llm_call_id="pe_x:planner:0:m",
                execution_id=None,
                tenant_id="t",
                feature_key=None,
                model="m",
                reserved_tokens=0,
                now=NOW,
            )

    def test_reserved_tokens_negative_raises(self, session: Session) -> None:
        with pytest.raises(ValueError, match="reserved_tokens"):
            reserve(
                session,
                llm_call_id="pe_x:planner:0:m",
                execution_id=None,
                tenant_id="t",
                feature_key=None,
                model="m",
                reserved_tokens=-1,
                now=NOW,
            )

    def test_reserve_with_real_execution_id_fk(self, session: Session) -> None:
        exec_id = _seed_execution(session)
        call_id = f"{exec_id}:planner:0:mimo-v2.5-pro"
        row = reserve(
            session,
            llm_call_id=call_id,
            execution_id=exec_id,
            tenant_id="tenant_test",
            feature_key="skill_a",
            model="mimo-v2.5-pro",
            reserved_tokens=200,
            now=NOW,
        )
        assert row.execution_id == exec_id
        assert row.state == "reserved"

    def test_reserve_execution_id_none_allowed(self, session: Session) -> None:
        """execution_id is nullable — reserve without an FK parent."""
        call_id = f"turn_abc:planner:0:m_{uuid4().hex[:6]}"
        row = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=50,
            now=NOW,
        )
        assert row.execution_id is None
        assert row.state == "reserved"


# ---------------------------------------------------------------------------
# reserve — idempotency
# ---------------------------------------------------------------------------


class TestReserveIdempotent:
    def test_second_reserve_returns_same_row(self, session: Session) -> None:
        """Retry must NOT create a second row."""
        call_id = f"pe_idem:planner:0:m_{uuid4().hex[:6]}"
        r1 = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=NOW,
        )
        r2 = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=LATER,
        )
        assert r1.id == r2.id

    def test_no_duplicate_row_after_retry(self, session: Session) -> None:
        call_id = f"pe_dup:planner:0:m_{uuid4().hex[:6]}"
        reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=NOW,
        )
        reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=LATER,
        )
        count = session.execute(
            select(func.count()).where(
                PlannerLlmReservation.llm_call_id == call_id
            )
        ).scalar()
        assert count == 1

    def test_reserve_idempotent_does_not_reset_charged_state(
        self, session: Session
    ) -> None:
        """If the row is already charged, a subsequent reserve call must NOT
        revert it to 'reserved'."""
        call_id = f"pe_charged:planner:0:m_{uuid4().hex[:6]}"
        reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=NOW,
        )
        charge(session, llm_call_id=call_id, actual_tokens=80, now=LATER)
        # Now call reserve again (simulating a caller that lost track of state)
        row = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="t",
            feature_key=None,
            model="m",
            reserved_tokens=100,
            now=LATER,
        )
        # Must return the charged row unchanged — NOT revert to reserved
        assert row.state == "charged"


# ---------------------------------------------------------------------------
# charge
# ---------------------------------------------------------------------------


class TestCharge:
    def test_reserved_to_charged(self, session: Session) -> None:
        call_id = f"pe_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, reserved_tokens=500)
        row = charge(session, llm_call_id=call_id, actual_tokens=300, now=LATER)
        assert row.state == "charged"
        assert row.actual_tokens == 300
        assert row.updated_at == LATER

    def test_charge_same_amount_twice_is_idempotent(self, session: Session) -> None:
        """CRASH-POINT 'retry-after-lost-response': charge twice with the SAME
        actual_tokens must be idempotent (no error)."""
        call_id = f"pe_idem_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        charge(session, llm_call_id=call_id, actual_tokens=200, now=LATER)
        # Second charge with same amount — must NOT raise
        row = charge(session, llm_call_id=call_id, actual_tokens=200, now=LATER)
        assert row.state == "charged"
        assert row.actual_tokens == 200

    def test_charge_different_amount_twice_raises(self, session: Session) -> None:
        """Charging with a different amount is a split-brain bug — fail loud."""
        call_id = f"pe_diff_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        charge(session, llm_call_id=call_id, actual_tokens=100, now=LATER)
        with pytest.raises(ValueError, match="Double-charge"):
            charge(session, llm_call_id=call_id, actual_tokens=999, now=LATER)

    def test_charge_from_released_raises(self, session: Session) -> None:
        call_id = f"pe_rel_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        release(session, llm_call_id=call_id, now=LATER)
        with pytest.raises(ValueError, match="released"):
            charge(session, llm_call_id=call_id, actual_tokens=50, now=LATER)

    def test_charge_from_expired_raises(self, session: Session) -> None:
        call_id = f"pe_exp_ch:planner:0:m_{uuid4().hex[:6]}"
        # TTL=3600 → expires_at=LATER; use EVEN_LATER so the TTL has lapsed.
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        expire(session, llm_call_id=call_id, now=EVEN_LATER)
        with pytest.raises(ValueError, match="expired"):
            charge(session, llm_call_id=call_id, actual_tokens=50, now=EVEN_LATER)

    def test_charge_negative_actual_tokens_raises(self, session: Session) -> None:
        call_id = f"pe_neg_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        with pytest.raises(ValueError, match="actual_tokens"):
            charge(session, llm_call_id=call_id, actual_tokens=-1, now=LATER)

    def test_charge_zero_actual_tokens_allowed(self, session: Session) -> None:
        """Zero tokens is a valid (if unusual) provider response."""
        call_id = f"pe_zero_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        row = charge(session, llm_call_id=call_id, actual_tokens=0, now=LATER)
        assert row.state == "charged"
        assert row.actual_tokens == 0

    def test_charge_missing_row_raises_lookup_error(self, session: Session) -> None:
        with pytest.raises(LookupError):
            charge(session, llm_call_id="nonexistent:planner:0:m", actual_tokens=1, now=NOW)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


class TestRelease:
    def test_reserved_to_released(self, session: Session) -> None:
        call_id = f"pe_rel:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        row = release(session, llm_call_id=call_id, now=LATER)
        assert row.state == "released"
        assert row.updated_at == LATER

    def test_release_idempotent(self, session: Session) -> None:
        call_id = f"pe_rel_idem:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        release(session, llm_call_id=call_id, now=LATER)
        # Second call must not raise
        row = release(session, llm_call_id=call_id, now=LATER)
        assert row.state == "released"

    def test_release_from_charged_raises(self, session: Session) -> None:
        call_id = f"pe_rel_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        charge(session, llm_call_id=call_id, actual_tokens=50, now=LATER)
        with pytest.raises(ValueError, match="charged"):
            release(session, llm_call_id=call_id, now=LATER)

    def test_release_missing_row_raises_lookup_error(self, session: Session) -> None:
        with pytest.raises(LookupError):
            release(session, llm_call_id="nonexistent:planner:0:m", now=NOW)


# ---------------------------------------------------------------------------
# mark_unknown_provider_outcome
# ---------------------------------------------------------------------------


class TestMarkUnknownProviderOutcome:
    def test_reserved_to_unknown_charges_max(self, session: Session) -> None:
        call_id = f"pe_unk:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, reserved_tokens=800)
        row = mark_unknown_provider_outcome(
            session, llm_call_id=call_id, now=LATER
        )
        assert row.state == "unknown_provider_outcome"
        # Billing policy: charge the reserved max, never under-charge
        assert row.actual_tokens == 800
        assert row.updated_at == LATER

    def test_mark_unknown_idempotent(self, session: Session) -> None:
        call_id = f"pe_unk_idem:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, reserved_tokens=600)
        mark_unknown_provider_outcome(session, llm_call_id=call_id, now=LATER)
        # Second call must not raise
        row = mark_unknown_provider_outcome(
            session, llm_call_id=call_id, now=LATER
        )
        assert row.state == "unknown_provider_outcome"

    def test_mark_unknown_from_charged_raises(self, session: Session) -> None:
        call_id = f"pe_unk_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        charge(session, llm_call_id=call_id, actual_tokens=50, now=LATER)
        with pytest.raises(ValueError, match="charged"):
            mark_unknown_provider_outcome(session, llm_call_id=call_id, now=LATER)

    def test_mark_unknown_missing_row_raises_lookup_error(
        self, session: Session
    ) -> None:
        with pytest.raises(LookupError):
            mark_unknown_provider_outcome(
                session, llm_call_id="nonexistent:planner:0:m", now=NOW
            )


# ---------------------------------------------------------------------------
# expire
# ---------------------------------------------------------------------------


class TestExpire:
    def test_reserved_to_expired(self, session: Session) -> None:
        call_id = f"pe_exp:planner:0:m_{uuid4().hex[:6]}"
        # TTL=3600 → expires_at=LATER; use EVEN_LATER so TTL has lapsed.
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        row = expire(session, llm_call_id=call_id, now=EVEN_LATER)
        assert row.state == "expired"
        assert row.updated_at == EVEN_LATER

    def test_expire_idempotent(self, session: Session) -> None:
        call_id = f"pe_exp_idem:planner:0:m_{uuid4().hex[:6]}"
        # TTL=3600 → expires_at=LATER; use EVEN_LATER so TTL has lapsed.
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        expire(session, llm_call_id=call_id, now=EVEN_LATER)
        # Second call must not raise (idempotent — already expired).
        row = expire(session, llm_call_id=call_id, now=EVEN_LATER)
        assert row.state == "expired"

    def test_expire_from_charged_raises(self, session: Session) -> None:
        call_id = f"pe_exp_ch:planner:0:m_{uuid4().hex[:6]}"
        # TTL present so expire() can reach the state check before the TTL guard.
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        charge(session, llm_call_id=call_id, actual_tokens=10, now=LATER)
        with pytest.raises(ValueError, match="charged"):
            expire(session, llm_call_id=call_id, now=EVEN_LATER)

    def test_expire_missing_row_raises_lookup_error(self, session: Session) -> None:
        with pytest.raises(LookupError):
            expire(session, llm_call_id="nonexistent:planner:0:m", now=NOW)


# ---------------------------------------------------------------------------
# CRASH-POINT: "after-reserve" — worker died after reserve, before provider call
# ---------------------------------------------------------------------------


class TestCrashPointAfterReserve:
    def test_reserved_row_can_transition_to_expired(self, session: Session) -> None:
        """Simulate: worker crashed after reserve, before provider call.
        The row stays 'reserved'.  The recovery scanner should be able to
        expire it via expire() once expires_at < now.
        """
        call_id = f"pe_crash:planner:0:m_{uuid4().hex[:6]}"
        row = _make_reservation(
            session, llm_call_id=call_id, reserved_tokens=300, ttl_seconds=3600
        )
        # Verify the row is still 'reserved' (no charge/release happened)
        assert row.state == "reserved"
        assert row.expires_at is not None  # expires_at = NOW + 1h = LATER

        # Recovery scanner calls expire() when expires_at < now.
        # expires_at == LATER, so now must be strictly > LATER → use EVEN_LATER.
        expired_row = expire(session, llm_call_id=call_id, now=EVEN_LATER)
        assert expired_row.state == "expired"

    def test_reserved_row_can_transition_to_released(
        self, session: Session
    ) -> None:
        """Reserved row can also be released by the scanner (if TTL has no
        usage-data risk — e.g. the call was never started)."""
        call_id = f"pe_crash_rel:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        row = release(session, llm_call_id=call_id, now=LATER)
        assert row.state == "released"


# ---------------------------------------------------------------------------
# Finding 1: FOR UPDATE lock — behavioral proxy tests on SQLite
# ---------------------------------------------------------------------------


class TestForUpdateLock:
    """Verify that state-transition guards hold under sequential contention.

    True concurrent locking cannot be tested on SQLite (single-writer), but
    the state-machine invariants it protects CAN be tested sequentially:
    charge→charge(different) raises, charge→release raises, etc.

    Additionally we verify that each mutator issues a SELECT ... FOR UPDATE
    by inspecting that ``with_for_update()`` is present in the query — we
    do this by monkeypatching ``Session.execute`` and capturing the compiled
    SQL text.
    """

    def test_charge_then_charge_different_amount_raises(
        self, session: Session
    ) -> None:
        """Split-brain guard: two concurrent charge(A) vs charge(B) — the
        second must fail even after the first commits."""
        call_id = f"pe_lock_ch:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        charge(session, llm_call_id=call_id, actual_tokens=100, now=LATER)
        with pytest.raises(ValueError, match="Double-charge"):
            charge(session, llm_call_id=call_id, actual_tokens=999, now=LATER)

    def test_charge_then_release_raises(self, session: Session) -> None:
        """release() after charge() must raise — not silently overwrite."""
        call_id = f"pe_lock_rel:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id)
        charge(session, llm_call_id=call_id, actual_tokens=50, now=LATER)
        with pytest.raises(ValueError, match="charged"):
            release(session, llm_call_id=call_id, now=LATER)

    def test_reserve_then_charge_then_expire_raises(
        self, session: Session
    ) -> None:
        """expire() after charge() must raise — not silently overwrite a
        committed charge with 'expired' (the classic split-brain scenario)."""
        call_id = f"pe_lock_exp:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        charge(session, llm_call_id=call_id, actual_tokens=42, now=LATER)
        with pytest.raises(ValueError, match="charged"):
            expire(session, llm_call_id=call_id, now=EVEN_LATER)

    def test_mutators_use_for_update_clause(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert each mutator issues a SELECT ... FOR UPDATE via SQLAlchemy.

        We intercept ``session.execute`` and record SQL strings; then verify
        that at least one captured statement contains 'FOR UPDATE' (Postgres)
        or that the query object carries ``with_for_update()`` (SQLite emits
        no FOR UPDATE keyword but the clause object is present).

        Because SQLite does not emit 'FOR UPDATE' in SQL text, we monkeypatch
        ``_get_reservation_for_update`` to count invocations — confirming the
        mutators call the locking helper, not the plain ``_get_reservation``.
        """
        import sreda.runtime.planner.billing as billing_module

        call_count: list[int] = [0]
        original_fn = billing_module._get_reservation_for_update

        def counting_wrapper(s: Session, lid: str) -> PlannerLlmReservation:
            call_count[0] += 1
            return original_fn(s, lid)

        monkeypatch.setattr(
            billing_module, "_get_reservation_for_update", counting_wrapper
        )

        call_id = f"pe_fu_spy:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)

        # charge, release, expire, mark_unknown_provider_outcome — 4 mutators.
        # We only exercise the ones that reach the lock (charge succeeds first;
        # then we test the others on fresh rows).
        charge(session, llm_call_id=call_id, actual_tokens=10, now=LATER)
        assert call_count[0] == 1, "charge() must call _get_reservation_for_update"

        call_id2 = f"pe_fu_spy2:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id2)
        release(session, llm_call_id=call_id2, now=LATER)
        assert call_count[0] == 2, "release() must call _get_reservation_for_update"

        call_id3 = f"pe_fu_spy3:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id3, ttl_seconds=3600)
        expire(session, llm_call_id=call_id3, now=EVEN_LATER)
        assert call_count[0] == 3, "expire() must call _get_reservation_for_update"

        call_id4 = f"pe_fu_spy4:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id4)
        mark_unknown_provider_outcome(session, llm_call_id=call_id4, now=LATER)
        assert call_count[0] == 4, (
            "mark_unknown_provider_outcome() must call _get_reservation_for_update"
        )


# ---------------------------------------------------------------------------
# Finding 2: expire() must verify the TTL actually lapsed
# ---------------------------------------------------------------------------


class TestExpireTtlLapse:
    def test_expire_no_ttl_raises(self, session: Session) -> None:
        """expires_at is None → expire() must raise ValueError."""
        call_id = f"pe_no_ttl:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=None)
        with pytest.raises(ValueError, match="TTL has not lapsed"):
            expire(session, llm_call_id=call_id, now=LATER)

    def test_expire_ttl_not_lapsed_raises(self, session: Session) -> None:
        """expires_at > now (TTL in the future) → expire() must raise."""
        call_id = f"pe_future_ttl:planner:0:m_{uuid4().hex[:6]}"
        # ttl_seconds=7200 → expires_at = NOW + 2h; LATER = NOW + 1h < expires_at
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=7200)
        with pytest.raises(ValueError, match="TTL has not lapsed"):
            expire(session, llm_call_id=call_id, now=LATER)

    def test_expire_ttl_equal_to_now_raises(self, session: Session) -> None:
        """expires_at == now → predicate is strict < → must raise."""
        call_id = f"pe_eq_ttl:planner:0:m_{uuid4().hex[:6]}"
        # ttl_seconds=3600 → expires_at = NOW + 1h = LATER; call with now=LATER
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        with pytest.raises(ValueError, match="TTL has not lapsed"):
            expire(session, llm_call_id=call_id, now=LATER)

    def test_expire_ttl_lapsed_succeeds(self, session: Session) -> None:
        """expires_at < now (TTL in the past) → expire() must succeed."""
        call_id = f"pe_lapsed_ttl:planner:0:m_{uuid4().hex[:6]}"
        # ttl_seconds=3600 → expires_at = NOW + 1h = LATER; now=EVEN_LATER > LATER
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        row = expire(session, llm_call_id=call_id, now=EVEN_LATER)
        assert row.state == "expired"

    def test_expire_idempotent_already_expired_skips_ttl_check(
        self, session: Session
    ) -> None:
        """If the row is already 'expired', expire() returns early before
        the TTL check — idempotency takes precedence."""
        call_id = f"pe_idem_ttl:planner:0:m_{uuid4().hex[:6]}"
        _make_reservation(session, llm_call_id=call_id, ttl_seconds=3600)
        expire(session, llm_call_id=call_id, now=EVEN_LATER)
        # Second call — even with now=LATER (would normally fail TTL check
        # if the row were still reserved) must NOT raise because the row
        # is already in the terminal 'expired' state.
        row = expire(session, llm_call_id=call_id, now=EVEN_LATER)
        assert row.state == "expired"


# ---------------------------------------------------------------------------
# Finding 3: make_llm_call_id call_seq semantics — docstring only (no sig change)
# ---------------------------------------------------------------------------


class TestMakeLlmCallIdCallSeqSemantics:
    def test_same_call_seq_same_key(self) -> None:
        """Provider retry of the same logical call must reuse the same key."""
        k1 = make_llm_call_id(
            execution_id="pe_retry", phase="planner", call_seq=0, model="m"
        )
        k2 = make_llm_call_id(
            execution_id="pe_retry", phase="planner", call_seq=0, model="m"
        )
        assert k1 == k2, (
            "Retries of the same logical call must produce the same llm_call_id"
        )

    def test_different_call_seq_different_key(self) -> None:
        """A second DISTINCT logical call (different call_seq) gets a different key."""
        k0 = make_llm_call_id(
            execution_id="pe_seq", phase="planner", call_seq=0, model="m"
        )
        k1 = make_llm_call_id(
            execution_id="pe_seq", phase="planner", call_seq=1, model="m"
        )
        assert k0 != k1


# ---------------------------------------------------------------------------
# Finding 4: make_llm_call_id ':' separator ambiguity
# ---------------------------------------------------------------------------


class TestMakeLlmCallIdColonValidation:
    def test_execution_id_with_colon_raises(self) -> None:
        """execution_id containing ':' is ambiguous — must raise ValueError."""
        with pytest.raises(ValueError, match="':'"):
            make_llm_call_id(
                execution_id="pe:bad", phase="planner", call_seq=0, model="m"
            )

    def test_execution_id_with_multiple_colons_raises(self) -> None:
        with pytest.raises(ValueError, match="':'"):
            make_llm_call_id(
                execution_id="a:b:c", phase="planner", call_seq=0, model="m"
            )

    def test_model_with_colon_allowed(self) -> None:
        """model is the final tail — a colon in model is unambiguous."""
        key = make_llm_call_id(
            execution_id="pe_ok", phase="planner", call_seq=0, model="provider:model-v1"
        )
        assert key == "pe_ok:planner:0:provider:model-v1"

    def test_clean_execution_id_still_works(self) -> None:
        """Regression: a normal execution_id (no colon) must still work."""
        key = make_llm_call_id(
            execution_id="pe_abc123", phase="composer", call_seq=2, model="gpt-4o"
        )
        assert key == "pe_abc123:composer:2:gpt-4o"


# ---------------------------------------------------------------------------
# Finding 5: reserve IntegrityError branch re-SELECT
# ---------------------------------------------------------------------------


class TestReserveIntegrityErrorBranch:
    def test_integrity_error_branch_re_selects_winner(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate concurrent INSERT race: first optimistic SELECT returns None
        (row not yet visible), INSERT raises IntegrityError (concurrent winner
        already committed), re-SELECT returns the winner row.

        We monkeypatch ``session.scalars`` so the FIRST call returns None
        (triggering the INSERT path) and the SECOND call (inside the except
        block) returns the pre-inserted winner row.
        """
        call_id = f"pe_integrity:planner:0:m_{uuid4().hex[:6]}"

        # Pre-insert the "winner" row directly (simulates the concurrent winner).
        winner_row = PlannerLlmReservation(
            id=uuid4().hex,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="tenant_test",
            feature_key=None,
            model="m",
            state="reserved",
            reserved_tokens=42,
            actual_tokens=None,
            created_at=NOW,
            updated_at=NOW,
            expires_at=None,
        )
        session.add(winner_row)
        session.flush()

        scalars_call_count: list[int] = [0]
        original_scalars = session.scalars

        def patched_scalars(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
            scalars_call_count[0] += 1
            if scalars_call_count[0] == 1:
                # First call: return empty result (pretend row not yet visible)
                from unittest.mock import MagicMock

                mock_result = MagicMock()
                mock_result.one_or_none.return_value = None
                return mock_result
            # Subsequent calls: delegate to real session (finds the winner)
            return original_scalars(stmt, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", patched_scalars)

        # reserve() should hit IntegrityError on INSERT (winner already there),
        # then re-SELECT and return the winner.
        result = reserve(
            session,
            llm_call_id=call_id,
            execution_id=None,
            tenant_id="tenant_test",
            feature_key=None,
            model="m",
            reserved_tokens=99,
            now=LATER,
        )
        assert result.id == winner_row.id, (
            "IntegrityError branch must return the winning row, not raise"
        )
        assert result.reserved_tokens == 42, (
            "Winner row values must be preserved, not overwritten"
        )


class TestForUpdateRefreshesStaleIdentityMap:
    """Codex A/B #10c R2 CRITICAL: a mutator must observe the CURRENT DB state
    even when this Session's identity map holds a STALE copy of the row.

    Without populate_existing on the FOR UPDATE select, SELECT ... FOR UPDATE
    acquires the lock but returns the already-loaded stale object, so a session
    that preloaded 'reserved' could overwrite a committed terminal state. These
    tests would FAIL if populate_existing=True were removed.
    """

    def test_expire_observes_committed_charge_via_populate_existing(
        self, session: Session
    ) -> None:
        exec_id = _seed_execution(session)
        cid = make_llm_call_id(
            execution_id=exec_id, phase="planner", call_seq=0, model="m"
        )
        # ttl=3600 → expires_at = NOW+1h = LATER; the expire() below uses
        # now=EVEN_LATER (>LATER) so the TTL guard PASSES. That makes this test
        # LOAD-BEARING for populate_existing (Codex A/B #10c R3 MINOR): without
        # the refresh, expire would see stale 'reserved' + lapsed TTL and
        # overwrite the committed 'charged' → 'expired'. With it, expire sees
        # 'charged' and raises. (Otherwise the TTL guard alone would mask the bug.)
        res = reserve(
            session, llm_call_id=cid, execution_id=exec_id, tenant_id="t",
            feature_key=None, model="m", reserved_tokens=100, now=NOW,
            ttl_seconds=3600,
        )
        session.flush()
        assert res.state == "reserved"

        # Simulate a competing transaction committing 'charged' WITHOUT syncing
        # this session's identity-map object (synchronize_session=False), so the
        # in-memory `res` stays stale at 'reserved'.
        session.execute(
            update(PlannerLlmReservation)
            .where(PlannerLlmReservation.llm_call_id == cid)
            .values(state="charged", actual_tokens=42),
            execution_options={"synchronize_session": False},
        )
        assert res.state == "reserved"  # in-memory copy is now STALE

        # expire() must re-read 'charged' under the lock (populate_existing) and
        # refuse — NOT overwrite the committed charge with 'expired'.
        with pytest.raises(ValueError, match="Cannot expire"):
            expire(session, llm_call_id=cid, now=EVEN_LATER)

        # DB row is still 'charged' (the charge was not lost).
        session.expire_all()
        row = session.scalars(
            select(PlannerLlmReservation).where(
                PlannerLlmReservation.llm_call_id == cid
            )
        ).one()
        assert row.state == "charged"
        assert row.actual_tokens == 42

    def test_charge_observes_committed_charge_via_populate_existing(
        self, session: Session
    ) -> None:
        """Same stale-identity race, but the second mutator is a DIFFERENT-amount
        charge — must hit the split-brain guard (see the committed amount, raise)
        rather than last-write-win over it."""
        exec_id = _seed_execution(session)
        cid = make_llm_call_id(
            execution_id=exec_id, phase="composer", call_seq=0, model="m"
        )
        res = reserve(
            session, llm_call_id=cid, execution_id=exec_id, tenant_id="t",
            feature_key=None, model="m", reserved_tokens=100, now=NOW,
        )
        session.flush()
        session.execute(
            update(PlannerLlmReservation)
            .where(PlannerLlmReservation.llm_call_id == cid)
            .values(state="charged", actual_tokens=42),
            execution_options={"synchronize_session": False},
        )
        assert res.state == "reserved"  # stale
        with pytest.raises(ValueError, match="[Dd]ouble-charge"):
            charge(session, llm_call_id=cid, actual_tokens=999, now=LATER)
        session.expire_all()
        row = session.scalars(
            select(PlannerLlmReservation).where(
                PlannerLlmReservation.llm_call_id == cid
            )
        ).one()
        assert row.actual_tokens == 42  # original charge preserved
