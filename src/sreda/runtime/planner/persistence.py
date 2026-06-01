"""Persistence helpers for planner_executions lifecycle — Sub-A12 Phase B.4.

Thin DB-write layer around the ``PlannerExecution`` model. Orchestrator
(``runtime/planner/orchestrator.py``) drives the lifecycle:

    insert_pending → mark_received → mark_invalid OR mark_valid

Each call is small: explicit SQL with named parameters, no ORM
gymnastics. Caller passes a Session and commits at its own boundaries
(orchestrator owns per-stage transactions, not this module).

Out of scope for Phase B.4 (deferred to Phase B.4-followup migration):
* ``idempotency_key`` column for atomic claim-or-replay
* ``failure_kind`` column for retryable vs final classification
* ``attempts_log_json`` per-attempt metadata array

This module sticks to the existing Sub-A7 schema. The orchestrator
captures attempt metadata in memory (returned in PlannerResult) so
follow-up work can persist it without rewriting the lifecycle.

Sub-A12 Phase E — PR-2a #9b: adds ``create_or_resume_execution`` for
turn-key resume: a re-delivered Telegram update re-attaches to the
existing PlannerExecution instead of creating a duplicate, preserving
stable operation_ids on the step_execution_ledger.

Sub-A12 Phase E — PR-2a #10a: PII write-mode enum + ``_write_pii``
helper + ``read_planner_pii`` dual-read accessor.  The pre-LIVE default
is ``encrypted_only``: plaintext PII columns are left NULL; the
``*_enc`` mirrors receive the ciphertext.  PR-2b will flip the default
to ``dual_write`` and later ``encrypted_only`` (post-migration).

PII columns in scope for #10a (5 fields):
    raw_planner_response, plan_json, execution_plan_json,
    validation_errors, turn_classification_reason
Out of scope for #10a (wired later):
    execution_log_json / execution_log_json_enc  (no writer yet)
    planner_gaps PII columns                     (no writer yet)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.planner import PlannerExecution

# ---------------------------------------------------------------------------
# PII write-mode — three-phase dual-write / expand / contract policy
# ---------------------------------------------------------------------------

PlannerPiiWriteMode = Literal["plaintext_only", "dual_write", "encrypted_only"]
"""Three-phase write policy for planner PII columns.

plaintext_only
    Legacy / transition-source mode.  Writes the plaintext column only;
    the ``*_enc`` mirror is left NULL.  Used when migrating FROM
    encrypted_only back to plaintext is needed (break-glass) or before
    any encrypted column existed.

dual_write
    Expand-window mode.  Writes BOTH the plaintext column AND the
    ``*_enc`` mirror on every write.  Allows a safe rollout: old readers
    still use the plaintext column, new readers prefer ``*_enc``.
    PR-2b will flip the default to this mode.

encrypted_only
    Pre-LIVE mode (current default, per Sub-A12 Phase E plan §5).
    Writes the ``*_enc`` mirror only; the plaintext column is set to
    NULL.  After the backfill migration (PR-2c) there will be no rows
    with plaintext PII, so this mode is safe for new rows immediately.
"""

_PLANNER_PII_WRITE_MODE: PlannerPiiWriteMode = "encrypted_only"
"""Active write mode for this process.  Change at import time for tests
or via PR-2b deployment toggle; do NOT mutate at runtime."""


# ---------------------------------------------------------------------------
# PII write / read helpers
# ---------------------------------------------------------------------------


def _write_pii(
    row: Any,
    *,
    plain_attr: str,
    enc_attr: str,
    value: Any,
    mode: PlannerPiiWriteMode | None = None,
) -> None:
    """Write a single PII field according to *mode*.

    For ``EncryptedString`` columns *value* must be a ``str`` (or
    ``None``); for ``JSONEncryptedString`` columns it must be a
    JSON-serialisable ``dict`` / ``list`` (or ``None``).  The
    TypeDecorators in ``sreda.db.types`` handle the actual
    encrypt/decrypt — this helper only decides WHICH attribute(s) to set.

    *mode* is resolved at CALL TIME from ``_PLANNER_PII_WRITE_MODE`` when not
    passed (Codex A/B #10a R1) — so a PR-2b deployment toggle of the module
    constant actually changes mark_received / mark_invalid / mark_valid
    behaviour, rather than being frozen at function-definition time.

    Each mode sets BOTH attributes to a defined state (the other is NULLed)
    so there is never a stale mirror that ``read_planner_pii`` could prefer:
      plaintext_only → sets *plain_attr*; NULLs *enc_attr*.
      encrypted_only → sets *enc_attr*; NULLs *plain_attr*.
      dual_write     → sets BOTH *plain_attr* AND *enc_attr*.

    FAIL-CLOSED (Codex A/B #10a R1 high MAJOR): an unrecognised *mode* raises
    rather than falling through to dual_write — a config typo like
    ``"encrypted-only"`` must NOT silently write the plaintext column and leak
    PII.
    """
    if mode is None:
        mode = _PLANNER_PII_WRITE_MODE
    if mode == "plaintext_only":
        setattr(row, plain_attr, value)
        setattr(row, enc_attr, None)
    elif mode == "encrypted_only":
        setattr(row, enc_attr, value)
        setattr(row, plain_attr, None)
    elif mode == "dual_write":
        setattr(row, plain_attr, value)
        setattr(row, enc_attr, value)
    else:
        raise ValueError(
            f"_write_pii: unknown PlannerPiiWriteMode {mode!r}; expected one "
            f"of 'plaintext_only' / 'dual_write' / 'encrypted_only'"
        )


def read_planner_pii(
    row: PlannerExecution,
    field: str,
) -> Any:
    """Dual-read accessor for the five in-scope PII fields.

    *field* must be one of::

        raw_planner_response
        validation_errors
        plan_json
        execution_plan_json
        turn_classification_reason

    Returns the decrypted ``*_enc`` value when it is not ``None``
    (preferred path for ``encrypted_only`` / ``dual_write`` rows),
    falling back to the plaintext column for legacy rows written under
    ``plaintext_only`` mode.

    The TypeDecorators on the ORM model handle all decryption
    transparently — this function just implements the "prefer enc"
    precedence rule.
    """
    _VALID_FIELDS = frozenset(
        {
            "raw_planner_response",
            "validation_errors",
            "plan_json",
            "execution_plan_json",
            "turn_classification_reason",
        }
    )
    if field not in _VALID_FIELDS:
        raise ValueError(
            f"read_planner_pii: unknown field {field!r}; "
            f"valid fields: {sorted(_VALID_FIELDS)}"
        )
    enc_val = getattr(row, f"{field}_enc")
    if enc_val is not None:
        return enc_val
    return getattr(row, field)


@dataclass(frozen=True)
class ResumeResult:
    """Returned by ``create_or_resume_execution`` to tell the caller whether
    it created a brand-new row or re-attached to an existing one.

    ``execution_id`` is always the canonical id that the caller should use
    for subsequent lifecycle calls (mark_received, mark_valid, …).

    ``resumed=True`` means a PlannerExecution with the given ``turn_key``
    already existed; the caller should skip the LLM call and continue
    execution from the existing row's state instead.
    ``resumed=False`` means this is a fresh execution.
    """

    execution_id: str
    resumed: bool  # True = re-attached to existing row; False = newly inserted


def make_execution_id() -> str:
    """Stable id format ``plan_exec_<32hex>`` — matches Sub-A7 id shape."""
    return f"plan_exec_{uuid.uuid4().hex[:24]}"


def create_or_resume_execution(
    session: Session,
    *,
    turn_key: str,
    execution_id: str,
    run_id: str,
    tenant_id: str,
    feature_key: str,
    planner_prompt_version: int,
    planner_provider: str,
    planner_model: str,
    tool_registry_version: str | None = None,
    composer_registry_snapshot_hash: str | None = None,
) -> ResumeResult:
    """Idempotent create-or-attach for turn-key resume (Sub-A12 Phase E PR-2a).

    A Telegram update can be re-delivered (Telegram retries on 5xx or
    timeout). Without this function the worker would call insert_pending
    twice, minting two PlannerExecution rows for the same turn. That
    causes:
      - duplicate LLM calls (wasted budget)
      - two sets of step_execution_ledger rows with *different*
        operation_ids → idempotency breaks for external tool calls.

    ``turn_key`` is the caller-supplied stable identity for the turn,
    typically ``f"{tenant_id}:{channel}:{external_update_id}"``.
    It maps 1-to-1 onto the UNIQUE column
    ``planner_executions.turn_key`` (migration 0054).

    Semantics
    ---------
    1. Blank turn_key → ValueError (fail-closed).  A blank key would
       collapse *all* turns onto the same execution row.
    2. SELECT … FOR UPDATE by turn_key.  The row lock prevents a narrow
       race on Postgres (no-op on SQLite; that is acceptable — SQLite
       uses table-level locking anyway).
    3. Row found → return ResumeResult(existing.id, resumed=True).
       The caller must NOT insert a new row; it should resume execution
       from the existing row's state.
    4. Row not found → INSERT a new PlannerExecution (same defaults as
       insert_pending) with turn_key set, then flush.

       RACE SAFETY: a concurrent worker may win the INSERT between our
       SELECT and our INSERT (possible on Postgres under high
       concurrency).  We wrap the add+flush in a SAVEPOINT
       (``with session.begin_nested():``).  On IntegrityError from the
       uq_planner_executions_turn_key violation the savepoint rolls back
       automatically (context manager __exit__ calls rollback on the
       nested transaction), leaving the parent transaction healthy.  We
       then re-SELECT by turn_key and return the winner's row.

       Pattern mirrors the recommended savepoint guidance documented in
       services/audit_feed.py (Codex R2 CRITICAL comment):

           try:
               with session.begin_nested():
                   session.add(row); session.flush()
           except IntegrityError:
               other = session.scalars(
                   select(PlannerExecution).where(
                       PlannerExecution.turn_key == turn_key
                   )
               ).one_or_none()
               if other is None:
                   raise   # genuinely unexpected: constraint mismatch
               return ResumeResult(other.id, resumed=True)

    5. insert_pending is unchanged.  Existing callers that don't need
       turn-key resume continue to work without modification.
    """
    # Guard: a blank turn_key would let unrelated turns share one row.
    # Codex A/B #9b R1 MAJOR — reject whitespace-only too (`"   "` is just as
    # collapsing as ""). We reject rather than silently normalise: the executor
    # feeds the SAME turn_key into allocate_operation_id(), so normalising here
    # (stripping) would diverge the ledger operation_id from this row's identity.
    # The caller owns a clean turn_key.
    if not turn_key or not turn_key.strip():
        raise ValueError("turn_key must be a non-empty, non-blank string")

    # Step 2: locked read — prevents narrow Postgres race on the happy path.
    # with_for_update() is a no-op on SQLite (table-level locking already
    # serialises writes); on Postgres it row-locks the found row.
    existing = session.scalars(
        select(PlannerExecution)
        .where(PlannerExecution.turn_key == turn_key)
        .with_for_update()
    ).one_or_none()

    # Step 3: re-attach to existing row.
    if existing is not None:
        return ResumeResult(existing.id, resumed=True)

    # Step 4: fresh INSERT.  Wrap in a SAVEPOINT so that a concurrent
    # winner's IntegrityError doesn't poison the parent transaction.
    new_row = PlannerExecution(
        id=execution_id,
        run_id=run_id,
        tenant_id=tenant_id,
        feature_key=feature_key,
        planner_prompt_version=planner_prompt_version,
        planner_provider=planner_provider,
        planner_model=planner_model,
        planner_status="pending",
        execution_status="pending",
        tool_registry_version=tool_registry_version,
        composer_registry_snapshot_hash=composer_registry_snapshot_hash,
        execution_log_json=[],
        created_at=datetime.now(timezone.utc),
        turn_key=turn_key,
    )
    try:
        with session.begin_nested():
            session.add(new_row)
            session.flush()
    except IntegrityError:
        # Concurrent inserter won the race on uq_planner_executions_turn_key.
        # The savepoint context manager already rolled back the failed nested
        # transaction; the parent transaction is intact.
        other = session.scalars(
            select(PlannerExecution).where(
                PlannerExecution.turn_key == turn_key
            )
        ).one_or_none()
        if other is None:
            # Re-raise: the unique violation was NOT from our turn_key,
            # which is genuinely unexpected (different constraint?).
            raise
        return ResumeResult(other.id, resumed=True)

    return ResumeResult(execution_id, resumed=False)


def insert_pending(
    session: Session,
    *,
    execution_id: str,
    run_id: str,
    tenant_id: str,
    feature_key: str,
    planner_prompt_version: int,
    planner_provider: str,
    planner_model: str,
    tool_registry_version: str | None = None,
    composer_registry_snapshot_hash: str | None = None,
) -> None:
    """Stage 1: INSERT row with status='pending' before LLM call.

    Uses ORM (PlannerExecution) so JSON columns (execution_log_json)
    get proper type adapter handling on both Postgres and SQLite —
    raw SQL via text() bypassed the JSON adapter and broke SQLite tests."""
    row = PlannerExecution(
        id=execution_id,
        run_id=run_id,
        tenant_id=tenant_id,
        feature_key=feature_key,
        planner_prompt_version=planner_prompt_version,
        planner_provider=planner_provider,
        planner_model=planner_model,
        planner_status="pending",
        execution_status="pending",
        tool_registry_version=tool_registry_version,
        composer_registry_snapshot_hash=composer_registry_snapshot_hash,
        execution_log_json=[],
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()


def mark_received(
    session: Session,
    *,
    execution_id: str,
    raw_response: str,
    latency_ms: int,
    planner_provider: str | None = None,
    planner_model: str | None = None,
) -> None:
    """Stage 2: LLM returned. Save raw text + cumulative latency. Status
    moves pending → received. Validation/compile happen next.

    Codex Sub-A12 B.4 R1 MAJOR fix: persist effective provider/model
    from PlannerCallResult — insert_pending wrote placeholders because
    the actual resolved model wasn't known yet. Update here once we
    have the real values from the LLM call.

    If this is a retry (attempt_no=2), caller should accumulate latency
    BEFORE passing here — orchestrator owns the running sum."""
    row = session.get(PlannerExecution, execution_id)
    if row is None:
        raise LookupError(f"planner_execution {execution_id!r} not found")
    _write_pii(
        row,
        plain_attr="raw_planner_response",
        enc_attr="raw_planner_response_enc",
        value=raw_response,
    )
    row.planner_latency_ms = latency_ms
    row.planner_status = "received"
    if planner_provider is not None:
        row.planner_provider = planner_provider
    if planner_model is not None:
        row.planner_model = planner_model
    session.flush()


def mark_invalid(
    session: Session,
    *,
    execution_id: str,
    validation_errors: str,
) -> None:
    """Terminal failure path: parsing/validation/compile failed and
    retry budget exhausted (or this is the only-attempt path).

    ``planner_status`` → 'invalid'. ``execution_status`` left as
    'pending' since we never reached executor. Caller maps this to
    PlannerResult(success=False)."""
    row = session.get(PlannerExecution, execution_id)
    if row is None:
        raise LookupError(f"planner_execution {execution_id!r} not found")
    row.planner_status = "invalid"
    _write_pii(
        row,
        plain_attr="validation_errors",
        enc_attr="validation_errors_enc",
        value=validation_errors,
    )
    session.flush()


def mark_valid(
    session: Session,
    *,
    execution_id: str,
    plan_json: dict,
    execution_plan_json: dict,
    is_new_turn: bool | None = None,
    turn_classification_reason: str | None = None,
) -> None:
    """Success path: plan parsed + validated + compiled. Snapshot the
    JSON forms for executor consumption AND audit.

    ``planner_status`` → 'valid'. ``execution_status`` stays 'pending'
    until executor (Phase C) starts work."""
    row = session.get(PlannerExecution, execution_id)
    if row is None:
        raise LookupError(f"planner_execution {execution_id!r} not found")
    _write_pii(
        row,
        plain_attr="plan_json",
        enc_attr="plan_json_enc",
        value=plan_json,
    )
    _write_pii(
        row,
        plain_attr="execution_plan_json",
        enc_attr="execution_plan_json_enc",
        value=execution_plan_json,
    )
    row.planner_status = "valid"
    row.is_new_turn = is_new_turn
    _write_pii(
        row,
        plain_attr="turn_classification_reason",
        enc_attr="turn_classification_reason_enc",
        value=turn_classification_reason,
    )
    session.flush()


__all__ = [
    "PlannerPiiWriteMode",
    "ResumeResult",
    "create_or_resume_execution",
    "insert_pending",
    "make_execution_id",
    "mark_invalid",
    "mark_received",
    "mark_valid",
    "read_planner_pii",
]
