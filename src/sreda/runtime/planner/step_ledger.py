"""Thin helpers over StepExecutionLedger for the planner executor (Sub-A12 Phase E).

Design constraints (mirrors emit_event() in sreda.services.audit_feed):

  - Helpers do NOT commit.  The executor passes a dedicated ledger session
    (separate from the tool's data session) and controls the transaction
    boundary; helpers only flush() so the row is visible within the same
    connection before the caller decides to commit or rollback.

  - Timestamps are passed in by the caller (``now: datetime``) — no clock
    calls inside.  This makes helpers deterministic in tests and keeps the
    timing consistent with the surrounding executor logic.

  - open_step is idempotent: a second call with the same (execution_id,
    step_id) after a crash returns the existing row unchanged.  This is the
    crash-resume guarantee — the executor must NOT insert a new row on retry.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sreda.db.models.planner import StepExecutionLedger


# Statuses that close a step.  'started' is NOT in this set — it can only
# be set by open_step(); mark_step_status() enforces this explicitly.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"committed", "unknown", "unknown_pending"}
)


def open_step(
    session: Session,
    *,
    execution_id: str,
    step_id: str,
    tool: str | None,
    operation_id: str,
    now: datetime,
) -> StepExecutionLedger:
    """Idempotent open: return the existing row for (execution_id, step_id),
    or insert a new one with status 'started'.

    A crash-retry calling open_step a second time receives the SAME row (same
    ``id``, same ``operation_id``) and does NOT reset its status.  This
    prevents the executor from losing the knowledge that a step was already
    started, which would break the unknown-outcome detection logic.

    The caller is responsible for committing or rolling back.
    """
    existing = session.execute(
        select(StepExecutionLedger).where(
            StepExecutionLedger.execution_id == execution_id,
            StepExecutionLedger.step_id == step_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Codex A/B #8b-1 R1 (both MAJOR): a resume MUST re-derive the SAME
        # operation_id (allocate_operation_id is deterministic over the stable
        # turn_key+step_id+tool). A mismatch means the caller's operation_id
        # diverged from the ledger row's — which would split ledger
        # idempotency from audit_outbox idempotency (the tool would dedup on a
        # different key than the ledger records). That is a bug, not a benign
        # resume; fail loud rather than silently attach.
        if existing.operation_id != operation_id:
            raise ValueError(
                f"open_step: ledger row for (execution_id={execution_id!r}, "
                f"step_id={step_id!r}) has operation_id="
                f"{existing.operation_id!r} but caller supplied "
                f"{operation_id!r}. A resume must re-derive the SAME "
                f"operation_id; divergence splits ledger/audit idempotency."
            )
        if tool is not None and existing.tool != tool:
            raise ValueError(
                f"open_step: ledger row for (execution_id={execution_id!r}, "
                f"step_id={step_id!r}) has tool={existing.tool!r} but caller "
                f"supplied tool={tool!r}. The step's tool must be stable "
                f"across retries."
            )
        return existing

    row = StepExecutionLedger(
        id=uuid.uuid4().hex,
        execution_id=execution_id,
        step_id=step_id,
        tool=tool,
        operation_id=operation_id,
        status="started",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def mark_step_status(
    session: Session,
    *,
    execution_id: str,
    step_id: str,
    status: str,
    now: datetime,
) -> StepExecutionLedger:
    """Update the status of an existing ledger row to a terminal value.

    Only statuses in ``_TERMINAL_STATUSES`` are accepted.  Attempting to
    set 'started' here raises ValueError — that status is exclusively managed
    by open_step().

    Raises
    ------
    ValueError
        If *status* is not in _TERMINAL_STATUSES.
    LookupError
        If no row exists for (execution_id, step_id).  Marking a step that
        was never opened is a bug in the executor.
    """
    if status not in _TERMINAL_STATUSES:
        raise ValueError(
            f"Invalid ledger status {status!r}. "
            f"Allowed values: {sorted(_TERMINAL_STATUSES)}. "
            "'started' is set only by open_step()."
        )

    row = session.execute(
        select(StepExecutionLedger).where(
            StepExecutionLedger.execution_id == execution_id,
            StepExecutionLedger.step_id == step_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(
            f"No StepExecutionLedger row found for "
            f"execution_id={execution_id!r}, step_id={step_id!r}. "
            "open_step() must be called before mark_step_status()."
        )

    row.status = status
    row.updated_at = now
    session.flush()
    return row


__all__ = [
    "open_step",
    "mark_step_status",
]
