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


def test_mark_fired_oneshot_first_fire_schedules_re_ping() -> None:
    """v1.2 escalation: first mark_fired does NOT transition to 'fired';
    it bumps escalation_count to 1 and schedules a re-ping 2 min later,
    waiting for user acknowledgement via inline buttons."""
    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="One", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    fire_at = datetime(2026, 5, 1, tzinfo=UTC)
    service.mark_fired(reminder, now=fire_at)

    assert reminder.status == "pending"
    assert reminder.escalation_count == 1
    assert reminder.last_fired_at is not None
    assert reminder.next_trigger_at is not None
    assert _coerce_utc(reminder.next_trigger_at) == fire_at + timedelta(minutes=2)


def test_mark_fired_oneshot_final_fire_transitions_to_fired() -> None:
    """After ESCALATION_MAX_FIRES firings, the one-shot closes out:
    status='fired', escalation_count reset, next_trigger_at cleared."""
    from sreda.services.housewife_reminders import ESCALATION_MAX_FIRES

    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="One", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    for i in range(ESCALATION_MAX_FIRES):
        service.mark_fired(reminder, now=now + timedelta(minutes=2 * i))

    assert reminder.status == "fired"
    assert reminder.next_trigger_at is None
    assert reminder.escalation_count == 0  # reset on finalize


def test_acknowledge_oneshot_closes_without_reping() -> None:
    """User taps "Сделал ✅" on first ping → reminder goes to 'fired'
    immediately, no re-ping waits."""
    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Buy bread", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    service.mark_fired(reminder, now=datetime(2026, 5, 1, tzinfo=UTC))
    # Simulate user tap 30 seconds later.
    ack_at = datetime(2026, 5, 1, 0, 0, 30, tzinfo=UTC)
    service.acknowledge(reminder, now=ack_at)

    assert reminder.status == "fired"
    assert reminder.next_trigger_at is None
    assert reminder.acknowledged_at is not None
    assert _coerce_utc(reminder.acknowledged_at) == ack_at
    assert reminder.escalation_count == 0


def test_acknowledge_recurring_advances_without_reping() -> None:
    """Acking a weekly reminder rolls to the next occurrence and
    clears the escalation counter without waiting for the re-ping."""
    session = _fresh_session()
    service = HousewifeReminderService(session)
    first_tuesday = datetime(2026, 5, 5, 16, 0, tzinfo=UTC)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Купить хлеб",
        trigger_at=first_tuesday,
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16",
    )
    service.mark_fired(reminder, now=first_tuesday)
    service.acknowledge(reminder, now=first_tuesday + timedelta(seconds=45))

    assert reminder.status == "pending"
    # Next Tuesday, not +2min re-ping.
    assert _coerce_utc(reminder.next_trigger_at) == first_tuesday + timedelta(days=7)
    assert reminder.escalation_count == 0


def test_snooze_pushes_trigger_out_and_resets_escalation() -> None:
    """User taps "Отложить 10м ⏰" → next_trigger_at = now+10, counter
    resets so fresh escalation starts from re-ping #1 next time."""
    from sreda.services.housewife_reminders import SNOOZE_DEFAULT_MINUTES

    session = _fresh_session()
    service = HousewifeReminderService(session)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Buy bread", trigger_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    service.mark_fired(reminder, now=datetime(2026, 5, 1, tzinfo=UTC))

    snooze_at = datetime(2026, 5, 1, 0, 0, 45, tzinfo=UTC)
    service.snooze(reminder, now=snooze_at)

    assert reminder.status == "pending"
    assert reminder.escalation_count == 0
    assert reminder.acknowledged_at is None
    expected = snooze_at + timedelta(minutes=SNOOZE_DEFAULT_MINUTES)
    assert _coerce_utc(reminder.next_trigger_at) == expected


def test_mark_fired_recurring_first_fire_schedules_re_ping() -> None:
    """Recurring reminder — first fire ALSO schedules a +2min re-ping
    before advancing to next week. Escalation applies uniformly."""
    session = _fresh_session()
    service = HousewifeReminderService(session)
    first_tuesday = datetime(2026, 5, 5, 16, 0, tzinfo=UTC)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Weekly", trigger_at=first_tuesday,
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0",
    )

    service.mark_fired(reminder, now=first_tuesday)

    assert reminder.status == "pending"
    assert reminder.escalation_count == 1
    assert _coerce_utc(reminder.next_trigger_at) == first_tuesday + timedelta(minutes=2)


def test_mark_fired_recurring_final_fire_advances_next_week() -> None:
    """After escalation caps out, recurring reminder rolls to next
    occurrence with escalation_count reset."""
    from sreda.services.housewife_reminders import ESCALATION_MAX_FIRES

    session = _fresh_session()
    service = HousewifeReminderService(session)
    first_tuesday = datetime(2026, 5, 5, 16, 0, tzinfo=UTC)
    reminder = service.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Weekly", trigger_at=first_tuesday,
        recurrence_rule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=16;BYMINUTE=0",
    )
    for i in range(ESCALATION_MAX_FIRES):
        service.mark_fired(reminder, now=first_tuesday + timedelta(minutes=2 * i))

    assert reminder.status == "pending"
    assert reminder.escalation_count == 0
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
    # Finalise rem3 via mark_fired + acknowledge (new escalation flow;
    # a single mark_fired now leaves status=pending with a re-ping
    # scheduled, so list_active would still return it).
    service.mark_fired(rem3, now=datetime(2026, 5, 3, tzinfo=UTC))
    service.acknowledge(rem3, now=datetime(2026, 5, 3, 0, 1, tzinfo=UTC))
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
