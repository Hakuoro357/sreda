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
