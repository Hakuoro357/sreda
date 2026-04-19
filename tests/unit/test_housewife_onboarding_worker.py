"""Integration-ish tests for HousewifeOnboardingKickoffWorker."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.services.housewife_onboarding import (
    HOUSEWIFE_FEATURE_KEY,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    HousewifeOnboardingService,
)
from sreda.workers.housewife_onboarding_worker import (
    HousewifeOnboardingKickoffWorker,
)


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="t1", name="Test"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.add(
        User(id="u1", tenant_id="t1", telegram_account_id="100")
    )
    session.commit()
    return session


def _schedule_kickoff_in_past(
    session, *, tenant_id="t1", user_id="u1", minutes_ago=1
):
    """Simulate a subscription made ``minutes_ago + 5`` minutes earlier
    — kickoff scheduled at now - minutes_ago."""
    service = HousewifeOnboardingService(session)
    service.initialize(tenant_id=tenant_id, user_id=user_id)
    # Reach into the state to push the timestamp into the past.
    state = service.get_raw_state(tenant_id=tenant_id, user_id=user_id)
    state["kickoff_scheduled_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat()
    service._persist(
        tenant_id=tenant_id, user_id=user_id, state=state, source="system"
    )
    session.commit()


def test_kickoff_flips_status_and_enqueues_outbox():
    session = _fresh_session()
    _schedule_kickoff_in_past(session)

    worker = HousewifeOnboardingKickoffWorker(session)
    fired = asyncio.run(worker.process_pending())

    assert fired == 1
    # Status flipped
    state = HousewifeOnboardingService(session).get_raw_state(
        tenant_id="t1", user_id="u1"
    )
    assert state["status"] == STATUS_IN_PROGRESS
    assert state["current_topic"] == "addressing"
    # Outbox row with intro
    outbox_rows = session.query(OutboxMessage).all()
    assert len(outbox_rows) == 1
    payload = json.loads(outbox_rows[0].payload_json)
    assert payload["chat_id"] == "100"
    assert "Среда" in payload["text"]
    assert "как мне к тебе обращаться" in payload["text"].lower()


def test_kickoff_skipped_when_user_already_in_progress():
    """If the user wrote first and the chat handler flipped status,
    the worker's filter skips this row — no duplicate intro."""
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.initialize(tenant_id="t1", user_id="u1")
    service.start(tenant_id="t1", user_id="u1")
    session.commit()

    worker = HousewifeOnboardingKickoffWorker(session)
    fired = asyncio.run(worker.process_pending())

    assert fired == 0
    assert session.query(OutboxMessage).count() == 0


def test_kickoff_not_fired_if_scheduled_in_future():
    session = _fresh_session()
    service = HousewifeOnboardingService(session)
    service.schedule_kickoff(
        tenant_id="t1", user_id="u1", delay_minutes=10
    )
    session.commit()

    worker = HousewifeOnboardingKickoffWorker(session)
    fired = asyncio.run(worker.process_pending())

    assert fired == 0
    assert session.query(OutboxMessage).count() == 0


def test_kickoff_without_telegram_binding_is_soft_skipped():
    session = _fresh_session()
    # Remove user's telegram binding.
    user = session.get(User, "u1")
    user.telegram_account_id = None
    session.commit()
    _schedule_kickoff_in_past(session)

    worker = HousewifeOnboardingKickoffWorker(session)
    fired = asyncio.run(worker.process_pending())

    assert fired == 0
    assert session.query(OutboxMessage).count() == 0


def test_kickoff_does_not_fire_twice_on_consecutive_ticks():
    """After first fire, status=in_progress — second tick must be no-op."""
    session = _fresh_session()
    _schedule_kickoff_in_past(session)
    worker = HousewifeOnboardingKickoffWorker(session)

    first = asyncio.run(worker.process_pending())
    second = asyncio.run(worker.process_pending())

    assert first == 1
    assert second == 0
    assert session.query(OutboxMessage).count() == 1
