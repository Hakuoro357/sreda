"""#166 Срез A2 — полные операции задач: перенос по времени + связки задача↔чек-лист.
Композит add_task (details_items) НЕ здесь — отложен в #163 (нужна within-turn идемпотентность)."""

from __future__ import annotations

from datetime import date, time

from sreda.runtime.react_loop import build_slice_tools
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.db.models.tasks import Task
from tests.unit.conftest import seed_telegram_user


def _tools(db_session, u):
    return {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}


def _ctx(u):
    return ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                              tool_name="add_task", tenant_id=u.tenant_id,
                              user_id=u.user_id, turn_key="tk")


def test_update_task_reschedule_moves_date_time(db_session):
    """Перенос: update_task со scheduled_date/time_start двигает задачу."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = _tools(db_session, u)
    with bind_tool_runtime(_ctx(u)):
        tools["add_task"].invoke({"title": "созвон", "scheduled_date": "2030-06-20"})
    t = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one()
    out = tools["update_task"].invoke(
        {"task_ref": t.id, "scheduled_date": "2030-06-21", "time_start": "09:00"})
    db_session.expire_all()
    t2 = db_session.query(Task).filter_by(id=t.id).one()
    assert t2.scheduled_date == date(2030, 6, 21)
    assert t2.time_start == time(9, 0)
    assert "ok:updated" in out


def test_update_task_reschedule_noop_replay_safe(db_session):
    """Идемпотентность переноса: повтор теми же датой/временем → no-op (updated_at не двигается,
    напоминание не пере-создаётся)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = _tools(db_session, u)
    with bind_tool_runtime(_ctx(u)):
        tools["add_task"].invoke(
            {"title": "созвон", "scheduled_date": "2030-06-21", "time_start": "09:00"})
    t = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one()
    before = t.updated_at
    tools["update_task"].invoke(
        {"task_ref": t.id, "scheduled_date": "2030-06-21", "time_start": "09:00"})
    db_session.expire_all()
    assert db_session.query(Task).filter_by(id=t.id).one().updated_at == before


def test_unlink_task_idempotent_when_not_linked(db_session):
    """Связки: отвязка не-связанной задачи → идемпотентно «и не была связана»."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = _tools(db_session, u)
    with bind_tool_runtime(_ctx(u)):
        tools["add_task"].invoke({"title": "созвон"})
    t = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one()
    out = tools["unlink_task"].invoke({"task_ref": t.id})
    assert "не была связана" in out.lower(), out


def test_link_task_unknown_checklist(db_session):
    """Связки: связать с несуществующим чек-листом → «не нашла»."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = _tools(db_session, u)
    with bind_tool_runtime(_ctx(u)):
        tools["add_task"].invoke({"title": "созвон"})
    t = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one()
    out = tools["link_task"].invoke({"task_ref": t.id, "checklist_ref": "chk_nope"})
    assert "не нашла" in out.lower(), out
