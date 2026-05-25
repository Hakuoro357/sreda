"""Tests for ``workers.message_dispatcher`` — job_runner tick consumer.

Focus on the orchestration loop:

- ``process_pending`` claims up to ``limit`` jobs and processes each
- Successful processing → ``mark_done`` lands → row status='done'
- Exception during processing → ``mark_failed`` (retry below MAX, then
  dead_letter) → no crash, no leaked lease
- ``limit=0`` is a no-op
- Empty queue → returns ``0``
- ``derive_thread_key`` is deterministic and groups by (tenant, channel,
  chat) tuple

The actual ``_dispatch_telegram`` is heavy (calls into
``ensure_telegram_user_bundle`` + ``_process_approved_turn``) — we mock
``_dispatch`` at the dispatcher level so the loop is exercised without
needing a full Telegram-side stack.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from sreda.db.models import MessageJob
from sreda.workers import message_dispatcher
from sreda.workers.message_queue import (
    derive_thread_key,
    enqueue_message,
)


# ---------------------------------------------------------------------------
# derive_thread_key — deterministic FIFO grouping
# ---------------------------------------------------------------------------


def test_derive_thread_key_is_deterministic() -> None:
    a = derive_thread_key("tenant_1", "telegram", "chat_42")
    b = derive_thread_key("tenant_1", "telegram", "chat_42")
    assert a == b


def test_derive_thread_key_different_chats_get_different_keys() -> None:
    a = derive_thread_key("tenant_1", "telegram", "chat_42")
    b = derive_thread_key("tenant_1", "telegram", "chat_99")
    assert a != b


def test_derive_thread_key_different_channels_get_different_keys() -> None:
    a = derive_thread_key("tenant_1", "telegram", "42")
    b = derive_thread_key("tenant_1", "max", "42")
    assert a != b


def test_derive_thread_key_different_tenants_get_different_keys() -> None:
    a = derive_thread_key("tenant_1", "telegram", "42")
    b = derive_thread_key("tenant_2", "telegram", "42")
    assert a != b


# ---------------------------------------------------------------------------
# process_pending — happy path
# ---------------------------------------------------------------------------


def _enqueue_telegram(session: Session, update_id: str) -> MessageJob:
    return enqueue_message(
        session,
        tenant_id="tenant_test",
        thread_id=derive_thread_key("tenant_test", "telegram", "chat_1"),
        channel="telegram",
        external_update_id=update_id,
        message_payload={
            "kind": "telegram_inbound",
            "payload": {"update_id": int(update_id), "message": {"text": "hi"}},
            "bot_key": "sreda",
            "inbound_message_id": f"inbound_{update_id}",
        },
    )


@pytest.fixture
def _patch_session_factory(db_session, monkeypatch):
    """Make ``get_session_factory`` return the test session's bound factory.

    The dispatcher opens its own sessions; redirect them to the test's
    bound connection so claims/marks are visible in the same DB.
    """
    from sqlalchemy.orm import sessionmaker

    bind = db_session.get_bind()
    test_factory = sessionmaker(bind=bind)

    def fake_factory():
        return test_factory

    monkeypatch.setattr(
        "sreda.workers.message_dispatcher.get_session_factory", fake_factory
    )
    yield


@pytest.mark.asyncio
async def test_process_pending_empty_queue_returns_zero(
    _patch_session_factory,
) -> None:
    count = await message_dispatcher.process_pending(limit=5)
    assert count == 0


@pytest.mark.asyncio
async def test_process_pending_zero_limit_is_noop(
    db_session: Session, _patch_session_factory
) -> None:
    _enqueue_telegram(db_session, "10")
    db_session.commit()
    count = await message_dispatcher.process_pending(limit=0)
    assert count == 0
    # Job still pending — not picked up
    job = db_session.query(MessageJob).one()
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_process_pending_dispatches_and_marks_done(
    db_session: Session, _patch_session_factory
) -> None:
    _enqueue_telegram(db_session, "11")
    db_session.commit()

    dispatched: list[Any] = []

    async def fake_dispatch(job_snapshot):
        dispatched.append(job_snapshot.id)

    with patch.object(message_dispatcher, "_dispatch", new=fake_dispatch):
        count = await message_dispatcher.process_pending(limit=5)

    assert count == 1
    assert len(dispatched) == 1
    # Verify the job is marked done
    db_session.expire_all()
    job = db_session.query(MessageJob).one()
    assert job.status == "done"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_process_pending_respects_limit(
    db_session: Session, _patch_session_factory
) -> None:
    # Enqueue 5 jobs to DIFFERENT threads so they can run in parallel
    # (same thread would serialize and only 1 claimable per tick).
    from sreda.workers.message_queue import enqueue_message

    for i in range(5):
        enqueue_message(
            db_session,
            tenant_id="tenant_test",
            thread_id=derive_thread_key("tenant_test", "telegram", f"chat_{i}"),
            channel="telegram",
            external_update_id=f"20{i}",
            message_payload={
                "kind": "telegram_inbound",
                "payload": {},
                "bot_key": "sreda",
                "inbound_message_id": f"inbound_{i}",
            },
        )
    db_session.commit()

    async def fake_dispatch(_):
        pass

    with patch.object(message_dispatcher, "_dispatch", new=fake_dispatch):
        count = await message_dispatcher.process_pending(limit=3)

    assert count == 3
    db_session.expire_all()
    done_jobs = db_session.query(MessageJob).filter_by(status="done").count()
    pending_jobs = db_session.query(MessageJob).filter_by(status="pending").count()
    assert done_jobs == 3
    assert pending_jobs == 2


# ---------------------------------------------------------------------------
# Failure path — exception during dispatch → mark_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_pending_marks_failed_on_exception(
    db_session: Session, _patch_session_factory
) -> None:
    _enqueue_telegram(db_session, "30")
    db_session.commit()

    async def crashing_dispatch(_):
        raise RuntimeError("simulated provider outage")

    # limit=1 so the failed job (which returns to ``pending``) isn't
    # immediately re-claimed in the same tick.
    with patch.object(message_dispatcher, "_dispatch", new=crashing_dispatch):
        count = await message_dispatcher.process_pending(limit=1)

    assert count == 1
    db_session.expire_all()
    job = db_session.query(MessageJob).one()
    # Below MAX_ATTEMPTS → back to pending for retry, last_error captured
    assert job.status == "pending"
    assert job.last_error is not None
    assert "simulated provider outage" in job.last_error


@pytest.mark.asyncio
async def test_process_pending_eventually_dead_letters_on_persistent_failure(
    db_session: Session, _patch_session_factory
) -> None:
    """Drive a job through MAX_ATTEMPTS failures and assert dead_letter."""
    from sreda.workers.message_queue import MAX_ATTEMPTS

    _enqueue_telegram(db_session, "40")
    db_session.commit()

    async def always_fail(_):
        raise RuntimeError("permanent failure")

    with patch.object(message_dispatcher, "_dispatch", new=always_fail):
        for _ in range(MAX_ATTEMPTS):
            await message_dispatcher.process_pending(limit=1)

    db_session.expire_all()
    job = db_session.query(MessageJob).one()
    assert job.status == "dead_letter"
    assert job.last_error == "RuntimeError('permanent failure')"


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_rejects_unknown_channel(
    db_session: Session, _patch_session_factory
) -> None:
    """Job with unknown channel raises — exercises the unknown-channel branch."""
    enqueue_message(
        db_session,
        tenant_id="tenant_test",
        thread_id=derive_thread_key("tenant_test", "future_chan", "x"),
        channel="future_chan",
        external_update_id="50",
        message_payload={"kind": "unknown_kind", "payload": {}},
    )
    db_session.commit()

    # limit=1 so we only see the first attempt — the retry loop is
    # exercised separately in test_process_pending_eventually_dead_letters.
    count = await message_dispatcher.process_pending(limit=1)
    assert count == 1
    db_session.expire_all()
    job = db_session.query(MessageJob).one()
    # Below MAX_ATTEMPTS → back to pending, error mentions the channel
    assert job.status == "pending"
    assert "future_chan" in (job.last_error or "")
