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
from urllib.parse import parse_qs, urlparse

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
_REMOTE_OPT_IN = (
    os.environ.get("SREDA_TEST_POSTGRES_REMOTE_OPT_IN") == "1"
)

# Hosts considered "local sandbox" — safe by default. Remote hosts
# need a separate opt-in even if URL passes other checks (defense
# in depth against pointing at a remote prod DB whose name happens
# to contain "test").
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "pg", "db"})


def _safety_reason(url: str) -> str | None:
    """Return None if the URL is safe to wipe; otherwise a human-readable
    rejection reason.

    Codex R5/R6 MAJOR — DB-name check alone is not enough; we validate:

      1. Scheme: only postgresql / postgres / postgresql+psycopg
      2. DB name (strict): exact 'test', 'test_*', '*_test', or
         separator-bounded '*_test_*'. Loose substring like
         'prod_testing' or 'sreda_testprod' is REJECTED.
      3. URL host: local-only by default, remote needs opt-in.
      4. Query-param host: libpq honors ``?host=...`` / ``?hostaddr=...``
         which can override the URL's hostname. We reject any such
         override unless the override ALSO passes the host allowlist
         (or REMOTE_OPT_IN is set). Closes the
         ``postgresql://...@localhost/test?host=prod-db`` bypass.

    Allowed (without REMOTE_OPT_IN):
      postgresql://user:pw@<local-host>/<test|test_*|*_test|*_test_*>
        (with no host/hostaddr override, or override also local)

    Remote hosts (URL or query-param override) require explicit
    ``SREDA_TEST_POSTGRES_REMOTE_OPT_IN=1`` in addition to destructive
    opt-in.
    """
    if not url:
        return "url is empty"

    parsed = urlparse(url)

    # 1. Scheme.
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg"}:
        return f"scheme {parsed.scheme!r} is not postgresql"

    # 2. DB name (strict — no loose substring matching).
    db_name = (parsed.path or "").lstrip("/").lower()
    if not db_name:
        return "DB name missing from URL"
    valid_name = (
        db_name == "test"
        or db_name.startswith("test_")
        or db_name.endswith("_test")
        or "_test_" in db_name
    )
    if not valid_name:
        return (
            f"DB name {db_name!r} doesn't look like a test DB "
            f"(must be exactly 'test', 'test_*', '*_test', or "
            f"'*_test_*' with underscore separators)"
        )

    # 3. URL host: local-only by default. Remote needs explicit opt-in.
    url_host = (parsed.hostname or "").lower()
    if url_host not in _LOCAL_HOSTS and not _REMOTE_OPT_IN:
        return (
            f"URL host {url_host!r} is not in the local-sandbox "
            f"allowlist ({sorted(_LOCAL_HOSTS)}). Set "
            f"SREDA_TEST_POSTGRES_REMOTE_OPT_IN=1 to allow remote hosts."
        )

    # 4. Query-param host overrides. libpq treats ?host=foo and
    # ?hostaddr=1.2.3.4 as connection targets that supersede the
    # URL's authority portion. Without checking these, a URL like
    # ``postgresql://u:p@localhost/test_db?host=prod-db`` would
    # appear local but connect to prod.
    query_params = parse_qs(parsed.query or "")
    for param in ("host", "hostaddr"):
        for raw_value in query_params.get(param, []):
            # libpq accepts comma-separated lists for failover.
            for override in raw_value.split(","):
                override_host = override.strip().lower()
                if not override_host:
                    continue
                if override_host not in _LOCAL_HOSTS and not _REMOTE_OPT_IN:
                    return (
                        f"query param {param}={override_host!r} "
                        f"would route the connection outside the "
                        f"local-sandbox allowlist ({sorted(_LOCAL_HOSTS)}). "
                        f"Set SREDA_TEST_POSTGRES_REMOTE_OPT_IN=1 "
                        f"if this is intentional."
                    )

    return None


_SAFETY_FAILURE = _safety_reason(_POSTGRES_URL or "") if _POSTGRES_URL else "URL not set"
_SAFETY_OK = (
    bool(_POSTGRES_URL)
    and _DESTRUCTIVE_OPT_IN
    and _SAFETY_FAILURE is None
)

pytestmark = pytest.mark.skipif(
    not _SAFETY_OK,
    reason=(
        "Postgres concurrency tests require BOTH "
        "SREDA_TEST_POSTGRES_URL (scheme=postgresql, host in local "
        "allowlist OR SREDA_TEST_POSTGRES_REMOTE_OPT_IN=1, DB name "
        "'test'/'test_*'/'*_test') and "
        "SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 — these tests DROP "
        "and re-create the message_jobs table. "
        f"Current failure: {_SAFETY_FAILURE or 'destructive opt-in missing'}"
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


def _start_claim_thread(
    session_factory,
    *,
    worker_id: str,
    now: datetime,
) -> tuple[threading.Thread, threading.Event, queue.Queue]:
    """Spawn a background thread that calls ``claim_next_job`` and
    captures the result. Returns ``(thread, before_claim_event, result_q)``.

    Codex R5 MINOR — the event is set IMMEDIATELY before
    ``claim_next_job()`` so the main thread can wait for it before
    asserting "still blocked". Without the event, a slow thread
    startup could let the main thread's commit happen BEFORE the
    background reached the claim call, making the blocking assertion
    false-pass.

    Caller protocol:
        t, ready, rq = _start_claim_thread(session_factory, worker_id="w2", now=t0)
        assert ready.wait(timeout=5.0)        # wait until claim is about to run
        t.join(timeout=1.0)
        assert t.is_alive()                   # must be blocked NOW
        ...  # commit the blocker
        t.join(timeout=5.0)
        status, payload = rq.get_nowait()
    """
    ready = threading.Event()
    result_q: queue.Queue = queue.Queue()

    def runner() -> None:
        sess = session_factory()
        try:
            sess.begin()
            ready.set()  # signal: about to call claim_next_job → block check is now valid
            claimed = claim_next_job(sess, worker_id=worker_id, now=now)
            sess.commit()
            result_q.put(("ok", claimed))
        except Exception as exc:  # noqa: BLE001
            result_q.put(("err", repr(exc)))
        finally:
            sess.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t, ready, result_q


def test_same_thread_fifo_under_concurrent_workers(session_factory):
    """Codex R3/R4/R5 — true FIFO under concurrent workers.

    Repro of the original bug:
      Thread T has pending A (older) and pending B (later).
      Without advisory lock + FOR UPDATE, Worker 1 could lock A,
      Worker 2 could simultaneously claim B for the same thread.

    R5 verification: Worker 2 runs in a background thread; we wait
    on a threading.Event set immediately before claim_next_job()
    to ensure the worker reached the call site (not just thread
    startup), then assert it's still blocked, commit Worker 1, and
    verify Worker 2 unblocks naturally with the correct result.
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

    t2, ready, result_q = _start_claim_thread(
        session_factory, worker_id="w2", now=t0 + timedelta(seconds=3)
    )

    # Wait for Worker 2 to reach the claim call site, then verify it
    # blocked (Event proves the call started; is_alive proves it's
    # waiting on the lock).
    assert ready.wait(timeout=5.0), "Worker 2 thread didn't reach claim_next_job"
    t2.join(timeout=1.0)
    assert t2.is_alive(), (
        "Worker 2 did NOT block on Worker 1's open claim transaction. "
        "advisory_xact_lock is not serializing claim ops — FIFO race "
        "is open."
    )
    # Result queue must be empty — worker 2 hasn't finished yet.
    assert result_q.empty(), "Worker 2 returned without blocking — race is open"

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

    t2, ready, result_q = _start_claim_thread(
        session_factory, worker_id="w2", now=t0 + timedelta(seconds=3)
    )
    assert ready.wait(timeout=5.0), "Worker 2 thread didn't reach claim_next_job"
    t2.join(timeout=1.0)
    assert t2.is_alive(), "Worker 2 should block on advisory lock."
    assert result_q.empty()

    worker1.commit()
    worker1.close()

    t2.join(timeout=5.0)
    assert not t2.is_alive()
    status, claimed = result_q.get_nowait()
    assert status == "ok"
    assert claimed is not None
    assert claimed.thread_id == "thread_b", (
        f"Worker 2 should have claimed thread_b's job (thread_a is "
        f"taken). Got thread_id={claimed.thread_id!r}"
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

    # Step 3: failover claim in background — should block on row lock.
    t_claim, ready, result_q = _start_claim_thread(
        session_factory, worker_id="w_failover", now=t0
    )
    assert ready.wait(timeout=5.0), "Claim thread didn't reach claim_next_job"
    t_claim.join(timeout=1.0)
    assert t_claim.is_alive(), (
        "Failover claim did NOT block on heartbeat's row lock — "
        "FOR UPDATE protection is missing."
    )
    assert result_q.empty()

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

    t_claim, ready, result_q = _start_claim_thread(
        session_factory, worker_id="w_failover", now=t0
    )
    assert ready.wait(timeout=5.0), "Claim thread didn't reach claim_next_job"
    t_claim.join(timeout=1.0)
    assert t_claim.is_alive(), "Failover claim did NOT block on mark_done's row lock."
    assert result_q.empty()

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
