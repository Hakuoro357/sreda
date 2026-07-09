"""Unit tests for HousewifeReminderWorker.

#138 Ф2: воркер САМ открывает seam-сессии (privileged-скан + tenant_session
на семью), а не принимает общую сессию. Тесты через фикстуру ``worker_db``
(коммитящая файловая SQLite + шов _factory_for привязан к ней). Сессия теста
и сессии воркера — РАЗНЫЕ, поэтому: (1) сид коммитим ДО прогона воркера,
(2) ``worker_db.expire_all()`` перед ассертом на изменённые засиженные строки.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyReminder
from sreda.services.housewife_reminders import HousewifeReminderService, _coerce_utc
from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker


def _seed_base(session) -> None:
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Default"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    session.commit()


def test_worker_fires_due_reminder_and_writes_outbox(worker_db) -> None:
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Купить молоко", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()  # #138: воркер читает своим соединением → сид коммитим

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert fired == 1
    outbox = worker_db.query(OutboxMessage).all()
    assert len(outbox) == 1
    assert outbox[0].feature_key == "housewife_assistant"
    assert outbox[0].status == "pending"
    assert "🔔 Купить молоко" in outbox[0].payload_json


def test_worker_fires_due_reminder_within_grace_window(worker_db) -> None:
    """2026-04-23 single-fire mode + LATE_FIRE_GRACE: due-в-окне 15мин
    напоминание отправляется один раз и сразу финализируется (oneshot →
    fired). Future-напоминание не трогается."""
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
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
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert fired == 1
    outbox = worker_db.query(OutboxMessage).all()
    assert len(outbox) == 1
    # DueNow → fired (oneshot single-fire); Future → pending.
    fired_titles = {
        r.title for r in
        worker_db.query(FamilyReminder).filter_by(status="fired").all()
    }
    pending_titles = {
        r.title for r in
        worker_db.query(FamilyReminder).filter_by(status="pending").all()
    }
    assert fired_titles == {"DueNow"}
    assert pending_titles == {"Future"}


def test_worker_recurring_first_fire_advances_to_next_week(worker_db) -> None:
    """2026-04-23 single-fire mode: recurring reminder при первом fire
    сразу advance'ит next_trigger_at до следующей итерации RRULE
    (next Tuesday), без +2min re-ping'а."""
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
    first_tuesday = datetime(2099, 5, 5, 16, 0, tzinfo=UTC)
    reminder = svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Weekly", trigger_at=first_tuesday,
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0",
    )
    worker_db.commit()

    asyncio.run(HousewifeReminderWorker().process_pending(now=first_tuesday))

    worker_db.expire_all()
    reminder = worker_db.get(FamilyReminder, reminder.id)
    assert reminder.status == "pending"
    # escalation_count сбрасывается при advance.
    assert reminder.escalation_count == 0
    next_at = reminder.next_trigger_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    assert next_at == first_tuesday + timedelta(days=7)


def test_worker_does_not_fire_recurring_past_anchor_immediately(worker_db, monkeypatch) -> None:
    """A recurring reminder anchored in the past starts at the next future occurrence."""
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("sreda.services.housewife_reminders._utcnow", lambda: now)
    reminder = svc.schedule(
        tenant_id="tenant_1",
        user_id="user_1",
        title="Daily",
        trigger_at=datetime(2020, 1, 1, 6, 0, tzinfo=UTC),
        recurrence_rule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
    )
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    reminder = worker_db.get(FamilyReminder, reminder.id)
    assert fired == 0
    assert worker_db.query(OutboxMessage).count() == 0
    assert reminder.status == "pending"
    assert _coerce_utc(reminder.next_trigger_at) == datetime(2026, 5, 21, 6, 0, tzinfo=UTC)


def test_worker_skips_tenant_without_telegram(worker_db) -> None:
    _seed_base(worker_db)
    # Second tenant has no user → no chat_id → delivery skipped.
    worker_db.add(Tenant(id="tenant_notg", name="NoTg"))
    worker_db.add(Workspace(id="workspace_notg", tenant_id="tenant_notg", name="Default"))
    worker_db.commit()

    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 2026-04-27: схедулим в окне grace (5 минут просрочки), чтобы
    # LATE_FIRE_GRACE_MINUTES=15 не зашибил silently.
    svc.schedule(
        tenant_id="tenant_notg", user_id=None,
        title="Orphan", trigger_at=now - timedelta(minutes=5),
    )
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    # Worker returns fired count 1 (reminder state advanced), but no
    # outbox row because chat_id was unresolvable.
    worker_db.expire_all()
    assert fired == 1
    outbox = worker_db.query(OutboxMessage).all()
    assert len(outbox) == 0
