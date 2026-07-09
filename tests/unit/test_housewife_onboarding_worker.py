"""Integration-ish tests for HousewifeOnboardingKickoffWorker.

#138 Ф2: воркер САМ открывает seam-сессии (privileged-скан configs всех
семей + tenant_session на конкретного юзера), а не принимает общую сессию.
Тесты через фикстуру ``worker_db`` (коммитящая файловая SQLite + шов
``_factory_for`` привязан к ней). Сессия теста и сессии воркера — РАЗНЫЕ,
поэтому: (1) сид/состояние онбординга коммитим ДО прогона воркера, (2)
``worker_db.expire_all()`` перед ассертом на изменённые засиженные строки
(воркер флипает onboarding status в skill_params_json засиженного
``TenantUserSkillConfig`` — без expire тест видит старый JSON; новые
outbox-строки видны и так).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

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


def _seed_base(session, *, tenant_id="t1", user_id="u1", chat_id="100") -> None:
    session.add(Tenant(id=tenant_id, name="Test"))
    session.add(Workspace(id="w1", tenant_id=tenant_id, name="W"))
    session.add(
        User(id=user_id, tenant_id=tenant_id, telegram_account_id=chat_id)
    )
    session.commit()


def _schedule_kickoff_in_past(
    session, *, tenant_id="t1", user_id="u1", minutes_ago=1
):
    """Simulate a subscription made ``minutes_ago + 5`` minutes earlier
    — kickoff scheduled at now - minutes_ago.

    Commits so the worker (which reads through its OWN seam-session) sees
    the onboarding state.
    """
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


def test_kickoff_flips_status_and_enqueues_outbox(worker_db):
    _seed_base(worker_db)
    _schedule_kickoff_in_past(worker_db)

    worker = HousewifeOnboardingKickoffWorker()
    fired = asyncio.run(worker.process_pending())

    worker_db.expire_all()
    assert fired == 1
    # Status flipped (mutation on skill_params_json — needs expire_all above)
    state = HousewifeOnboardingService(worker_db).get_raw_state(
        tenant_id="t1", user_id="u1"
    )
    assert state["status"] == STATUS_IN_PROGRESS
    assert state["current_topic"] == "addressing"
    # Outbox row with intro
    outbox_rows = worker_db.query(OutboxMessage).all()
    assert len(outbox_rows) == 1
    payload = json.loads(outbox_rows[0].payload_json)
    assert payload["chat_id"] == "100"
    assert "Среда" in payload["text"]
    assert "как мне к тебе обращаться" in payload["text"].lower()


def test_kickoff_skipped_when_user_already_in_progress(worker_db):
    """If the user wrote first and the chat handler flipped status,
    the worker's filter skips this row — no duplicate intro."""
    _seed_base(worker_db)
    service = HousewifeOnboardingService(worker_db)
    service.initialize(tenant_id="t1", user_id="u1")
    service.start(tenant_id="t1", user_id="u1")
    worker_db.commit()

    worker = HousewifeOnboardingKickoffWorker()
    fired = asyncio.run(worker.process_pending())

    worker_db.expire_all()
    assert fired == 0
    assert worker_db.query(OutboxMessage).count() == 0


def test_kickoff_not_fired_if_scheduled_in_future(worker_db):
    _seed_base(worker_db)
    service = HousewifeOnboardingService(worker_db)
    service.schedule_kickoff(
        tenant_id="t1", user_id="u1", delay_minutes=10
    )
    worker_db.commit()

    worker = HousewifeOnboardingKickoffWorker()
    fired = asyncio.run(worker.process_pending())

    worker_db.expire_all()
    assert fired == 0
    assert worker_db.query(OutboxMessage).count() == 0


def test_kickoff_without_telegram_binding_is_soft_skipped(worker_db):
    _seed_base(worker_db)
    # Remove user's telegram binding.
    user = worker_db.get(User, "u1")
    user.telegram_account_id = None
    worker_db.commit()
    _schedule_kickoff_in_past(worker_db)

    worker = HousewifeOnboardingKickoffWorker()
    fired = asyncio.run(worker.process_pending())

    worker_db.expire_all()
    assert fired == 0
    assert worker_db.query(OutboxMessage).count() == 0


def test_kickoff_does_not_fire_twice_on_consecutive_ticks(worker_db):
    """After first fire, status=in_progress — second tick must be no-op."""
    _seed_base(worker_db)
    _schedule_kickoff_in_past(worker_db)
    worker = HousewifeOnboardingKickoffWorker()

    first = asyncio.run(worker.process_pending())
    second = asyncio.run(worker.process_pending())

    worker_db.expire_all()
    assert first == 1
    assert second == 0
    assert worker_db.query(OutboxMessage).count() == 1
