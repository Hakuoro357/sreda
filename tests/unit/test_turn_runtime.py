"""Unit tests for ``sreda.runtime.planner.turn_runtime`` (Sub-A12 Phase E, PR-2a).

Coverage
--------
* remaining_ms  — positive before deadline; exactly 0 at/after (never negative)
* is_exhausted  — False before deadline; True at/after
* has_budget    — boundary cases; need_ms < 0 → ValueError
* from_turn_start — timeout-only; lease-earlier reconciliation; timeout-earlier;
                    timeout <= 0 → ValueError; naive datetime → ValueError
* WORST-CASE    — turn started long ago / near deadline; has_budget returns False
                  (the "fall back before exhaustion" guard from plan item #8)
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from sreda.runtime.planner.turn_runtime import (
    TurnRuntimeContext,
    _DEFAULT_TURN_TIMEOUT_SECONDS,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _utc(ts: str) -> datetime:
    """Parse an ISO-8601 string with Z suffix into an aware UTC datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# A fixed "now" used as a reference point in most tests.
_BASE = _utc("2026-06-01T12:00:00Z")


# ---------------------------------------------------------------------------
# remaining_ms
# ---------------------------------------------------------------------------


class TestRemainingMs:
    def test_positive_before_deadline(self) -> None:
        """Returns a positive number of ms when deadline has not passed."""
        deadline = _BASE + timedelta(seconds=10)
        ctx = TurnRuntimeContext(deadline_at=deadline)
        now = _BASE  # 10 seconds before deadline
        result = ctx.remaining_ms(now)
        assert result == 10_000

    def test_fractional_seconds_truncated(self) -> None:
        """remaining_ms uses int(), not round() — fractional ms are truncated."""
        deadline = _BASE + timedelta(milliseconds=1500)
        ctx = TurnRuntimeContext(deadline_at=deadline)
        now = _BASE
        assert ctx.remaining_ms(now) == 1500

    def test_zero_at_deadline(self) -> None:
        """Exactly at the deadline remaining_ms must return 0."""
        ctx = TurnRuntimeContext(deadline_at=_BASE)
        assert ctx.remaining_ms(_BASE) == 0

    def test_zero_after_deadline(self) -> None:
        """After the deadline remaining_ms must return 0 (clamped, never negative)."""
        ctx = TurnRuntimeContext(deadline_at=_BASE)
        now = _BASE + timedelta(seconds=60)  # one minute past
        assert ctx.remaining_ms(now) == 0

    def test_large_remaining(self) -> None:
        """Arbitrary large remaining budget is reported correctly (ms arithmetic)."""
        deadline = _BASE + timedelta(seconds=300)
        ctx = TurnRuntimeContext(deadline_at=deadline)
        assert ctx.remaining_ms(_BASE) == 300_000


# ---------------------------------------------------------------------------
# is_exhausted
# ---------------------------------------------------------------------------


class TestIsExhausted:
    def test_false_before_deadline(self) -> None:
        ctx = TurnRuntimeContext(deadline_at=_BASE + timedelta(seconds=1))
        assert ctx.is_exhausted(_BASE) is False

    def test_true_at_deadline(self) -> None:
        ctx = TurnRuntimeContext(deadline_at=_BASE)
        assert ctx.is_exhausted(_BASE) is True

    def test_true_after_deadline(self) -> None:
        ctx = TurnRuntimeContext(deadline_at=_BASE)
        assert ctx.is_exhausted(_BASE + timedelta(milliseconds=1)) is True


# ---------------------------------------------------------------------------
# has_budget
# ---------------------------------------------------------------------------


class TestHasBudget:
    def test_true_when_remaining_exceeds_need(self) -> None:
        deadline = _BASE + timedelta(seconds=60)
        ctx = TurnRuntimeContext(deadline_at=deadline)
        assert ctx.has_budget(_BASE, need_ms=30_000) is True

    def test_false_when_remaining_less_than_need(self) -> None:
        """This is the "forbid new attempt" case — insufficient budget."""
        deadline = _BASE + timedelta(seconds=10)  # 10 000 ms remaining
        ctx = TurnRuntimeContext(deadline_at=deadline)
        assert ctx.has_budget(_BASE, need_ms=30_000) is False

    def test_boundary_exactly_equal(self) -> None:
        """remaining_ms == need_ms exactly should return True (>= semantics)."""
        deadline = _BASE + timedelta(seconds=30)  # exactly 30 000 ms remaining
        ctx = TurnRuntimeContext(deadline_at=deadline)
        assert ctx.has_budget(_BASE, need_ms=30_000) is True

    def test_need_ms_zero_before_deadline(self) -> None:
        """need_ms=0 is always True when there is any budget."""
        deadline = _BASE + timedelta(seconds=1)
        ctx = TurnRuntimeContext(deadline_at=deadline)
        assert ctx.has_budget(_BASE, need_ms=0) is True

    def test_need_ms_zero_at_or_after_deadline_is_false(self) -> None:
        """HARD WALL (Codex A/B #10d R1 MAJOR): even need_ms=0 must NOT grant
        budget at/after the deadline — a zero-cost gate must not bypass the
        wall. is_exhausted=True ⇒ has_budget=False regardless of need_ms."""
        ctx = TurnRuntimeContext(deadline_at=_BASE)
        assert ctx.is_exhausted(_BASE) is True
        assert ctx.has_budget(_BASE, need_ms=0) is False
        assert ctx.has_budget(_BASE + timedelta(seconds=1), need_ms=0) is False

    def test_negative_need_ms_raises(self) -> None:
        """need_ms < 0 must raise ValueError — negative cost is a caller bug."""
        ctx = TurnRuntimeContext(deadline_at=_BASE + timedelta(seconds=30))
        with pytest.raises(ValueError, match="need_ms must be >= 0"):
            ctx.has_budget(_BASE, need_ms=-1)

    def test_false_after_deadline(self) -> None:
        """Any positive need_ms after deadline → False."""
        ctx = TurnRuntimeContext(deadline_at=_BASE)
        now = _BASE + timedelta(seconds=1)
        assert ctx.has_budget(now, need_ms=1) is False


# ---------------------------------------------------------------------------
# from_turn_start — construction / reconciliation
# ---------------------------------------------------------------------------


class TestFromTurnStart:
    def test_timeout_only_no_lease(self) -> None:
        """Without a lease, deadline = turn_start + turn_timeout_seconds."""
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=_BASE,
            turn_timeout_seconds=180.0,
        )
        expected = _BASE + timedelta(seconds=180)
        assert ctx.deadline_at == expected

    def test_default_timeout_mirrors_constant(self) -> None:
        """Default turn_timeout_seconds must equal _DEFAULT_TURN_TIMEOUT_SECONDS."""
        ctx = TurnRuntimeContext.from_turn_start(turn_started_at=_BASE)
        expected = _BASE + timedelta(seconds=_DEFAULT_TURN_TIMEOUT_SECONDS)
        assert ctx.deadline_at == expected

    def test_lease_earlier_than_timeout(self) -> None:
        """Reconciliation: when lease expires BEFORE the turn timeout, lease wins."""
        # Turn timeout = 180 s → timeout_deadline = _BASE + 180 s
        # Lease expires at _BASE + 60 s → earlier → should be the deadline.
        lease_expires_at = _BASE + timedelta(seconds=60)
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=_BASE,
            turn_timeout_seconds=180.0,
            lease_expires_at=lease_expires_at,
        )
        assert ctx.deadline_at == lease_expires_at

    def test_timeout_earlier_than_lease(self) -> None:
        """Reconciliation: when the turn timeout expires BEFORE the lease, timeout wins."""
        # Turn timeout = 60 s → timeout_deadline = _BASE + 60 s
        # Lease expires at _BASE + 300 s → later → timeout should be the deadline.
        lease_expires_at = _BASE + timedelta(seconds=300)
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=_BASE,
            turn_timeout_seconds=60.0,
            lease_expires_at=lease_expires_at,
        )
        expected = _BASE + timedelta(seconds=60)
        assert ctx.deadline_at == expected

    def test_lease_equals_timeout(self) -> None:
        """When lease and timeout coincide, either value works (same result)."""
        deadline_common = _BASE + timedelta(seconds=180)
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=_BASE,
            turn_timeout_seconds=180.0,
            lease_expires_at=deadline_common,
        )
        assert ctx.deadline_at == deadline_common

    def test_zero_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="turn_timeout_seconds must be > 0"):
            TurnRuntimeContext.from_turn_start(
                turn_started_at=_BASE,
                turn_timeout_seconds=0,
            )

    def test_negative_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="turn_timeout_seconds must be > 0"):
            TurnRuntimeContext.from_turn_start(
                turn_started_at=_BASE,
                turn_timeout_seconds=-10,
            )

    def test_naive_datetime_raises(self) -> None:
        """A timezone-naive turn_started_at must be rejected to prevent UTC-vs-local bugs."""
        naive_now = datetime(2026, 6, 1, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            TurnRuntimeContext.from_turn_start(turn_started_at=naive_now)


# ---------------------------------------------------------------------------
# WORST-CASE test (plan item #8)
# ---------------------------------------------------------------------------


class TestWorstCase:
    """Demonstrate the "fall back before exhaustion" guard.

    Scenario: the worker received the job, but the turn has been sitting in
    the queue for a long time (or prior retries ate most of the budget) and
    now only a few seconds remain.  The planner driver checks has_budget()
    with a typical per-attempt LLM budget (30 000 ms = 30 s) and MUST get
    False, causing it to fall back instead of starting another doomed attempt.
    """

    # A representative LLM-attempt budget in milliseconds.
    # 30 s is a conservative lower bound for a single planner LLM call;
    # real calls average 15–45 s depending on plan complexity.
    TYPICAL_PLANNER_ATTEMPT_BUDGET_MS = 30_000

    def test_near_deadline_forbids_new_attempt(self) -> None:
        """Turn started 175 s ago with 180 s timeout → 5 s left → no new attempt."""
        turn_start = _BASE - timedelta(seconds=175)  # started 175 s ago
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=turn_start,
            turn_timeout_seconds=180.0,
        )
        # "now" is _BASE, so 5 000 ms remain before the hard deadline.
        remaining = ctx.remaining_ms(_BASE)
        assert remaining == 5_000, f"Expected 5 000 ms remaining, got {remaining}"

        # The driver MUST NOT start a new planner attempt.
        can_start = ctx.has_budget(_BASE, need_ms=self.TYPICAL_PLANNER_ATTEMPT_BUDGET_MS)
        assert can_start is False, (
            "has_budget() returned True with only 5 s remaining and a 30 s "
            "planner-attempt budget — the driver would start an attempt that "
            "is guaranteed to be killed by the deadline.  This is the "
            '"fall back before exhaustion" guard.'
        )

    def test_past_deadline_forbids_new_attempt(self) -> None:
        """Turn that has already exceeded its timeout → 0 ms left → no new attempt."""
        turn_start = _BASE - timedelta(seconds=200)  # 200 s > 180 s timeout
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=turn_start,
            turn_timeout_seconds=180.0,
        )
        assert ctx.is_exhausted(_BASE) is True
        assert ctx.remaining_ms(_BASE) == 0
        assert (
            ctx.has_budget(_BASE, need_ms=self.TYPICAL_PLANNER_ATTEMPT_BUDGET_MS)
            is False
        )

    def test_lease_expiry_near_forbids_new_attempt(self) -> None:
        """Job lease expires in 3 s even though turn timeout is still 2 min away.

        Reconciliation: the lease bound dominates → 3 000 ms remaining →
        has_budget(30 000) returns False.
        """
        turn_start = _BASE - timedelta(seconds=10)  # fresh turn
        lease_expires_at = _BASE + timedelta(seconds=3)  # lease nearly gone
        ctx = TurnRuntimeContext.from_turn_start(
            turn_started_at=turn_start,
            turn_timeout_seconds=180.0,
            lease_expires_at=lease_expires_at,
        )
        assert ctx.deadline_at == lease_expires_at, (
            "Lease (earlier) should dominate the turn-timeout (later) in reconciliation."
        )
        assert ctx.remaining_ms(_BASE) == 3_000
        assert (
            ctx.has_budget(_BASE, need_ms=self.TYPICAL_PLANNER_ATTEMPT_BUDGET_MS)
            is False
        )


# ---------------------------------------------------------------------------
# Timezone-awareness validation (Codex A/B #10d R1 MAJOR) — every datetime
# entry point must reject naive values with a clean ValueError (not a raw
# TypeError from min/subtraction/comparison on a fallback path).
# ---------------------------------------------------------------------------

_NAIVE = datetime(2026, 6, 1, 12, 0, 0)  # no tzinfo


class TestTimezoneValidation:
    def test_direct_construction_naive_deadline_raises(self) -> None:
        with pytest.raises(ValueError, match="deadline_at must be timezone-aware"):
            TurnRuntimeContext(deadline_at=_NAIVE)

    def test_from_turn_start_naive_turn_started_raises(self) -> None:
        with pytest.raises(ValueError, match="turn_started_at must be timezone-aware"):
            TurnRuntimeContext.from_turn_start(turn_started_at=_NAIVE)

    def test_from_turn_start_naive_lease_raises(self) -> None:
        with pytest.raises(ValueError, match="lease_expires_at must be timezone-aware"):
            TurnRuntimeContext.from_turn_start(
                turn_started_at=_BASE,
                turn_timeout_seconds=180.0,
                lease_expires_at=_NAIVE,
            )

    def test_remaining_ms_naive_now_raises(self) -> None:
        ctx = TurnRuntimeContext(deadline_at=_BASE + timedelta(seconds=10))
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            ctx.remaining_ms(_NAIVE)

    def test_is_exhausted_naive_now_raises(self) -> None:
        ctx = TurnRuntimeContext(deadline_at=_BASE + timedelta(seconds=10))
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            ctx.is_exhausted(_NAIVE)

    def test_has_budget_naive_now_raises(self) -> None:
        ctx = TurnRuntimeContext(deadline_at=_BASE + timedelta(seconds=10))
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            ctx.has_budget(_NAIVE, need_ms=1)


# ---------------------------------------------------------------------------
# Drift guard (Codex A/B #10d R1 MINOR): the duplicated default must stay in
# sync with handlers.CHAT_TURN_TIMEOUT_SECONDS. Importing handlers is heavy but
# acceptable in the test suite; this fails loud if the two values diverge.
# ---------------------------------------------------------------------------


def test_default_timeout_matches_handlers_constant() -> None:
    from sreda.runtime.handlers import CHAT_TURN_TIMEOUT_SECONDS

    assert _DEFAULT_TURN_TIMEOUT_SECONDS == CHAT_TURN_TIMEOUT_SECONDS, (
        "turn_runtime._DEFAULT_TURN_TIMEOUT_SECONDS must mirror "
        "handlers.CHAT_TURN_TIMEOUT_SECONDS — they have diverged."
    )
