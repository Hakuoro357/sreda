"""Integration tests for the 9 task LLM tools.

Each test builds the housewife tool set (just like the chat handler
does) and exercises one of the add/list/update/complete/... tools
end-to-end. Goal: verify the tool → TaskService → Task row wiring
is correct and that the string outputs the LLM reads back are the
expected shape.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.tasks import Task
from sreda.services.housewife_chat_tools import build_housewife_tools


@pytest.fixture
def tools():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    # Expose session too so tests can query DB directly.
    registered = {
        t.name: t
        for t in build_housewife_tools(
            session=sess, tenant_id="t1", user_id="u1"
        )
    }
    registered["_session"] = sess  # type: ignore[assignment]
    return registered


def _get_session(tools):
    return tools["_session"]


# ---------------------------------------------------------------------------
# add_task
# ---------------------------------------------------------------------------


def test_add_task_minimal(tools):
    res = tools["add_task"].invoke({"title": "разминка"})
    assert res.startswith("ok:created:task_")

    sess = _get_session(tools)
    row = sess.query(Task).one()
    assert row.title == "разминка"
    assert row.scheduled_date is None
    assert row.reminder_id is None


def test_add_task_with_today_and_time(tools):
    res = tools["add_task"].invoke({
        "title": "X",
        "scheduled_date": "today",
        "time_start": "07:00",
        "time_end": "07:30",
    })
    assert res.startswith("ok:created:task_")
    sess = _get_session(tools)
    row = sess.query(Task).one()
    assert row.scheduled_date is not None
    assert row.time_start == time(7, 0)
    assert row.time_end == time(7, 30)


def test_add_task_with_recurrence(tools):
    rrule = "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=4;BYMINUTE=0"
    res = tools["add_task"].invoke({
        "title": "Разминка",
        "scheduled_date": "tomorrow",
        "time_start": "07:00",
        "recurrence_rule": rrule,
    })
    assert res.startswith("ok:created:task_")
    sess = _get_session(tools)
    assert sess.query(Task).one().recurrence_rule == rrule


def test_add_task_inline_reminder_creates_reminder_row(tools):
    res = tools["add_task"].invoke({
        "title": "X",
        "scheduled_date": "tomorrow",
        "time_start": "10:00",
        "reminder_offset_minutes": 15,
    })
    assert res.startswith("ok:created:")
    assert "reminder=за 15мин" in res
    sess = _get_session(tools)
    assert sess.query(FamilyReminder).count() == 1


def test_add_task_reminder_without_time_errors_out(tools):
    """Inline reminder with no time is a contract violation — tool
    returns an error string so the LLM knows to re-call without it
    and then ask the user the post-creation question."""
    res = tools["add_task"].invoke({
        "title": "X",
        "scheduled_date": "today",
        "reminder_offset_minutes": 5,
    })
    assert res.startswith("error:")


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


def test_list_tasks_today_empty(tools):
    res = tools["list_tasks"].invoke({"date": "today"})
    assert res == "no tasks"


def test_list_tasks_today_returns_ids_and_shape(tools):
    # seed two tasks — one today, one inbox
    tools["add_task"].invoke({
        "title": "Утренняя разминка",
        "scheduled_date": "today",
        "time_start": "07:00",
        "time_end": "07:30",
    })
    tools["add_task"].invoke({"title": "inbox item"})

    res = tools["list_tasks"].invoke({"date": "today"})
    # Must show today's task, not inbox
    assert "Утренняя разминка" in res
    assert "inbox item" not in res
    # Task id present for chaining
    assert "task_" in res


def test_list_tasks_inbox_filter(tools):
    tools["add_task"].invoke({
        "title": "scheduled",
        "scheduled_date": "today",
        "time_start": "10:00",
    })
    tools["add_task"].invoke({"title": "inbox-only"})
    res = tools["list_tasks"].invoke({"date": "inbox"})
    assert "inbox-only" in res
    assert "scheduled" not in res


def test_list_tasks_all_scope(tools):
    tools["add_task"].invoke({
        "title": "today",
        "scheduled_date": "today", "time_start": "10:00",
    })
    tools["add_task"].invoke({
        "title": "tomorrow",
        "scheduled_date": "tomorrow", "time_start": "10:00",
    })
    res = tools["list_tasks"].invoke({"date": "all"})
    assert "today" in res
    assert "tomorrow" in res


# ---------------------------------------------------------------------------
# complete / uncomplete / cancel / delete
# ---------------------------------------------------------------------------


def _add_today_task(tools, title="X") -> str:
    res = tools["add_task"].invoke({
        "title": title, "scheduled_date": "today", "time_start": "10:00",
    })
    return res.split(":")[2].split(":")[0]  # extract task_<id>


def test_complete_task_marks_completed(tools):
    task_id = _add_today_task(tools)
    res = tools["complete_task"].invoke({"task_id": task_id})
    assert res == f"ok:completed:{task_id}"
    sess = _get_session(tools)
    assert sess.query(Task).one().status == "completed"


def test_uncomplete_task_restores_pending(tools):
    task_id = _add_today_task(tools)
    tools["complete_task"].invoke({"task_id": task_id})
    res = tools["uncomplete_task"].invoke({"task_id": task_id})
    assert res == f"ok:uncompleted:{task_id}"
    sess = _get_session(tools)
    assert sess.query(Task).one().status == "pending"


def test_cancel_task_soft(tools):
    task_id = _add_today_task(tools)
    res = tools["cancel_task"].invoke({"task_id": task_id})
    assert res == f"ok:cancelled:{task_id}"
    sess = _get_session(tools)
    assert sess.query(Task).count() == 1  # row still there
    assert sess.query(Task).one().status == "cancelled"


def test_delete_task_hard(tools):
    task_id = _add_today_task(tools)
    res = tools["delete_task"].invoke({"task_id": task_id})
    assert res == "ok:deleted"
    sess = _get_session(tools)
    assert sess.query(Task).count() == 0


def test_complete_unknown_task_errors(tools):
    res = tools["complete_task"].invoke({"task_id": "task_nope"})
    assert res.startswith("error:")


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


def test_update_task_title(tools):
    task_id = _add_today_task(tools, title="original")
    res = tools["update_task"].invoke({
        "task_id": task_id, "title": "new title",
    })
    assert res == f"ok:updated:{task_id}"
    sess = _get_session(tools)
    assert sess.query(Task).one().title == "new title"


# ---------------------------------------------------------------------------
# attach_reminder / detach_reminder
# ---------------------------------------------------------------------------


def test_attach_reminder_to_existing_task(tools):
    task_id = _add_today_task(tools)
    res = tools["attach_reminder"].invoke({
        "task_id": task_id, "offset_minutes": 10,
    })
    assert res.startswith("ok:reminder_attached:")
    assert "10мин" in res
    sess = _get_session(tools)
    assert sess.query(FamilyReminder).count() == 1


def test_attach_reminder_rejects_nonpositive(tools):
    task_id = _add_today_task(tools)
    assert tools["attach_reminder"].invoke({
        "task_id": task_id, "offset_minutes": 0,
    }).startswith("error:")
    assert tools["attach_reminder"].invoke({
        "task_id": task_id, "offset_minutes": -5,
    }).startswith("error:")


def test_detach_reminder(tools):
    # First create task with reminder
    res = tools["add_task"].invoke({
        "title": "X", "scheduled_date": "today",
        "time_start": "10:00", "reminder_offset_minutes": 15,
    })
    task_id = res.split(":")[2].split(":")[0]
    assert tools["detach_reminder"].invoke({"task_id": task_id}) == "ok:reminder_detached"
    sess = _get_session(tools)
    task = sess.query(Task).one()
    assert task.reminder_id is None


# ---------------------------------------------------------------------------
# all 9 tool names exposed
# ---------------------------------------------------------------------------


def test_all_nine_task_tools_registered(tools):
    expected = {
        "add_task", "list_tasks", "update_task",
        "complete_task", "uncomplete_task", "cancel_task",
        "delete_task", "attach_reminder", "detach_reminder",
    }
    missing = expected - set(tools.keys())
    assert not missing, f"task tools not registered: {missing}"
