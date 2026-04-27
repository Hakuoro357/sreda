"""Tests for TaskService — CRUD + date/status filters + RRULE validation.

Reminder-linkage behaviour (attach/detach/cancel-on-complete) has
its own file ``test_task_reminder_link.py`` so the scenarios stay
readable; this file exercises the task-only paths.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.tasks import Task
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
# add / basic fields
# ---------------------------------------------------------------------------


def test_add_minimal(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="Утренняя разминка")
    assert t.id.startswith("task_")
    assert t.title == "Утренняя разминка"
    assert t.status == "pending"
    assert t.scheduled_date is None
    assert t.time_start is None
    assert t.reminder_id is None


def test_add_with_full_fields(session):
    svc = TaskService(session)
    t = svc.add(
        tenant_id="t1", user_id="u1",
        title="Встреча с врачом",
        scheduled_date=date(2026, 4, 24),
        time_start=time(10, 0),
        time_end=time(11, 0),
        notes="в поликлинике на Ленина",
    )
    assert t.scheduled_date == date(2026, 4, 24)
    assert t.time_start == time(10, 0)
    assert t.time_end == time(11, 0)
    assert t.notes == "в поликлинике на Ленина"


def test_add_title_required(session):
    svc = TaskService(session)
    with pytest.raises(ValueError, match="title"):
        svc.add(tenant_id="t1", user_id="u1", title="   ")


def test_add_title_is_trimmed_and_capped(session):
    svc = TaskService(session)
    long_title = "  задача " + "X" * 1000
    t = svc.add(tenant_id="t1", user_id="u1", title=long_title)
    # Encrypted column returns what was stored — trimming + 500-cap applied
    assert t.title.startswith("задача")
    assert len(t.title) <= 500


# ---------------------------------------------------------------------------
# update / complete / cancel / delete
# ---------------------------------------------------------------------------


def test_update_partial_fields_only(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="Первоначальное")
    updated = svc.update(
        tenant_id="t1", user_id="u1", task_id=t.id,
        title="Новое название",
    )
    assert updated is not None
    assert updated.title == "Новое название"
    # Untouched fields stay None
    assert updated.scheduled_date is None


def test_update_unknown_task_returns_none(session):
    svc = TaskService(session)
    assert svc.update(
        tenant_id="t1", user_id="u1",
        task_id="task_does_not_exist", title="X",
    ) is None


def test_update_cross_tenant_safe(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="моя задача")
    # Different tenant/user tries to update — service returns None
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()
    result = svc.update(tenant_id="t2", user_id="u2", task_id=t.id, title="hacked")
    assert result is None
    # Original row unchanged
    assert svc._get("t1", "u1", t.id).title == "моя задача"


def test_complete_sets_status_and_timestamp(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="разминка")
    done = svc.complete(tenant_id="t1", user_id="u1", task_id=t.id)
    assert done is not None
    assert done.status == "completed"
    assert done.completed_at is not None


def test_uncomplete_restores_pending(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="X")
    svc.complete(tenant_id="t1", user_id="u1", task_id=t.id)
    back = svc.uncomplete(tenant_id="t1", user_id="u1", task_id=t.id)
    assert back is not None
    assert back.status == "pending"
    assert back.completed_at is None


def test_cancel_soft(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="X")
    cancelled = svc.cancel(tenant_id="t1", user_id="u1", task_id=t.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    # Row still in DB
    assert session.query(Task).count() == 1


def test_delete_hard(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="X")
    ok = svc.delete(tenant_id="t1", user_id="u1", task_id=t.id)
    assert ok is True
    assert session.query(Task).count() == 0


def test_delete_unknown_returns_false(session):
    svc = TaskService(session)
    assert svc.delete(
        tenant_id="t1", user_id="u1", task_id="task_none",
    ) is False


# ---------------------------------------------------------------------------
# list / filters
# ---------------------------------------------------------------------------


def test_list_today_only_today_pending(session):
    svc = TaskService(session)
    today = date(2026, 4, 23)
    tomorrow = date(2026, 4, 24)
    svc.add(tenant_id="t1", user_id="u1", title="сегодня 1",
            scheduled_date=today, time_start=time(7, 0))
    svc.add(tenant_id="t1", user_id="u1", title="завтра",
            scheduled_date=tomorrow, time_start=time(9, 0))
    svc.add(tenant_id="t1", user_id="u1", title="inbox")  # без даты

    rows = svc.list_today(tenant_id="t1", user_id="u1", today=today)
    titles = [r.title for r in rows]
    assert titles == ["сегодня 1"]


# ---------------------------------------------------------------------------
# RRULE expansion — recurring tasks must surface on every matching day
# without an auto-clone worker. Regression guard for 2026-04-23 prod:
# a daily task stayed bound to its original scheduled_date.
# ---------------------------------------------------------------------------


def test_list_today_expands_daily_rrule_on_later_day(session):
    """A daily task created on day N must show on day N+5 too."""
    svc = TaskService(session)
    start_day = date(2026, 4, 23)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Прогулка с собакой",
        scheduled_date=start_day,
        time_start=time(18, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=15;BYMINUTE=0",
    )
    rows = svc.list_today(
        tenant_id="t1", user_id="u1",
        today=date(2026, 4, 28),
    )
    assert [r.title for r in rows] == ["Прогулка с собакой"]


def test_list_today_skips_recurring_before_its_start_date(session):
    """A daily task starting tomorrow must not leak into today."""
    svc = TaskService(session)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Future daily",
        scheduled_date=date(2026, 5, 1),
        time_start=time(9, 0),
        recurrence_rule="FREQ=DAILY",
    )
    rows = svc.list_today(
        tenant_id="t1", user_id="u1",
        today=date(2026, 4, 30),
    )
    assert rows == []


def test_list_today_no_dup_for_recurring_on_its_start_day(session):
    """On the very first day of a recurring task the row must appear once,
    not twice (once from one-shot scan, once from RRULE expansion)."""
    svc = TaskService(session)
    today = date(2026, 4, 23)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Daily from today",
        scheduled_date=today,
        time_start=time(18, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=15;BYMINUTE=0",
    )
    rows = svc.list_today(tenant_id="t1", user_id="u1", today=today)
    assert len(rows) == 1


def test_list_today_weekly_rrule_only_on_matching_weekday(session):
    """MO-only weekly rule yields occurrence on Mondays, not Tuesdays."""
    svc = TaskService(session)
    start = date(2026, 4, 20)  # 2026-04-20 is a Monday
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Понедельничное совещание",
        scheduled_date=start,
        time_start=time(10, 0),
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0",
    )
    # Next Monday = 2026-04-27
    rows_mo = svc.list_today(
        tenant_id="t1", user_id="u1", today=date(2026, 4, 27),
    )
    assert len(rows_mo) == 1
    # Tuesday = 2026-04-28
    rows_tu = svc.list_today(
        tenant_id="t1", user_id="u1", today=date(2026, 4, 28),
    )
    assert rows_tu == []


def test_list_completed_filter(session):
    svc = TaskService(session)
    t = svc.add(tenant_id="t1", user_id="u1", title="X",
                scheduled_date=date(2026, 4, 23))
    svc.complete(tenant_id="t1", user_id="u1", task_id=t.id)
    # pending view: empty
    assert svc.list(
        tenant_id="t1", user_id="u1",
        scheduled_date=date(2026, 4, 23), status="pending",
    ) == []
    # completed view: 1
    rows = svc.list(
        tenant_id="t1", user_id="u1",
        scheduled_date=date(2026, 4, 23), status="completed",
    )
    assert len(rows) == 1


def test_list_orders_by_time(session):
    svc = TaskService(session)
    today = date(2026, 4, 23)
    svc.add(tenant_id="t1", user_id="u1", title="вечер",
            scheduled_date=today, time_start=time(20, 0))
    svc.add(tenant_id="t1", user_id="u1", title="утро",
            scheduled_date=today, time_start=time(7, 0))
    svc.add(tenant_id="t1", user_id="u1", title="день",
            scheduled_date=today, time_start=time(13, 0))
    rows = svc.list_today(tenant_id="t1", user_id="u1", today=today)
    assert [r.title for r in rows] == ["утро", "день", "вечер"]


def test_list_no_date_rows(session):
    svc = TaskService(session)
    svc.add(tenant_id="t1", user_id="u1", title="inbox-1")
    svc.add(tenant_id="t1", user_id="u1", title="scheduled",
            scheduled_date=date(2026, 4, 23))
    rows = svc.list(
        tenant_id="t1", user_id="u1",
        scheduled_date=None, include_no_date=True,
    )
    assert [r.title for r in rows] == ["inbox-1"]


# ---------------------------------------------------------------------------
# find_by_title — the "выполнил разминку" path
# ---------------------------------------------------------------------------


def test_find_by_title_substring_case_insensitive(session):
    svc = TaskService(session)
    today = date(2026, 4, 23)
    svc.add(tenant_id="t1", user_id="u1", title="Утренняя разминка",
            scheduled_date=today, time_start=time(7, 0))
    svc.add(tenant_id="t1", user_id="u1", title="Встреча с врачом",
            scheduled_date=today, time_start=time(10, 0))

    hit = svc.find_by_title(
        tenant_id="t1", user_id="u1", needle="РАЗМИНКА",
        scheduled_date=today,
    )
    assert hit is not None
    assert hit.title == "Утренняя разминка"


def test_find_by_title_no_match(session):
    svc = TaskService(session)
    svc.add(tenant_id="t1", user_id="u1", title="X",
            scheduled_date=date(2026, 4, 23), time_start=time(7, 0))
    assert svc.find_by_title(
        tenant_id="t1", user_id="u1", needle="несуществующее",
        scheduled_date=date(2026, 4, 23),
    ) is None


def test_find_by_title_empty_needle(session):
    """Defensive — empty search shouldn't return the first random task."""
    svc = TaskService(session)
    svc.add(tenant_id="t1", user_id="u1", title="X",
            scheduled_date=date(2026, 4, 23))
    assert svc.find_by_title(
        tenant_id="t1", user_id="u1", needle="  ",
        scheduled_date=date(2026, 4, 23),
    ) is None


# ---------------------------------------------------------------------------
# list_range — multi-day query for the Mini App week view.
# Must respect the same RRULE-expansion rules as list_today, but across
# an arbitrary window. Returns dict[date, list[Task]] keyed by every
# date in [from_date, to_date] (inclusive). Empty days get an empty
# list so callers don't need to handle missing keys.
# ---------------------------------------------------------------------------


def test_list_range_includes_one_shots_across_days(session):
    svc = TaskService(session)
    svc.add(tenant_id="t1", user_id="u1", title="mon-task",
            scheduled_date=date(2026, 4, 20), time_start=time(9, 0))
    svc.add(tenant_id="t1", user_id="u1", title="wed-task",
            scheduled_date=date(2026, 4, 22), time_start=time(14, 0))
    svc.add(tenant_id="t1", user_id="u1", title="fri-task",
            scheduled_date=date(2026, 4, 24), time_start=time(18, 0))
    svc.add(tenant_id="t1", user_id="u1", title="out-of-range",
            scheduled_date=date(2026, 5, 1), time_start=time(9, 0))

    result = svc.list_range(
        tenant_id="t1", user_id="u1",
        from_date=date(2026, 4, 20), to_date=date(2026, 4, 24),
    )
    assert set(result.keys()) == {
        date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22),
        date(2026, 4, 23), date(2026, 4, 24),
    }
    assert [t.title for t in result[date(2026, 4, 20)]] == ["mon-task"]
    assert result[date(2026, 4, 21)] == []
    assert [t.title for t in result[date(2026, 4, 22)]] == ["wed-task"]
    assert result[date(2026, 4, 23)] == []
    assert [t.title for t in result[date(2026, 4, 24)]] == ["fri-task"]


def test_list_range_expands_daily_recurring_across_full_range(session):
    svc = TaskService(session)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Прогулка",
        scheduled_date=date(2026, 4, 20),
        time_start=time(18, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=15;BYMINUTE=0",
    )
    result = svc.list_range(
        tenant_id="t1", user_id="u1",
        from_date=date(2026, 4, 20), to_date=date(2026, 4, 26),
    )
    assert len(result) == 7
    for d, rows in result.items():
        assert len(rows) == 1, f"missing on {d}"
        assert rows[0].title == "Прогулка"


def test_list_range_weekly_rrule_only_on_byday(session):
    """BYDAY=MO,WE yields exactly two occurrences in a full ПН–ВС week."""
    svc = TaskService(session)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Встреча команды",
        scheduled_date=date(2026, 4, 20),  # monday
        time_start=time(10, 0),
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO,WE;BYHOUR=7;BYMINUTE=0",
    )
    result = svc.list_range(
        tenant_id="t1", user_id="u1",
        from_date=date(2026, 4, 20), to_date=date(2026, 4, 26),
    )
    days_with_task = [d for d, rows in result.items() if rows]
    assert days_with_task == [date(2026, 4, 20), date(2026, 4, 22)]


def test_list_range_skips_before_start_date(session):
    """A task starting 2026-05-01 must not appear on any April day."""
    svc = TaskService(session)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Future",
        scheduled_date=date(2026, 5, 1),
        time_start=time(9, 0),
        recurrence_rule="FREQ=DAILY",
    )
    result = svc.list_range(
        tenant_id="t1", user_id="u1",
        from_date=date(2026, 4, 20), to_date=date(2026, 4, 30),
    )
    assert all(rows == [] for rows in result.values())


def test_list_today_still_works_after_refactor(session):
    """Regression for plugin count: list_today must behave exactly
    as before (one-day RRULE-aware list) after being folded into
    list_range."""
    svc = TaskService(session)
    today = date(2026, 4, 23)
    svc.add(
        tenant_id="t1", user_id="u1",
        title="Прогулка",
        scheduled_date=date(2026, 4, 20),
        time_start=time(18, 0),
        recurrence_rule="FREQ=DAILY;BYHOUR=15;BYMINUTE=0",
    )
    rows = svc.list_today(tenant_id="t1", user_id="u1", today=today)
    assert [r.title for r in rows] == ["Прогулка"]
