"""Tests for sreda.runtime.planner.recovery (Sub-A12 Phase E, task #9a).

Two test classes:
  - TestDecideRecovery  — pure-function parametrised table; no DB needed.
  - TestProbeOperation  — in-memory SQLite; inserts rows and checks probe.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sreda.db.models  # noqa: F401 — registers ALL ORM mappers with Base
from sreda.db.base import Base
from sreda.db.models.audit_feed import AuditOutboxEvent, UserDataChangeFeedEvent
from sreda.runtime.planner.recovery import (
    TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL,
    TERMINAL_FAILED_NEEDS_MANUAL,
    decide_recovery,
    probe_operation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def session():
    """In-memory SQLite session with all ORM tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _make_outbox_event(operation_id: str) -> AuditOutboxEvent:
    """Minimal valid AuditOutboxEvent row (SQLite doesn't enforce CHECKs)."""
    return AuditOutboxEvent(
        operation_id=operation_id,
        tenant_id="t1",
        user_id=None,
        source="system",
        entity_type="task",
        entity_id=None,
        action="created",
        payload=None,
        payload_hash=None,
        caused_by=None,
        occurred_at=_utcnow(),
        enqueued_at=_utcnow(),
        attempts=0,
        last_attempt_at=None,
        last_error=None,
    )


def _make_feed_event(operation_id: str) -> UserDataChangeFeedEvent:
    """Minimal valid UserDataChangeFeedEvent row."""
    return UserDataChangeFeedEvent(
        operation_id=operation_id,
        tenant_id="t1",
        user_id=None,
        source="system",
        entity_type="task",
        entity_id=None,
        action="created",
        payload=None,
        payload_hash=None,
        caused_by=None,
        occurred_at=_utcnow(),
    )


# ---------------------------------------------------------------------------
# TestDecideRecovery — pure function, no DB
# ---------------------------------------------------------------------------

class TestDecideRecovery:
    """Parametrised table covering every branch in decide_recovery()."""

    @pytest.mark.parametrize(
        "ledger_status, is_durable_write, probed_committed, settle_window_elapsed, expected",
        [
            # --- committed: always skip regardless of other flags -----------
            ("committed", False, False, False, "skip"),
            ("committed", True,  True,  True,  "skip"),
            ("committed", True,  False, True,  "skip"),

            # --- started, non-durable: skip (no side effect to recover) ----
            ("started", False, False, False, "skip"),
            ("started", False, True,  False, "skip"),

            # --- started, durable, probe confirmed: record the commit ------
            ("started", True, True,  False, "mark_committed"),

            # --- started, durable, probe not confirmed ---------------------
            # audit 2026-07-18 (planner-exec MAJOR): settle-window symmetry —
            # window NOT elapsed → re_probe (a stuck worker's to_thread may
            # still commit); only after the window → escalate.
            ("started", True, False, False, "re_probe"),
            ("started", True, False, True,  "mark_failed_needs_manual"),

            # --- unknown, non-durable: defensive skip ----------------------
            ("unknown", False, False, False, "skip"),
            ("unknown", False, True,  False, "skip"),

            # --- unknown, durable, probe confirmed -------------------------
            ("unknown", True, True,  False, "mark_committed"),

            # --- unknown, durable, probe not confirmed ---------------------
            ("unknown", True, False, False, "mark_failed_needs_manual"),
            ("unknown", True, False, True,  "mark_failed_needs_manual"),

            # --- unknown_pending, probe confirmed (window irrelevant) ------
            ("unknown_pending", True, True,  False, "mark_committed"),
            ("unknown_pending", True, True,  True,  "mark_committed"),

            # --- unknown_pending, not committed, window elapsed → failed ---
            ("unknown_pending", True, False, True,  "mark_failed_needs_manual"),

            # --- unknown_pending, not committed, window NOT elapsed → wait -
            ("unknown_pending", True, False, False, "re_probe"),

            # --- unknown_pending, non-durable: defensive skip (anomalous; the
            #     executor never marks a non-durable step unknown_pending) ----
            ("unknown_pending", False, False, False, "skip"),
            ("unknown_pending", False, True,  True,  "skip"),
        ],
    )
    def test_decide_recovery_matrix(
        self,
        ledger_status: str,
        is_durable_write: bool,
        probed_committed: bool,
        settle_window_elapsed: bool,
        expected: str,
    ) -> None:
        result = decide_recovery(
            ledger_status=ledger_status,
            is_durable_write=is_durable_write,
            probed_committed=probed_committed,
            settle_window_elapsed=settle_window_elapsed,
        )
        assert result == expected

    def test_decide_recovery_unknown_status_raises(self) -> None:
        """Any unrecognised ledger_status must raise ValueError (fail-closed)."""
        with pytest.raises(ValueError, match="unrecognised ledger_status"):
            decide_recovery(
                ledger_status="bogus_status",
                is_durable_write=True,
                probed_committed=False,
                settle_window_elapsed=False,
            )

    def test_decide_recovery_is_pure(self) -> None:
        """Same inputs always produce the same output — no hidden state."""
        kwargs = dict(
            ledger_status="unknown_pending",
            is_durable_write=True,
            probed_committed=False,
            settle_window_elapsed=False,
        )
        assert decide_recovery(**kwargs) == decide_recovery(**kwargs) == "re_probe"

    def test_constants_have_expected_values(self) -> None:
        """Constant strings must match the CHECK constraint literals in planner.py."""
        assert TERMINAL_FAILED_NEEDS_MANUAL == "failed_needs_manual"
        assert TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL == "committed_unserved_needs_manual"


# ---------------------------------------------------------------------------
# TestProbeOperation — requires DB
# ---------------------------------------------------------------------------

class TestProbeOperation:
    """probe_operation checks audit_outbox and user_data_change_feed."""

    def test_probe_true_when_row_in_outbox(self, session) -> None:
        """A row in audit_outbox → probe returns True."""
        op_id = "op-outbox-001"
        session.add(_make_outbox_event(op_id))
        session.flush()

        assert probe_operation(session, op_id) is True

    def test_probe_false_when_no_row_anywhere(self, session) -> None:
        """No row in either table → probe returns False."""
        assert probe_operation(session, "op-missing-999") is False

    def test_probe_true_when_row_in_feed_only(self, session) -> None:
        """Simulate post-relay state: row moved to user_data_change_feed only."""
        op_id = "op-feed-002"
        session.add(_make_feed_event(op_id))
        session.flush()

        assert probe_operation(session, op_id) is True

    def test_probe_does_not_cross_operation_ids(self, session) -> None:
        """Inserting op_id A does not make op_id B probe True."""
        session.add(_make_outbox_event("op-a"))
        session.flush()

        assert probe_operation(session, "op-b") is False

    def test_probe_true_when_row_in_both_tables(self, session) -> None:
        """Row present in both tables (relay bug / test state) → still True."""
        op_id = "op-both-003"
        session.add(_make_outbox_event(op_id))
        session.add(_make_feed_event(op_id))
        session.flush()

        assert probe_operation(session, op_id) is True

    def test_probe_does_not_autoflush_pending_matching_row(self, session) -> None:
        """LINCHPIN regression (Codex A/B #9a R1): a PENDING (unflushed) audit
        row for the same operation_id must NOT be flushed by the probe and must
        NOT make probe return True. probe must see only previously-committed
        state — `with session.no_autoflush` guards this."""
        op_id = "op-pending-004"
        pending = _make_outbox_event(op_id)
        session.add(pending)  # pending, NOT flushed

        result = probe_operation(session, op_id)

        assert result is False, "probe must not observe an unflushed pending row"
        # The pending row must still be pending (probe did not autoflush it).
        assert pending in session.new

    def test_probe_short_circuits_on_outbox_hit(self, session, monkeypatch) -> None:
        """When the outbox hits, probe must NOT query the feed (short-circuit).
        We count session.execute calls: exactly ONE for an outbox hit."""
        op_id = "op-shortcircuit-005"
        session.add(_make_outbox_event(op_id))
        session.flush()

        calls = {"n": 0}
        real_execute = session.execute

        def _counting_execute(*args, **kwargs):
            calls["n"] += 1
            return real_execute(*args, **kwargs)

        monkeypatch.setattr(session, "execute", _counting_execute)
        assert probe_operation(session, op_id) is True
        assert calls["n"] == 1, (
            f"expected a single SELECT (outbox hit short-circuits the feed query), "
            f"got {calls['n']}"
        )
