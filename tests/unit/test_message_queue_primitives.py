"""Tests for ``workers.message_queue`` primitives (Sub-A2, Epic #74).

Critical invariants covered:

- **FIFO per thread**: oldest pending job for an idle thread is claimed
  first; threads with an active lease are skipped over.
- **Parallel across threads**: two workers on two different threads of
  the same tenant must both succeed in the same tick.
- **Lease fencing**: ``mark_done`` / ``mark_failed`` / ``extend_lease``
  are conditional on ``(worker_id, attempt)`` — a slow original worker
  cannot land its outcome after a retry worker has taken over.
- **Failover**: an expired lease is claimable by another worker; the
  ``attempt`` token is bumped so the slow original's UPDATE will no-op.
- **Retry → dead_letter**: ``mark_failed`` puts back to pending until
  ``MAX_ATTEMPTS`` is reached, then transitions to ``dead_letter``.
- **Cross-channel idempotency**: ``enqueue_message`` raises
  ``DuplicateMessageJob`` instead of failing silently when the same
  ``(channel, external_update_id)`` is enqueued twice.

The Postgres-only ``FOR UPDATE SKIP LOCKED`` concurrency property is
not exercised here — these tests run on SQLite. They prove the SQL
shape is correct; production concurrency lands in integration tests
once the worker loop integrates with real Postgres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from sreda.db.models import MessageJob
from sreda.workers.message_queue import (
    DuplicateMessageJob,
    LEASE_DURATION_SEC,
    MAX_ATTEMPTS,
    claim_next_job,
    enqueue_message,
    extend_lease,
    mark_done,
    mark_failed,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enqueue(
    session: Session,
    *,
    thread_id: str = "thread_t1",
    tenant_id: str = "tenant_t1",
    channel: str = "telegram",
    external_update_id: str,
    enqueued_at: datetime | None = None,
) -> MessageJob:
    return enqueue_message(
        session,
        tenant_id=tenant_id,
        thread_id=thread_id,
        channel=channel,
        external_update_id=external_update_id,
        message_payload={"text": "x"},
        now=enqueued_at or _now(),
    )


# ---------------------------------------------------------------------------
# enqueue_message
# ---------------------------------------------------------------------------


def test_enqueue_message_creates_pending_row(db_session: Session) -> None:
    job = _enqueue(db_session, external_update_id="100")
    db_session.commit()
    assert job.id.startswith("job_")
    assert job.status == "pending"
    assert job.attempt == 0
    assert job.thread_id == "thread_t1"


def test_enqueue_duplicate_channel_update_id_raises(db_session: Session) -> None:
    _enqueue(db_session, external_update_id="200")
    db_session.commit()
    with pytest.raises(DuplicateMessageJob) as exc:
        _enqueue(db_session, external_update_id="200")
    assert exc.value.existing.external_update_id == "200"


def test_enqueue_same_update_id_different_channels_both_succeed(db_session: Session) -> None:
    _enqueue(db_session, channel="telegram", external_update_id="300")
    _enqueue(db_session, channel="max", external_update_id="300")
    db_session.commit()
    rows = db_session.query(MessageJob).filter_by(external_update_id="300").all()
    channels = {r.channel for r in rows}
    assert channels == {"telegram", "max"}


# ---------------------------------------------------------------------------
# claim_next_job — FIFO per thread + parallel across threads
# ---------------------------------------------------------------------------


def test_claim_picks_oldest_pending_first(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    _enqueue(
        db_session,
        thread_id="thread_t2",  # different thread so the second is claimable
        external_update_id="B",
        enqueued_at=t0 - timedelta(seconds=10),  # older
    )
    db_session.commit()

    claimed = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert claimed is not None
    assert claimed.external_update_id == "B"  # older one wins


def test_claim_returns_none_when_queue_empty(db_session: Session) -> None:
    assert claim_next_job(db_session, worker_id="w1") is None


def test_claim_serializes_same_thread(db_session: Session) -> None:
    """Second job of the same thread must NOT be claimable while the
    first is processing with an active lease."""
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    _enqueue(
        db_session,
        external_update_id="B",
        enqueued_at=t0 + timedelta(seconds=1),
    )
    db_session.commit()

    first = claim_next_job(db_session, worker_id="w1", now=t0 + timedelta(seconds=2))
    db_session.commit()
    assert first is not None

    second = claim_next_job(
        db_session, worker_id="w2", now=t0 + timedelta(seconds=3)
    )
    assert second is None, (
        "Second job of the same thread should not be claimable while "
        "first is processing"
    )


def test_claim_parallel_across_threads(db_session: Session) -> None:
    """Two threads of the same tenant should both be claimable."""
    t0 = _now()
    _enqueue(db_session, thread_id="thread_t1_dm", external_update_id="A", enqueued_at=t0)
    _enqueue(
        db_session,
        thread_id="thread_t1_group",
        external_update_id="B",
        enqueued_at=t0 + timedelta(seconds=1),
    )
    db_session.commit()

    first = claim_next_job(db_session, worker_id="w1", now=t0 + timedelta(seconds=2))
    db_session.commit()
    second = claim_next_job(
        db_session, worker_id="w2", now=t0 + timedelta(seconds=3)
    )
    db_session.commit()

    assert first is not None
    assert second is not None
    assert {first.thread_id, second.thread_id} == {"thread_t1_dm", "thread_t1_group"}


def test_claim_sets_attempt_and_lease(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()

    claimed = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()

    assert claimed is not None
    assert claimed.status == "processing"
    assert claimed.worker_id == "w1"
    assert claimed.attempt == 1
    assert claimed.lease_expires_at is not None
    # Lease end ≈ now + LEASE_DURATION_SEC
    delta = claimed.lease_expires_at - t0
    assert delta == timedelta(seconds=LEASE_DURATION_SEC)


# ---------------------------------------------------------------------------
# Failover — expired lease becomes claimable; attempt token bumps
# ---------------------------------------------------------------------------


def test_expired_lease_is_claimable(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()

    first = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert first is not None
    assert first.attempt == 1

    # Forward "now" past the lease end
    failover_time = t0 + timedelta(seconds=LEASE_DURATION_SEC + 1)
    second = claim_next_job(db_session, worker_id="w2", now=failover_time)
    db_session.commit()

    assert second is not None
    assert second.id == first.id  # same row, not a duplicate
    assert second.worker_id == "w2"
    assert second.attempt == 2  # fencing token incremented


# ---------------------------------------------------------------------------
# extend_lease — heartbeat fencing
# ---------------------------------------------------------------------------


def test_extend_lease_pushes_expiration_forward(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    claimed = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert claimed is not None
    original_lease = claimed.lease_expires_at

    later = t0 + timedelta(seconds=60)
    ok = extend_lease(
        db_session,
        job_id=claimed.id,
        worker_id="w1",
        attempt=claimed.attempt,
        now=later,
    )
    db_session.commit()
    assert ok is True

    db_session.refresh(claimed)
    assert claimed.lease_expires_at == later + timedelta(seconds=LEASE_DURATION_SEC)
    assert claimed.lease_expires_at > original_lease  # pushed forward


def test_extend_lease_fails_after_failover(db_session: Session) -> None:
    """Slow worker tries to extend its lease but a retry has taken over."""
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    first = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert first is not None

    failover_time = t0 + timedelta(seconds=LEASE_DURATION_SEC + 1)
    claim_next_job(db_session, worker_id="w2", now=failover_time)
    db_session.commit()

    # w1 still thinks it has the job — tries to extend
    ok = extend_lease(
        db_session,
        job_id=first.id,
        worker_id="w1",
        attempt=first.attempt,  # still 1, but the row is now at attempt=2
        now=failover_time + timedelta(seconds=1),
    )
    assert ok is False, "Slow worker's lease extension should not land"


# ---------------------------------------------------------------------------
# mark_done — fencing token guards
# ---------------------------------------------------------------------------


def test_mark_done_transitions_processing_to_done(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    claimed = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert claimed is not None

    ok = mark_done(
        db_session,
        job_id=claimed.id,
        worker_id="w1",
        attempt=claimed.attempt,
        now=t0 + timedelta(seconds=30),
    )
    db_session.commit()
    assert ok is True
    db_session.refresh(claimed)
    assert claimed.status == "done"
    assert claimed.finished_at is not None
    assert claimed.lease_expires_at is None


def test_mark_done_after_failover_does_not_land(db_session: Session) -> None:
    """Original worker tries to mark done after retry has taken over."""
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    first = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert first is not None
    failover = t0 + timedelta(seconds=LEASE_DURATION_SEC + 1)
    claim_next_job(db_session, worker_id="w2", now=failover)
    db_session.commit()

    # w1 finally finishes — tries to mark done with its stale attempt token
    ok = mark_done(
        db_session,
        job_id=first.id,
        worker_id="w1",
        attempt=first.attempt,  # 1, row is now at 2
        now=failover + timedelta(seconds=1),
    )
    assert ok is False, "Slow worker's mark_done must not land"


# ---------------------------------------------------------------------------
# mark_failed — retry vs dead_letter
# ---------------------------------------------------------------------------


def test_mark_failed_below_max_attempts_returns_to_pending(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    claimed = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert claimed is not None
    assert claimed.attempt == 1

    ok = mark_failed(
        db_session,
        job_id=claimed.id,
        worker_id="w1",
        attempt=claimed.attempt,
        error="transient_network_error",
        now=t0 + timedelta(seconds=10),
    )
    db_session.commit()
    assert ok is True
    db_session.refresh(claimed)
    assert claimed.status == "pending"
    assert claimed.started_at is None  # cleared so pending CHECK passes
    assert claimed.finished_at is None
    assert claimed.lease_expires_at is None
    assert claimed.last_error == "transient_network_error"


def test_mark_failed_at_max_attempts_dead_letters(db_session: Session) -> None:
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    claimed = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert claimed is not None

    ok = mark_failed(
        db_session,
        job_id=claimed.id,
        worker_id="w1",
        attempt=claimed.attempt,
        error="provider_outage",
        now=t0 + timedelta(seconds=10),
        max_attempts=1,  # MAX_ATTEMPTS=1 → first failure is terminal
    )
    db_session.commit()
    assert ok is True
    db_session.refresh(claimed)
    assert claimed.status == "dead_letter"
    assert claimed.finished_at is not None
    assert claimed.last_error == "provider_outage"


def test_mark_failed_after_failover_does_not_land(db_session: Session) -> None:
    """Slow worker tries to fail after retry already took over."""
    t0 = _now()
    _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    first = claim_next_job(db_session, worker_id="w1", now=t0)
    db_session.commit()
    assert first is not None
    failover = t0 + timedelta(seconds=LEASE_DURATION_SEC + 1)
    claim_next_job(db_session, worker_id="w2", now=failover)
    db_session.commit()

    ok = mark_failed(
        db_session,
        job_id=first.id,
        worker_id="w1",
        attempt=first.attempt,
        error="took_too_long",
        now=failover + timedelta(seconds=1),
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Retry loop — pending → claim → fail → pending → claim again
# ---------------------------------------------------------------------------


def test_retry_loop_eventually_dead_letters(db_session: Session) -> None:
    """Drive a job through MAX_ATTEMPTS failures and assert it terminates."""
    t0 = _now()
    job = _enqueue(db_session, external_update_id="A", enqueued_at=t0)
    db_session.commit()
    job_id = job.id

    for i in range(MAX_ATTEMPTS):
        claimed = claim_next_job(db_session, worker_id=f"w{i}", now=t0 + timedelta(seconds=i))
        db_session.commit()
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.attempt == i + 1
        ok = mark_failed(
            db_session,
            job_id=claimed.id,
            worker_id=f"w{i}",
            attempt=claimed.attempt,
            error=f"fail_{i}",
            now=t0 + timedelta(seconds=i, milliseconds=500),
        )
        db_session.commit()
        assert ok is True

    job_row = db_session.get(MessageJob, job_id)
    assert job_row is not None
    assert job_row.status == "dead_letter"
    assert job_row.last_error == f"fail_{MAX_ATTEMPTS - 1}"
