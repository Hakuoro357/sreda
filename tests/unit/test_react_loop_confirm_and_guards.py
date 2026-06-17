"""Фаза 0 #162 — confirm/resume через граф + гарды + регрессы.

Закрывает чеклист-пункты, не покрытые ранее (код-ревью субагента, ПРАВИЛО #7):
  п.3 — destructive confirm: «да»→удаление, «нет»→отказ, повтор идемпотентен;
  п.8 — fail-closed user_id в ctx-пути create;
  п.12 — scrub rem_/task_/checklist_/(ref …), без съедания нормального текста;
  регресс — list_tasks возвращает датированную задачу (баг include_no_date, найден живым прогоном).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from langchain_core.messages import AIMessage

from sreda.db.models.housewife import FamilyReminder
from sreda.runtime import react_loop
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.runtime.react_loop import _scrub_ids, build_slice_tools, handle_turn
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.tasks import TaskService
from tests.unit.conftest import seed_telegram_user


class _StubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


def _seed_reminder(db_session, u) -> str:
    rid = f"rem_{uuid4().hex[:20]}"
    when = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    db_session.add(FamilyReminder(
        id=rid, tenant_id=u.tenant_id, user_id=u.user_id, title="разминка",
        trigger_at=when, next_trigger_at=when, status="pending"))
    db_session.commit()
    return rid


def _cancel_script(rid: str) -> _StubLLM:
    return _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "cancel_reminder", "args": {"reminder_ref": rid}, "id": "call_1"}]),
        AIMessage(content="Готово."),
    ])


@pytest.mark.asyncio
async def test_confirm_cancel_yes_deletes(db_session):
    """п.3: ход1 → пауза-подтверждение (не удалено); ход2 «да» → удалено."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="удали разминку", inbound_message_id="m1", channel="max")
    assert "удалить" in r1.lower(), r1
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "pending", "до «да» не удалять"
    # ход2 «да» (resume) — НОВЫЙ stub не нужен: cancel re-run сам, chat вернёт финал
    r2 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "cancelled", r2


@pytest.mark.asyncio
async def test_confirm_cancel_no_keeps(db_session):
    """п.3: ход2 «нет» → НЕ удалено (fail-closed)."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                      thread_id=thread, llm=_cancel_script(rid),
                      user_text="удали разминку", inbound_message_id="m1", channel="max")
    await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                      thread_id=thread, llm=_cancel_script(rid),
                      user_text="нет", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "pending", "«нет» не должно удалять"


def test_cancel_reminder_idempotent_repeat(db_session):
    """п.3: повторный cancel уже-отменённого → «уже неактивно», без ошибки."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    r = db_session.get(FamilyReminder, rid)
    r.status = "cancelled"; db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    out = tools["cancel_reminder"].invoke({"reminder_ref": rid})
    assert "неактивно" in out.lower(), out


def test_schedule_ctx_fail_closed_empty_user(db_session):
    """п.8: ctx-путь create с пустым user_id → ValueError (fail-closed)."""
    u = seed_telegram_user(db_session); db_session.commit()
    svc = HousewifeReminderService(db_session)
    ctx = ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                             tool_name="schedule_reminder", tenant_id=u.tenant_id,
                             user_id="", turn_key="tk")
    with bind_tool_runtime(ctx):
        with pytest.raises(ValueError):
            svc.schedule(tenant_id=u.tenant_id, user_id="", title="x",
                         trigger_at=datetime(2030, 1, 1, tzinfo=timezone.utc))


def test_add_task_ctx_fail_closed_empty_user(db_session):
    """п.8: то же для задач."""
    u = seed_telegram_user(db_session); db_session.commit()
    svc = TaskService(db_session)
    ctx = ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                             tool_name="add_task", tenant_id=u.tenant_id,
                             user_id="", turn_key="tk")
    with bind_tool_runtime(ctx):
        with pytest.raises(ValueError):
            svc.add(tenant_id=u.tenant_id, user_id="", title="x")


def test_scrub_ids_strips_internal_keeps_text():
    """п.12: снимает ref/id-паттерны (реальные id = 24 hex), не съедает текст."""
    assert _scrub_ids("Удалить (ref rem_0123456789abcdef01234567)?") == "Удалить?"
    rid = "rem_" + "0123456789abcdef01234567"  # 24 hex как в проде
    assert "rem_" not in _scrub_ids(f"{rid} готово")
    tid = "task_" + "abcdef0123456789abcdef01"
    assert "task_" not in _scrub_ids(f"{tid} осталось")
    assert _scrub_ids("Готово, удалила «разминка».") == "Готово, удалила «разминка»."
    # короткие hex-подобные слова НЕ трогаем (граница длины {12,})
    assert _scrub_ids("task_face и rem_dead") == "task_face и rem_dead"
    assert _scrub_ids("видеоидентификатор и рефлекс") == "видеоидентификатор и рефлекс"


def test_destructive_extra_families_confirm_coverage(db_session):
    """ПРАВИЛО #7 confirm-гейт: все разрушающие добранных семей покрыты
    _CONFIRM_PHRASE (включая move_task_to_checklist, который отменяет задачу);
    utility (log_unsupported_request) НЕ просачивается в цикл."""
    from sreda.runtime.react_loop import _CONFIRM_PHRASE

    u = seed_telegram_user(db_session); db_session.commit()
    names = {t.name for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    assert set(_CONFIRM_PHRASE) <= names, set(_CONFIRM_PHRASE) - names  # все привязаны
    assert "move_task_to_checklist" in _CONFIRM_PHRASE  # destructive cross-family под confirm
    assert "log_unsupported_request" not in names  # utility отфильтрован


def test_list_tasks_returns_dated_task(db_session):
    """Регресс: list_tasks без фильтра даты возвращает ДАТИРОВАННУЮ задачу
    (баг include_no_date=True исключал её; найден живым прогоном)."""
    u = seed_telegram_user(db_session); db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx = ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                             tool_name="add_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk")
    with bind_tool_runtime(ctx):
        tools["add_task"].invoke({"title": "полить цветы", "scheduled_date": "2030-06-20"})
    out = tools["list_tasks"].invoke({"scheduled_date": ""})
    assert "цвет" in out.lower(), out


def test_add_task_with_reminder_not_regressed_under_ctx(db_session):
    """MAJOR (Codex medium): ctx биндит И plan-execute. add_task с
    reminder_offset_minutes НЕ должен уходить в idempotent-ветку (иначе напоминание
    молча теряется у не-ReAct тенантов) — должен реально прицепить напоминание."""
    from datetime import date, time

    u = seed_telegram_user(db_session); db_session.commit()
    svc = TaskService(db_session)
    ctx = ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                             tool_name="add_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk")
    with bind_tool_runtime(ctx):
        t = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="принять лекарство",
                    scheduled_date=date(2030, 1, 1), time_start=time(9, 0),
                    reminder_offset_minutes=10)
    db_session.expire_all()
    assert t.reminder_id is not None, "напоминание должно быть прицеплено (нет регрессии)"


def test_update_task_noop_same_values_keeps_updated_at(db_session):
    """п.5: повтор update теми же значениями → updated_at не двигается."""
    from sreda.db.models.tasks import Task

    u = seed_telegram_user(db_session); db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx = ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                             tool_name="add_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk")
    with bind_tool_runtime(ctx):
        tools["add_task"].invoke({"title": "созвон"})
    t = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one()
    before = t.updated_at
    tools["update_task"].invoke({"task_ref": t.id, "title": "созвон"})  # те же значения
    db_session.expire_all()
    t2 = db_session.query(Task).filter_by(id=t.id).one()
    assert t2.updated_at == before, "no-op update не должен двигать updated_at"
