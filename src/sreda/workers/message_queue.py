"""Per-thread FIFO queue primitives (Sub-A2, Epic #74).

This module owns the low-level ``message_jobs`` operations that any
worker (long-running standalone, or a tick-based job-runner integration)
will compose into a loop:

* ``enqueue_message`` — webhook handler / poller writes one row at
  inbound time; cross-channel ``UNIQUE (channel, external_update_id)``
  collapses redeliveries.
* ``claim_next_job`` — pick the oldest ``pending`` (or expired-lease
  ``processing``) job for a thread that has no other worker active.
  Uses ``FOR UPDATE SKIP LOCKED`` on Postgres for true concurrency;
  falls back to plain SELECT on SQLite (test path).
* ``extend_lease`` — heartbeat. Conditional UPDATE: if another worker
  already took over (via the failover path), our UPDATE rowcount==0 and
  the caller knows to cancel.
* ``mark_done`` / ``mark_failed`` — conditional UPDATE keyed by
  ``(id, worker_id, attempt)``; the ``attempt`` token is the fencing
  bit — a slow original worker's mark cannot land if a retry worker
  already incremented ``attempt``.

Lease fencing math (see Group 1 of the architecture plan):
    lease (300s) > chain timeout (120s) > worst step timeout (90s)
A worker holds a job for at most ``LEASE_DURATION_SEC``; ``HEARTBEAT_INTERVAL_SEC``
extends it while the worker is healthy. After ``MAX_ATTEMPTS`` failures
the job goes to ``dead_letter`` and an admin alert fires.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sreda.db.models import MessageJob

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lease / retry constants
# ---------------------------------------------------------------------------

LEASE_DURATION_SEC = 300
"""How long a worker holds a job before another may take it via failover.

Must exceed ``chain timeout (120s) + worst step timeout (90s) + cleanup
grace`` so a healthy worker never has its lease yanked mid-execution.
"""

HEARTBEAT_INTERVAL_SEC = 60
"""How often a healthy worker extends its lease (well under LEASE_DURATION_SEC)."""

MAX_ATTEMPTS = 3
"""After this many failed attempts a job becomes ``dead_letter``."""


# ---------------------------------------------------------------------------
# Enqueue (webhook / poller side)
# ---------------------------------------------------------------------------


class DuplicateMessageJob(Exception):
    """Inbound event already enqueued via (channel, external_update_id).

    Surfaces the row that won the race so the caller can ack without
    re-processing — Telegram retry, MAX redelivery, etc.
    """

    def __init__(self, existing: MessageJob) -> None:
        super().__init__(
            f"Job for (channel={existing.channel!r}, "
            f"external_update_id={existing.external_update_id!r}) "
            f"already exists as id={existing.id!r}"
        )
        self.existing = existing


def enqueue_message(
    session: Session,
    *,
    tenant_id: str,
    thread_id: str,
    channel: str,
    external_update_id: str,
    message_payload: dict[str, Any],
    now: datetime | None = None,
) -> MessageJob:
    """Insert a new pending job.

    Raises ``DuplicateMessageJob`` if the composite
    ``(channel, external_update_id)`` is already present — the caller
    treats this as success (ack and move on), no re-processing.

    The session is **not committed** here — the caller controls the
    transaction boundary so the enqueue can be batched with whatever
    inbound bookkeeping the webhook handler is doing.
    """
    when = now or datetime.now(timezone.utc)

    # Pre-check before INSERT so we can give the caller a clean
    # DuplicateMessageJob without poisoning the outer transaction. The
    # composite UNIQUE constraint backs us up against the rare race
    # between two webhook handlers enqueuing the same (channel, id) at
    # the same time — in that rare case one will get IntegrityError on
    # flush and the surrounding transaction will need to be retried.
    existing = session.execute(
        select(MessageJob).where(
            MessageJob.channel == channel,
            MessageJob.external_update_id == external_update_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateMessageJob(existing)

    job = MessageJob(
        id=f"job_{uuid4().hex[:24]}",
        tenant_id=tenant_id,
        thread_id=thread_id,
        channel=channel,
        external_update_id=external_update_id,
        message_payload=message_payload,
        status="pending",
        enqueued_at=when,
        attempt=0,
    )
    session.add(job)
    session.flush()
    return job


# ---------------------------------------------------------------------------
# Claim (worker side)
# ---------------------------------------------------------------------------


def claim_next_job(
    session: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = LEASE_DURATION_SEC,
) -> MessageJob | None:
    """Pick up the next job for a thread no other worker is processing.

    Selection rules:

    1. Job must be ``pending`` OR ``processing`` with an expired lease
       (the failover path — the original worker is presumed dead).
    2. No other ``processing`` job exists for that ``thread_id`` with an
       active lease (FIFO per-thread invariant).
    3. Oldest ``enqueued_at`` first across threads (fair-ish — newer
       threads don't starve older ones).

    On claim the row's ``status`` becomes ``processing``, ``worker_id``
    is set, ``attempt`` is incremented, and ``lease_expires_at`` is set
    to ``now + lease_seconds``.

    Returns ``None`` when no claimable job is found.

    The session is **not committed** — caller controls the transaction.
    Tests typically commit immediately so subsequent claims see the
    update.
    """
    when = now or datetime.now(timezone.utc)
    lease_end = when + timedelta(seconds=lease_seconds)

    dialect = session.bind.dialect.name if session.bind else ""

    # Codex R1 MAJOR 2026-05-26 — concurrent workers race on same-thread
    # FIFO. Old SELECT-then-UPDATE let worker A lock job X (older) while
    # worker B picked up Y (later) for the same thread because the
    # NOT EXISTS subquery couldn't see A's uncommitted ``processing``
    # row. The R1 attempt was a single atomic UPDATE … RETURNING with
    # SKIP LOCKED on the inner SELECT — that closes the double-claim
    # window (B's subquery skips A's locked row) but does NOT close the
    # same-thread FIFO window, because skipping A's row still lets
    # NOT EXISTS conclude "no processing for thread T" and pick Y.
    # See ``plans/phase-a-review-r2.md`` for the repro logic.
    #
    # Codex R2 MAJOR 2026-05-26 — proper fix below uses a session-wide
    # advisory_xact_lock so claim transactions are fully serialized.
    # SKIP LOCKED is then redundant (no concurrent claim transactions
    # ever hold message_jobs row locks) and is removed.
    if dialect == "postgresql":
        # Codex R2 MAJOR 2026-05-26 — true FIFO under concurrent workers.
        #
        # Even with UPDATE … RETURNING + FOR UPDATE SKIP LOCKED, two
        # workers can race on same-thread FIFO:
        #   t1: Worker A locks job X (older). A's UPDATE not yet committed.
        #   t2: Worker B's subquery skips locked X, evaluates job Y (later).
        #       NOT EXISTS doesn't see A's uncommitted ``processing`` row
        #       for that thread, so Y looks claimable → B grabs Y.
        #   Result: Y processed before X within the same thread (FIFO violation).
        #
        # Fix: take a session-wide advisory lock for the duration of the
        # claim transaction. Claims are <10ms each so even at 100 workers
        # the contention window is tiny — the queue throughput we care
        # about is bounded by dispatch time (5-90s per turn) not claim
        # time. The lock key is a constant (one global lock for all
        # claim ops); finer-grained per-thread locking would require
        # knowing thread_id before SELECT, which we don't.
        #
        # The advisory_xact_lock auto-releases at commit/rollback, so
        # no manual cleanup is needed.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('message_jobs_claim'))")
        )
        sql = """
            UPDATE message_jobs
            SET status = 'processing',
                started_at = COALESCE(started_at, :now),
                worker_id = :worker,
                attempt = attempt + 1,
                lease_expires_at = :lease_end
            WHERE id = (
                SELECT j.id FROM message_jobs j
                WHERE (j.status = 'pending'
                       OR (j.status = 'processing' AND j.lease_expires_at < :now))
                  AND NOT EXISTS (
                      SELECT 1 FROM message_jobs j2
                      WHERE j2.thread_id = j.thread_id
                        AND j2.status = 'processing'
                        AND j2.lease_expires_at >= :now
                        AND j2.id <> j.id
                  )
                ORDER BY j.enqueued_at
                LIMIT 1
            )
            RETURNING id
        """
        row = session.execute(
            text(sql),
            {"now": when, "lease_end": lease_end, "worker": worker_id},
        ).first()
        if row is None:
            return None
        return session.get(MessageJob, row[0])

    # SQLite test path — no SKIP LOCKED, no concurrent workers. Keep
    # the simpler SELECT-then-UPDATE for SQLite where tests don't
    # exercise the concurrency surface.
    candidate = session.execute(
        text(
            """
            SELECT j.id FROM message_jobs j
            WHERE (j.status = 'pending'
                   OR (j.status = 'processing' AND j.lease_expires_at < :now))
              AND NOT EXISTS (
                  SELECT 1 FROM message_jobs j2
                  WHERE j2.thread_id = j.thread_id
                    AND j2.status = 'processing'
                    AND j2.lease_expires_at >= :now
                    AND j2.id <> j.id
              )
            ORDER BY j.enqueued_at
            LIMIT 1
            """
        ),
        {"now": when},
    ).first()
    if candidate is None:
        return None
    job_id = candidate[0]
    result = session.execute(
        text(
            """
            UPDATE message_jobs
            SET status = 'processing',
                started_at = COALESCE(started_at, :now),
                worker_id = :worker,
                attempt = attempt + 1,
                lease_expires_at = :lease_end
            WHERE id = :id
              AND (
                  status = 'pending'
                  OR (status = 'processing' AND lease_expires_at < :now)
              )
            """
        ),
        {"id": job_id, "worker": worker_id, "now": when, "lease_end": lease_end},
    )
    if result.rowcount == 0:
        return None
    return session.get(MessageJob, job_id)


# ---------------------------------------------------------------------------
# Lease management
# ---------------------------------------------------------------------------


def extend_lease(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    attempt: int,
    now: datetime | None = None,
    lease_seconds: int = LEASE_DURATION_SEC,
) -> bool:
    """Heartbeat — push ``lease_expires_at`` forward.

    Conditional on ``(id, worker_id, attempt)`` so if a retry worker has
    already claimed the job (incrementing ``attempt``), our UPDATE
    rowcount is 0 and the caller learns the lease was lost.

    Returns ``True`` if the lease was extended, ``False`` if another
    worker has taken over.
    """
    when = now or datetime.now(timezone.utc)
    lease_end = when + timedelta(seconds=lease_seconds)
    result = session.execute(
        text(
            """
            UPDATE message_jobs
            SET lease_expires_at = :lease_end
            WHERE id = :id
              AND worker_id = :worker
              AND attempt = :attempt
              AND status = 'processing'
            """
        ),
        {
            "id": job_id,
            "worker": worker_id,
            "attempt": attempt,
            "lease_end": lease_end,
        },
    )
    return bool(result.rowcount)


# ---------------------------------------------------------------------------
# Mark done / failed
# ---------------------------------------------------------------------------


def mark_done(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    attempt: int,
    now: datetime | None = None,
) -> bool:
    """Mark job done. Conditional on the fencing token (worker_id, attempt).

    Returns ``True`` if the row transitioned to ``done``; ``False`` if a
    retry worker already took over (the caller should NOT trigger any
    side effects because someone else is in charge now).
    """
    when = now or datetime.now(timezone.utc)
    result = session.execute(
        text(
            """
            UPDATE message_jobs
            SET status = 'done',
                finished_at = :now,
                lease_expires_at = NULL
            WHERE id = :id
              AND worker_id = :worker
              AND attempt = :attempt
              AND status = 'processing'
            """
        ),
        {
            "id": job_id,
            "worker": worker_id,
            "attempt": attempt,
            "now": when,
        },
    )
    return bool(result.rowcount)


def mark_failed(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    attempt: int,
    error: str,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    """Mark this attempt failed. Decides between retryable and dead_letter.

    If ``attempt < max_attempts``: the row goes back to ``pending`` so
    the next claim tick (or the next worker) can retry. ``started_at`` /
    ``finished_at`` / ``lease_expires_at`` are cleared because pending
    rows must satisfy the timestamp CHECK constraint.

    If ``attempt >= max_attempts``: the row is terminal — status
    ``dead_letter``, ``finished_at`` set, alert the admin.

    Returns ``True`` if the transition happened. ``False`` means a retry
    worker already took over and our outcome should be discarded.
    """
    when = now or datetime.now(timezone.utc)
    if attempt < max_attempts:
        # Retry path — back to pending, clear timestamps to satisfy CHECK.
        result = session.execute(
            text(
                """
                UPDATE message_jobs
                SET status = 'pending',
                    started_at = NULL,
                    finished_at = NULL,
                    lease_expires_at = NULL,
                    worker_id = NULL,
                    last_error = :err
                WHERE id = :id
                  AND worker_id = :worker
                  AND attempt = :attempt
                  AND status = 'processing'
                """
            ),
            {
                "id": job_id,
                "worker": worker_id,
                "attempt": attempt,
                "err": error,
            },
        )
    else:
        # Dead-letter path — terminal.
        result = session.execute(
            text(
                """
                UPDATE message_jobs
                SET status = 'dead_letter',
                    finished_at = :now,
                    lease_expires_at = NULL,
                    last_error = :err
                WHERE id = :id
                  AND worker_id = :worker
                  AND attempt = :attempt
                  AND status = 'processing'
                """
            ),
            {
                "id": job_id,
                "worker": worker_id,
                "attempt": attempt,
                "now": when,
                "err": error,
            },
        )
    return bool(result.rowcount)


def derive_thread_key(
    tenant_id: str, channel: str, external_chat_id: str
) -> str:
    """Deterministic FIFO key for one chat.

    Two messages from the same (tenant, channel, chat) get the same
    ``thread_key`` and are therefore serialized by the per-thread FIFO.
    Different chats of the same tenant produce different keys and can
    run in parallel.

    The key intentionally does NOT match ``AgentThread.id`` (which is
    a UUID-based random id) — ``message_jobs.thread_id`` and
    ``agent_threads.id`` are separate identifiers serving different
    purposes:

    - ``message_jobs.thread_id`` — queue's FIFO grouping bucket
    - ``agent_threads.id``       — physical channel container row

    Deriving instead of looking up keeps the enqueue path inside the
    webhook's 10-ms budget — no DB query just to compute a key.

    Separator invariant (code-review 2026-05-25 MINOR-2): we rely on
    ``::`` not appearing in any component. Currently safe — tenant_id
    is ``tenant_{channel_prefix}_{numeric}``, channel is a short ASCII
    enum (``telegram``/``max``), external_chat_id is a numeric string
    from the messenger. If any of these formats expand to allow ``::``,
    switch to a hash or use a control-byte separator.
    """
    return f"{tenant_id}::{channel}::{external_chat_id}"


def is_queue_enabled_for(tenant_id: str) -> bool:
    """Whether ``tenant_id`` should go through the new FIFO queue.

    Reads ``SREDA_MESSAGE_QUEUE_ENABLED_TENANTS`` from settings. Empty
    whitelist (default) returns ``False`` for everyone — current legacy
    inline behaviour is preserved.

    The poller calls this on each inbound message:
        if is_queue_enabled_for(tenant_id):
            enqueue_message(...)
        else:
            # existing handlers.chat() inline path
            ...

    Same-process import is fine — ``get_settings()`` is cached so this
    is a frozenset membership check per call.
    """
    from sreda.config.settings import get_settings

    return tenant_id in get_settings().message_queue_enabled_tenants


__all__ = [
    "DuplicateMessageJob",
    "HEARTBEAT_INTERVAL_SEC",
    "LEASE_DURATION_SEC",
    "MAX_ATTEMPTS",
    "claim_next_job",
    "derive_thread_key",
    "enqueue_message",
    "extend_lease",
    "is_queue_enabled_for",
    "mark_done",
    "mark_failed",
]
