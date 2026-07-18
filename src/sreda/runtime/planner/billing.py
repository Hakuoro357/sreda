"""LLM billing reservation ledger helpers — Sub-A12 Phase E PR-2a #10c.

Provides the *write* side of ``planner_llm_reservations``: mint a stable
``llm_call_id``, persist a reservation BEFORE the provider call, and
transition it to a terminal state AFTER (charged / released / expired /
unknown_provider_outcome).

Design invariants
-----------------
* **Idempotent reserve**: a retry of the same call cannot double-reserve.
  Implemented via SAVEPOINT + IntegrityError re-SELECT (mirrors
  ``create_or_resume_execution`` in persistence.py).
* **Exactly-once charge**: charging twice with the SAME amount is
  idempotent; charging twice with a DIFFERENT amount raises ValueError
  (split-brain guard).
* **Never under-charge on unknown outcome**: if the provider returns no
  usage data, ``mark_unknown_provider_outcome`` charges the RESERVED MAX
  and sets the state to ``unknown_provider_outcome`` so a human/admin can
  reconcile later.
* **No TTL under-charge**: the ``expire`` helper is called ONLY by the
  recovery scanner (PR-2b/#9) when ``expires_at < now()``.  The scanner
  uses the (state, expires_at) index on ``planner_llm_reservations``
  to find candidates efficiently.

PR-2b wiring note
-----------------
Each planner/composer/finalize-rescue LLM caller should:
    1. ``reserve(session, ...)``          — pre-call
    2. commit/flush
    3. make the provider call
    4. on success:   ``charge(session, ...)``
    5. on no-usage:  ``mark_unknown_provider_outcome(session, ...)``
    6. on cancelled: ``release(session, ...)``
    7. commit/flush

#9 recovery scanner note
------------------------
The scanner queries::

    SELECT * FROM planner_llm_reservations
    WHERE state = 'reserved' AND expires_at < :now
    ORDER BY expires_at
    LIMIT :batch

and calls ``expire(session, llm_call_id=row.llm_call_id, now=now)`` for
each row, then commits.  The (state, expires_at) composite index
(``ix_planner_llm_reservations_state_expires``) makes this efficient.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.planner import PlannerLlmReservation

# ---------------------------------------------------------------------------
# Phase literal — all phases that can produce an LLM billing reservation
# ---------------------------------------------------------------------------

LlmCallPhase = Literal["planner", "composer", "finalize_rescue"]
"""Which planner-path stage is making the LLM call.

planner         — the main planning LLM call that produces the plan JSON.
composer        — the reply-composition LLM call.
finalize_rescue — the emergency finalize-rescue call when composition fails.
"""

# ---------------------------------------------------------------------------
# State-transition matrix (documented for audit purposes)
# ---------------------------------------------------------------------------
# Terminal states: charged, released, expired, unknown_provider_outcome.
# From a terminal state, NO further transition is allowed (idempotent
# returns are the only exception, noted per-function below).
#
#   reserved  → charged                  (charge)
#   reserved  → released                 (release)
#   reserved  → expired                  (expire — scanner only)
#   reserved  → unknown_provider_outcome (mark_unknown_provider_outcome)
#   charged   → (terminal; idempotent charge with same amount allowed)
#   released  → (terminal)
#   expired   → (terminal)
#   unknown_provider_outcome → (terminal)

_TERMINAL_STATES: frozenset[str] = frozenset(
    {"charged", "released", "expired", "unknown_provider_outcome"}
)


# ---------------------------------------------------------------------------
# make_llm_call_id
# ---------------------------------------------------------------------------


def make_llm_call_id(
    *,
    execution_id: str,
    phase: LlmCallPhase,
    call_seq: int,
    model: str,
) -> str:
    """Build a stable, PII-free idempotency key for one LLM call.

    Format: ``{execution_id}:{phase}:{call_seq}:{model}``

    This key is stored in ``planner_llm_reservations.llm_call_id``
    (String(128), UNIQUE).  It must be stable across retries: the same
    call site with the same inputs always yields the same key, so a
    crash-retry cannot double-reserve.

    Parameters
    ----------
    execution_id:
        The ``PlannerExecution.id`` for the current turn.  Must be
        non-empty and must NOT contain ``':'`` — colons are used as the
        composite-key separator and their presence in execution_id would
        make the key ambiguous/collidable.
    phase:
        Which planner stage is making the call ("planner", "composer",
        "finalize_rescue").  Fixed ``Literal`` — no colons possible.
    call_seq:
        0-based STABLE LOGICAL call index within the phase for this
        execution (e.g. 0 = first distinct planner call, 1 = a second
        DISTINCT planner call that is conceptually different from the
        first).  A provider RETRY of the SAME logical call MUST reuse
        the SAME ``call_seq`` so it re-attaches to the existing
        reservation and never mints a new one.  Must be >= 0.
    model:
        Normalised provider model id (e.g. "mimo-v2.5-pro").  Must be
        non-empty and fit within the column length limit after joining.
        Colons in model are allowed — model is the final tail of the
        composite key and therefore unambiguous.

    Raises
    ------
    ValueError
        If ``execution_id`` is empty or contains ``':'``, if ``model``
        is empty, if ``call_seq < 0``, if ``phase`` is not a recognised
        value, or if the resulting key exceeds 128 characters (the
        column width).  Truncation is refused because it would break
        idempotency.
    """
    if not execution_id:
        raise ValueError("execution_id must be a non-empty string")
    if ":" in execution_id:
        raise ValueError(
            f"execution_id must not contain ':' (composite-key separator); "
            f"got {execution_id!r}"
        )
    if not model:
        raise ValueError("model must be a non-empty string")
    if call_seq < 0:
        raise ValueError(f"call_seq must be >= 0, got {call_seq!r}")
    # phase is constrained by the Literal type; runtime-validate for
    # callers that pass a plain str (e.g. from config).
    _valid_phases: tuple[str, ...] = ("planner", "composer", "finalize_rescue")
    if phase not in _valid_phases:
        raise ValueError(
            f"phase must be one of {_valid_phases}, got {phase!r}"
        )
    key = f"{execution_id}:{phase}:{call_seq}:{model}"
    if len(key) > 128:
        raise ValueError(
            f"llm_call_id would be {len(key)} chars, exceeding the 128-char "
            f"column limit.  Pass a shorter model id."
        )
    return key


# ---------------------------------------------------------------------------
# reserve
# ---------------------------------------------------------------------------


def reserve(
    session: Session,
    *,
    llm_call_id: str,
    execution_id: str | None,
    tenant_id: str,
    feature_key: str | None,
    model: str | None,
    reserved_tokens: int,
    now: datetime,
    ttl_seconds: int | None = None,
) -> PlannerLlmReservation:
    """INSERT a new reservation row, or re-attach if one already exists.

    Persists the budget hold BEFORE the provider call.  The UNIQUE
    constraint on ``llm_call_id`` guarantees that a crash-retry calling
    ``reserve`` a second time gets the same row back rather than minting
    a duplicate.

    Idempotency contract
    --------------------
    * If a row with ``llm_call_id`` already exists (any state), it is
      returned unchanged.  The caller must NOT reset its state, even if
      the row is already in a terminal state (e.g. the first call charged
      it and the retry must not re-open it).
    * Race safety: the INSERT is wrapped in a SAVEPOINT
      (``session.begin_nested()``).  On ``IntegrityError`` from the
      unique constraint, the savepoint rolls back automatically and we
      re-SELECT to find the winner row.  Pattern mirrors
      ``create_or_resume_execution`` in persistence.py.

    Parameters
    ----------
    session:
        Active SQLAlchemy Session.  Caller owns the outer transaction and
        commits after this call returns.
    llm_call_id:
        Stable idempotency key — use ``make_llm_call_id()``.
    execution_id:
        ``PlannerExecution.id`` FK (nullable — None when billing is
        tracked outside a full execution context).
    tenant_id:
        Billing tenant.  Required, non-nullable in schema.
    feature_key:
        Optional feature / skill context for cost attribution.
    model:
        Provider model id.  Stored for billing breakdowns.
    reserved_tokens:
        Pre-call max-token budget.  Must be > 0.
    now:
        Caller-supplied current UTC datetime (avoids hidden ``datetime.now``
        calls inside helpers — testable).
    ttl_seconds:
        If set, ``expires_at = now + timedelta(seconds=ttl_seconds)``.
        The recovery scanner uses ``expires_at`` to expire abandoned
        reservations.  Pass ``None`` when no TTL is needed (e.g.
        synchronous single-worker flows).

    Raises
    ------
    ValueError
        If ``reserved_tokens <= 0``.
    """
    if reserved_tokens <= 0:
        raise ValueError(
            f"reserved_tokens must be > 0, got {reserved_tokens!r}"
        )

    # Step 1: optimistic SELECT — happy path avoids the SAVEPOINT overhead.
    existing = session.scalars(
        select(PlannerLlmReservation).where(
            PlannerLlmReservation.llm_call_id == llm_call_id
        )
    ).one_or_none()
    if existing is not None:
        return existing

    # Step 2: INSERT inside a SAVEPOINT so that a concurrent winner's
    # IntegrityError does not poison the parent transaction.
    expires_at: datetime | None = None
    if ttl_seconds is not None:
        expires_at = now + timedelta(seconds=ttl_seconds)

    new_row = PlannerLlmReservation(
        id=uuid.uuid4().hex,
        llm_call_id=llm_call_id,
        execution_id=execution_id,
        tenant_id=tenant_id,
        feature_key=feature_key,
        model=model,
        state="reserved",
        reserved_tokens=reserved_tokens,
        actual_tokens=None,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
    )
    try:
        with session.begin_nested():
            session.add(new_row)
            session.flush()
    except IntegrityError:
        # Concurrent inserter won the race on uq_planner_llm_reservations_llm_call_id.
        # The savepoint context manager already rolled back the failed nested
        # transaction; the parent transaction is intact.
        winner = session.scalars(
            select(PlannerLlmReservation).where(
                PlannerLlmReservation.llm_call_id == llm_call_id
            )
        ).one_or_none()
        if winner is None:
            # Genuinely unexpected: unique violation not from our key.
            raise
        return winner

    return new_row


# ---------------------------------------------------------------------------
# Internal lookup helper
# ---------------------------------------------------------------------------


def _get_reservation(
    session: Session, llm_call_id: str
) -> PlannerLlmReservation:
    """Fetch the reservation row or raise LookupError (no row lock).

    Use only for read-only helpers.  All state-mutating transitions MUST
    use ``_get_reservation_for_update`` instead.
    """
    row = session.scalars(
        select(PlannerLlmReservation).where(
            PlannerLlmReservation.llm_call_id == llm_call_id
        )
    ).one_or_none()
    if row is None:
        raise LookupError(
            f"No PlannerLlmReservation found for llm_call_id={llm_call_id!r}"
        )
    return row


def _get_reservation_for_update(
    session: Session, llm_call_id: str
) -> PlannerLlmReservation:
    """Fetch the reservation row with a row-level lock or raise LookupError.

    Uses ``SELECT ... FOR UPDATE`` (Postgres) which serializes competing
    terminal transitions: each caller reads-checks-writes under the lock
    so no two sessions can both observe ``state='reserved'`` and both
    commit a different terminal state (split-brain).  On SQLite the
    ``with_for_update()`` clause is a no-op — the whole database file is
    locked at write time, which achieves the same serialization effect.

    ALL state-mutating helpers (charge, release, expire,
    mark_unknown_provider_outcome) MUST use this variant so the
    in-Python state check is only performed while holding the row lock.

    ``populate_existing=True`` is REQUIRED (Codex A/B #10c R2 CRITICAL): the
    row may already be in this Session's identity map (e.g. a scanner loaded
    it as 'reserved'). Without populate_existing, ``SELECT ... FOR UPDATE``
    acquires the lock but returns the STALE identity-map object — so after a
    competing transaction committed 'charged', this session would still see
    'reserved' and could overwrite the committed terminal state (the exact
    split-brain / lost-charge class). populate_existing forces the locked read
    to OVERWRITE the in-memory object with the current DB row, so the in-Python
    state check reflects the just-locked truth.
    """
    row = session.scalars(
        select(PlannerLlmReservation)
        .where(PlannerLlmReservation.llm_call_id == llm_call_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise LookupError(
            f"No PlannerLlmReservation found for llm_call_id={llm_call_id!r}"
        )
    return row


# ---------------------------------------------------------------------------
# charge
# ---------------------------------------------------------------------------


def charge(
    session: Session,
    *,
    llm_call_id: str,
    actual_tokens: int,
    now: datetime,
) -> PlannerLlmReservation:
    """Reconcile actual token usage after a successful provider call.

    Transitions ``reserved`` → ``charged``.

    Idempotency contract
    --------------------
    Charging twice with the SAME ``actual_tokens`` is idempotent: the row
    is returned unchanged (no error).  This covers the "lost response"
    crash point — the worker died after the provider replied but before
    the charge was persisted; on retry it finds ``charged`` + same amount
    and returns cleanly.

    Charging twice with a DIFFERENT ``actual_tokens`` raises ``ValueError``
    — this signals a split-brain bug (two different callers both claim to
    have the authoritative usage figure) and must fail loud.

    Parameters
    ----------
    actual_tokens:
        Actual tokens consumed, as reported by the provider.  Must be
        >= 0.  A zero-token response (e.g. provider error with partial
        usage) is valid.

    Raises
    ------
    LookupError
        If no row exists for ``llm_call_id``.
    ValueError
        If ``actual_tokens < 0``, if the row is already charged with a
        different amount, or if the row is in a non-reservable state
        (released / expired / unknown_provider_outcome).
    """
    if actual_tokens < 0:
        raise ValueError(f"actual_tokens must be >= 0, got {actual_tokens!r}")

    # Acquire row lock BEFORE the state check so that competing callers
    # (e.g. charge vs expire racing on a 'reserved' row) are serialized.
    row = _get_reservation_for_update(session, llm_call_id)

    if row.state == "charged":
        # Idempotent: same amount → no-op; different amount → bug.
        if row.actual_tokens == actual_tokens:
            return row
        raise ValueError(
            f"Double-charge detected for llm_call_id={llm_call_id!r}: "
            f"stored actual_tokens={row.actual_tokens!r}, "
            f"new actual_tokens={actual_tokens!r}.  "
            f"Charging twice with different amounts is a bug."
        )

    if row.state != "reserved":
        raise ValueError(
            f"Cannot charge reservation in state={row.state!r} "
            f"(llm_call_id={llm_call_id!r}).  "
            f"Only 'reserved' rows can be charged."
        )

    row.state = "charged"
    row.actual_tokens = actual_tokens
    row.updated_at = now
    session.flush()
    return row


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def release(
    session: Session,
    *,
    llm_call_id: str,
    now: datetime,
) -> PlannerLlmReservation:
    """Release a reservation for a call that was cancelled before billing.

    Transitions ``reserved`` → ``released``.  Idempotent if the row is
    already ``released``.

    Use this when the planner call was cancelled *before* the provider
    was contacted (e.g. execution aborted by user or timeout).

    Raises
    ------
    LookupError
        If no row exists for ``llm_call_id``.
    ValueError
        If the row is in a terminal state other than ``released``
        (charged / expired / unknown_provider_outcome).
    """
    # Acquire row lock BEFORE the state check so that competing callers
    # are serialized (see _get_reservation_for_update docstring).
    row = _get_reservation_for_update(session, llm_call_id)

    if row.state == "released":
        # Already released — idempotent.
        return row

    if row.state != "reserved":
        raise ValueError(
            f"Cannot release reservation in state={row.state!r} "
            f"(llm_call_id={llm_call_id!r}).  "
            f"Only 'reserved' rows can be released."
        )

    row.state = "released"
    row.updated_at = now
    session.flush()
    return row


# ---------------------------------------------------------------------------
# mark_unknown_provider_outcome
# ---------------------------------------------------------------------------


def mark_unknown_provider_outcome(
    session: Session,
    *,
    llm_call_id: str,
    now: datetime,
) -> PlannerLlmReservation:
    """Handle the case where the provider returned no usable usage data.

    Transitions ``reserved`` → ``unknown_provider_outcome``.

    Policy (never under-charge)
    ---------------------------
    ``actual_tokens`` is set to ``reserved_tokens`` (the max budget), so
    billing never under-charges.  A human/admin must reconcile the
    difference later.  This is intentionally conservative — it is better
    to temporarily over-charge and reconcile than to silently lose
    revenue or create a double-spend via a blind retry.

    Idempotent if the row is already ``unknown_provider_outcome``
    (crash-retry safe).

    Raises
    ------
    LookupError
        If no row exists for ``llm_call_id``.
    ValueError
        If the row is in any terminal state other than
        ``unknown_provider_outcome`` (charged / released / expired).
    """
    # Acquire row lock BEFORE the state check so that competing callers
    # are serialized (see _get_reservation_for_update docstring).
    row = _get_reservation_for_update(session, llm_call_id)

    if row.state == "unknown_provider_outcome":
        # Already marked — idempotent.
        return row

    if row.state != "reserved":
        raise ValueError(
            f"Cannot mark unknown_provider_outcome for reservation in "
            f"state={row.state!r} (llm_call_id={llm_call_id!r}).  "
            f"Only 'reserved' rows can be transitioned to unknown_provider_outcome."
        )

    row.state = "unknown_provider_outcome"
    # Charge the reserved max — never under-charge.
    row.actual_tokens = row.reserved_tokens
    row.updated_at = now
    session.flush()
    return row


# ---------------------------------------------------------------------------
# expire
# ---------------------------------------------------------------------------


def expire(
    session: Session,
    *,
    llm_call_id: str,
    now: datetime,
) -> PlannerLlmReservation:
    """Expire a stale reservation that was never resolved.

    Transitions ``reserved`` → ``expired``.  Called ONLY by the recovery
    scanner (#9) when ``expires_at < now()``.  Idempotent if the row is
    already ``expired``.

    The scanner should query using the (state, expires_at) composite
    index::

        SELECT * FROM planner_llm_reservations
        WHERE state = 'reserved' AND expires_at < :now
        ORDER BY expires_at
        LIMIT :batch

    and call this function for each row.

    Raises
    ------
    LookupError
        If no row exists for ``llm_call_id``.
    ValueError
        If the row is in any terminal state other than ``expired``
        (charged / released / unknown_provider_outcome).
    """
    # Acquire row lock BEFORE the state check so that competing callers
    # are serialized (see _get_reservation_for_update docstring).
    row = _get_reservation_for_update(session, llm_call_id)

    if row.state == "expired":
        # Already expired — idempotent.
        return row

    if row.state != "reserved":
        raise ValueError(
            f"Cannot expire reservation in state={row.state!r} "
            f"(llm_call_id={llm_call_id!r}).  "
            f"Only 'reserved' rows can be expired."
        )

    # Require the TTL to have actually lapsed.  Expiring a row whose TTL
    # has not elapsed (or that has no TTL at all) is a caller/scanner bug
    # that would cause TTL under-charge / lost revenue.
    #
    # Timezone normalisation: SQLite stores datetimes as naive strings and
    # returns them without tzinfo.  Compare on the same footing by stripping
    # tzinfo from `now` when `expires_at` is naive (offset-naive vs
    # offset-aware comparison raises TypeError on Python 3.x).  On Postgres
    # both will be offset-aware, so the branch is never taken there.
    #
    # Convert `now` to UTC BEFORE stripping (Codex A/B #10c R2 MINOR): the
    # SQLite-stored naive value represents UTC, so a non-UTC aware `now` (e.g.
    # Europe/Moscow) must be shifted to UTC first — naively stripping tzinfo
    # would compare the local wall-clock against a UTC instant and expire
    # early/late by the offset.
    #
    # 2026-07-18 audit fix: нормализация теперь СИММЕТРИЧНАЯ — обратный случай
    # (expires_at aware, now naive) раньше не обрабатывался и бросал TypeError
    # на сравнении. Naive `now` трактуем как UTC (конвенция budget._ensure_utc).
    expires_at = row.expires_at
    cmp_now = now
    if expires_at is not None:
        if expires_at.tzinfo is None and cmp_now.tzinfo is not None:
            cmp_now = cmp_now.astimezone(timezone.utc).replace(tzinfo=None)
        elif expires_at.tzinfo is not None and cmp_now.tzinfo is None:
            cmp_now = cmp_now.replace(tzinfo=timezone.utc)
    if expires_at is None or expires_at >= cmp_now:
        raise ValueError(
            f"Cannot expire reservation for llm_call_id={llm_call_id!r}: "
            f"TTL has not lapsed (expires_at={row.expires_at!r}, now={now!r}).  "
            f"expire() must only be called by the recovery scanner after "
            f"expires_at < now."
        )

    row.state = "expired"
    row.updated_at = now
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "LlmCallPhase",
    "make_llm_call_id",
    "reserve",
    "charge",
    "release",
    "mark_unknown_provider_outcome",
    "expire",
]
