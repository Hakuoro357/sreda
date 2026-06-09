"""R-33 (2026-05-15): Task ↔ Checklist 1-to-1 optional link tests.

Issue: vex-assistant#42
User feedback (tg_755682022): связать расписание с чеклистами — короткое
название task'а в расписании, детали — в отдельном чеклисте, клик
открывает detail view.

Tests:
1. Migration: Task model has `checklist_id` column.
2. TaskService.add_task_with_details:
   - Without details_items → ordinary task (no checklist created)
   - With details_items → fresh checklist + items + link in same TX
   - force_new behavior: title collision creates "(2)" suffix
3. TaskService.link_to_checklist:
   - happy path linking
   - idempotent same-pair → ok:already_linked
   - task already linked elsewhere → error
   - checklist already linked → error
   - task/checklist not found → error
   - archived checklist → error
4. TaskService.unlink_from_checklist:
   - linked → ok:unlinked
   - not linked → ok:not_linked (idempotent)
   - not found → error
5. GET /miniapp/api/v1/checklist/{id} endpoint includes linked_task_id.
6. R-31 archive endpoint auto-unlinks linked task.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.checklists import Checklist, ChecklistItem
from sreda.db.models.core import Tenant, User
from sreda.db.models.tasks import Task
from sreda.services.checklists import ChecklistService
from sreda.services.tasks import TaskService


def _setup_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(Tenant(id="t1", name="Test"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    s.commit()
    return s


# ── Task model has checklist_id column ──────────────────────────────


def test_task_model_has_checklist_id_column() -> None:
    """Schema: Task.checklist_id exists and is nullable."""
    assert hasattr(Task, "checklist_id")
    # SQLAlchemy column inspection
    col = Task.__table__.columns["checklist_id"]
    assert col.nullable is True


# ── TaskService.add_task_with_details ──────────────────────────────


def test_add_task_with_details_none_creates_plain_task() -> None:
    """details_items=None → ordinary task, no checklist linked."""
    s = _setup_session()
    svc = TaskService(s)
    task, _created = svc.add_task_with_details(  # #115 returns (task, created_titles)
        tenant_id="t1",
        user_id="u1",
        title="Купить молоко",
        scheduled_date=date(2026, 5, 16),
        time_start=None,
        time_end=None,
        recurrence_rule=None,
        notes=None,
        delegated_to=None,
        reminder_offset_minutes=None,
        details_items=None,
    )
    s.commit()
    assert task.checklist_id is None
    assert task.title == "Купить молоко"
    # No checklist created
    assert s.query(Checklist).count() == 0


def test_add_task_with_details_creates_fresh_checklist_with_items() -> None:
    """details_items=[...] → creates fresh checklist + items + links task atomically."""
    s = _setup_session()
    svc = TaskService(s)
    task, _created = svc.add_task_with_details(  # #115 returns (task, created_titles)
        tenant_id="t1",
        user_id="u1",
        title="Крой",
        scheduled_date=date(2026, 5, 15),
        time_start=None,
        time_end=None,
        recurrence_rule=None,
        notes=None,
        delegated_to=None,
        reminder_offset_minutes=None,
        details_items=["страйп белый", "ледяная мята", "жемчуг"],
    )
    s.commit()
    # Task linked
    assert task.checklist_id is not None
    # Checklist created with same title
    chk = s.query(Checklist).filter(Checklist.id == task.checklist_id).one()
    assert chk.title == "Крой"
    # 3 items created
    items = s.query(ChecklistItem).filter(
        ChecklistItem.checklist_id == chk.id
    ).all()
    assert len(items) == 3
    titles = sorted(it.title for it in items)
    assert titles == sorted(["страйп белый", "ледяная мята", "жемчуг"])


def test_add_task_with_details_dedups_and_reports_actual_created_titles() -> None:
    """#115: duplicate input items collapse to one row, and the returned
    created_titles reflect what was ACTUALLY created (no over-report — Codex MAJOR)."""
    s = _setup_session()
    svc = TaskService(s)
    task, created = svc.add_task_with_details(
        tenant_id="t1",
        user_id="u1",
        title="Сборы",
        scheduled_date=None,
        time_start=None,
        time_end=None,
        recurrence_rule=None,
        notes=None,
        delegated_to=None,
        reminder_offset_minutes=None,
        details_items=["паспорт", " паспорт ", "билеты"],  # 1st≡2nd after strip
    )
    s.commit()
    assert sorted(created) == sorted(["паспорт", "билеты"])  # NOT 3 — dedup applied
    items = s.query(ChecklistItem).filter(
        ChecklistItem.checklist_id == task.checklist_id
    ).all()
    assert len(items) == 2


def test_add_task_with_details_force_new_suffix_on_collision() -> None:
    """If active checklist с same title уже существует → fresh с suffix «(2)»."""
    s = _setup_session()
    svc = TaskService(s)
    # First create
    t1, _c1 = svc.add_task_with_details(  # #115
        tenant_id="t1", user_id="u1", title="Крой",
        scheduled_date=date(2026, 5, 15),
        time_start=None, time_end=None,
        recurrence_rule=None, notes=None, delegated_to=None,
        reminder_offset_minutes=None,
        details_items=["A"],
    )
    s.commit()
    # Second create — same title
    t2, _c2 = svc.add_task_with_details(  # #115
        tenant_id="t1", user_id="u1", title="Крой",
        scheduled_date=date(2026, 5, 16),
        time_start=None, time_end=None,
        recurrence_rule=None, notes=None, delegated_to=None,
        reminder_offset_minutes=None,
        details_items=["B"],
    )
    s.commit()
    chk1 = s.query(Checklist).filter(Checklist.id == t1.checklist_id).one()
    chk2 = s.query(Checklist).filter(Checklist.id == t2.checklist_id).one()
    assert chk1.title == "Крой"
    assert chk2.title == "Крой (2)"
    assert t1.checklist_id != t2.checklist_id


# ── TaskService.link_to_checklist ──────────────────────────────────


def test_link_to_checklist_happy_path() -> None:
    """task + checklist exist + active + unlinked → ok:linked."""
    s = _setup_session()
    chk_svc = ChecklistService(s)
    task_svc = TaskService(s)
    chk = chk_svc.create_list(tenant_id="t1", user_id="u1", title="L1")
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk.id,
    )
    assert status == "ok:linked"
    assert info is None
    s.commit()
    s.refresh(task)
    assert task.checklist_id == chk.id


def test_link_idempotent_same_pair() -> None:
    """Same pair → ok:already_linked."""
    s = _setup_session()
    chk_svc = ChecklistService(s)
    task_svc = TaskService(s)
    chk = chk_svc.create_list(tenant_id="t1", user_id="u1", title="L1")
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk.id,
    )
    s.commit()
    # Second call
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk.id,
    )
    assert status == "ok:already_linked"


def test_link_task_already_linked_to_other_errors() -> None:
    """Task имеет другой checklist → error."""
    s = _setup_session()
    chk_svc = ChecklistService(s)
    task_svc = TaskService(s)
    chk_a = chk_svc.create_list(tenant_id="t1", user_id="u1", title="A")
    chk_b = chk_svc.create_list(tenant_id="t1", user_id="u1", title="B")
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk_a.id,
    )
    s.commit()
    # Try to link to B
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk_b.id,
    )
    assert status == "error:task_already_linked_to_other"
    assert info == chk_a.id


def test_link_checklist_already_linked_errors() -> None:
    """Checklist already linked с another task → error."""
    s = _setup_session()
    chk_svc = ChecklistService(s)
    task_svc = TaskService(s)
    chk = chk_svc.create_list(tenant_id="t1", user_id="u1", title="C")
    t_a = task_svc.add(tenant_id="t1", user_id="u1", title="A")
    t_b = task_svc.add(tenant_id="t1", user_id="u1", title="B")
    task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=t_a.id, checklist_id=chk.id,
    )
    s.commit()
    # Try to link B
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=t_b.id, checklist_id=chk.id,
    )
    assert status == "error:checklist_already_linked_to_other"
    assert info == t_a.id


def test_link_archived_checklist_errors() -> None:
    """Archived checklist → error:archived."""
    s = _setup_session()
    chk_svc = ChecklistService(s)
    task_svc = TaskService(s)
    chk = chk_svc.create_list(tenant_id="t1", user_id="u1", title="A")
    chk_svc.archive_list(tenant_id="t1", user_id="u1", list_id=chk.id)
    s.commit()
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk.id,
    )
    assert status == "error:archived"


def test_link_nonexistent_task() -> None:
    s = _setup_session()
    task_svc = TaskService(s)
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1",
        task_id="task_nonexistent", checklist_id="checklist_x",
    )
    assert status == "error:not_found"
    assert info == "task_not_found"


def test_link_nonexistent_checklist() -> None:
    s = _setup_session()
    task_svc = TaskService(s)
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    status, info = task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1",
        task_id=task.id, checklist_id="checklist_nonexistent",
    )
    assert status == "error:not_found"
    assert info == "checklist_not_found"


# ── TaskService.unlink_from_checklist ──────────────────────────────


def test_unlink_linked_task() -> None:
    """Linked task → ok:unlinked + returns prev checklist_id."""
    s = _setup_session()
    chk_svc = ChecklistService(s)
    task_svc = TaskService(s)
    chk = chk_svc.create_list(tenant_id="t1", user_id="u1", title="A")
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    task_svc.link_to_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id, checklist_id=chk.id,
    )
    s.commit()
    status, info = task_svc.unlink_from_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id,
    )
    assert status == "ok:unlinked"
    assert info == chk.id
    s.commit()
    s.refresh(task)
    assert task.checklist_id is None


def test_unlink_unlinked_idempotent() -> None:
    """Not linked → ok:not_linked (idempotent)."""
    s = _setup_session()
    task_svc = TaskService(s)
    task = task_svc.add(tenant_id="t1", user_id="u1", title="T1")
    status, info = task_svc.unlink_from_checklist(
        tenant_id="t1", user_id="u1", task_id=task.id,
    )
    assert status == "ok:not_linked"
    assert info is None


def test_unlink_nonexistent_task() -> None:
    s = _setup_session()
    task_svc = TaskService(s)
    status, info = task_svc.unlink_from_checklist(
        tenant_id="t1", user_id="u1", task_id="task_nonexistent",
    )
    assert status == "error:not_found"


# ── ChecklistService no_commit variants ────────────────────────────


def test_create_list_no_commit_does_not_commit() -> None:
    """create_list_no_commit flushes but doesn't commit — rollback works."""
    s = _setup_session()
    svc = ChecklistService(s)
    chk = svc.create_list_no_commit(
        tenant_id="t1", user_id="u1", title="L1",
    )
    # Not yet committed → rollback removes it
    s.rollback()
    assert s.query(Checklist).count() == 0


def test_create_list_no_commit_force_new_suffix() -> None:
    """force_new=True + collision → suffix."""
    s = _setup_session()
    svc = ChecklistService(s)
    svc.create_list(tenant_id="t1", user_id="u1", title="A")
    chk2 = svc.create_list_no_commit(
        tenant_id="t1", user_id="u1", title="A", force_new=True,
    )
    s.commit()
    assert chk2.title == "A (2)"


def test_add_items_no_commit_skips_duplicates() -> None:
    s = _setup_session()
    svc = ChecklistService(s)
    chk = svc.create_list(tenant_id="t1", user_id="u1", title="A")
    created, skipped = svc.add_items_no_commit(
        list_id=chk.id, items=["A", "B", "A", "  B  "],
    )
    s.commit()
    assert len(created) == 2  # A + B (first occurrences)
    # 2 duplicates: 2nd "A" (exact match), "B " (normalized to "B")
    # Returned strings are normalized (after .strip(), pre-dedup form)
    assert sorted(skipped) == ["A", "B"]
