"""Фаза 0 #162 — confirm/resume через граф + гарды + регрессы.

Закрывает чеклист-пункты, не покрытые ранее (код-ревью субагента, ПРАВИЛО #7):
  п.3 — destructive confirm: «да»→удаление, «нет»→отказ, повтор идемпотентен;
  п.8 — fail-closed user_id в ctx-пути create;
  п.12 — scrub rem_/task_/checklist_/(ref …), без съедания нормального текста;
  регресс — list_tasks возвращает датированную задачу (баг include_no_date, найден живым прогоном).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def test_update_reminder_exact_replay_no_reapply_163(db_session):
    """#163 Фаза 3 named-X: повтор того же update (тот же ctx→op_id, committed) → сохранённый
    payload БЕЗ переприменения (replay-after-change не откатывает более новое состояние БД)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    rid = _seed_reminder(db_session, u)
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx1 = ToolRuntimeContext(operation_id="o1", execution_id="e1", step_id="s1",
                              tool_name="update_reminder", tenant_id=u.tenant_id,
                              user_id=u.user_id, turn_key="tk1")
    with bind_tool_runtime(ctx1):
        r1 = tools["update_reminder"].invoke({"reminder_ref": rid, "title": "новое"})
    assert "новое" in r1, r1
    # более новое состояние БД (как будто прошёл ещё один ход и поменял title)
    row = db_session.get(FamilyReminder, rid)
    row.title = "новейшее"
    db_session.commit()
    # replay ТОГО ЖЕ хода (ctx1 → тот же op_id) → сохранённый payload, БЕЗ переприменения
    with bind_tool_runtime(ctx1):
        r2 = tools["update_reminder"].invoke({"reminder_ref": rid, "title": "новое"})
    assert r2 == r1, f"replay должен вернуть сохранённый payload: {r2!r} != {r1!r}"
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).title == "новейшее", \
        "replay НЕ должен переприменять (нет отката к 'новое')"


def test_update_task_exact_replay_no_reapply_163(db_session):
    """#163 Фаза 3a-2 named-X: повтор update_task (тот же ctx→op_id) → сохранённый payload без
    переприменения (не откатывает более новое состояние)."""
    from datetime import date
    from sreda.db.models.tasks import Task

    u = seed_telegram_user(db_session); db_session.commit()
    tid = f"task_{uuid4().hex[:20]}"
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    db_session.add(Task(id=tid, tenant_id=u.tenant_id, user_id=u.user_id, title="старое",
                        scheduled_date=date(2030, 6, 20), status="pending",
                        created_at=now, updated_at=now))
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx1 = ToolRuntimeContext(operation_id="o1", execution_id="e1", step_id="s1",
                              tool_name="update_task", tenant_id=u.tenant_id,
                              user_id=u.user_id, turn_key="tk1")
    with bind_tool_runtime(ctx1):
        r1 = tools["update_task"].invoke({"task_ref": tid, "title": "новое"})
    assert "новое" in r1, r1
    db_session.get(Task, tid).title = "новейшее"; db_session.commit()
    with bind_tool_runtime(ctx1):
        r2 = tools["update_task"].invoke({"task_ref": tid, "title": "новое"})
    assert r2 == r1, f"replay → сохранённый payload: {r2!r}"
    db_session.expire_all()
    assert db_session.get(Task, tid).title == "новейшее", "replay не переприменил"


def test_update_task_reschedule_linked_reminder_ctx_atomic_163(db_session):
    """#163 Фаза 3a-2 (субагент R1 CRITICAL): ctx-путь update_task с ПЕРЕНОСОМ + связанным
    напоминанием НЕ падает (schedule изнутри helper-savepoint раньше → InvalidRequestError);
    recreate атомарен (старое cancelled, новое создано+linked, задача обновлена)."""
    from datetime import date
    from datetime import time as _time

    from sreda.db.models.housewife import FamilyReminder
    from sreda.db.models.tasks import Task

    u = seed_telegram_user(db_session); db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    when = datetime(2030, 6, 20, 9, 0, tzinfo=timezone.utc)
    rid = f"rem_{uuid4().hex[:20]}"
    db_session.add(FamilyReminder(id=rid, tenant_id=u.tenant_id, user_id=u.user_id,
                                  title="⏰ задача", trigger_at=when, next_trigger_at=when,
                                  status="pending", bot_key="sreda"))
    tid = f"task_{uuid4().hex[:20]}"
    db_session.add(Task(id=tid, tenant_id=u.tenant_id, user_id=u.user_id, title="задача",
                        scheduled_date=date(2030, 6, 20), time_start=_time(9, 0),
                        reminder_id=rid, reminder_offset_minutes=0, status="pending",
                        created_at=when, updated_at=when))
    db_session.commit()
    ctx = ToolRuntimeContext(operation_id="o1", execution_id="e1", step_id="s1",
                             tool_name="update_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk1")
    with bind_tool_runtime(ctx):
        r = tools["update_task"].invoke({"task_ref": tid, "scheduled_date": "2030-06-21"})
    assert r.startswith("ok:updated"), f"ctx-перенос со связанным напоминанием не должен падать: {r!r}"
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "cancelled", "старое напоминание отменено"
    t = db_session.get(Task, tid)
    assert t.scheduled_date == date(2030, 6, 21), "дата обновлена"
    assert t.reminder_id and t.reminder_id != rid, "новое напоминание привязано"


def test_update_reminder_replay_after_delete_returns_stored_163(db_session):
    """Codex R1 MAJOR: replay update ПОСЛЕ удаления цели → СОХРАНЁННЫЙ payload (exact-replay), а не
    живое «не найдено». Со старым кодом (not-found ДО _idempotent_write) вернулось бы «нет»."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx1 = ToolRuntimeContext(operation_id="o1", execution_id="e1", step_id="s1",
                              tool_name="update_reminder", tenant_id=u.tenant_id,
                              user_id=u.user_id, turn_key="tk1")
    with bind_tool_runtime(ctx1):
        r1 = tools["update_reminder"].invoke({"reminder_ref": rid, "title": "новое"})
    assert "новое" in r1, r1
    # удалить цель
    db_session.delete(db_session.get(FamilyReminder, rid)); db_session.commit()
    with bind_tool_runtime(ctx1):
        r2 = tools["update_reminder"].invoke({"reminder_ref": rid, "title": "новое"})
    assert r2 == r1, f"replay после удаления → сохранённый payload, не «не найдено»: {r2!r}"


def test_delete_task_tombstone_replay_163(db_session, monkeypatch):
    """#163 Фаза 3a-3 named-Z: replay delete_task ПОСЛЕ исчезновения строки задачи → сохранённый
    payload (tombstone op-результата), не «не найдено». Без фикса peek вернулось бы «нет»."""
    from datetime import date
    from sreda.db.models.tasks import Task

    monkeypatch.setattr(react_loop, "interrupt", lambda *a, **k: "да")
    u = seed_telegram_user(db_session); db_session.commit()
    tid = f"task_{uuid4().hex[:20]}"
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    db_session.add(Task(id=tid, tenant_id=u.tenant_id, user_id=u.user_id, title="удаляемая",
                        scheduled_date=date(2030, 6, 20), status="pending",
                        created_at=now, updated_at=now))
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx = ToolRuntimeContext(operation_id="o1", execution_id="e1", step_id="s1",
                             tool_name="delete_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk1")
    with bind_tool_runtime(ctx):
        r1 = tools["delete_task"].invoke({"task_ref": tid})
    assert r1.startswith("Готово, удаляю"), r1
    assert db_session.get(Task, tid) is None  # строка удалена
    with bind_tool_runtime(ctx):
        r2 = tools["delete_task"].invoke({"task_ref": tid})
    assert r2 == r1, f"replay после hard-delete → сохранённый payload (tombstone): {r2!r}"


def test_cancel_task_exact_replay_163(db_session, monkeypatch):
    """#163 Фаза 3a-3 named-X: повтор cancel_task (тот же ctx→op_id) → сохранённый payload."""
    from datetime import date
    from sreda.db.models.tasks import Task

    monkeypatch.setattr(react_loop, "interrupt", lambda *a, **k: "да")
    u = seed_telegram_user(db_session); db_session.commit()
    tid = f"task_{uuid4().hex[:20]}"
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    db_session.add(Task(id=tid, tenant_id=u.tenant_id, user_id=u.user_id, title="отменяемая",
                        scheduled_date=date(2030, 6, 20), status="pending",
                        created_at=now, updated_at=now))
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx = ToolRuntimeContext(operation_id="o1", execution_id="e1", step_id="s1",
                             tool_name="cancel_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk1")
    with bind_tool_runtime(ctx):
        r1 = tools["cancel_task"].invoke({"task_ref": tid})
    assert r1.startswith("Готово, отменяю"), r1
    with bind_tool_runtime(ctx):
        r2 = tools["cancel_task"].invoke({"task_ref": tid})
    assert r2 == r1, f"replay cancel → сохранённый payload: {r2!r}"


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
    assert "удалю" in r1.lower(), r1  # #265: «Я сейчас удалю напоминание …»
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


def test_schedule_reminder_past_trigger_not_scheduled_174(db_session):
    """#174: trigger_iso в прошлом → НЕ ставить в прошлое (иначе past-due → мгновенный fired).
    Решение владельца: перекатывать вперёд → инструмент возвращает ДИРЕКТИВУ Фредди пересчитать
    на будущее и вызвать снова (без вопроса юзеру); строка в БД на этом вызове НЕ создаётся."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    out = tools["schedule_reminder"].invoke(
        {"title": "позвонить врачу", "trigger_iso": "2020-06-19T14:00:00+03:00"})
    assert "ok:scheduled" not in out, out          # в прошлое НЕ поставлено
    assert "прош" in out.lower(), out              # «уже прошло»
    assert "будущ" in out.lower(), out             # директива переката на будущее
    rows = db_session.query(FamilyReminder).filter(
        FamilyReminder.tenant_id == u.tenant_id).all()
    assert rows == [], f"past-due напоминание не должно создаваться: {rows}"


def test_schedule_reminder_future_still_works_174(db_session):
    """#174 регресс: будущее время по-прежнему ставится нормально (relative — не time-bomb)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    future_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    out = tools["schedule_reminder"].invoke(
        {"title": "стоматолог", "trigger_iso": future_iso})
    assert "ok:scheduled" in out, out
    rows = db_session.query(FamilyReminder).filter(
        FamilyReminder.tenant_id == u.tenant_id).all()
    assert len(rows) == 1 and rows[0].title == "стоматолог"


@pytest.mark.asyncio
async def test_run_tools_tool_exception_does_not_kill_turn_163(db_session, monkeypatch):
    """#163 Фаза 1а: инструмент БРОСАЕТ исключение (а не возвращает строку) → run_tools отдаёт
    error-ToolMessage, ход НЕ падает в «потеряла контекст», модель продолжает и отвечает.
    (Фундамент честного частичного отчёта named-P.)"""
    from langchain_core.tools import tool as _lc_tool

    @_lc_tool
    def schedule_reminder(title: str = "", trigger_iso: str = "") -> str:
        """raising stub (имя core-инструмента → попадает в bound)."""
        raise RuntimeError("boom")

    u = seed_telegram_user(db_session)
    db_session.commit()
    monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [schedule_reminder])
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "schedule_reminder",
            "args": {"title": "x", "trigger_iso": "2030-01-01T09:00:00+03:00"}, "id": "c1"}]),
        AIMessage(content="Готово."),
    ])
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="i163-raise", llm=stub, user_text="поставь напоминание",
        inbound_message_id="i163-raise-msg", channel="react")
    assert "потеряла контекст" not in str(reply), f"ход упал вместо error-ToolMessage: {reply!r}"
    assert "отово" in str(reply), f"модель не продолжила после сбоя инструмента: {reply!r}"


def test_update_reminder_past_trigger_rolls_forward_174(db_session):
    """#174 (Codex medium R1): перенос напоминания на ПРОШЛОЕ — тот же класс бага → перекат.
    update_reminder с прошедшим trigger_iso НЕ меняет момент, отдаёт директиву переката."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    rid = _seed_reminder(db_session, u)  # trigger 2030 (future), pending
    orig = db_session.get(FamilyReminder, rid).trigger_at
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    out = tools["update_reminder"].invoke(
        {"reminder_ref": rid, "trigger_iso": "2020-06-19T14:00:00+03:00"})
    assert "ok:updated" not in out and "прош" in out.lower(), out
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).trigger_at == orig, "момент не должен уехать в прошлое"


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


def test_format_lists_breaks_crammed_keeps_rest():
    """Пост-формат: скомканный список → построчно; «— название — время» (1 тире)
    и обычный текст НЕ трогаем."""
    from sreda.runtime.react_loop import _format_lists

    inp = "Вот твой список покупок:\n— молоко — хлеб (1) — яйца — мёд (1 банка)"
    assert _format_lists(inp) == (
        "Вот твой список покупок:\n— молоко\n— хлеб (1)\n— яйца\n— мёд (1 банка)"
    ), repr(_format_lists(inp))
    # напоминание «— название — время» (1 тире) — НЕ дробим
    rem = "Вот твои напоминания:\n— принять витамины — 18 июня, 08:00"
    assert _format_lists(rem) == rem
    # обычный текст с одним тире — не трогаем
    prose = "Готово — удалила напоминание про зал."
    assert _format_lists(prose) == prose
    # инлайн-интро «список: a — b — c» → построчно
    inline = "Вот список: — молоко — хлеб — яйца"
    assert _format_lists(inline) == "Вот список:\n— молоко\n— хлеб\n— яйца"
    # уже построчно — не трогаем
    ml = "Список:\n— молоко\n— хлеб"
    assert _format_lists(ml) == ml


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


@pytest.mark.asyncio
async def test_confirm_pause_sets_awaiting_confirm(db_session):
    """#166 B: confirm-пауза («Точно удалить?») → _Reply.awaiting_confirm=True (канал вешает
    кнопки [Да][Нет])."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="удали разминку", inbound_message_id="m1", channel="max")
    assert getattr(r1, "awaiting_confirm", False) is True, repr(r1)
    assert "подтверждение" in r1.lower()  # #265: «…Нужно твоё подтверждение.»


@pytest.mark.asyncio
async def test_ask_human_pause_not_confirm(db_session):
    """#166 B: ask_human-пауза (уточнение «какое из нескольких?») → awaiting_confirm=False
    (кнопки Да/Нет НЕ вешаем)."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "ask_human", "args": {"question": "Какое из двух удалить?"},
            "id": "call_1"}]),
        AIMessage(content="ок"),
    ])
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=stub,
                           user_text="удали что-нибудь", inbound_message_id="m1", channel="max")
    assert getattr(r1, "awaiting_confirm", False) is False, repr(r1)
    assert "какое" in r1.lower()


def test_confirm_resume_text_public_helper():
    """#166 B R2/R3: публичный маппинг callback_data → resume-текст + id паузы + сборка
    (каналы не лезут в приватные структуры; терпит суффикс :<pause_id>)."""
    # resume-текст: без суффикса и с суффиксом
    assert react_loop.confirm_resume_text("react:yes") == "да"
    assert react_loop.confirm_resume_text("react:no") == "нет"
    assert react_loop.confirm_resume_text("react:yes:abc123") == "да"
    assert react_loop.confirm_resume_text("react:no:abc123") == "нет"
    assert react_loop.confirm_resume_text("btn_reply:x") is None
    assert react_loop.confirm_resume_text("") is None
    assert react_loop.confirm_resume_text("react:maybe") is None
    # id паузы из callback_data
    assert react_loop.confirm_callback_id("react:yes:abc123") == "abc123"
    assert react_loop.confirm_callback_id("react:no") == ""
    assert react_loop.confirm_callback_id("btn_reply:x") == ""
    # сборка callback_data (с id и без)
    assert react_loop.confirm_callback_data("yes", "abc123") == "react:yes:abc123"
    assert react_loop.confirm_callback_data("no", "") == "react:no"
    # round-trip
    cb = react_loop.confirm_callback_data("yes", "deadbeef")
    assert react_loop.confirm_resume_text(cb) == "да"
    assert react_loop.confirm_callback_id(cb) == "deadbeef"


@pytest.mark.asyncio
async def test_resume_only_repeat_tap_is_noop(db_session):
    """#166 B R2 (Codex MAJOR): первый тап «да» (resume_only) возобновляет паузу и удаляет;
    ПОВТОРНЫЙ тап (живой паузы уже нет) → пустой _Reply (no-op), свежий ход с текстом «да»
    НЕ стартует, состояние не меняется."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="удали разминку", inbound_message_id="m1", channel="max")
    tok = r1.confirm_id  # токен паузы из кнопки
    # первый тап [Да] — resume_only с верным токеном: возобновляет паузу
    r2 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid), resume_only=True,
                           user_text="да", expected_confirm_id=tok,
                           inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "cancelled", r2
    assert str(r2) != "", "первый тап должен дать непустой ответ"
    # повторный тап [Да] — живой паузы уже нет → no-op, пустой ответ
    r3 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid), resume_only=True,
                           user_text="да", expected_confirm_id=tok,
                           inbound_message_id="m3", channel="max")
    assert str(r3) == "", f"повторный тап должен быть no-op (пустой), а не свежий ход: {r3!r}"
    assert getattr(r3, "awaiting_confirm", False) is False


@pytest.mark.asyncio
async def test_resume_only_legacy_no_id_is_noop_against_id_pause(db_session):
    """#166 B R4 (Codex high/medium MAJOR, fail-closed): legacy-кнопка БЕЗ токена
    (expected_confirm_id="") НЕ подтверждает confirm-паузу, У КОТОРОЙ токен есть.
    Старые react:yes/no из R1/R2 (уже в чатах) → no-op. Текстовое «да» (не resume_only) — ок."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="удали разминку", inbound_message_id="m1", channel="max")
    assert r1.confirm_id, "у паузы должен быть непустой токен"
    # legacy-тап (resume_only, токен пустой) → fail-closed no-op
    r_legacy = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                                 thread_id=thread, llm=_cancel_script(rid), resume_only=True,
                                 user_text="да", expected_confirm_id="",
                                 inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert str(r_legacy) == "", f"legacy без токена → no-op: {r_legacy!r}"
    assert db_session.get(FamilyReminder, rid).status == "pending", "legacy-тап не должен удалять"
    # текстовое «да» (НЕ resume_only) — обычный resume, подтверждает
    r_txt = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                              thread_id=thread, llm=_cancel_script(rid),
                              user_text="да", inbound_message_id="m3", channel="max")
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "cancelled", r_txt


def test_pause_token_discriminates_chain_same_id_different_question():
    """#166 B R4 (субагент MAJOR): Interrupt.id одинаков для цепочки confirm в одном
    исполнении узла → токен паузы ДОЛЖЕН различать их по тексту вопроса. Иначе старая
    кнопка A подтвердит цепочечный confirm B (другое разрушающее действие)."""
    from sreda.runtime.react_loop import _pause_token

    class _It:
        def __init__(self, id_, value):
            self.id, self.value = id_, value

    # один id (общий namespace узла), РАЗНЫЕ вопросы → РАЗНЫЕ токены
    a = _It("samens", {"confirm": "Точно отменить задачу «X»?"})
    b = _It("samens", {"confirm": "Точно отменить задачу «Y»?"})
    assert _pause_token(a) != _pause_token(b)
    # тот же id+вопрос → стабильный токен (легитимный тап матчит)
    assert _pause_token(a) == _pause_token(_It("samens", {"confirm": "Точно отменить задачу «X»?"}))
    # разный id (межходовой), один вопрос → разные токены
    assert _pause_token(_It("id1", {"confirm": "q"})) != _pause_token(_It("id2", {"confirm": "q"}))
    # нет паузы → пустой токен
    assert _pause_token(_It("", None)) == ""


def test_pause_token_discriminates_identical_text_via_hidden_key():
    """#166 B R5 (Codex high+medium MAJOR): один id + ИДЕНТИЧНЫЙ текст вопроса (две задачи
    «купить хлеб»), но РАЗНЫЕ скрытые ключи цели → РАЗНЫЕ токены. Текст один не различил бы."""
    from sreda.runtime.react_loop import _pause_token

    class _It:
        def __init__(self, id_, value):
            self.id, self.value = id_, value

    q = "Точно отменяю задачу «купить хлеб»?"
    a = _It("samens", {"confirm": q, "key": "task:t1:отменяю"})
    b = _It("samens", {"confirm": q, "key": "task:t2:отменяю"})
    assert _pause_token(a) != _pause_token(b), "разные цели при одном тексте → разные токены"
    # тот же id+текст+ключ → стабильный токен (легитимный тап матчит)
    assert _pause_token(a) == _pause_token(_It("samens", {"confirm": q, "key": "task:t1:отменяю"}))
    # тот же объект, РАЗНОЕ действие (cancel vs delete) → разные токены
    c = _It("samens", {"confirm": q, "key": "task:t1:удаляю"})
    assert _pause_token(a) != _pause_token(c)


@pytest.mark.asyncio
async def test_chain_confirm_stale_token_does_not_confirm_next(db_session):
    """#166 B R4 (субагент MAJOR, e2e): цепочка confirm в ОДНОМ ходе («удали напоминание И
    задачу») → две паузы с РАЗНЫМИ токенами; устаревший токен A не подтверждает паузу B."""
    from sreda.db.models.tasks import Task

    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    # задача через add_task (известно-рабочий путь)
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    ctx = ToolRuntimeContext(operation_id="o", execution_id="e", step_id="s",
                             tool_name="add_task", tenant_id=u.tenant_id,
                             user_id=u.user_id, turn_key="tk")
    with bind_tool_runtime(ctx):
        tools["add_task"].invoke({"title": "купить хлеб"})
    tid = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).one().id

    thread = f"react:t:{uuid4().hex}"
    stub = _StubLLM([  # ОДИН AIMessage с ДВУМЯ разрушающими вызовами
        AIMessage(content="", tool_calls=[
            {"name": "cancel_reminder", "args": {"reminder_ref": rid}, "id": "c1"},
            {"name": "cancel_task", "args": {"task_ref": tid}, "id": "c2"}]),
        AIMessage(content="Готово."),
    ])
    # ход1 → пауза A (напоминание)
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=stub, user_text="удали напоминание и задачу",
                           inbound_message_id="m1", channel="max")
    tok_a = r1.confirm_id
    assert getattr(r1, "awaiting_confirm", False) is True and tok_a, repr(r1)
    # тап A (верный токен) → напоминание отменено, цепочка → пауза B (задача)
    r2 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=stub, resume_only=True, user_text="да",
                           expected_confirm_id=tok_a, inbound_message_id="m2", channel="max")
    tok_b = r2.confirm_id
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "cancelled", "A должно отмениться"
    assert getattr(r2, "awaiting_confirm", False) is True, f"должна быть пауза B: {r2!r}"
    assert tok_a != tok_b, "цепочка confirm в одном ходе → РАЗНЫЕ токены (иначе дыра B)"
    assert db_session.get(Task, tid).status == "pending", "B ещё не подтверждено"
    # устаревший тап токеном A по живой паузе B → no-op, задача НЕ отменяется
    r_stale = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                                thread_id=thread, llm=stub, resume_only=True, user_text="да",
                                expected_confirm_id=tok_a, inbound_message_id="m3", channel="max")
    db_session.expire_all()
    assert str(r_stale) == "", f"старый токен A → no-op на паузе B: {r_stale!r}"
    assert db_session.get(Task, tid).status == "pending", "чужой токен не должен отменять задачу"
    # верный токен B → задача отменяется
    await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                      thread_id=thread, llm=stub, resume_only=True, user_text="да",
                      expected_confirm_id=tok_b, inbound_message_id="m4", channel="max")
    db_session.expire_all()
    assert db_session.get(Task, tid).status == "cancelled", "верный токен B → отмена задачи"


@pytest.mark.asyncio
async def test_chain_confirm_same_title_stale_token_noop(db_session):
    """#166 B R5 (Codex high+medium MAJOR, e2e): ДВЕ задачи с ОДИНАКОВЫМ названием в одной
    цепочке confirm → токены РАЗНЫЕ (скрытый ключ цели task:<id>:<verb>); устаревший токен A
    НЕ подтверждает паузу B, хотя текст вопроса идентичен."""
    from sreda.db.models.tasks import Task

    u = seed_telegram_user(db_session); db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}
    # ДВЕ задачи с одним названием: разные ctx (execution_id/step_id), иначе within-turn
    # дедуп схлопнет их в одну.
    for i in range(2):
        ctx_i = ToolRuntimeContext(operation_id=f"o{i}", execution_id=f"e{i}", step_id=f"s{i}",
                                   tool_name="add_task", tenant_id=u.tenant_id,
                                   user_id=u.user_id, turn_key=f"tk{i}")
        with bind_tool_runtime(ctx_i):
            tools["add_task"].invoke({"title": "купить хлеб"})
    rows = db_session.query(Task).filter_by(tenant_id=u.tenant_id, user_id=u.user_id).all()
    assert len(rows) == 2
    t1, t2 = rows[0].id, rows[1].id

    thread = f"react:t:{uuid4().hex}"
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "cancel_task", "args": {"task_ref": t1}, "id": "c1"},
            {"name": "cancel_task", "args": {"task_ref": t2}, "id": "c2"}]),
        AIMessage(content="Готово."),
    ])
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=stub, user_text="отмени обе задачи купить хлеб",
                           inbound_message_id="m1", channel="max")
    tok_a = r1.confirm_id
    r2 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=stub, resume_only=True, user_text="да",
                           expected_confirm_id=tok_a, inbound_message_id="m2", channel="max")
    tok_b = r2.confirm_id
    db_session.expire_all()
    assert getattr(r2, "awaiting_confirm", False) is True, f"должна быть пауза B: {r2!r}"
    assert tok_a != tok_b, "ОДИН текст, но РАЗНЫЕ цели → разные токены (ключ цели)"
    # устаревший токен A по паузе B → no-op, t2 не отменяется
    r_stale = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                                thread_id=thread, llm=stub, resume_only=True, user_text="да",
                                expected_confirm_id=tok_a, inbound_message_id="m3", channel="max")
    db_session.expire_all()
    assert str(r_stale) == "", f"старый токен A → no-op на B (одинаковый текст!): {r_stale!r}"
    assert db_session.get(Task, t2).status == "pending", "t2 не должна отмениться чужим токеном"


@pytest.mark.asyncio
async def test_confirm_pause_carries_confirm_id(db_session):
    """#166 B R3: confirm-пауза несёт _Reply.confirm_id (id паузы для callback_data кнопок)."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="удали разминку", inbound_message_id="m1", channel="max")
    assert getattr(r1, "confirm_id", "") != "", "confirm-пауза должна нести id паузы"


@pytest.mark.asyncio
async def test_resume_only_wrong_confirm_id_is_noop(db_session):
    """#166 B R3 (Codex MAJOR B): тап кнопки с ЧУЖИМ id паузы → no-op (не подтверждает
    активную паузу). С ВЕРНЫМ id — возобновляет."""
    u = seed_telegram_user(db_session); db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=_cancel_script(rid),
                           user_text="удали разминку", inbound_message_id="m1", channel="max")
    good_id = r1.confirm_id
    assert good_id, "нужен непустой id паузы для теста"
    # тап со СТАРЫМ/ЧУЖИМ id — no-op, напоминание не трогаем
    r_wrong = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                                thread_id=thread, llm=_cancel_script(rid), resume_only=True,
                                user_text="да", expected_confirm_id="deadbeef-not-this-pause",
                                inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert str(r_wrong) == "", f"чужой id → no-op: {r_wrong!r}"
    assert db_session.get(FamilyReminder, rid).status == "pending", "чужой тап не должен удалять"
    # тап с ВЕРНЫМ id — возобновляет, удаляет
    r_ok = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                             thread_id=thread, llm=_cancel_script(rid), resume_only=True,
                             user_text="да", expected_confirm_id=good_id,
                             inbound_message_id="m3", channel="max")
    db_session.expire_all()
    assert str(r_ok) != "", "верный id → непустой ответ"
    assert db_session.get(FamilyReminder, rid).status == "cancelled", r_ok


@pytest.mark.asyncio
async def test_resume_only_rejects_non_confirm_pause(db_session):
    """#166 B R3 (Codex MAJOR A): устаревший тап [Да]/[Нет] НЕ должен отвечать «да/нет»
    на ask_human-уточнение (не-confirm пауза) → no-op. Обычный текстовый ответ — отвечает."""
    u = seed_telegram_user(db_session); db_session.commit()
    thread = f"react:t:{uuid4().hex}"
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "ask_human", "args": {"question": "Какое из двух удалить?"},
            "id": "call_1"}]),
        AIMessage(content="понял, удаляю первое"),
    ])
    r1 = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                           thread_id=thread, llm=stub,
                           user_text="удали что-нибудь", inbound_message_id="m1", channel="max")
    assert getattr(r1, "awaiting_confirm", False) is False  # ask_human, не confirm
    # тап кнопки (resume_only) по ask_human-паузе → no-op
    r_btn = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                              thread_id=thread, llm=stub, resume_only=True,
                              user_text="да", expected_confirm_id="whatever",
                              inbound_message_id="m2", channel="max")
    assert str(r_btn) == "", f"кнопка не должна отвечать на ask_human: {r_btn!r}"
    # обычный текстовый ответ (НЕ resume_only) — отвечает на уточнение
    r_txt = await handle_turn(session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
                              thread_id=thread, llm=stub,
                              user_text="первое", inbound_message_id="m3", channel="max")
    assert str(r_txt) != "", "текстовый ответ должен возобновить ask_human"
