"""Worker dispatcher — consumes ``message_jobs`` and runs the turn.

Sub-A2 of Plan-Execute Epic (#76). Integrates with the existing
``sreda-job-runner`` tick model: ``process_pending(*, limit, now)`` is
the entry point that the job-runner calls on each tick. No new
long-running process is introduced; the queue still flows through
the same systemd service that runs ``housewife_onboarding_worker`` and
``housewife_reminder_worker``.

Each tick:

1. Claim up to ``limit`` jobs (one at a time, to keep the transaction
   per-claim short and let the lease be released quickly on success).
2. For each claimed job: deserialize the saved telegram payload and
   re-run the same ``_process_approved_turn`` that the inline path
   would have invoked. The job is bookkeeping; the actual response
   logic is unchanged.
3. ``mark_done`` on success, ``mark_failed`` (with retry policy) on
   exception. Both are conditional UPDATEs keyed by (worker_id,
   attempt) — slow original worker outcomes cannot land after a retry
   worker has taken over.

When the long-running worker design (LISTEN/NOTIFY wake-up + per-worker
heartbeat) lands in a later sub-issue this module is reused as-is —
only the loop driver changes (job-runner tick vs systemd-template
worker process).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncio

from sreda.db.models import MessageJob
from sreda.db.session import get_session_factory
from sreda.workers.message_queue import (
    HEARTBEAT_INTERVAL_SEC,
    claim_next_job,
    extend_lease,
    mark_done,
    mark_failed,
)

logger = logging.getLogger(__name__)

_WORKER_ID = "job_runner"
"""Worker id used in ``message_jobs`` rows.

Single shared id is fine while we only have one consumer (the job-runner
tick). When the standalone-worker design lands this will become per-process
(``f"message_worker_{n}"``) — until then a single ``job_runner`` id is
enough to expose the fencing token.
"""


async def process_pending(
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> int:
    """Job-runner tick — claim and dispatch up to ``limit`` queued messages.

    Returns the number of jobs that were claimed and processed (success
    or failure). The job-runner uses this as a yield-cooperatively hint —
    if we processed our full limit and there might be more pending, the
    runner will call us again on the next tick. ``0`` means the queue
    was empty.

    The ``now`` parameter is for tests — production passes ``None`` and
    we use ``datetime.now(timezone.utc)``.
    """
    # ``now`` is recomputed per iteration (Codex review MINOR) so a
    # slow first dispatch doesn't give later jobs in this tick a stale
    # / shortened lease. The fixture parameter ``now`` (when provided
    # by tests) overrides only the first iteration's reference point.
    SessionLocal = get_session_factory()
    processed = 0

    while processed < limit:
        when = now or datetime.now(timezone.utc)
        # Short transaction: claim only. Holding the lease implicitly
        # via the row's ``status='processing'`` + ``lease_expires_at``
        # is enough — no need to keep the SQL transaction open while
        # the heavy turn runs.
        with SessionLocal() as session, session.begin():
            job = claim_next_job(session, worker_id=_WORKER_ID, now=when)
            if job is None:
                break
            # Snapshot the fields we need outside the session — the row
            # object will detach after commit.
            job_snapshot = _snapshot_job(job)

        # Codex R1 MAJOR 2026-05-26 — long dispatches need to extend
        # their lease. With LEASE_DURATION_SEC=300 and typical turns
        # in the 5-30s range we never see expiry, but recipe lookups
        # with web_search + LLM fallback can push 90s+, and a worker
        # crash mid-dispatch should release the lease only after the
        # configured timeout — not slip past silently if the loop runs
        # long. Spawn a heartbeat task that extends the lease every
        # HEARTBEAT_INTERVAL_SEC; cancel as soon as dispatch finishes.
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                job_id=job_snapshot.id,
                worker_id=_WORKER_ID,
                attempt=job_snapshot.attempt,
            ),
            name=f"queue_heartbeat:{job_snapshot.id}",
        )
        try:
            await _dispatch(job_snapshot)
            now_after = now or datetime.now(timezone.utc)
            with SessionLocal() as session, session.begin():
                landed = mark_done(
                    session,
                    job_id=job_snapshot.id,
                    worker_id=_WORKER_ID,
                    attempt=job_snapshot.attempt,
                    now=now_after,
                )
                if not landed:
                    logger.warning(
                        "mark_done no-op for job_id=%s — lease was taken by "
                        "another worker, our side-effect may overlap with "
                        "retry (idempotency via operation_id is on the "
                        "per-tool side).",
                        job_snapshot.id,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Message job processing failed: job_id=%s tenant=%s attempt=%s",
                job_snapshot.id,
                job_snapshot.tenant_id,
                job_snapshot.attempt,
            )
            # ``mark_failed`` is itself a DB call and can fail (connection
            # drop, lock timeout). If it does we MUST NOT let the
            # exception escape — that would kill the whole tick and
            # leave the job in ``processing`` with an active lease,
            # blocking the thread's FIFO for the next 300s. Code-review
            # 2026-05-25 MINOR-1.
            now_after = now or datetime.now(timezone.utc)
            try:
                with SessionLocal() as session, session.begin():
                    mark_failed(
                        session,
                        job_id=job_snapshot.id,
                        worker_id=_WORKER_ID,
                        attempt=job_snapshot.attempt,
                        error=_truncate_error(repr(exc)),
                        now=now_after,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mark_failed itself failed for job_id=%s — lease "
                    "will expire in %ds and another worker will pick "
                    "it up via failover",
                    job_snapshot.id,
                    300,  # LEASE_DURATION_SEC, hardcoded to avoid import cycle in log
                )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        processed += 1

    return processed


class _JobSnapshot:
    """Plain values copied out of the ORM row for use after the session closes."""

    __slots__ = (
        "id",
        "tenant_id",
        "thread_id",
        "channel",
        "external_update_id",
        "message_payload",
        "attempt",
    )

    def __init__(
        self,
        *,
        id: str,
        tenant_id: str,
        thread_id: str,
        channel: str,
        external_update_id: str,
        message_payload: dict,
        attempt: int,
    ) -> None:
        self.id = id
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self.channel = channel
        self.external_update_id = external_update_id
        self.message_payload = message_payload
        self.attempt = attempt


def _snapshot_job(job: MessageJob) -> _JobSnapshot:
    return _JobSnapshot(
        id=job.id,
        tenant_id=job.tenant_id,
        thread_id=job.thread_id,
        channel=job.channel,
        external_update_id=job.external_update_id,
        message_payload=dict(job.message_payload or {}),
        attempt=job.attempt,
    )


async def _heartbeat_loop(
    *, job_id: str, worker_id: str, attempt: int
) -> None:
    """Extend the job's lease every ``HEARTBEAT_INTERVAL_SEC``.

    Codex R1 MAJOR 2026-05-26 — if ``_dispatch`` runs longer than
    ``LEASE_DURATION_SEC`` (default 300s), the lease expires and a
    failover worker can claim the same job → double-processing. With a
    healthy heartbeat the lease is continuously pushed forward.

    Conditional UPDATE in ``extend_lease`` returns ``False`` if the row
    has been taken over (attempt token incremented by a failover claim).
    In that case we cancel ourselves — the dispatch will still complete
    but its ``mark_done`` will no-op via the same fencing.

    Cancellation is the normal exit path — the caller cancels this task
    when ``_dispatch`` returns or raises.
    """
    SessionLocal = get_session_factory()
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        except asyncio.CancelledError:
            return
        try:
            with SessionLocal() as session, session.begin():
                still_ours = extend_lease(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    attempt=attempt,
                    now=datetime.now(timezone.utc),
                )
            if not still_ours:
                logger.warning(
                    "Lease lost for job_id=%s during dispatch — failover "
                    "claimed it, our outcome won't land via fencing",
                    job_id,
                )
                return
        except Exception:  # noqa: BLE001
            # Heartbeat is best-effort. If the UPDATE itself fails
            # (DB connection blip), keep trying — short interval means
            # we recover quickly. We don't want a transient hiccup to
            # silently drop the lease.
            logger.exception(
                "heartbeat UPDATE failed for job_id=%s — retrying next tick",
                job_id,
            )


def _truncate_error(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Dispatch — bridges the queue to existing telegram_inbound processing
# ---------------------------------------------------------------------------


async def _dispatch(job: _JobSnapshot) -> None:
    """Route a claimed job to its handler based on ``channel``.

    Currently only ``telegram`` is wired. MAX adds an analogous branch
    that calls into ``services.max_inbound`` when the queue is enabled
    for MAX users.
    """
    kind = job.message_payload.get("kind")
    if job.channel == "telegram" and kind == "telegram_inbound":
        await _dispatch_telegram(job)
        return
    raise RuntimeError(
        f"No dispatcher wired for channel={job.channel!r} "
        f"kind={kind!r} (job_id={job.id!r})"
    )


async def _dispatch_telegram(job: _JobSnapshot) -> None:
    """Re-invoke ``_process_approved_turn`` from worker context.

    We deliberately re-derive ``onboarding`` via
    ``ensure_telegram_user_bundle`` instead of trying to serialize the
    NamedTuple into JSON — the bundle creation is idempotent (handles
    already-existing users gracefully), runs in milliseconds, and keeps
    the queue payload Telegram-shaped (just the raw update dict).

    Crash detection via processing_status (Codex review MAJOR-1):
    ``_process_approved_turn`` catches all exceptions internally and
    logs them — it NEVER re-raises. To detect silent crashes the
    dispatcher reads back the inbound row's ``processing_status`` after
    the call: ``"processed"`` means the success path completed,
    anything else (``"processing_started"`` left behind, or unchanged)
    means the turn crashed and we must report failure to the queue
    so retry / dead_letter can engage.
    """
    from sreda.db.models import InboundMessage
    from sreda.services.telegram_inbound import (
        _process_approved_turn,
        ensure_telegram_user_bundle,
    )

    payload = job.message_payload["payload"]
    bot_key = job.message_payload.get("bot_key", "sreda")
    inbound_message_id = job.message_payload.get("inbound_message_id")

    SessionLocal = get_session_factory()
    # Explicit transaction — ``ensure_telegram_user_bundle`` may create
    # Tenant/Workspace/User rows on first message; without commit those
    # writes get rolled back at ``with`` exit (code-review 2026-05-25
    # MAJOR-1).
    with SessionLocal() as session, session.begin():
        onboarding = ensure_telegram_user_bundle(session, payload)

    # #136: dispatcher MUST mirror the inline path's call shape exactly —
    # ``_process_approved_turn`` is kw-only (bot_key/payload/onboarding/
    # inbound_message_id); a stray ``bot_token=`` raised TypeError on every
    # queue job. bot_token is derived downstream from ``bot_key``, never passed.
    await _process_approved_turn(
        bot_key=bot_key,
        payload=payload,
        onboarding=onboarding,
        inbound_message_id=inbound_message_id,
    )

    # Crash detection — see docstring.
    if inbound_message_id is not None:
        with SessionLocal() as session:
            inbound = session.get(InboundMessage, inbound_message_id)
            status = getattr(inbound, "processing_status", None) if inbound else None
        if status != "processed":
            raise RuntimeError(
                f"_process_approved_turn left inbound_message_id="
                f"{inbound_message_id!r} at processing_status={status!r} "
                f"(expected 'processed'). Treating as silent failure — "
                f"queue will retry or dead_letter per policy."
            )


__all__ = [
    "process_pending",
]
