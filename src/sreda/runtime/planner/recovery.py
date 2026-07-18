"""Recovery decision primitives for the Sub-A12 Phase E crash-safety scanner.

This module is the DECISION layer consumed by the #9c scanner.  It has two
responsibilities:

  1. ``probe_operation`` — ask the DB "did this durable write commit?".
     It answers by checking ``audit_outbox`` and ``user_data_change_feed``
     for a row with the given ``operation_id``.  The crash-safety linchpin:
     ``emit_event()`` writes the audit row IN THE SAME TRANSACTION as the
     tool's data mutation, so:

         probe == True  ⟺  the durable write committed
         probe == False ⟺  the durable write did NOT commit

  2. ``decide_recovery`` — a PURE function (no DB, no clock) that maps
     ledger state → a ``RecoveryAction``.  The scanner calls this after
     gathering all inputs; it then applies the action via step_ledger helpers.

Design constraints:
  - ``decide_recovery`` NEVER mutates state; the caller (#9c) is responsible
    for translating the returned action into ledger + execution-status writes.
  - Neither function blind-retries a failed durable write.  When probe==False
    for a durable step, the correct action is ``"mark_failed_needs_manual"``
    so the failure surfaces to the user and admin rather than being silently
    replayed with an unknown side-effect risk.
  - ``"re_probe"`` is the only non-terminal action; it means "the
    unknown_pending settle window has not elapsed — wait and probe again."
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from sreda.db.models.audit_feed import AuditOutboxEvent, UserDataChangeFeedEvent


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Execution-level terminal status written when a durable step's write did NOT
# commit (probe==False) and no automated retry is safe.  Surfaces to the user
# as generic_tool_error and triggers an admin alert.
TERMINAL_FAILED_NEEDS_MANUAL: str = "failed_needs_manual"

# Execution-level terminal status written by the PR-2b outbound send path
# (not by this module) when a committed durable step's response could not be
# delivered to the user.  Defined here for completeness so all crash-safety
# terminal constants live in one place.
TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL: str = "committed_unserved_needs_manual"

# RecoveryAction — all four leaf values the scanner may receive from
# decide_recovery.
RecoveryAction = Literal[
    "skip",                     # step is already terminal or has no side effects
    "mark_committed",           # probe confirmed the write landed; record it
    "mark_failed_needs_manual", # probe says write did NOT commit; escalate
    "re_probe",                 # settle window not elapsed; probe again later
]


# ---------------------------------------------------------------------------
# probe_operation
# ---------------------------------------------------------------------------

def probe_operation(session: Session, operation_id: str) -> bool:
    """Return True iff a row with *operation_id* exists in audit_outbox OR
    user_data_change_feed.

    Crash-safety linchpin invariant
    --------------------------------
    ``emit_event()`` writes the audit outbox row in the SAME database
    transaction as the tool's durable data mutation.  Therefore:

        probe_operation(...) == True   ⟺   the durable write committed
        probe_operation(...) == False  ⟺   the durable write did NOT commit

    The relay worker moves rows from ``audit_outbox`` → ``user_data_change_feed``
    asynchronously, so a committed operation_id may appear in either table
    depending on whether the relay has run.  We check both, short-circuiting
    on the first hit.

    Parameters
    ----------
    session:
        An active SQLAlchemy session (read-only usage; no writes, no flush).
    operation_id:
        The opaque idempotency key stored in StepExecutionLedger.operation_id.

    Returns
    -------
    bool
        True if the operation committed (row found in either audit table).
    """
    # NO-AUTOFLUSH (Codex A/B #9a R1 — medium MAJOR / high MINOR): a bare
    # ``session.execute(select(...))`` triggers autoflush by default. If the
    # scanner's session ever held a pending (uncommitted) audit/feed row for
    # this same operation_id, autoflush would push it to the DB and the SELECT
    # would then observe it — a FALSE-True that breaks the linchpin invariant
    # (probe==True must mean *previously committed*, not "flushed by probe").
    # Wrapping in no_autoflush keeps the probe strictly read-only: it sees only
    # what was already committed/flushed before this call.
    with session.no_autoflush:
        # Check audit_outbox first — the common case for a recent write is that
        # the relay has not yet moved the row.
        in_outbox = session.execute(
            select(AuditOutboxEvent.id).where(
                AuditOutboxEvent.operation_id == operation_id
            ).limit(1)
        ).first() is not None

        if in_outbox:
            # Short-circuit: do NOT query the feed once the outbox hit.
            return True

        # Fall through to the durable feed — the relay may have already moved it.
        in_feed = session.execute(
            select(UserDataChangeFeedEvent.id).where(
                UserDataChangeFeedEvent.operation_id == operation_id
            ).limit(1)
        ).first() is not None

    return in_feed


# ---------------------------------------------------------------------------
# decide_recovery
# ---------------------------------------------------------------------------

def decide_recovery(
    *,
    ledger_status: str,
    is_durable_write: bool,
    probed_committed: bool,
    settle_window_elapsed: bool,
) -> RecoveryAction:
    """Map ledger state to a recovery action.  PURE — no DB, no clock.

    Parameters
    ----------
    ledger_status:
        The current ``StepExecutionLedger.status`` value.
        Valid values: ``'started'``, ``'committed'``, ``'unknown'``,
        ``'unknown_pending'``.
    is_durable_write:
        ``True`` when the step's tool spec has ``is_durable_write=True``
        (i.e. the tool mutates external or DB state that cannot be undone
        by simply not doing anything).
    probed_committed:
        The result of ``probe_operation()`` for this step's operation_id.
        Only semantically meaningful when ``is_durable_write=True``; for
        non-durable steps the scanner may pass any value — it is ignored.
    settle_window_elapsed:
        ``True`` when the settle window has passed, meaning the background
        tool thread can provably no longer commit (its deadline is in the
        past).  Relevant for ``ledger_status == 'unknown_pending'`` AND
        (audit 2026-07-18, planner-exec MAJOR) ``ledger_status ==
        'started'`` — a started-but-unterminated step may also have a live
        ``asyncio.to_thread`` tool still in flight (worker alive but stuck
        past its recovery lease), so terminalizing before the window
        expires would declare «write did NOT commit» while the thread can
        still land the write seconds later → user retry = duplicate write.

    Returns
    -------
    RecoveryAction
        One of ``"skip"``, ``"mark_committed"``,
        ``"mark_failed_needs_manual"``, or ``"re_probe"``.

    Raises
    ------
    ValueError
        For any unrecognised *ledger_status* — fail-closed so an unexpected
        status never silently resolves to "skip".

    Decision matrix
    ---------------
    committed
        → skip
        WHY: the step is already recorded as terminal-done; nothing to do.

    started, non-durable
        → skip
        WHY: a read / request-local step has no side effect to recover.
        There is no write to confirm or roll back; the step is idempotent
        from a data-safety perspective.

    started, durable, probe==True
        → mark_committed
        WHY: the audit row exists, so the write landed despite the ledger
        not being updated to 'committed' (the worker crashed between the
        tool call and the ledger write).  Record the fact.

    started, durable, probe==False, settle_window_elapsed==False
        → re_probe
        WHY (audit 2026-07-18, planner-exec MAJOR): symmetric to
        unknown_pending.  probe==False only proves the write had not
        committed AT PROBE TIME; a stuck-but-alive worker's
        ``asyncio.to_thread`` tool (uncancellable by design,
        executor.py) may still commit within the settle window.
        Terminalizing now would create a false «write did NOT commit» →
        the user retries → duplicate write.  Wait; the scanner revisits
        after the window.

    started, durable, probe==False, settle_window_elapsed==True
        → mark_failed_needs_manual
        WHY: probe==False after the settle window proves the write did
        NOT commit (same-txn invariant + no live thread can remain).
        The plan FORBIDS blind replay — we cannot know whether the
        user's intent has changed, and auto-retrying a durable write
        may cause duplicate side effects if any part of the system
        already partially executed.  Surface to user (generic_tool_error)
        + admin.

    unknown, non-durable
        → skip
        WHY: defensive; we should only mark a step 'unknown' for durable
        writes.  A non-durable step with ledger status 'unknown' is
        anomalous but harmless — skip rather than escalate.

    unknown, durable, probe==True
        → mark_committed
        WHY: same reasoning as started+durable+probe==True.

    unknown, durable, probe==False
        → mark_failed_needs_manual
        WHY: same reasoning as started+durable+probe==False.

    unknown_pending, probe==True
        → mark_committed
        WHY: the write committed while the worker was still running (or just
        after the timeout was recorded).  Settle-window state is irrelevant;
        the audit row is conclusive.

    unknown_pending, probe==False, settle_window_elapsed==True
        → mark_failed_needs_manual
        WHY: the settle window has expired, so the background thread cannot
        commit any more.  probe==False + window elapsed = the write
        definitively did not commit.  Escalate.

    unknown_pending, probe==False, settle_window_elapsed==False
        → re_probe
        WHY: the tool thread may still be running and may yet commit.
        Declaring terminal-not-landed BEFORE the window expires would create
        a false failure.  Wait; the scanner should revisit after the window.
    """
    if ledger_status == "committed":
        # Already the terminal "done" state.
        return "skip"

    if ledger_status == "started":
        if not is_durable_write:
            # No external side effect — nothing to recover.
            return "skip"
        # Durable write: authority is the audit probe.
        if probed_committed:
            return "mark_committed"
        # audit 2026-07-18 (planner-exec MAJOR): same settle-window
        # semantics as unknown_pending — a stuck worker's to_thread tool
        # may still commit while the window is open.
        if not settle_window_elapsed:
            return "re_probe"
        return "mark_failed_needs_manual"

    if ledger_status == "unknown":
        if not is_durable_write:
            # Anomalous but safe to ignore for non-durable steps.
            return "skip"
        if probed_committed:
            return "mark_committed"
        return "mark_failed_needs_manual"

    if ledger_status == "unknown_pending":
        if not is_durable_write:
            # Defensive (Codex A/B #9a R1 — medium MINOR): the executor only
            # marks DURABLE steps 'unknown_pending'. A non-durable step in this
            # status is anomalous and has no side effect to recover — skip,
            # consistent with the started/unknown non-durable branches. This
            # also prevents escalating a side-effect-free step to
            # failed_needs_manual on anomalous input.
            return "skip"
        # Durable write: probe is authoritative regardless of settle state.
        if probed_committed:
            return "mark_committed"
        if settle_window_elapsed:
            # Window passed: background thread cannot commit anymore.
            return "mark_failed_needs_manual"
        # Window still open: a late commit may still arrive.
        return "re_probe"

    # Unrecognised status — fail-closed.
    raise ValueError(
        f"decide_recovery: unrecognised ledger_status={ledger_status!r}. "
        "Expected one of 'started', 'committed', 'unknown', 'unknown_pending'."
    )


__all__ = [
    "probe_operation",
    "decide_recovery",
    "RecoveryAction",
    "TERMINAL_FAILED_NEEDS_MANUAL",
    "TERMINAL_COMMITTED_UNSERVED_NEEDS_MANUAL",
]
