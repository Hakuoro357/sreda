"""Unit tests for HousewifeReminderWorker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyReminder
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(
        Workspace(id="workspace_1", tenant_id="tenant_1", name="Default")
    )
    session.add(
        User(id="user_1", tenant_id="tenant_1", telegram_account_id="100")
    )
    session.commit()
    return session


def test_worker_fires_due_reminder_and_writes_outbox() -> None:
    session = _fresh_session()
    svc = HousewifeReminderService(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Купить молоко", trigger_at=now - timedelta(minutes=1),
    )

    worker = HousewifeReminderWorker(session)
    fired = asyncio.run(worker.process_pending(now=now))

    assert fired == 1
    outbox = session.query(OutboxMessage).all()
    assert len(outbox) == 1
    assert outbox[0].feature_key == "housewife_assistant"
    assert outbox[0].status == "pending"
    assert "🔔 Купить молоко" in outbox[0].payload_json


def test_worker_fires_due_reminder_within_grace_window() -> None:
    """2026-04-23 single-fire mode + LATE_FIRE_GRACE: due-в-окне 15мин
    напоминание отправляется один раз и сразу финализируется (oneshot →
    fired). Future-напоминание не трогается."""
    session = _fresh_session()
    svc = HousewifeReminderService(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 5 минут просрочки — внутри grace window, отправляем.
    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="DueNow", trigger_at=now - timedelta(minutes=5),
    )
    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Future", trigger_at=now + timedelta(hours=1),
    )

    worker = HousewifeReminderWorker(session)
    fired = asyncio.run(worker.process_pending(now=now))

    assert fired == 1
    outbox = session.query(OutboxMessage).all()
    assert len(outbox) == 1
    # DueNow → fired (oneshot single-fire); Future → pending.
    fired_titles = {
        r.title for r in
        session.query(FamilyReminder).filter_by(status="fired").all()
    }
    pending_titles = {
        r.title for r in
        session.query(FamilyReminder).filter_by(status="pending").all()
    }
    assert fired_titles == {"DueNow"}
    assert pending_titles == {"Future"}


def test_worker_recurring_first_fire_advances_to_next_week() -> None:
    """2026-04-23 single-fire mode: recurring reminder при первом fire
    сразу advance'ит next_trigger_at до следующей итерации RRULE
    (next Tuesday), без +2min re-ping'а."""
    session = _fresh_session()
    svc = HousewifeReminderService(session)
    first_tuesday = datetime(2026, 5, 5, 16, 0, tzinfo=UTC)
    reminder = svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Weekly", trigger_at=first_tuesday,
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0",
    )

    worker = HousewifeReminderWorker(session)
    asyncio.run(worker.process_pending(now=first_tuesday))

    session.refresh(reminder)
    assert reminder.status == "pending"
    # escalation_count сбрасывается при advance.
    assert reminder.escalation_count == 0
    next_at = reminder.next_trigger_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    assert next_at == first_tuesday + timedelta(days=7)


def test_worker_skips_tenant_without_telegram() -> None:
    session = _fresh_session()
    # Second tenant has no user → no chat_id → delivery skipped.
    session.add(Tenant(id="tenant_notg", name="NoTg"))
    session.add(Workspace(id="workspace_notg", tenant_id="tenant_notg", name="Default"))
    session.commit()

    svc = HousewifeReminderService(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 2026-04-27: схедулим в окне grace (5 минут просрочки), чтобы
    # LATE_FIRE_GRACE_MINUTES=15 не зашибил silently.
    svc.schedule(
        tenant_id="tenant_notg", user_id=None,
        title="Orphan", trigger_at=now - timedelta(minutes=5),
    )

    worker = HousewifeReminderWorker(session)
    fired = asyncio.run(worker.process_pending(now=now))

    # Worker returns fired count 1 (reminder state advanced), but no
    # outbox row because chat_id was unresolvable.
    assert fired == 1
    outbox = session.query(OutboxMessage).all()
    assert len(outbox) == 0
