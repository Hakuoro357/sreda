"""Postgres-only concurrency tests for ``claim_next_job`` (Codex R3/R4 MAJOR).

The Phase A unit tests (``tests/unit/test_message_queue_primitives.py``)
run against SQLite, which cannot exercise the race conditions that the
production code is designed to handle:

  1. Two workers simultaneously holding open transactions against the
     same ``message_jobs`` row — only Postgres has row-level locking
     with visibility semantics that match production.
  2. ``pg_advisory_xact_lock`` and ``FOR UPDATE`` — SQLite doesn't
     model these at all.

These integration tests close that gap. They require a real Postgres
instance AND explicit opt-in to confirm the DB is safe to wipe:

  $ SREDA_TEST_POSTGRES_URL=postgresql://user:pw@localhost/sreda_test \
    SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 \
    .venv/Scripts/python.exe -m pytest tests/integration/test_message_queue_postgres_concurrency.py -v

When either env var is unset (default in CI without infrastructure), the
whole module is skipped — the unit-test suite stays green without
needing Postgres.

Codex R4 reference: ``plans/phase-a-review-r4.md``.
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from sreda.db.models import MessageJob
from sreda.workers.message_queue import claim_next_job

# ---------------------------------------------------------------------------
# Safety: skip module unless both URL AND destructive opt-in are set, AND
# the DB name passes a sanity check that it's a throwaway test DB.
# ---------------------------------------------------------------------------

_POSTGRES_URL = os.environ.get("SREDA_TEST_POSTGRES_URL")
_DESTRUCTIVE_OPT_IN = os.environ.get("SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN") == "1"


def _is_safe_test_db(url: str) -> bool:
    """Refuse to drop tables if the URL doesn't look like a throwaway test
    DB. We require the database name to contain the string 'test' — this
    is paranoid but cheap, and catches the worst misconfigurations
    (e.g. pointing at prod by mistake)."""
    if not url:
        return False
    parsed = urlparse(url)
    db_name = (parsed.path or "").lstrip("/")
    return "test" in db_name.lower()


_SAFETY_OK = bool(_POSTGRES_URL) and _DESTRUCTIVE_OPT_IN and _is_safe_test_db(_POSTGRES_URL or "")

pytestmark = pytest.mark.skipif(
    not _SAFETY_OK,
    reason=(
        "Postgres concurrency tests require BOTH "
        "SREDA_TEST_POSTGRES_URL (with 'test' in DB name) and "
        "SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 — these tests DROP "
        "and re-create the message_jobs table."
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def engine():
    """Per-test Postgres engine. Each test gets a fresh ``message_jobs``
    table to avoid bleed-through. Safety: we only drop if the URL passed
    ``_is_safe_test_db`` above."""
    eng = create_engine(_POSTGRES_URL, echo=False, future=True)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS message_jobs"))
        MessageJob.__table__.create(conn)
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS message_jobs"))
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    """Factory returning fresh, independent ``Session`` instances bound
    to separate connections — needed to model concurrent workers."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_same_thread_fifo_under_concurrent_workers(session_factory):
    """Codex R3/R4 — true FIFO under concurrent workers.

    Repro of the original bug:
      Thread T has pending A (older) and pending B (later).
      Without advisory lock + FOR UPDATE, Worker 1 could lock A,
      Worker 2 could simultaneously claim B for the same thread.

    R4 verification: instead of using a statement_timeout as the
    blocked-signal (weak — broken impl might also timeout for
    unrelated reasons), we run Worker 2's claim in a background
    thread, observe it stays blocked while Worker 1 holds the
    transaction, then commit Worker 1 and assert Worker 2
    eventually returns ``None`` (no other claimable job exists
    for that thread).
    """
    setup = session_factory()
    t0 = _now()
    _enqueue(setup, external_update_id="A", enqueued_at=t0)
    _enqueue(
        setup, external_update_id="B", enqueued_at=t0 + timedelta(seconds=1)
    )
    setup.commit()
    setup.close()

    worker1 = session_factory()
    worker1.begin()
    first = claim_next_job(worker1, worker_id="w1", now=t0 + timedelta(seconds=2))
    assert first is not None
    assert first.external_update_id == "A"
    # Worker 1 deliberately does NOT commit yet — holds the lock.

    # Worker 2 runs in a background thread. We capture its return value
    # via a Queue so we can join() with timeout and observe blocking.
    result_q: queue.Queue = queue.Queue()

    def run_worker2() -> None:
        worker2 = session_factory()
        try:
            worker2.begin()
            claimed = claim_next_job(
                worker2, worker_id="w2", now=t0 + timedelta(seconds=3)
            )
            worker2.commit()
            result_q.put(("ok", claimed))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))
        finally:
            worker2.close()

    t2 = threading.Thread(target=run_worker2)
    t2.start()

    # Worker 2 should be blocked on advisory lock. Wait 1s — if it
    # returned that means the advisory lock didn't fire (regression).
    t2.join(timeout=1.0)
    assert t2.is_alive(), (
        "Worker 2 did NOT block on Worker 1's open claim transaction. "
        "advisory_xact_lock is not serializing claim ops — FIFO race "
        "is open."
    )

    # Now commit worker 1, releasing the advisory lock + row lock.
    worker1.commit()
    worker1.close()

    # Worker 2 should unblock and finish. Since job A is now ``processing``
    # for thread_t1 with active lease, NOT EXISTS excludes B from
    # being claimed — Worker 2 returns None.
    t2.join(timeout=5.0)
    assert not t2.is_alive(), "Worker 2 didn't unblock after Worker 1 committed."

    status, payload = result_q.get_nowait()
    assert status == "ok", f"Worker 2 errored: {payload!r}"
    assert payload is None, (
        f"Worker 2 should have returned None (job A holds active lease "
        f"for thread_t1, NOT EXISTS excludes B). Got: {payload!r}"
    )


def test_different_thread_claimable_after_worker1_commits(session_factory):
    """Sister test to the above: if Worker 2 has a job for a DIFFERENT
    thread, it should claim that one after Worker 1 commits. Verifies
    the NOT EXISTS guard scopes by thread_id, not by tenant."""
    setup = session_factory()
    t0 = _now()
    _enqueue(setup, thread_id="thread_a", external_update_id="A", enqueued_at=t0)
    _enqueue(
        setup,
        thread_id="thread_b",
        external_update_id="B",
        enqueued_at=t0 + timedelta(seconds=1),
    )
    setup.commit()
    setup.close()

    worker1 = session_factory()
    worker1.begin()
    first = claim_next_job(worker1, worker_id="w1", now=t0 + timedelta(seconds=2))
    assert first is not None
    assert first.thread_id == "thread_a"

    result_q: queue.Queue = queue.Queue()

    def run_worker2() -> None:
        worker2 = session_factory()
        try:
            worker2.begin()
            claimed = claim_next_job(
                worker2, worker_id="w2", now=t0 + timedelta(seconds=3)
            )
            worker2.commit()
            result_q.put(("ok", claimed.thread_id if claimed else None))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))
        finally:
            worker2.close()

    t2 = threading.Thread(target=run_worker2)
    t2.start()
    t2.join(timeout=1.0)
    assert t2.is_alive(), "Worker 2 should block on advisory lock."

    worker1.commit()
    worker1.close()

    t2.join(timeout=5.0)
    assert not t2.is_alive()
    status, thread_claimed = result_q.get_nowait()
    assert status == "ok"
    assert thread_claimed == "thread_b", (
        f"Worker 2 should have claimed thread_b's job (thread_a is "
        f"taken). Got thread_id={thread_claimed!r}"
    )


def test_heartbeat_race_does_not_resurrect_active_job(session_factory):
    """Codex R3/R4 CRITICAL — outer UPDATE must not overwrite a row
    whose state changed between SELECT and UPDATE.

    R4 strengthening: instead of rolling back the claim (which makes
    the test pass whether the bug exists or not — any error closes
    the transaction), we commit the heartbeat, then let the claim
    transaction complete naturally, then assert the final row state
    shows the original worker still owns it.

    Sequence:
      1. Job X is ``processing`` with lease that LOOKS expired
         to a failover worker.
      2. Heartbeat from original worker extends the lease (locks row).
      3. Failover worker tries to claim X — blocks on row lock from
         heartbeat (because ``FOR UPDATE`` makes the claim's inner
         SELECT wait for the heartbeat's row lock to release).
      4. Heartbeat commits — row now has extended lease (no longer
         matches claim predicate ``lease_expires_at < :now``).
      5. Failover claim resumes — re-evaluation under FOR UPDATE
         finds the row no longer matches, returns None.
      6. Final state: original worker still owns X, lease extended.
    """
    setup = session_factory()
    t0 = _now()
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
        lease_expires_at=t0 - timedelta(seconds=10),  # expired
    )
    setup.add(expired)
    setup.commit()
    setup.close()

    heartbeat_session = session_factory()
    claim_session = session_factory()

    # Step 1+2: heartbeat extends lease, holds row lock.
    heartbeat_session.begin()
    new_lease = t0 + timedelta(minutes=5)
    heartbeat_session.execute(
        text(
            """
            UPDATE message_jobs
            SET lease_expires_at = :new_lease
            WHERE id='job_x' AND worker_id='w_original' AND attempt=1
              AND status='processing'
            """
        ),
        {"new_lease": new_lease},
    )

    # Step 3: failover claim in background — should block.
    result_q: queue.Queue = queue.Queue()

    def run_claim() -> None:
        try:
            claim_session.begin()
            claimed = claim_next_job(claim_session, worker_id="w_failover", now=t0)
            claim_session.commit()
            result_q.put(("ok", claimed))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))

    t_claim = threading.Thread(target=run_claim)
    t_claim.start()
    t_claim.join(timeout=1.0)
    assert t_claim.is_alive(), (
        "Failover claim did NOT block on heartbeat's row lock — "
        "FOR UPDATE protection is missing."
    )

    # Step 4: commit heartbeat → row lock released, lease extended.
    heartbeat_session.commit()
    heartbeat_session.close()

    # Step 5: claim should resume and return None (row no longer eligible).
    t_claim.join(timeout=5.0)
    assert not t_claim.is_alive()
    status, payload = result_q.get_nowait()
    assert status == "ok", f"Claim errored: {payload!r}"
    assert payload is None, (
        f"Claim should have returned None — heartbeat extended the "
        f"lease so the row no longer matches expired-lease predicate. "
        f"Got: {payload!r} → claim resurrected a live job (R3 bug)."
    )

    # Step 6: verify final state — original worker still owns job_x.
    check = session_factory()
    final = check.get(MessageJob, "job_x")
    assert final is not None
    assert final.worker_id == "w_original", (
        f"R3 bug regressed: failover claim stole live job. "
        f"Expected worker_id='w_original', got {final.worker_id!r}"
    )
    assert final.attempt == 1, (
        f"R3 bug regressed: failover incremented attempt token. "
        f"Expected 1, got {final.attempt}"
    )
    assert final.lease_expires_at >= new_lease - timedelta(milliseconds=1), (
        f"R3 bug regressed: failover overwrote heartbeat's extended "
        f"lease. Expected >= {new_lease}, got {final.lease_expires_at}"
    )
    check.close()
    claim_session.close()


def test_mark_done_race_does_not_resurrect_completed_job(session_factory):
    """Companion test to the heartbeat race: if a worker commits
    ``mark_done`` on a row while a failover claim is waiting, the
    claim must not resurrect the row to ``processing``.

    Sequence is the same pattern as the heartbeat test, but with
    a mark_done UPDATE instead of an extend_lease UPDATE. End state:
    row stays ``status='done'``, claim returns None.
    """
    setup = session_factory()
    t0 = _now()
    expiring = MessageJob(
        id="job_y",
        tenant_id="t1",
        thread_id="thread_t1",
        channel="telegram",
        external_update_id="Y",
        message_payload={},
        status="processing",
        enqueued_at=t0,
        started_at=t0,
        worker_id="w_original",
        attempt=1,
        lease_expires_at=t0 - timedelta(seconds=10),  # expired
    )
    setup.add(expiring)
    setup.commit()
    setup.close()

    mark_done_session = session_factory()
    claim_session = session_factory()

    mark_done_session.begin()
    mark_done_session.execute(
        text(
            """
            UPDATE message_jobs
            SET status='done', finished_at=:fin, lease_expires_at=NULL
            WHERE id='job_y' AND worker_id='w_original' AND attempt=1
              AND status='processing'
            """
        ),
        {"fin": t0},
    )

    result_q: queue.Queue = queue.Queue()

    def run_claim() -> None:
        try:
            claim_session.begin()
            claimed = claim_next_job(claim_session, worker_id="w_failover", now=t0)
            claim_session.commit()
            result_q.put(("ok", claimed))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))

    t_claim = threading.Thread(target=run_claim)
    t_claim.start()
    t_claim.join(timeout=1.0)
    assert t_claim.is_alive(), "Failover claim did NOT block on mark_done's row lock."

    mark_done_session.commit()
    mark_done_session.close()

    t_claim.join(timeout=5.0)
    assert not t_claim.is_alive()
    status, payload = result_q.get_nowait()
    assert status == "ok"
    assert payload is None, (
        f"Claim resurrected a 'done' job to 'processing' (R3 bug). "
        f"Got: {payload!r}"
    )

    check = session_factory()
    final = check.get(MessageJob, "job_y")
    assert final.status == "done", (
        f"R3 bug regressed: 'done' row was resurrected. status={final.status!r}"
    )
    assert final.worker_id == "w_original", (
        f"R3 bug regressed: failover overwrote worker_id. "
        f"Got {final.worker_id!r}"
    )
    check.close()
    claim_session.close()
