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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from sreda.db.models import InboundMessage, MessageJob
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
    """Redirect the #138 Ф2 seam to the test session's bound factory.

    The dispatcher now opens sessions via ``tenant_session`` /
    ``privileged_session`` (#138 — queue-mgmt cross-tenant → privileged,
    per-tenant dispatch → tenant_session), which both build sessions
    through ``sreda.db.session._factory_for``. Patch that single seam so
    claims/marks land in the test's bound connection while the ContextVar
    isolation wiring still runs. (Role/engine split is exercised by the Ф3
    integration red-suite, not these behaviour tests.)
    """
    from sqlalchemy.orm import sessionmaker

    bind = db_session.get_bind()
    test_factory = sessionmaker(bind=bind)

    monkeypatch.setattr(
        "sreda.db.session._factory_for", lambda _engine: test_factory
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


# ---------------------------------------------------------------------------
# Codex review 2026-05-25 — silent-crash detection (MAJOR-1)
# ---------------------------------------------------------------------------


def _seed_inbound(db_session: Session, *, status: str) -> str:
    """Insert a minimal InboundMessage row at the given processing_status.

    We only need ``id`` + ``processing_status`` for the dispatcher's
    crash detection. Other fields are NULLed or set to sensible test
    defaults (``bot_key`` is required by schema).
    """
    from uuid import uuid4

    from sreda.db.models.core import Tenant

    # #187 Phase 1: the dispatcher now gates on ``is_tenant_active(job.tenant_id)``
    # BEFORE ensure/turn (fail-closed — a missing tenant row = inactive = early
    # terminal close). These dispatch tests must seed an *active* tenant row so
    # the gate passes and the real downstream behaviour (crash-detection /
    # kwargs forwarding) is still exercised.
    if db_session.get(Tenant, "tenant_test") is None:
        db_session.add(Tenant(id="tenant_test", name="Test Tenant"))
        db_session.flush()

    inbound_id = f"inbound_{uuid4().hex[:24]}"
    row = InboundMessage(
        id=inbound_id,
        tenant_id="tenant_test",
        user_id="user_test",
        channel_type="telegram",
        bot_key="sreda",
        sender_chat_id="chat_1",
        external_update_id="999",
        processing_status=status,
    )
    db_session.add(row)
    db_session.commit()
    return inbound_id


@pytest.mark.asyncio
async def test_dispatch_telegram_raises_when_turn_silently_crashes(
    db_session: Session, _patch_session_factory, monkeypatch
) -> None:
    """``_process_approved_turn`` swallows exceptions — dispatcher must
    notice via processing_status read-back (Codex MAJOR-1)."""
    inbound_id = _seed_inbound(db_session, status="processing_started")

    fake_bundle = SimpleNamespace(
        tenant_id="tenant_test",
        user_id="user_test",
        chat_id="chat_1",
    )
    monkeypatch.setattr(
        "sreda.services.telegram_inbound.ensure_telegram_user_bundle",
        lambda session, payload, bot_key="sreda": fake_bundle,
    )
    monkeypatch.setattr(
        "sreda.services.telegram_inbound._process_approved_turn",
        AsyncMock(return_value=None),
    )

    job = message_dispatcher._JobSnapshot(
        id="job_silent",
        tenant_id="tenant_test",
        thread_id="thread_x",
        channel="telegram",
        external_update_id="999",
        message_payload={
            "kind": "telegram_inbound",
            "payload": {"update_id": 999},
            "bot_key": "sreda",
            "inbound_message_id": inbound_id,
        },
        attempt=1,
    )
    with pytest.raises(RuntimeError) as exc:
        await message_dispatcher._dispatch_telegram(job)
    msg = str(exc.value).lower()
    assert "processing_started" in msg or "silent failure" in msg


@pytest.mark.asyncio
async def test_dispatch_telegram_succeeds_when_status_is_processed(
    db_session: Session, _patch_session_factory, monkeypatch
) -> None:
    """Success path — processing_status='processed' → no exception."""
    inbound_id = _seed_inbound(db_session, status="processed")

    fake_bundle = SimpleNamespace(
        tenant_id="tenant_test",
        user_id="user_test",
        chat_id="chat_1",
    )
    monkeypatch.setattr(
        "sreda.services.telegram_inbound.ensure_telegram_user_bundle",
        lambda session, payload, bot_key="sreda": fake_bundle,
    )
    monkeypatch.setattr(
        "sreda.services.telegram_inbound._process_approved_turn",
        AsyncMock(return_value=None),
    )

    job = message_dispatcher._JobSnapshot(
        id="job_ok",
        tenant_id="tenant_test",
        thread_id="thread_x",
        channel="telegram",
        external_update_id="999",
        message_payload={
            "kind": "telegram_inbound",
            "payload": {"update_id": 999},
            "bot_key": "sreda",
            "inbound_message_id": inbound_id,
        },
        attempt=1,
    )
    # Should NOT raise
    await message_dispatcher._dispatch_telegram(job)


@pytest.mark.asyncio
async def test_dispatch_telegram_calls_process_turn_with_accepted_kwargs_only(
    db_session: Session, _patch_session_factory, monkeypatch
) -> None:
    """#136 regression: the dispatcher must call ``_process_approved_turn``
    with ONLY the kwargs its signature accepts (bot_key / payload /
    onboarding / inbound_message_id). A stray ``bot_token=`` kwarg →
    ``TypeError`` on EVERY queue job (latent while the queue is off, but
    blocks ever enabling it).

    The other dispatch tests mock ``_process_approved_turn`` with an
    ``AsyncMock`` that swallows any kwargs — which is exactly why the
    mismatch went unnoticed. Here the stub mirrors the REAL signature, so
    an extra kwarg raises, just like the real call would."""
    inbound_id = _seed_inbound(db_session, status="processed")

    fake_bundle = SimpleNamespace(
        tenant_id="tenant_test",
        user_id="user_test",
        chat_id="chat_1",
    )
    captured: dict[str, Any] = {}

    def fake_bundle_fn(session, payload, *, bot_key):
        # #136 R2: dispatcher MUST pass bot_key (kw-only, no default here) —
        # mirrors the inline path. An omitted bot_key raises TypeError, just
        # like a non-default bot would be mis-onboarded under "sreda".
        captured["onboard_bot_key"] = bot_key
        return fake_bundle

    monkeypatch.setattr(
        "sreda.services.telegram_inbound.ensure_telegram_user_bundle",
        fake_bundle_fn,
    )

    async def fake_turn(*, bot_key, payload, onboarding, inbound_message_id):
        # Signature-faithful: NO bot_token. A stray kwarg from the
        # dispatcher raises TypeError before this body runs.
        captured["bot_key"] = bot_key
        captured["inbound_message_id"] = inbound_message_id

    monkeypatch.setattr(
        "sreda.services.telegram_inbound._process_approved_turn", fake_turn
    )

    job = message_dispatcher._JobSnapshot(
        id="job_136",
        tenant_id="tenant_test",
        thread_id="thread_x",
        channel="telegram",
        external_update_id="999",
        message_payload={
            "kind": "telegram_inbound",
            "payload": {"update_id": 999},
            "bot_key": "sreda",
            "inbound_message_id": inbound_id,
        },
        attempt=1,
    )
    # RED before fix: dispatcher passes bot_token= → TypeError here.
    await message_dispatcher._dispatch_telegram(job)
    assert captured["bot_key"] == "sreda"
    assert captured["inbound_message_id"] == inbound_id
    # #136 R2: dispatcher mirrors inline path — bot_key forwarded to onboarding.
    assert captured["onboard_bot_key"] == "sreda"


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
