"""Unit tests for HousewifeReminderService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyReminder
from sreda.services.housewife_reminders import HousewifeReminderService, _coerce_utc


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    session.commit()
    return session


def test_schedule_oneshot_sets_pending_and_next_trigger() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    trigger_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    reminder = service.schedule(
        tenant_id="tenant_1",
        user_id="user_1",
        title="Купить молоко",
        trigger_at=trigger_at,
    )

    assert reminder.status == "pending"
    # SQLite strips tzinfo on round-trip; compare after coercion.
    assert _coerce_utc(reminder.next_trigger_at) == trigger_at
    assert reminder.recurrence_rule is None
    assert reminder.last_fired_at is None


def test_schedule_weekly_rrule_preserved() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)

    reminder = service.schedule(
        tenant_id="tenant_1",
        user_id="user_1",
        title="Кружок Пети",
        trigger_at=datetime(2026, 5, 5, 16, 0, tzinfo=UTC),  # Tuesday
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0",
    )

    assert reminder.status == "pending"
    assert reminder.recurrence_rule == "FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0"


def test_schedule_rejects_invalid_rrule() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)

    with pytest.raises(ValueError, match="invalid recurrence_rule"):
        service.schedule(
            tenant_id="tenant_1",
            user_id="user_1",
            title="Bad",
            trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
            recurrence_rule="NOT_A_VALID_RRULE",
        )


def test_due_now_returns_past_pending_only() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Past", trigger_at=now - timedelta(hours=1),
    )
    service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Future", trigger_at=now + timedelta(hours=1),
    )

    due = service.due_now(now=now)

    assert len(due) == 1
    assert due[0].title == "Past"


def test_mark_fired_oneshot_transitions_to_fired() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="One", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    service.mark_fired(reminder, now=datetime(2026, 5, 1, tzinfo=UTC))

    assert reminder.status == "fired"
    assert reminder.next_trigger_at is None
    assert reminder.last_fired_at is not None


def test_mark_fired_recurring_advances_next_trigger() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    first_tuesday = datetime(2026, 5, 5, 16, 0, tzinfo=UTC)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Weekly", trigger_at=first_tuesday,
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0",
    )

    # Simulate firing at the first occurrence; next should be +7 days.
    service.mark_fired(reminder, now=first_tuesday)

    assert reminder.status == "pending"
    assert _coerce_utc(reminder.next_trigger_at) == first_tuesday + timedelta(days=7)


def test_cancel_sets_cancelled_status_and_clears_next_trigger() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="X", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    ok = service.cancel(tenant_id="tenant_1", reminder_id=reminder.id)

    assert ok is True
    session.refresh(reminder)
    assert reminder.status == "cancelled"
    assert reminder.next_trigger_at is None


def test_cancel_cross_tenant_denied() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="X", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    ok = service.cancel(tenant_id="tenant_other", reminder_id=reminder.id)

    assert ok is False


def test_list_active_excludes_fired_and_cancelled() -> None:
    session = _fresh_session()
    service = HousewifeReminderService(session)
    rem1 = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Active", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    rem2 = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="ToCancel", trigger_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    rem3 = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="ToFire", trigger_at=datetime(2026, 5, 3, tzinfo=UTC),
    )
    service.cancel(tenant_id="tenant_1", reminder_id=rem2.id)
    service.mark_fired(rem3, now=datetime(2026, 5, 3, tzinfo=UTC))
    session.commit()

    active = service.list_active(tenant_id="tenant_1")

    assert [r.id for r in active] == [rem1.id]


# ---------------------------------------------------------------------------
# Timezone regression (2026-04-19): reminders scheduled with an explicit
# offset like +03:00 were stored as local clock values instead of UTC,
# so due_now() never matched. Fix: _coerce_utc now astimezone's to UTC.
# ---------------------------------------------------------------------------


def test_coerce_utc_converts_aware_non_utc() -> None:
    from datetime import timezone as tz_mod

    msk = tz_mod(timedelta(hours=3))
    aware_msk = datetime(2026, 4, 19, 13, 30, tzinfo=msk)

    converted = _coerce_utc(aware_msk)

    assert converted.tzinfo is UTC
    assert converted.hour == 10  # 13:30 MSK = 10:30 UTC


def test_coerce_utc_noop_on_utc_aware() -> None:
    aware_utc = datetime(2026, 4, 19, 10, 30, tzinfo=UTC)
    assert _coerce_utc(aware_utc) == aware_utc


def test_coerce_utc_tags_naive_as_utc() -> None:
    naive = datetime(2026, 4, 19, 10, 30)
    converted = _coerce_utc(naive)
    assert converted.tzinfo is UTC
    assert converted.hour == 10


def test_schedule_with_msk_offset_fires_when_utc_due() -> None:
    """Regression: MSK-offset reminder must become due at the correct
    UTC instant. Stores 13:30 MSK; worker at 12:30 UTC (=15:30 MSK)
    must see it as due because 10:30 UTC < 12:30 UTC."""
    from datetime import timezone as tz_mod

    msk = tz_mod(timedelta(hours=3))
    session = _fresh_session()
    service = HousewifeReminderService(session)

    service.schedule(
        tenant_id="tenant_1",
        user_id="user_1",
        title="Сделать оливье",
        trigger_at=datetime(2026, 4, 19, 13, 30, tzinfo=msk),  # 10:30 UTC
    )

    # Simulated "now" at 12:30 UTC (=15:30 MSK, after the scheduled time).
    now_utc = datetime(2026, 4, 19, 12, 30, tzinfo=UTC)
    due = service.due_now(now=now_utc)

    assert len(due) == 1
    assert due[0].title == "Сделать оливье"
