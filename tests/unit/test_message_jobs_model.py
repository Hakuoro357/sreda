"""Tests for ``MessageJob`` model + table constraints (Sub-A2, Epic #74).

The constraints are load-bearing for queue safety:

- ``UNIQUE (channel, external_update_id)`` — cross-channel idempotency.
  Telegram redelivery / MAX retry / future channel duplicates all
  collapse at INSERT time without reaching the worker loop.
- ``CHECK status IN (...)`` — keeps the state-machine enum honest.
- ``CHECK status_timestamps`` — pending rows must not have ``started_at``;
  processing rows must have lease set; terminal rows must have
  ``finished_at``. Prevents partial/corrupt rows from breaking the
  worker's claim logic.

Pg-specific behaviour (partial-index pushdown, ``FOR UPDATE SKIP LOCKED``)
is not testable in SQLite — the worker-loop integration tests will
cover that when they land. Here we only verify model + table semantics
that hold across dialects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models import MessageJob


# ---------------------------------------------------------------------------
# Basic insert / model wiring
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pending_job(**overrides: object) -> MessageJob:
    base = dict(
        id="job_abc",
        tenant_id="tenant_1",
        thread_id="thread_t1_dm",
        channel="telegram",
        external_update_id="42",
        message_payload={"text": "купи молоко"},
        status="pending",
        enqueued_at=_now(),
        attempt=0,
    )
    base.update(overrides)
    return MessageJob(**base)  # type: ignore[arg-type]


def test_message_job_pending_row_persists(db_session: Session) -> None:
    job = _pending_job()
    db_session.add(job)
    db_session.flush()
    loaded = db_session.query(MessageJob).one()
    assert loaded.id == "job_abc"
    assert loaded.status == "pending"
    assert loaded.thread_id == "thread_t1_dm"


def test_message_job_processing_row_persists(db_session: Session) -> None:
    started = _now()
    job = _pending_job(
        status="processing",
        started_at=started,
        worker_id="worker_1",
        attempt=1,
        lease_expires_at=started + timedelta(minutes=5),
    )
    db_session.add(job)
    db_session.flush()
    loaded = db_session.query(MessageJob).one()
    assert loaded.status == "processing"
    assert loaded.worker_id == "worker_1"


def test_message_job_done_row_persists(db_session: Session) -> None:
    started = _now()
    finished = started + timedelta(seconds=30)
    job = _pending_job(
        status="done",
        started_at=started,
        finished_at=finished,
        worker_id="worker_1",
        attempt=1,
    )
    db_session.add(job)
    db_session.flush()
    loaded = db_session.query(MessageJob).one()
    assert loaded.status == "done"
    assert loaded.finished_at is not None


# ---------------------------------------------------------------------------
# UNIQUE (channel, external_update_id) — cross-channel idempotency
# ---------------------------------------------------------------------------


def test_duplicate_channel_update_id_rejected(db_session: Session) -> None:
    db_session.add(_pending_job(id="job_1", external_update_id="42"))
    db_session.flush()

    # Same (channel, external_update_id) — duplicate Telegram delivery
    db_session.add(
        _pending_job(id="job_2", external_update_id="42", thread_id="other_thread")
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_update_id_different_channels_allowed(db_session: Session) -> None:
    """Different channels share an update id namespace but each has its own.

    Telegram update_id=42 and MAX update_id=42 are unrelated events.
    """
    db_session.add(
        _pending_job(id="job_tg", channel="telegram", external_update_id="42")
    )
    db_session.add(
        _pending_job(id="job_max", channel="max", external_update_id="42")
    )
    db_session.flush()  # No IntegrityError


# ---------------------------------------------------------------------------
# CHECK status enum
# ---------------------------------------------------------------------------


def test_invalid_status_rejected(db_session: Session) -> None:
    db_session.add(_pending_job(status="weird_status"))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------------------
# CHECK status / timestamp consistency
# ---------------------------------------------------------------------------


def test_pending_with_started_at_rejected(db_session: Session) -> None:
    """Pending rows must not have ``started_at`` set."""
    db_session.add(_pending_job(status="pending", started_at=_now()))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_processing_without_lease_rejected(db_session: Session) -> None:
    """Processing rows must declare their lease (for failover detection)."""
    db_session.add(
        _pending_job(
            status="processing",
            started_at=_now(),
            worker_id="worker_1",
            attempt=1,
            lease_expires_at=None,  # ← invalid
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_done_without_finished_at_rejected(db_session: Session) -> None:
    """Terminal rows must declare when they finished."""
    db_session.add(
        _pending_job(
            status="done",
            started_at=_now(),
            finished_at=None,  # ← invalid for terminal state
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_failed_state_persists(db_session: Session) -> None:
    started = _now()
    finished = started + timedelta(seconds=5)
    db_session.add(
        _pending_job(
            id="job_fail",
            status="failed",
            started_at=started,
            finished_at=finished,
            worker_id="worker_1",
            attempt=1,
            last_error="provider_timeout",
        )
    )
    db_session.flush()
    loaded = db_session.query(MessageJob).filter_by(id="job_fail").one()
    assert loaded.status == "failed"
    assert loaded.last_error == "provider_timeout"


def test_dead_letter_state_persists(db_session: Session) -> None:
    started = _now()
    finished = started + timedelta(seconds=5)
    db_session.add(
        _pending_job(
            id="job_dl",
            status="dead_letter",
            started_at=started,
            finished_at=finished,
            attempt=3,
        )
    )
    db_session.flush()
    loaded = db_session.query(MessageJob).filter_by(id="job_dl").one()
    assert loaded.status == "dead_letter"
    assert loaded.attempt == 3


# ---------------------------------------------------------------------------
# Index / table sanity (across-dialect)
# ---------------------------------------------------------------------------


def test_table_has_expected_indexes(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    index_names = {ix["name"] for ix in inspector.get_indexes("message_jobs")}
    expected = {
        "ix_message_jobs_pending",
        "ix_message_jobs_processing",
        "ix_message_jobs_expired_lease",
        "ix_message_jobs_tenant_analytics",
    }
    missing = expected - index_names
    assert not missing, f"Missing indexes: {missing}. Found: {index_names}"


def test_table_has_unique_channel_update_constraint(db_session: Session) -> None:
    inspector = inspect(db_session.bind)
    unique_constraints = inspector.get_unique_constraints("message_jobs")
    names = {uc["name"] for uc in unique_constraints}
    assert "uq_message_jobs_channel_external_update_id" in names, (
        f"Missing cross-channel idempotency constraint. Found: {names}"
    )


# ---------------------------------------------------------------------------
# JSON payload roundtrip
# ---------------------------------------------------------------------------


def test_message_payload_roundtrips_complex_dict(db_session: Session) -> None:
    payload = {
        "kind": "ActionEnvelope",
        "text": "купи молоко",
        "metadata": {"voice_confidence": 0.92, "is_voice": True},
        "tags": ["shopping", "voice"],
    }
    db_session.add(_pending_job(message_payload=payload))
    db_session.flush()
    db_session.expire_all()  # force reload from DB
    loaded = db_session.query(MessageJob).one()
    assert loaded.message_payload == payload
