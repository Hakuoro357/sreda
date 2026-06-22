"""Фаза 0 #162 — идемпотентность создания задач в ReAct-пути.

Симметрично напоминаниям (см. test_react_loop_reminder_idempotency.py), но
logical_key задачи включает scheduled_date (а не trigger_iso):
  п.1 — повтор внутри хода (тот же ctx) → одна строка.
  п.2 — тот же title, ДРУГАЯ дата → две строки.
  п.11 — без ctx (легаси) → поведение прежнее.
"""

from __future__ import annotations

from datetime import date

from sreda.db.models.tasks import Task
from sreda.runtime.planner.tool_runtime import (
    ToolRuntimeContext,
    bind_tool_runtime,
)
from sreda.services.tasks import TaskService
from tests.unit.conftest import seed_telegram_user


def _ctx(*, tenant_id: str, user_id: str, step_id: str = "step_1") -> ToolRuntimeContext:
    return ToolRuntimeContext(
        operation_id="op_step_level_test",
        execution_id="exec_react_test",
        step_id=step_id,
        tool_name="add_task",
        tenant_id=tenant_id,
        user_id=user_id,
        turn_key="react:max:tenant_1:msg_1",
    )


def test_add_task_ctx_replay_single_row(db_session):
    """п.1: повтор того же tool_call внутри хода → одна строка."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)
    when = date(2030, 1, 1)
    ctx = _ctx(tenant_id=u.tenant_id, user_id=u.user_id)

    with bind_tool_runtime(ctx):
        svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="купить корм", scheduled_date=when)
    with bind_tool_runtime(ctx):
        svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="купить корм", scheduled_date=when)

    rows = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).all()
    assert len(rows) == 1, f"ожидали 1 строку (идемпотентно), получили {len(rows)}"


def test_add_task_same_title_different_date_two_rows(db_session):
    """п.2: тот же ctx-шаг и title, но ДРУГАЯ дата → две строки (дата в ключе)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)
    ctx = _ctx(tenant_id=u.tenant_id, user_id=u.user_id)

    with bind_tool_runtime(ctx):
        svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="полить цветы", scheduled_date=date(2030, 1, 1))
    with bind_tool_runtime(ctx):
        svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="полить цветы", scheduled_date=date(2030, 1, 2))

    rows = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).all()
    assert len(rows) == 2, f"разная дата — ожидали 2 строки, получили {len(rows)}"


def test_add_task_legacy_no_ctx_unchanged(db_session):
    """п.11: без ctx — каждый вызов создаёт строку."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)
    when = date(2030, 1, 1)

    svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="купить корм", scheduled_date=when)
    svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="купить корм", scheduled_date=when)

    rows = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).all()
    assert len(rows) == 2, "легаси без ctx — без дедупа, 2 вызова = 2 строки"


def test_tasks_update_commit_false_does_not_persist_163():
    """#163 Фаза 3a-2: tasks.update(commit=False) НЕ коммитит (helper владеет). Плайн-сессия."""
    from datetime import date, datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant, User
    from sreda.db.models.tasks import Task
    from sreda.services.tasks import TaskService

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(Tenant(id="t1", name="T"))
    s.add(User(id="u1", tenant_id="t1"))
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    s.add(Task(id="task_x", tenant_id="t1", user_id="u1", title="старое",
               scheduled_date=date(2030, 6, 20), status="pending", created_at=now, updated_at=now))
    s.commit()
    TaskService(s).update(tenant_id="t1", user_id="u1", task_id="task_x",
                          title="новое", commit=False)
    assert s.get(Task, "task_x").title == "новое"  # в сессии (flush)
    s.rollback()
    assert s.get(Task, "task_x").title == "старое", "commit=False не должен фиксировать"


def test_tasks_cancel_delete_commit_false_does_not_persist_163():
    """#163 Фаза 3 cancel/delete: commit=False НЕ коммитит (helper владеет). Плайн-сессия."""
    from datetime import date, datetime, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.core import Tenant, User
    from sreda.db.models.tasks import Task
    from sreda.services.tasks import TaskService

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(Tenant(id="t1", name="T"))
    s.add(User(id="u1", tenant_id="t1"))
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    s.add(Task(id="c1", tenant_id="t1", user_id="u1", title="отменяемая",
               scheduled_date=date(2030, 6, 20), status="pending", created_at=now, updated_at=now))
    s.add(Task(id="d1", tenant_id="t1", user_id="u1", title="удаляемая",
               scheduled_date=date(2030, 6, 20), status="pending", created_at=now, updated_at=now))
    s.commit()
    svc = TaskService(s)
    svc.cancel(tenant_id="t1", user_id="u1", task_id="c1", commit=False)
    svc.delete(tenant_id="t1", user_id="u1", task_id="d1", commit=False)
    assert s.get(Task, "c1").status == "cancelled"  # в сессии
    assert s.get(Task, "d1") is None  # удалена в сессии (flush)
    s.rollback()
    assert s.get(Task, "c1").status == "pending", "cancel(commit=False) не должен фиксировать"
    assert s.get(Task, "d1") is not None, "delete(commit=False) не должен фиксировать"
