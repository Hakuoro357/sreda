"""Tests for Task ↔ FamilyReminder linkage.

The integration points that matter:

  * ``add(reminder_offset_minutes=N)`` on a scheduled+timed task
    creates a FamilyReminder at ``scheduled_datetime - N minutes``,
    copying the RRULE if the task is recurring.
  * ``attach_reminder`` does the same for an existing task.
  * ``detach_reminder`` cancels the underlying FamilyReminder and
    clears the FK.
  * ``complete`` on a ONE-SHOT task cancels the reminder (no ping
    for done work). ``complete`` on a RECURRING task leaves the
    reminder alive (tomorrow's occurrence still fires).
  * ``cancel`` / ``delete`` always cancel the reminder.
  * ``update`` that moves ``time_start`` reschedules the reminder.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyReminder
from sreda.services.tasks import TaskService


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


# ---------------------------------------------------------------------------
# add with reminder_offset_minutes
# ---------------------------------------------------------------------------


def test_add_with_reminder_creates_linked_reminder(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1",
        title="Встреча",
        scheduled_date=date(2026, 4, 24),
        time_start=time(10, 0),
        reminder_offset_minutes=15,
    )
    assert t.reminder_id is not None
    assert t.reminder_offset_minutes == 15

    reminder = (
        session.query(FamilyReminder)
        .filter(FamilyReminder.id == t.reminder_id)
        .one()
    )
    # trigger = 10:00 - 15m = 09:45 UTC (task times are stored as UTC
    # in MVP — user-facing conversion happens at LLM boundary)
    expected = datetime(2026, 4, 24, 9, 45, tzinfo=timezone.utc)
    assert reminder.trigger_at.replace(tzinfo=timezone.utc) == expected
    assert reminder.status == "pending"
    assert reminder.title.startswith("⏰")  # clock emoji marker


def test_add_without_time_rejects_reminder_param(session):
    """Reminder needs a concrete trigger — just a date isn't enough."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1",
        title="задача без времени",
        scheduled_date=date(2026, 4, 24),
        # no time_start
        reminder_offset_minutes=15,
    )
    # Task still created, just without the reminder link
    assert t.reminder_id is None
    assert t.reminder_offset_minutes is None


def test_add_recurring_task_with_reminder_copies_rrule(session):
    svc = TaskService(session)
    rrule = "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=4;BYMINUTE=0"
    t = svc.add(
        tenant_id="t1", user_id="u1",
        title="Утренняя разминка",
        scheduled_date=date(2026, 4, 27),  # Monday
        time_start=time(7, 0),
        recurrence_rule=rrule,
        reminder_offset_minutes=10,
    )
    reminder = (
        session.query(FamilyReminder)
        .filter(FamilyReminder.id == t.reminder_id)
        .one()
    )
    # Recurrence copied verbatim so the reminder pings every weekday
    assert reminder.recurrence_rule == rrule


# ---------------------------------------------------------------------------
# attach_reminder (late binding)
# ---------------------------------------------------------------------------


def test_attach_reminder_to_existing_task(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1",
        title="X",
        scheduled_date=date(2026, 4, 24),
        time_start=time(10, 0),
    )
    assert t.reminder_id is None  # no reminder yet

    attached = svc.attach_reminder(
        tenant_id="t1", user_id="u1", task_id=t.id, offset_minutes=5,
    )
    assert attached is not None
    assert attached.reminder_id is not None
    assert attached.reminder_offset_minutes == 5


def test_attach_reminder_without_schedule_raises(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="inbox task")
    with pytest.raises(ValueError, match="scheduled"):
        svc.attach_reminder(
            tenant_id="t1", user_id="u1", task_id=t.id, offset_minutes=10,
        )


def test_attach_reminder_replaces_existing(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1",
        title="X", scheduled_date=date(2026, 4, 24),
        time_start=time(10, 0), reminder_offset_minutes=15,
    )
    first_reminder_id = t.reminder_id

    svc.attach_reminder(
        tenant_id="t1", user_id="u1", task_id=t.id, offset_minutes=5,
    )
    # Old reminder cancelled
    old = session.query(FamilyReminder).filter(
        FamilyReminder.id == first_reminder_id
    ).one()
    assert old.status == "cancelled"
    # New reminder linked
    session.refresh(t)
    assert t.reminder_id != first_reminder_id
    assert t.reminder_offset_minutes == 5


def test_detach_reminder_cancels_and_nulls(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="X",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15,
    )
    rid = t.reminder_id

    detached = svc.detach_reminder(
        tenant_id="t1", user_id="u1", task_id=t.id,
    )
    assert detached.reminder_id is None
    assert detached.reminder_offset_minutes is None

    old = session.query(FamilyReminder).filter(FamilyReminder.id == rid).one()
    assert old.status == "cancelled"


# ---------------------------------------------------------------------------
# complete behaviour: one-shot cancels, recurring keeps alive
# ---------------------------------------------------------------------------


def test_complete_one_shot_cancels_reminder(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="X",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15,
    )
    rid = t.reminder_id

    svc.complete(tenant_id="t1", user_id="u1", task_id=t.id)

    reminder = session.query(FamilyReminder).filter(
        FamilyReminder.id == rid
    ).one()
    assert reminder.status == "cancelled"
    session.refresh(t)
    assert t.reminder_id is None  # link cleared


def test_complete_recurring_keeps_reminder_active(session):
    """For a recurring task, "выполнил" closes today's instance but
    tomorrow's ping should still fire — reminder stays pending."""
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Разминка",
        scheduled_date=date(2026, 4, 27),
        time_start=time(7, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=4",
        reminder_offset_minutes=5,
    )
    rid = t.reminder_id

    svc.complete(tenant_id="t1", user_id="u1", task_id=t.id)

    reminder = session.query(FamilyReminder).filter(
        FamilyReminder.id == rid
    ).one()
    assert reminder.status == "pending"  # still alive
    # Task still references the reminder for tomorrow's ping context
    session.refresh(t)
    assert t.reminder_id == rid


# ---------------------------------------------------------------------------
# cancel / delete always cancel the reminder
# ---------------------------------------------------------------------------


def test_cancel_cancels_linked_reminder(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="X",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15,
    )
    rid = t.reminder_id

    svc.cancel(tenant_id="t1", user_id="u1", task_id=t.id)
    assert session.query(FamilyReminder).filter(
        FamilyReminder.id == rid
    ).one().status == "cancelled"


def test_delete_cancels_linked_reminder(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="X",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15,
    )
    rid = t.reminder_id

    svc.delete(tenant_id="t1", user_id="u1", task_id=t.id)
    assert session.query(FamilyReminder).filter(
        FamilyReminder.id == rid
    ).one().status == "cancelled"


# ---------------------------------------------------------------------------
# update with schedule change reschedules reminder
# ---------------------------------------------------------------------------


def test_update_time_reschedules_reminder(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="X",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15,
    )
    old_rid = t.reminder_id

    updated = svc.update(
        tenant_id="t1", user_id="u1", task_id=t.id,
        time_start=time(14, 0),  # push to 14:00
    )
    # Old reminder cancelled, new one attached
    assert updated.reminder_id is not None
    assert updated.reminder_id != old_rid
    assert updated.reminder_offset_minutes == 15

    new_reminder = session.query(FamilyReminder).filter(
        FamilyReminder.id == updated.reminder_id
    ).one()
    # Trigger = 14:00 - 15m = 13:45
    assert new_reminder.trigger_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 4, 24, 13, 45, tzinfo=timezone.utc
    )
    # Old reminder cancelled
    assert session.query(FamilyReminder).filter(
        FamilyReminder.id == old_rid
    ).one().status == "cancelled"
