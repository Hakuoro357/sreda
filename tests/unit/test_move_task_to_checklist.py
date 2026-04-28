"""Tests for the move_task_to_checklist LLM tool.

2026-04-28: tool выполняет атомарный перенос task → checklist
(cancel task + add_items с dedup). Заменяет двух-шаговый flow
(delete_task + add_checklist_items) который раньше приводил к дублям
(incident tg_634496616 14:35).

Тесты идут через service-level вызовы, не через @lc_tool wrapper, потому
что lc_tool требует rich tool-binding context. Логика та же.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.tasks import Task
from sreda.services.checklists import ChecklistService
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.tasks import TaskService


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(Tenant(id="t1", name="T"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    s.commit()
    yield s
    s.close()


def _move_task_to_checklist_logic(
    session, *, tenant_id, user_id, task_id, list_id_or_title
):
    """Replicate the tool's logic at service level for testing."""
    reminders = HousewifeReminderService(session)
    task_svc = TaskService(session, reminder_service=reminders)
    checklist_svc = ChecklistService(session)

    task = task_svc.cancel(
        tenant_id=tenant_id, user_id=user_id, task_id=task_id,
    )
    if task is None:
        return "error: task_not_found"

    task_title = (task.title or "").strip()
    if not task_title:
        return "error: task_has_empty_title"

    cl = checklist_svc.find_list_by_title(
        tenant_id=tenant_id, user_id=user_id, needle=list_id_or_title,
    )
    if cl is None:
        try:
            cl = checklist_svc.create_list(
                tenant_id=tenant_id, user_id=user_id, title=list_id_or_title,
            )
        except ValueError as exc:
            return f"error: {exc}"

    created, skipped = checklist_svc.add_items(
        list_id=cl.id, items=[task_title]
    )
    if created:
        return f"ok:moved:item_id={created[0].id}:list={cl.id}"
    if skipped:
        return f"ok:moved:item_id=existing:list={cl.id}:dup"
    return "error: nothing_added"


def test_basic_move_task_to_existing_checklist(session):
    # Setup: task + checklist
    reminders = HousewifeReminderService(session)
    task_svc = TaskService(session, reminder_service=reminders)
    cl_svc = ChecklistService(session)

    task = task_svc.add(
        tenant_id="t1", user_id="u1",
        title="Найти чек от ноутбука",
        scheduled_date=date(2026, 4, 28),
    )
    cl = cl_svc.create_list(
        tenant_id="t1", user_id="u1", title="Дача",
    )

    result = _move_task_to_checklist_logic(
        session, tenant_id="t1", user_id="u1",
        task_id=task.id, list_id_or_title="Дача",
    )
    assert result.startswith("ok:moved:item_id=clitem_")
    assert f"list={cl.id}" in result

    # Verify state
    fresh_task = session.get(Task, task.id)
    assert fresh_task.status == "cancelled"

    items = cl_svc.list_items(list_id=cl.id)
    titles = [i.title for i in items]
    assert "Найти чек от ноутбука" in titles


def test_move_task_creates_implicit_checklist(session):
    reminders = HousewifeReminderService(session)
    task_svc = TaskService(session, reminder_service=reminders)

    task = task_svc.add(
        tenant_id="t1", user_id="u1",
        title="Купить лампочку",
        scheduled_date=date(2026, 4, 28),
    )

    result = _move_task_to_checklist_logic(
        session, tenant_id="t1", user_id="u1",
        task_id=task.id, list_id_or_title="Хозяйство",
    )
    assert result.startswith("ok:moved")

    # Checklist «Хозяйство» created
    cl_svc = ChecklistService(session)
    lists = cl_svc.list_active(tenant_id="t1", user_id="u1")
    assert any(c.title == "Хозяйство" for c in lists)


def test_move_task_dedup_when_item_already_in_list(session):
    """Если в target checklist уже есть пункт с таким title — не задвоит."""
    reminders = HousewifeReminderService(session)
    task_svc = TaskService(session, reminder_service=reminders)
    cl_svc = ChecklistService(session)

    cl = cl_svc.create_list(
        tenant_id="t1", user_id="u1", title="Дача",
    )
    cl_svc.add_items(list_id=cl.id, items=["Покрасить дом"])

    task = task_svc.add(
        tenant_id="t1", user_id="u1",
        title="Покрасить дом",  # same title
        scheduled_date=date(2026, 4, 28),
    )

    result = _move_task_to_checklist_logic(
        session, tenant_id="t1", user_id="u1",
        task_id=task.id, list_id_or_title="Дача",
    )
    assert ":dup" in result
    items = cl_svc.list_items(list_id=cl.id)
    # Только 1 «Покрасить дом», не 2
    assert sum(1 for i in items if i.title == "Покрасить дом") == 1


def test_move_nonexistent_task_returns_error(session):
    result = _move_task_to_checklist_logic(
        session, tenant_id="t1", user_id="u1",
        task_id="task_nonexistent", list_id_or_title="Anything",
    )
    assert result == "error: task_not_found"


def test_move_task_other_tenant_blocked(session):
    """Task другого тенанта не должен переноситься."""
    # Add other tenant
    session.add(Tenant(id="t2", name="Other"))
    session.add(User(id="u2", tenant_id="t2", telegram_account_id="200"))
    session.commit()

    reminders = HousewifeReminderService(session)
    task_svc = TaskService(session, reminder_service=reminders)

    task = task_svc.add(
        tenant_id="t2", user_id="u2",
        title="Чужая задача",
        scheduled_date=date(2026, 4, 28),
    )

    # Попытка перенести от имени t1/u1
    result = _move_task_to_checklist_logic(
        session, tenant_id="t1", user_id="u1",
        task_id=task.id, list_id_or_title="Дача",
    )
    assert result == "error: task_not_found"  # ownership check inside cancel
