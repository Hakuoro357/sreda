"""Crash-safety recovery scanner for the Sub-A12 Phase E planner.

This module implements the *watchdog tick* that finds in-progress
``planner_executions`` whose recovery lease has expired (or was never
set), probes each stalled step's durable-write outcome, and brings the
execution to a terminal state or schedules a re-probe.

Three public functions:

  ``claim_stale_executions``
      Atomically lease up to *N* in-progress executions that have no
      active recovery lease.  Mirrors the ``claim_next_job`` pattern
      from ``sreda.workers.message_queue`` (advisory lock on Postgres;
      SELECT-then-UPDATE on SQLite).

  ``recover_execution``
      For one claimed execution, iterate its ``StepExecutionLedger`` rows,
      probe durable steps via ``probe_operation``, apply
      ``decide_recovery``, and write the resulting execution-level status.

  ``run_recovery_scan``
      One full scan pass: claim → recover (per-execution isolation) →
      return a summary dict.  The periodic scheduling (interval, worker
      registration) is wired in PR-2b/deploy; this function is the tick.

Design constraints (mirrors recovery.py and message_queue.py):
  - No function commits.  ``claim_stale_executions`` and
    ``recover_execution`` flush only; the *caller* (``run_recovery_scan``)
    commits at transaction boundaries.
  - No blind replay.  The scanner marks / probes; it NEVER re-invokes a
    tool.  A failed durable write surfaces as ``failed_needs_manual`` so a
    human can decide.
  - Fail-closed on unknown tools.  A registry miss on a step's tool name
    is treated as ``is_durable_write=True`` so we probe and escalate
    rather than silently skip a possibly-durable write.

Reference values from ``message_queue.py``:
  - ``LEASE_DURATION_SEC = 300``  (lease > chain-timeout 120s +
    worst-step 90s + grace)
  - Settle window must exceed chain timeout (120s) + worst step (90s) =
    210s minimum.  Typical value: 240–300s.  The actual value is
    caller-supplied (PR-2b/config); this module never hardcodes it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sreda.db.models.planner import PlannerExecution, StepExecutionLedger
from sreda.runtime.planner.recovery import (
    TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL,
    TERMINAL_FAILED_NEEDS_MANUAL,
    decide_recovery,
    probe_operation,
)
from sreda.runtime.planner.step_ledger import mark_step_status
from sreda.services.tool_schemas.base import ToolSpec


logger = logging.getLogger(__name__)

# Execution statuses that are fully terminal — these rows must never be
# re-claimed by the recovery scanner.
_TERMINAL_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {
        "completed",
        "partial_failure",
        "failed",
        "aborted",
        "aborted_partial",
        "failed_needs_manual",
        "committed_unserved_needs_manual",
    }
)


@dataclass(frozen=True)
class _AlertPayload:
    """A pending P1 admin alert. Returned by ``recover_execution`` rather than
    fired inline, so ``run_recovery_scan`` can fire it ONLY AFTER the recovery
    transaction commits (Codex A/B #9c R2 MINOR — never alert for a terminal
    state that a later flush/commit failure rolled back)."""

    title: str
    body: str
    dedupe_key: str


@dataclass(frozen=True)
class RecoverOutcome:
    """Result of ``recover_execution``: the resulting status (an execution_status
    value, the sentinel ``'re_probe_pending'``, or ``'not_found'``) plus an
    optional alert to fire post-commit."""

    status: str
    alert: _AlertPayload | None = None


# ---------------------------------------------------------------------------
# 1. claim_stale_executions
# ---------------------------------------------------------------------------


def claim_stale_executions(
    session: Session,
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
    limit: int = 10,
) -> list[str]:
    """Claim ``planner_executions`` rows that are stalled and need recovery.

    A row qualifies when:
    - ``execution_status == 'in_progress'``, AND
    - ``recovery_lease_until IS NULL`` OR ``recovery_lease_until < now``
      (no lease, or the lease has expired).

    On claim the row is updated atomically:
      - ``recovery_worker_id = worker_id``
      - ``recovery_attempt  += 1``
      - ``recovery_lease_until = now + lease_seconds``

    Terminal-status rows and rows with an active lease are never touched.

    Returns the list of claimed ``execution_id`` strings (up to *limit*).
    The session is **not committed** here — the caller controls the
    transaction boundary.

    Concurrency strategy (mirrors ``claim_next_job``):
      - Postgres: ``pg_advisory_xact_lock('planner_recovery_claim')`` to
        serialize concurrent claim transactions, then
        ``FOR UPDATE OF e`` on the inner SELECT to hold the row lock
        through the outer UPDATE … RETURNING.
      - SQLite: SELECT candidate ids then UPDATE each by id (no concurrent
        workers in tests; no FOR UPDATE / advisory-lock needed).

    Parameters
    ----------
    session:
        Active SQLAlchemy session.
    worker_id:
        Stable identifier of the scanning worker instance.
    now:
        Caller-supplied current time (tz-aware UTC).
    lease_seconds:
        How long to hold the recovery lease.  Reference: 300s from
        ``message_queue.LEASE_DURATION_SEC``.
    limit:
        Maximum executions to claim in one pass.
    """
    lease_end = now + timedelta(seconds=lease_seconds)

    dialect = session.bind.dialect.name if session.bind else ""

    if dialect == "postgresql":
        # Advisory lock serializes concurrent claim transactions so two
        # workers cannot both pick up the same stalled execution.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('planner_recovery_claim'))")
        )
        # UPDATE … RETURNING with an inner SELECT + FOR UPDATE OF e.
        # FOR UPDATE OF e row-locks the candidate from identification time
        # through commit, closing the TOCTOU window.
        sql = """
            UPDATE planner_executions
            SET recovery_worker_id  = :worker_id,
                recovery_attempt    = recovery_attempt + 1,
                recovery_lease_until = :lease_end
            WHERE id IN (
                SELECT e.id
                FROM   planner_executions e
                WHERE  e.execution_status = 'in_progress'
                  AND (e.recovery_lease_until IS NULL
                       OR e.recovery_lease_until < :now)
                ORDER  BY e.created_at
                LIMIT  :limit
                FOR    UPDATE OF e
            )
            RETURNING id
        """
        rows = session.execute(
            text(sql),
            {
                "worker_id": worker_id,
                "lease_end": lease_end,
                "now": now,
                "limit": limit,
            },
        ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # SQLite path — simple SELECT-then-UPDATE (tests; no concurrency).
    # ------------------------------------------------------------------
    candidate_rows = session.execute(
        text(
            """
            SELECT id
            FROM   planner_executions
            WHERE  execution_status = 'in_progress'
              AND (recovery_lease_until IS NULL
                   OR recovery_lease_until < :now)
            ORDER  BY created_at
            LIMIT  :limit
            """
        ),
        {"now": now, "limit": limit},
    ).fetchall()

    claimed: list[str] = []
    for row in candidate_rows:
        exec_id = row[0]
        result = session.execute(
            text(
                """
                UPDATE planner_executions
                SET recovery_worker_id   = :worker_id,
                    recovery_attempt     = recovery_attempt + 1,
                    recovery_lease_until = :lease_end
                WHERE id = :id
                  AND execution_status = 'in_progress'
                  AND (recovery_lease_until IS NULL
                       OR recovery_lease_until < :now)
                """
            ),
            {
                "id": exec_id,
                "worker_id": worker_id,
                "lease_end": lease_end,
                "now": now,
            },
        )
        if result.rowcount > 0:
            claimed.append(exec_id)

    return claimed


# ---------------------------------------------------------------------------
# 2. recover_execution
# ---------------------------------------------------------------------------


def recover_execution(
    session: Session,
    *,
    execution_id: str,
    registry: Mapping[str, ToolSpec],
    now: datetime,
    settle_window_seconds: int,
) -> RecoverOutcome:
    """Recover one claimed execution.

    Loads the ``PlannerExecution`` row and ALL its ``StepExecutionLedger``
    rows, then for each ledger row:

    1. Looks up the tool spec.  **Fail-closed**: an unknown tool (not in
       *registry*) is treated as ``is_durable_write=True`` so we probe and
       escalate rather than silently skipping a possibly-durable write.

    2. Probes the operation via ``probe_operation`` (only for durable
       writes; probe is meaningless for read-only steps).

    3. Evaluates the settle window: ``(now - ledger.updated_at).total_seconds()
       > settle_window_seconds``.  The column is tz-aware; ``now`` must be
       tz-aware (UTC) too.

    4. Calls ``decide_recovery`` (pure decision function) and applies the
       action:
       - ``"skip"`` → nothing.
       - ``"mark_committed"`` → update ledger row to ``'committed'``.
       - ``"mark_failed_needs_manual"`` → set ``needs_manual`` flag.
         (``failed_needs_manual`` is an EXECUTION-level status, not a
         valid ledger status — we do NOT call ``mark_step_status`` with it.)
       - ``"re_probe"`` → set ``re_probe_pending`` flag.

    After all rows, the execution status is resolved (NEVER 'completed' —
    the ledger covers only STARTED steps, so it proves neither plan
    completeness nor that the user was served; Codex A/B #9c R1, 3 MAJOR).
    PRIORITY (Codex A/B #9c R2, both MAJOR):
    - any durable write committed → ``'committed_unserved_needs_manual'``,
      lease cleared, alert payload returned. DOMINATES every other outcome
      (incl. needs_manual and an unsettled re_probe) — committed data is the
      most dangerous signal and must surface immediately.
    - else ``re_probe_pending`` → leave ``'in_progress'`` and set a BACKOFF
      lease to the earliest re_probe settle deadline (never None — that would
      tight-loop), no alert.
    - else ``needs_manual`` → ``'failed_needs_manual'``, lease cleared, alert.
    - else (no durable commit, nothing pending — incl. empty ledger) →
      ``'failed'``, lease cleared, no alert.

    Alerts are NOT fired here. The optional ``RecoverOutcome.alert`` payload is
    returned and fired by ``run_recovery_scan`` ONLY AFTER the recovery
    transaction commits (Codex A/B #9c R2 MINOR — never alert for a terminal
    state a later commit failure rolled back). The user-facing
    ``generic_tool_error`` reply is the PR-2b live path's responsibility.

    ``session.flush()`` is called before returning; the CALLER commits.

    Parameters
    ----------
    session:
        Active SQLAlchemy session for this execution (fresh per-execution
        session from ``run_recovery_scan`` so failures are isolated).
    execution_id:
        PlannerExecution.id to recover.
    registry:
        ``dict[tool_name, ToolSpec]`` — e.g.
        ``{s.name: s for s in MIGRATED_TOOL_SPECS}``.
    now:
        Caller-supplied current time (tz-aware UTC).
    settle_window_seconds:
        How many seconds must elapse after ``ledger.updated_at`` before an
        ``unknown_pending`` step without an audit row is declared terminal.
        Reference: must exceed chain timeout (120s) + worst step (90s) ≈
        210s; typical 240–300s.

    Returns
    -------
    RecoverOutcome
        ``.status`` is the resulting execution_status (or ``'re_probe_pending'``
        / ``'not_found'``); ``.alert`` is an optional payload for
        ``run_recovery_scan`` to fire post-commit.
    """
    execution: PlannerExecution | None = session.get(PlannerExecution, execution_id)
    if execution is None:
        logger.error(
            "recover_execution: execution_id=%r not found; skipping",
            execution_id,
        )
        return RecoverOutcome("not_found")

    ledger_rows: list[StepExecutionLedger] = list(
        session.execute(
            select(StepExecutionLedger).where(
                StepExecutionLedger.execution_id == execution_id
            )
        ).scalars()
    )

    needs_manual: bool = False
    re_probe_pending: bool = False
    # A durable write that DID land (probe confirmed or already 'committed').
    # If any exists, the execution is NOT a clean failure — data changed but
    # the user's serving is unproven → committed_unserved_needs_manual.
    any_committed_durable: bool = False
    # Earliest time a re_probe step's settle window elapses — used to set a
    # BACKOFF lease so the scanner does not tight-loop re-claiming the row
    # before any pending step could possibly settle.
    earliest_reprobe_deadline: datetime | None = None

    for ledger in ledger_rows:
        # --- Fail-closed: unknown tool treated as durable ----------------
        spec: ToolSpec | None = registry.get(ledger.tool) if ledger.tool else None
        if spec is None:
            # Unknown tool name (or NULL): assume durable so we probe/escalate
            # rather than silently skipping a possibly-durable write.
            # Document: this is the explicit fail-closed decision per spec §PR-2a.1.
            is_durable_write = True
        else:
            is_durable_write = spec.is_durable_write

        # --- Probe (only meaningful for durable writes) ------------------
        probed: bool = False
        if is_durable_write:
            probed = probe_operation(session, ledger.operation_id)

        # --- Settle-window check -----------------------------------------
        # ledger.updated_at is tz-aware (DateTime(timezone=True)).
        # Ensure `now` is also tz-aware for a valid subtraction.
        updated_at = ledger.updated_at
        if updated_at.tzinfo is None:
            # Defensive: column declared tz-aware; guard anyway.
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        settle_elapsed: bool = (
            (now - updated_at).total_seconds() > settle_window_seconds
        )

        # --- Decision ----------------------------------------------------
        action = decide_recovery(
            ledger_status=ledger.status,
            is_durable_write=is_durable_write,
            probed_committed=probed,
            settle_window_elapsed=settle_elapsed,
        )

        # --- Apply action ------------------------------------------------
        if action == "skip":
            # A durable step already recorded 'committed' means its data
            # landed (decide_recovery returns 'skip' for committed). Track it
            # so the execution resolves to committed_unserved_needs_manual
            # rather than a clean failure / completed.
            if is_durable_write and ledger.status == "committed":
                any_committed_durable = True

        elif action == "mark_committed":
            mark_step_status(
                session,
                execution_id=execution_id,
                step_id=ledger.step_id,
                status="committed",
                now=now,
            )
            any_committed_durable = True  # durable write confirmed landed

        elif action == "mark_failed_needs_manual":
            # ``failed_needs_manual`` is EXECUTION-level, not a valid
            # StepExecutionLedger status.  Do NOT call mark_step_status
            # with it.  Record the flag so the execution is escalated below.
            needs_manual = True

        elif action == "re_probe":
            re_probe_pending = True
            # Earliest time THIS step's settle window elapses. The backoff
            # lease (below) uses the minimum across all re_probe steps so the
            # scanner never re-claims before any pending step could settle.
            step_deadline = updated_at + timedelta(seconds=settle_window_seconds)
            if (
                earliest_reprobe_deadline is None
                or step_deadline < earliest_reprobe_deadline
            ):
                earliest_reprobe_deadline = step_deadline

    # --- Resolve execution status ----------------------------------------
    # The scanner NEVER marks 'completed' (Codex A/B #9c R1, 3 MAJOR): the
    # StepExecutionLedger covers only STARTED steps, so it proves neither that
    # every planned step ran NOR that the user was served.
    #
    # PRIORITY (Codex A/B #9c R2, both MAJOR — mixed-outcome ordering):
    #   1. any_committed_durable → committed_unserved_needs_manual
    #   2. re_probe_pending      → in_progress + backoff lease (keep probing)
    #   3. needs_manual          → failed_needs_manual
    #   4. else                  → failed
    # Committed-durable DOMINATES every other outcome (incl. needs_manual and an
    # unsettled re_probe step): once ANY durable write is known to have
    # committed, the turn is "data changed, serving unproven" — the strongest,
    # most dangerous signal (a naive resend/retry could double-apply), so it
    # must surface immediately. Reviewers split on committed+re_probe (high:
    # keep probing first; medium: committed dominates) — we take committed-first
    # because the pending step's idempotency is independently protected by its
    # operation_id + ON CONFLICT, so terminalizing now causes no data harm, the
    # operator does a manual state re-check anyway, and we avoid indefinite
    # in_progress churn. No alert is fired inline — see RecoverOutcome.alert,
    # fired by run_recovery_scan AFTER commit (Codex A/B #9c R2 MINOR).

    if any_committed_durable:
        execution.execution_status = TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL
        execution.recovery_lease_until = None  # terminal — never re-claim
        session.flush()
        return RecoverOutcome(
            TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL,
            _AlertPayload(
                title="planner recovery: committed_unserved_needs_manual",
                body=(
                    f"execution_id={execution_id}\n"
                    f"A durable write committed but the user's reply was not "
                    f"proven served (worker crashed mid-turn). Manual "
                    f"verification required before any resend.\n"
                    f"See step_execution_ledger for execution_id={execution_id}."
                ),
                dedupe_key=f"planner_recovery_unserved:{execution_id[:16]}",
            ),
        )

    if re_probe_pending:
        # A durable timeout (unknown_pending) has not yet settled and no durable
        # write has committed yet. Leave 'in_progress' and set a BACKOFF lease
        # to the earliest settle deadline — NEVER None, which would let the next
        # scan tick re-claim immediately and tight-loop before any step could
        # settle (Codex A/B #9c R1 MAJOR). A 1s epsilon floor guards a past
        # deadline (clock skew) so the row still becomes claimable.
        epsilon_floor = now + timedelta(seconds=1)
        backoff = earliest_reprobe_deadline or epsilon_floor
        if backoff < epsilon_floor:
            backoff = epsilon_floor
        execution.recovery_lease_until = backoff
        session.flush()
        return RecoverOutcome("re_probe_pending")

    if needs_manual:
        # A durable step's write did NOT land and cannot be safely replayed,
        # and nothing else committed or is pending.
        execution.execution_status = TERMINAL_FAILED_NEEDS_MANUAL
        execution.recovery_lease_until = None  # terminal — never re-claim
        session.flush()
        return RecoverOutcome(
            TERMINAL_FAILED_NEEDS_MANUAL,
            _AlertPayload(
                title="planner recovery: execution failed_needs_manual",
                body=(
                    f"execution_id={execution_id}\n"
                    f"A durable step's write did not commit and could not be "
                    f"safely retried. Manual intervention required.\n"
                    f"See step_execution_ledger for execution_id={execution_id}."
                ),
                dedupe_key=f"planner_recovery_failed:{execution_id[:16]}",
            ),
        )

    # No durable write committed and nothing pending: the turn was interrupted
    # with no data impact. Terminalize as 'failed' (the user was not served;
    # PR-2b's live path surfaces a generic error). NOT 'completed' — serving is
    # never provable from the ledger alone, and an empty ledger (crash before
    # any step started) lands here correctly rather than as false success.
    execution.execution_status = "failed"
    execution.recovery_lease_until = None  # terminal — never re-claim
    session.flush()
    return RecoverOutcome("failed")


# ---------------------------------------------------------------------------
# 3. run_recovery_scan — tick function
# ---------------------------------------------------------------------------


def run_recovery_scan(
    session_factory: Callable[[], Session],
    *,
    worker_id: str,
    now_fn: Callable[[], datetime],
    lease_seconds: int,
    settle_window_seconds: int,
    registry: Mapping[str, ToolSpec],
    alert_fn: Callable[..., None] | None = None,
    limit: int = 10,
) -> dict[str, int]:
    """Run one recovery scan pass.

    Steps:
    1. Open a session, call ``claim_stale_executions``, **commit** the claim
       so the lease is visible to other workers immediately.
    2. For each claimed execution_id open a **fresh session** and call
       ``recover_execution``, then **commit**.  Per-execution isolation
       means one execution's crash or error does not roll back others.
    3. Return a summary dict with counts of each outcome.

    The actual periodic scheduling (interval, worker registration) is
    wired in PR-2b/deploy.

    PR-2b wiring notes (for the live driver):
    - The live executor must set ``execution_status='in_progress'`` and
      populate ``recovery_lease_until`` (to ``now + lease_seconds``) when
      it starts processing a turn, so the scanner can detect it as stalled
      if the worker crashes.
    - ``worker_id`` should be a stable, per-process identifier
      (e.g. ``f"recovery:{socket.gethostname()}:{os.getpid()}"``) to aid
      forensic debugging.
    - ``now_fn`` should return ``datetime.now(timezone.utc)``; injectable
      for tests.

    Parameters
    ----------
    session_factory:
        Zero-argument callable returning a new ``Session``.  Typically a
        ``sessionmaker`` instance.
    worker_id:
        Stable identifier for this scanner worker.
    now_fn:
        Zero-argument callable returning the current tz-aware UTC time.
        Injected for deterministic testing.
    lease_seconds:
        Recovery lease duration.  Reference: 300s from
        ``message_queue.LEASE_DURATION_SEC``.
    settle_window_seconds:
        Settle window for ``unknown_pending`` steps.  Reference: must
        exceed chain timeout (120s) + worst step (90s) ≈ 210s.
    registry:
        Tool registry mapping ``{name: ToolSpec}``.
    alert_fn:
        Optional ``(severity=, title=, body=, dedupe_key=) -> None`` callable
        (mirrors orchestrator._maybe_alert). Fired by THIS function AFTER each
        per-execution commit succeeds — never inline in recover_execution — so
        an alert always reflects committed DB state.
    limit:
        Max executions to claim per scan pass.

    Returns
    -------
    dict[str, int]
        Summary with keys: ``"claimed"``, ``"failed"``,
        ``"failed_needs_manual"``, ``"committed_unserved_needs_manual"``,
        ``"re_probe_pending"``, ``"errored"``. The scanner never reports
        ``"completed"`` — serving / plan-completeness is not provable from the
        ledger, so a recovered execution is at best committed_unserved_needs_manual.
    """
    summary: dict[str, int] = {
        "claimed": 0,
        "failed": 0,
        "failed_needs_manual": 0,
        "committed_unserved_needs_manual": 0,
        "re_probe_pending": 0,
        "errored": 0,
    }

    # --- Phase 1: claim ---------------------------------------------------
    claim_session = session_factory()
    try:
        now = now_fn()
        claimed_ids = claim_stale_executions(
            claim_session,
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
            limit=limit,
        )
        claim_session.commit()
    except Exception:
        logger.exception("run_recovery_scan: claim phase failed")
        claim_session.rollback()
        return summary
    finally:
        claim_session.close()

    summary["claimed"] = len(claimed_ids)

    if not claimed_ids:
        return summary

    # --- Phase 2: recover (per-execution isolation) -----------------------
    for execution_id in claimed_ids:
        exec_session = session_factory()
        try:
            outcome = recover_execution(
                exec_session,
                execution_id=execution_id,
                registry=registry,
                now=now_fn(),
                settle_window_seconds=settle_window_seconds,
            )
            exec_session.commit()
        except Exception:
            logger.exception(
                "run_recovery_scan: recovery failed for execution_id=%r",
                execution_id,
            )
            exec_session.rollback()
            summary["errored"] += 1
            continue
        finally:
            exec_session.close()

        # Fire the alert ONLY AFTER the recovery transaction committed, so we
        # never alert for a terminal state a commit failure rolled back
        # (Codex A/B #9c R2 MINOR). Swallow alert failures — the DB state is
        # already durable; a missed alert must not crash the scan.
        if outcome.alert is not None and alert_fn is not None:
            try:
                alert_fn(
                    severity="P1",
                    title=outcome.alert.title,
                    body=outcome.alert.body,
                    dedupe_key=outcome.alert.dedupe_key,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "run_recovery_scan: admin alert failed for execution_id=%r "
                    "(DB state already committed)",
                    execution_id,
                )

        # Tally the outcome. Every claimed execution lands in exactly one
        # bucket so `claimed == failed + failed_needs_manual +
        # committed_unserved_needs_manual + re_probe_pending + errored`.
        if outcome.status == "failed":
            summary["failed"] += 1
        elif outcome.status == TERMINAL_FAILED_NEEDS_MANUAL:
            summary["failed_needs_manual"] += 1
        elif outcome.status == TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL:
            summary["committed_unserved_needs_manual"] += 1
        elif outcome.status == "re_probe_pending":
            summary["re_probe_pending"] += 1
        else:
            # 'not_found' (execution vanished) or any unexpected status —
            # count as errored rather than silently dropping it (Codex A/B
            # #9c R1 MINOR: the summary must not under-count).
            logger.error(
                "run_recovery_scan: unexpected recovery status %r for "
                "execution_id=%r — counting as errored",
                outcome.status,
                execution_id,
            )
            summary["errored"] += 1

    return summary


__all__ = [
    "RecoverOutcome",
    "claim_stale_executions",
    "recover_execution",
    "run_recovery_scan",
]
