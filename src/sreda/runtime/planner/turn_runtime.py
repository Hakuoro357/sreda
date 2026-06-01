"""End-to-end turn-deadline primitive (Sub-A12 Phase E, PR-2a #10d).

A single frozen dataclass ``TurnRuntimeContext`` encapsulates the hard wall
clock for one conversation turn.  It is constructed once at the entry point
(where both the turn start time and the job-lease expiry are known) and then
passed down through planner → executor → composer → finalize → rescue so
that every layer can gate new work on ``has_budget()``.

Reconciliation rule (plan item #8)
------------------------------------
The effective deadline is the **earliest** of two bounds:

* ``turn_started_at + turn_timeout_seconds`` — the legacy
  ``CHAT_TURN_TIMEOUT_SECONDS`` ceiling, which terminates a turn that has
  been running too long regardless of where the slowdown occurred.
* ``job_lease_expires_at`` (optional) — the message-job lease held by the
  worker.  If the lease expires before the turn timeout, the worker loses
  the job to a failover peer, so attempting more work past that point is
  wasteful (and may produce duplicate side-effects if the recovery path also
  fires).

Whichever of the two bounds comes first IS the deadline.

Import note — why we do NOT import ``CHAT_TURN_TIMEOUT_SECONDS`` from
``sreda.runtime.handlers``
---------------------------------------------------------------------------
``handlers.py`` imports sqlalchemy, heavy LLM wrappers, billing services,
and more — the whole action-handler tree.  Importing it here would pull all
of that into every test and any planner-internal module that only needs the
deadline primitive.  Instead we define a module-level constant
``_DEFAULT_TURN_TIMEOUT_SECONDS = 180`` that intentionally mirrors the value
in ``handlers.CHAT_TURN_TIMEOUT_SECONDS``.

**Duplication risk**: if that value is ever changed, both constants must be
updated together.  The comment below and the cross-reference in handlers.py
(to be added in PR-2b) serve as the coupling indicator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # no extra type-only imports needed at this layer

__all__ = [
    "TurnRuntimeContext",
    "_DEFAULT_TURN_TIMEOUT_SECONDS",
]

# ---------------------------------------------------------------------------
# Default timeout constant
# ---------------------------------------------------------------------------

# Mirrors ``sreda.runtime.handlers.CHAT_TURN_TIMEOUT_SECONDS = 180``.
# We intentionally avoid importing handlers here because it pulls heavy deps
# (sqlalchemy, LLM clients, billing).  Keep both values in sync manually;
# grep for "_DEFAULT_TURN_TIMEOUT_SECONDS" when changing CHAT_TURN_TIMEOUT_SECONDS.
_DEFAULT_TURN_TIMEOUT_SECONDS: float = 180.0


def _require_aware(dt: datetime, name: str) -> None:
    """Raise ValueError unless *dt* is timezone-aware.

    This primitive sits on fallback / rescue paths where a raw ``TypeError``
    from a naive-vs-aware ``min`` / subtraction / comparison would be far
    worse than a clean rejection at the boundary (Codex A/B #10d R1, both
    MAJOR). We require tz-aware datetimes EVERYWHERE — deadline_at, the
    construction inputs, and every method's ``now`` — so the deadline maths
    is always unambiguous UTC-comparable.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"{name} must be timezone-aware (e.g. datetime.now(timezone.utc)); "
            f"got naive {dt!r}. A naive datetime risks silent UTC-vs-local bugs "
            f"and TypeError in deadline comparisons."
        )


# ---------------------------------------------------------------------------
# TurnRuntimeContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnRuntimeContext:
    """Hard-wall deadline context for one conversation turn.

    Pass this object from the worker's job-dispatch site down through every
    layer that consumes budget (planner LLM calls, tool executor, composer,
    finalize, rescue).  Every layer gates new work with::

        if not ctx.has_budget(now, need_ms=<expected_ms>):
            # fall back; do NOT start a new attempt
            ...

    All methods accept an explicit ``now: datetime`` parameter — the module
    has no hidden clock access.  Pass ``datetime.now(timezone.utc)`` at the
    call site.  This makes the class trivially testable with synthetic times.

    Attributes
    ----------
    deadline_at:
        The hard wall time (tz-aware UTC) by which the whole turn must
        finish.  Computed by ``from_turn_start()`` as the earlier of the
        turn-timeout deadline and the job-lease expiry.
    """

    deadline_at: datetime

    def __post_init__(self) -> None:
        # Validate even on direct construction (the class is public) — Codex
        # A/B #10d R1 MAJOR. A naive deadline_at would TypeError in every
        # comparison below.
        _require_aware(self.deadline_at, "deadline_at")

    # ------------------------------------------------------------------
    # Instance methods
    # ------------------------------------------------------------------

    def remaining_ms(self, now: datetime) -> int:
        """Milliseconds left until ``deadline_at``.

        Clamps to ``0`` — never returns a negative value.

        Parameters
        ----------
        now:
            Current wall time (tz-aware UTC).  ValueError if naive.
        """
        _require_aware(now, "now")
        delta = (self.deadline_at - now).total_seconds()
        return max(0, int(delta * 1000))

    def is_exhausted(self, now: datetime) -> bool:
        """Return ``True`` when ``now >= deadline_at`` (no budget left).

        Parameters
        ----------
        now:
            Current wall time (tz-aware UTC).  ValueError if naive.
        """
        _require_aware(now, "now")
        return now >= self.deadline_at

    def has_budget(self, now: datetime, *, need_ms: int) -> bool:
        """Return ``True`` iff at least ``need_ms`` milliseconds remain.

        The planner driver MUST call this before starting any new LLM attempt
        (including retries).  If it returns ``False``, the driver must fall
        back rather than starting a new attempt — this is the
        "forbid a new planner attempt when remaining is insufficient; fall
        back before exhaustion" rule from plan item #8.

        Parameters
        ----------
        now:
            Current wall time (tz-aware UTC).
        need_ms:
            Estimated milliseconds required for the upcoming operation.
            Must be >= 0; raises ``ValueError`` otherwise.
        """
        if need_ms < 0:
            raise ValueError(
                f"need_ms must be >= 0, got {need_ms!r}. "
                "Pass the estimated cost of the operation you are about to start."
            )
        # Hard wall (Codex A/B #10d R1 MAJOR): at/after the deadline there is
        # NO budget regardless of need_ms — even need_ms=0 must NOT let a caller
        # start work past exhaustion. is_exhausted() validates `now`.
        if self.is_exhausted(now):
            return False
        return self.remaining_ms(now) >= need_ms

    # ------------------------------------------------------------------
    # Constructor classmethod
    # ------------------------------------------------------------------

    @classmethod
    def from_turn_start(
        cls,
        *,
        turn_started_at: datetime,
        turn_timeout_seconds: float = _DEFAULT_TURN_TIMEOUT_SECONDS,
        lease_expires_at: datetime | None = None,
    ) -> TurnRuntimeContext:
        """Construct a ``TurnRuntimeContext`` from job-dispatch inputs.

        Reconciliation
        --------------
        ``deadline_at`` = min(turn_started_at + turn_timeout_seconds,
                              lease_expires_at)

        If ``lease_expires_at`` is ``None``, only the turn-timeout bound is
        used.

        Parameters
        ----------
        turn_started_at:
            When the worker began processing this turn (tz-aware UTC required;
            this value anchors the turn-timeout deadline).
        turn_timeout_seconds:
            Maximum seconds the turn may run, measured from ``turn_started_at``.
            Defaults to ``_DEFAULT_TURN_TIMEOUT_SECONDS`` (180 s), which
            mirrors ``handlers.CHAT_TURN_TIMEOUT_SECONDS``.  Must be > 0.
        lease_expires_at:
            When the message-job lease held by this worker expires (tz-aware
            UTC).  If given, the effective deadline is the earlier of the
            turn-timeout deadline and this value.  Pass ``None`` when no job
            lease is in scope (e.g. direct invocation, CLI, tests).

        Raises
        ------
        ValueError
            If ``turn_timeout_seconds`` is <= 0.
        ValueError
            If ``turn_started_at`` is timezone-naive (we require tz-aware
            datetimes throughout to avoid silent UTC-vs-local bugs).
        """
        if turn_timeout_seconds <= 0:
            raise ValueError(
                f"turn_timeout_seconds must be > 0, got {turn_timeout_seconds!r}."
            )
        _require_aware(turn_started_at, "turn_started_at")

        timeout_deadline: datetime = turn_started_at + timedelta(
            seconds=turn_timeout_seconds
        )

        if lease_expires_at is None:
            deadline_at = timeout_deadline
        else:
            # Both bounds must be tz-aware or min()/comparison TypeErrors
            # (Codex A/B #10d R1 MAJOR).
            _require_aware(lease_expires_at, "lease_expires_at")
            # Reconciliation: whichever bound arrives first IS the deadline.
            deadline_at = min(timeout_deadline, lease_expires_at)

        return cls(deadline_at=deadline_at)
