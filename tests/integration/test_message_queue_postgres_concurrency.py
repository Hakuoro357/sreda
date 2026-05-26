"""Postgres-only concurrency tests for ``claim_next_job`` (Codex R3 MAJOR).

The Phase A unit tests (``tests/unit/test_message_queue_primitives.py``)
run against SQLite, which cannot exercise the race conditions that the
production code is designed to handle:

  1. Two workers simultaneously holding open transactions against the
     same ``message_jobs`` row — only Postgres has row-level locking
     with visibility semantics that match production.
  2. ``pg_advisory_xact_lock`` and ``FOR UPDATE`` — SQLite doesn't
     model these at all.

The unit-test ``test_claim_serializes_same_thread`` only verifies the
SQL-level invariant (a single connection that commits after each claim
shouldn't see the second job as claimable) but not the cross-worker
concurrency invariant that motivated the fix.

These integration tests close that gap. They require a real Postgres
instance:

  $ SREDA_TEST_POSTGRES_URL=postgresql://user:pw@localhost/sreda_test \
    .venv/Scripts/python.exe -m pytest tests/integration/test_message_queue_postgres_concurrency.py -v

When the env var is unset (default in CI without infrastructure), the
whole module is skipped — the unit-test suite stays green without
needing Postgres.

Codex R3 reference: ``plans/phase-a-review-r3.md``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from sreda.db.models import MessageJob
from sreda.workers.message_queue import claim_next_job

# Skip the entire module if no Postgres test URL is configured.
_POSTGRES_URL = os.environ.get("SREDA_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _POSTGRES_URL,
    reason=(
        "SREDA_TEST_POSTGRES_URL not set — integration tests require a "
        "real Postgres instance. Set it to a connection string of a "
        "throwaway test database to run these tests."
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def engine():
    """Per-test Postgres engine. Each test gets a fresh ``message_jobs``
    table to avoid bleed-through.

    We re-create the table here rather than relying on Alembic so the
    test is self-contained and can be pointed at any empty schema."""
    eng = create_engine(_POSTGRES_URL, echo=False, future=True)
    with eng.begin() as conn:
        # Drop & recreate just message_jobs for isolation.
        conn.execute(text("DROP TABLE IF EXISTS message_jobs"))
        MessageJob.__table__.create(conn)
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS message_jobs"))
    eng.dispose()


@pytest.fixture
def make_session(engine):
    """Factory for new sessions — each call returns a fresh Session
    bound to its own connection so we can model two workers
    concurrently."""
    SessionFactory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    sessions: list[Session] = []

    def _make() -> Session:
        s = SessionFactory()
        sessions.append(s)
        return s

    yield _make
    for s in sessions:
        s.close()


def _enqueue(
    session: Session,
    *,
    thread_id: str = "thread_t1",
    external_update_id: str,
    enqueued_at: datetime | None = None,
) -> MessageJob:
    job = MessageJob(
        id=f"job_{external_update_id}",
        tenant_id="t1",
        thread_id=thread_id,
        channel="telegram",
        external_update_id=external_update_id,
        message_payload={},
        status="pending",
        enqueued_at=enqueued_at or _now(),
        attempt=0,
    )
    session.add(job)
    return job


def test_same_thread_fifo_under_concurrent_workers(make_session):
    """Codex R3 — true FIFO under concurrent workers.

    Repro of the bug that R2 review caught:
      Thread T has pending A (older) and pending B (later).
      Without advisory lock + FOR UPDATE, Worker 1 could lock A,
      Worker 2 could simultaneously claim B for the same thread.

    Expected with the R3 fix: Worker 2 blocks on the advisory lock
    until Worker 1's claim commits. Worker 2 then sees A as
    ``processing`` for thread T, NOT EXISTS excludes B, so B is
    not claimed (or only claimable after A finishes).
    """
    setup = make_session()
    t0 = _now()
    _enqueue(setup, external_update_id="A", enqueued_at=t0)
    _enqueue(
        setup, external_update_id="B", enqueued_at=t0 + timedelta(seconds=1)
    )
    setup.commit()
    setup.close()

    worker1 = make_session()
    worker2 = make_session()

    # Worker 1 begins claim transaction, holds open without committing.
    worker1.begin()
    first = claim_next_job(worker1, worker_id="w1", now=t0 + timedelta(seconds=2))
    assert first is not None
    assert first.external_update_id == "A"

    # Worker 2 attempts claim in parallel. With advisory_xact_lock in
    # place, this should block on Worker 1's lock. We test the
    # invariant by giving Worker 2 a strict statement_timeout — if
    # the lock holds, Worker 2 errors out (proving it blocked); if
    # there's no lock, Worker 2 races through and claims B (proving
    # the bug).
    worker2.begin()
    worker2.execute(text("SET LOCAL statement_timeout = '500ms'"))

    blocked = False
    try:
        claim_next_job(worker2, worker_id="w2", now=t0 + timedelta(seconds=3))
    except Exception as exc:  # noqa: BLE001
        # Expect "canceling statement due to statement timeout" —
        # worker 2 was waiting on the advisory lock or row lock
        # and got killed by the timeout. That's the success signal.
        if "statement timeout" in str(exc).lower() or "57014" in str(exc):
            blocked = True
        else:
            raise
    finally:
        worker2.rollback()
        worker1.commit()
        worker2.close()
        worker1.close()

    assert blocked, (
        "Worker 2's claim_next_job did NOT block on Worker 1's "
        "uncommitted claim transaction. The FIFO race is still open."
    )


def test_heartbeat_race_does_not_resurrect_job(make_session):
    """Codex R3 CRITICAL — outer UPDATE must not overwrite a row
    whose state changed between SELECT and UPDATE.

    With ``FOR UPDATE`` on the inner SELECT, a concurrent heartbeat
    blocks until claim commits; then heartbeat's WHERE clause
    (worker_id + attempt) no-ops because claim incremented ``attempt``.
    Without FOR UPDATE, claim could overwrite the heartbeat's new
    lease and silently steal the live job.

    This test verifies that scenario doesn't happen.
    """
    setup = make_session()
    t0 = _now()
    # Job with an EXPIRED lease — looks claimable to a failover worker.
    expired = MessageJob(
        id="job_x",
        tenant_id="t1",
        thread_id="thread_t1",
        channel="telegram",
        external_update_id="X",
        message_payload={},
        status="processing",
        enqueued_at=t0,
        started_at=t0,
        worker_id="w_original",
        attempt=1,
        lease_expires_at=t0 - timedelta(seconds=10),  # already expired
    )
    setup.add(expired)
    setup.commit()
    setup.close()

    worker_heartbeat = make_session()
    worker_claim = make_session()

    # Heartbeat (from original worker) extends lease — locks row X.
    worker_heartbeat.begin()
    worker_heartbeat.execute(
        text(
            """
            UPDATE message_jobs
            SET lease_expires_at = :new_lease
            WHERE id='job_x' AND worker_id='w_original' AND attempt=1
              AND status='processing'
            """
        ),
        {"new_lease": t0 + timedelta(minutes=5)},
    )
    # Don't commit yet — heartbeat holds row lock.

    # Failover claim tries to grab the (apparently) expired job.
    worker_claim.begin()
    worker_claim.execute(text("SET LOCAL statement_timeout = '500ms'"))
    timed_out = False
    try:
        claim_next_job(worker_claim, worker_id="w_failover", now=t0)
    except Exception as exc:  # noqa: BLE001
        if "statement timeout" in str(exc).lower() or "57014" in str(exc):
            timed_out = True
        else:
            raise
    finally:
        worker_claim.rollback()

    # Now commit the heartbeat — lease was extended.
    worker_heartbeat.commit()

    # Verify row X is still under the original worker, not the failover.
    check = make_session()
    final = check.get(MessageJob, "job_x")
    assert final.worker_id == "w_original", (
        f"Heartbeat race regressed: failover claim stole the live job. "
        f"Expected worker_id='w_original', got worker_id={final.worker_id!r}"
    )
    assert final.attempt == 1, (
        f"attempt should not have been incremented by failover claim "
        f"(heartbeat doesn't touch attempt). Got attempt={final.attempt}."
    )
    check.close()
    worker_heartbeat.close()
    worker_claim.close()

    # The claim should have either blocked (timed out) or seen the
    # extended lease and skipped the row. Either is correct.
    assert timed_out or final.worker_id == "w_original"
