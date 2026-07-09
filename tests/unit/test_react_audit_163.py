"""#163 Фаза 3d-A — аудит-трейл durable-действий ReAct (helper-пути update/cancel/delete).

named-W (atomic audit) + #192 (caused_by провенанс). RED до реализации:
- helper-операция ReAct пишет ОДНУ audit_outbox-строку с caused_by.source="react" атомарно;
- нет ToolRuntimeContext (легаси/прямой вызов) → no-op (путь не падает);
- replay того же op_id → НЕ вторая audit-строка;
- гонка аудита (IntegrityError) → savepoint откат, мутация ВЫЖИВАЕТ;
- non-race сбой emit → откат всей операции (claim+мутация).
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from langchain_core.messages import AIMessage

from sreda.db.models.audit_feed import AuditOutboxEvent
from sreda.db.models.tool_operations import ToolOperationResult
from sreda.runtime import react_loop
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.services import audit_feed
from sreda.services.audit_feed import emit_tool_audit
from sreda.services.idempotent_ops import compute_args_hmac, execute_idempotent_durable_op
from tests.unit.conftest import seed_telegram_user


def _audit(session, tenant_id, entity_type=None):
    rows = (session.query(AuditOutboxEvent)
            .filter(AuditOutboxEvent.tenant_id == tenant_id).all())
    return [r for r in rows if entity_type is None or r.entity_type == entity_type]


def _ctx(tenant_id, user_id, *, op_id="op_test", tool="update_task"):
    return ToolRuntimeContext(
        operation_id=op_id, execution_id="ex", step_id="st", tool_name=tool,
        tenant_id=tenant_id, user_id=user_id, turn_key="react:max:t1:msg1",
        channel="max", thread_id="thr1", origin="react")


def _run_helper(session, tenant_id, user_id, *, op_id, mutate, audit=True):
    return execute_idempotent_durable_op(
        session, operation_id=op_id, tenant_id=tenant_id, user_id=user_id,
        operation_family="task", args_hmac=compute_args_hmac({}, secret="s"),
        mutate_fn=mutate, tool_name="update",
        audit_entity_type="task" if audit else None,
        audit_entity_id="task_x" if audit else None,
        audit_action="updated" if audit else None)


# ───────── emit_tool_audit (юнит) ─────────

def test_emit_tool_audit_no_ctx_noop(db_session):
    """Нет ToolRuntimeContext → no-op (не-ReAct/легаси путь не пишет react-аудит и не падает)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    emit_tool_audit(db_session, operation_id="o1", tenant_id=u.tenant_id, user_id=u.user_id,
                    entity_type="task", entity_id="task_x", action="updated")
    assert _audit(db_session, u.tenant_id) == []


def test_emit_tool_audit_non_react_origin_noop(db_session):
    """Codex medium R1 MAJOR (3d-B): ctx БЕЗ origin=react (напр. легаси plan-execute executor биндит
    ToolRuntimeContext с origin=None) → emit_tool_audit no-op (нет ложного react-провенанса)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    planner_ctx = ToolRuntimeContext(  # origin не задан → None (не react)
        operation_id="oplan", execution_id="ex", step_id="st", tool_name="add_task",
        tenant_id=u.tenant_id, user_id=u.user_id, turn_key="plan:1")
    with bind_tool_runtime(planner_ctx):
        emit_tool_audit(db_session, operation_id="oplan", tenant_id=u.tenant_id, user_id=u.user_id,
                        entity_type="task", entity_id="task_x", action="created")
    db_session.flush()
    assert _audit(db_session, u.tenant_id) == [], "non-react ctx → react-аудит не пишется"


def test_emit_tool_audit_with_ctx_emits_react(db_session):
    """ctx привязан → 1 строка с caused_by.source=react + turn_key/channel/tool_name."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id)):
        emit_tool_audit(db_session, operation_id="o2", tenant_id=u.tenant_id, user_id=u.user_id,
                        entity_type="task", entity_id="task_x", action="updated")
    db_session.flush()
    rows = _audit(db_session, u.tenant_id, "task")
    assert len(rows) == 1
    cb = rows[0].caused_by or {}
    assert cb.get("source") == "react", cb
    assert cb.get("turn_key") == "react:max:t1:msg1", cb
    assert cb.get("channel") == "max", cb
    assert cb.get("tool_name") == "update_task", cb
    assert rows[0].action == "updated"
    assert rows[0].entity_id == "task_x"


# ───────── helper-интеграция ─────────

def test_helper_emits_audit_on_fresh_and_replay_dedup(db_session):
    """Свежая операция → 1 audit-строка + мутация; replay того же op_id → НЕ повторяет ни то, ни то."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    calls = []
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="oph")):
        _run_helper(db_session, u.tenant_id, u.user_id, op_id="oph",
                    mutate=lambda: (calls.append(1), "payload")[1])
        _run_helper(db_session, u.tenant_id, u.user_id, op_id="oph",
                    mutate=lambda: (calls.append(1), "payload")[1])
    assert len(calls) == 1, "mutate один раз (replay не переприменяет)"
    assert len(_audit(db_session, u.tenant_id, "task")) == 1, "одна audit-строка на op_id"


def test_helper_audit_race_mutation_survives(db_session, monkeypatch):
    """Гонка аудита (IntegrityError на emit) → savepoint откат аудита, но мутация committed."""
    u = seed_telegram_user(db_session)
    db_session.commit()

    def _raise_integrity(*a, **k):
        raise IntegrityError("audit race", None, Exception())

    # Реальную concurrent outbox-unique гонку single-thread не воспроизвести: emit_event lookup-first
    # (находит существующий op_id ДО insert → не доходит до IntegrityError). Потому мокаем emit_event,
    # имитируя racer'а; реальное восстановление savepoint = тот же begin_nested-конструкт, что в
    # проде housewife_shopping.add_items + подтверждён независимым probe субагента (R1).
    monkeypatch.setattr(audit_feed, "emit_event", _raise_integrity)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="orace")):
        out = _run_helper(db_session, u.tenant_id, u.user_id, op_id="orace",
                          mutate=lambda: "payload")
    assert out == "payload"
    row = (db_session.query(ToolOperationResult)
           .filter(ToolOperationResult.operation_id == "orace").one_or_none())
    assert row is not None and row.status == "committed", "мутация/claim выжили"
    assert _audit(db_session, u.tenant_id, "task") == [], "audit-гонка поглощена"


def test_helper_audit_nonrace_fail_rolls_back(db_session, monkeypatch):
    """Non-race сбой emit (ValueError) → откат claim+мутации (named-W: audit-fail → rollback op)."""
    u = seed_telegram_user(db_session)
    db_session.commit()

    def _raise_value(*a, **k):
        raise ValueError("bad audit")

    monkeypatch.setattr(audit_feed, "emit_event", _raise_value)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="ofail")):
        with pytest.raises(ValueError):
            _run_helper(db_session, u.tenant_id, u.user_id, op_id="ofail",
                        mutate=lambda: "payload")
    db_session.rollback()
    row = (db_session.query(ToolOperationResult)
           .filter(ToolOperationResult.operation_id == "ofail").one_or_none())
    assert row is None, "claim+мутация откатились при non-race сбое аудита"


def test_emit_tool_audit_identity_conflict_raises(db_session):
    """MAJOR #2 (Codex medium): пред-существующая audit-строка того же op_id с ДРУГИМИ метаданными →
    сверка идентичности поднимает RuntimeError → откат всей операции (аудит не подменяется молча)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    audit_feed.emit_event(  # ЧУЖАЯ строка тем же op_id: action=created (helper эмитит updated)
        db_session, operation_id="oconf", tenant_id=u.tenant_id, user_id=u.user_id,
        source="среда", entity_type="task", entity_id="task_x", action="created",
        caused_by={"source": "react"})
    db_session.commit()
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="oconf")):
        with pytest.raises(RuntimeError, match="коллизия operation_id"):
            _run_helper(db_session, u.tenant_id, u.user_id, op_id="oconf",
                        mutate=lambda: "payload")  # audit_action="updated" != "created"
    db_session.rollback()
    row = (db_session.query(ToolOperationResult)
           .filter(ToolOperationResult.operation_id == "oconf").one_or_none())
    assert row is None, "claim откатился при identity-конфликте аудита"


def test_emit_tool_audit_feed_collision_raises(db_session):
    """Codex high+medium R2 MAJOR: чужая строка того же op_id в уже-отрелеенном FEED (не outbox) →
    PRE-проверка ловит (emit_event на feed-коллизии вернул бы transient из запроса → пост-сверки мало)."""
    from sreda.db.models.audit_feed import UserDataChangeFeedEvent

    u = seed_telegram_user(db_session)
    db_session.commit()
    db_session.add(UserDataChangeFeedEvent(
        operation_id="ofeed", tenant_id=u.tenant_id, user_id=u.user_id, source="среда",
        entity_type="task", entity_id="task_x", action="created",  # action≠updated → чужая
        caused_by={"source": "react"}))
    db_session.commit()
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="ofeed")):
        with pytest.raises(RuntimeError, match="коллизия operation_id"):
            _run_helper(db_session, u.tenant_id, u.user_id, op_id="ofeed",
                        mutate=lambda: "payload")  # audit_action="updated"
    db_session.rollback()
    row = (db_session.query(ToolOperationResult)
           .filter(ToolOperationResult.operation_id == "ofeed").one_or_none())
    assert row is None, "claim откатился при feed-коллизии аудита"


def test_emit_tool_audit_matching_existing_no_double(db_session):
    """mimo R2 MINOR: пред-существующая audit-строка того же op_id с ТЕМИ ЖЕ метаданными →
    PRE-проверка НЕ raise, emit_event дедупит → ровно 1 строка, мутация проходит."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    audit_feed.emit_event(  # ровно те поля, что эмитит _run_helper
        db_session, operation_id="omatch", tenant_id=u.tenant_id, user_id=u.user_id,
        source="среда", entity_type="task", entity_id="task_x", action="updated",
        caused_by={"source": "react"})
    db_session.commit()
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="omatch")):
        out = _run_helper(db_session, u.tenant_id, u.user_id, op_id="omatch",
                          mutate=lambda: "payload")
    assert out == "payload"
    assert len(_audit(db_session, u.tenant_id, "task")) == 1, "дедуп → одна строка, не двоит"


def test_helper_audit_action_none_no_audit(db_session):
    """mimo R1 MINOR: ctx есть, но audit_action=None → аудит НЕ пишется (forward-compat), мутация ок."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    calls = []
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="onone")):
        out = execute_idempotent_durable_op(
            db_session, operation_id="onone", tenant_id=u.tenant_id, user_id=u.user_id,
            operation_family="task", args_hmac=compute_args_hmac({}, secret="s"),
            mutate_fn=lambda: (calls.append(1), "payload")[1], tool_name="weird",
            audit_entity_type="task", audit_entity_id="task_x", audit_action=None)
    assert out == "payload" and len(calls) == 1
    assert _audit(db_session, u.tenant_id) == []


def test_helper_emits_audit_delete_and_reminder(db_session):
    """subagent R1 MINOR: delete→"deleted" + путь family_reminder (механизм entity/action-agnostic)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="odel", tool="delete_task")):
        execute_idempotent_durable_op(
            db_session, operation_id="odel", tenant_id=u.tenant_id, user_id=u.user_id,
            operation_family="family_reminder", args_hmac=compute_args_hmac({}, secret="s"),
            mutate_fn=lambda: "payload", tool_name="delete",
            audit_entity_type="family_reminder", audit_entity_id="rem_x", audit_action="deleted")
    rows = _audit(db_session, u.tenant_id, "family_reminder")
    assert len(rows) == 1 and rows[0].action == "deleted"
    assert (rows[0].caused_by or {}).get("tool_name") == "delete_task"


# ───────── интеграция handle_turn (прокидка channel/thread_id в ctx) ─────────

class _Stub:
    def __init__(self, scripted):
        self._s = scripted
        self._i = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        msg = self._s[min(self._i, len(self._s) - 1)]
        self._i += 1
        return msg


@pytest.mark.asyncio
async def test_react_update_task_emits_react_audit_integration(db_session):
    """handle_turn → run_tools биндит ctx с channel/thread_id → update_task через helper →
    1 react-audit строка. Доказывает прокидку channel в ctx (инфра 3d-A), не только юнит."""
    from sreda.services.tasks import TaskService

    u = seed_telegram_user(db_session)
    db_session.commit()
    t = TaskService(db_session).add(tenant_id=u.tenant_id, user_id=u.user_id, title="старое")
    db_session.commit()
    stub = _Stub([
        AIMessage(content="", tool_calls=[{
            "name": "update_task",
            "args": {"task_ref": t.id, "title": "новое название"}, "id": "ut"}]),
        AIMessage(content="Готово."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="audit3d-1", llm=stub, user_text="переименуй задачу",
        inbound_message_id="audit3d-1-msg", channel="max")
    rows = _audit(db_session, u.tenant_id, "task")
    assert len(rows) == 1, f"одна react-audit строка на update: {len(rows)}"
    cb = rows[0].caused_by or {}
    assert cb.get("source") == "react" and cb.get("channel") == "max", cb
    assert cb.get("tool_name") == "update_task" and cb.get("turn_key"), cb
    assert cb.get("thread_id") == "audit3d-1", cb  # прокидка thread_id (base) в ctx
    assert rows[0].action == "updated"


# ───────── 3d-B: аудит на СОЗДАНИИ (schedule_reminder / add_task) + react-тег shopping ─────────

@pytest.mark.asyncio
async def test_react_create_task_emits_react_audit(db_session):
    """3d-B: создание задачи через ReAct → 1 react-audit (created), атомарно с записью."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    stub = _Stub([
        AIMessage(content="", tool_calls=[{
            "name": "add_task", "args": {"title": "купить хлеб"}, "id": "at"}]),
        AIMessage(content="Готово."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="audit3db-t", llm=stub, user_text="добавь задачу купить хлеб",
        inbound_message_id="audit3db-t-msg", channel="max")
    rows = _audit(db_session, u.tenant_id, "task")
    assert len(rows) == 1, f"одна react-audit на создание задачи: {len(rows)}"
    cb = rows[0].caused_by or {}
    assert cb.get("source") == "react" and cb.get("tool_name") == "add_task", cb
    assert rows[0].action == "created" and rows[0].entity_id


@pytest.mark.asyncio
async def test_react_create_reminder_emits_react_audit(db_session):
    """3d-B: создание напоминания через ReAct → 1 react-audit (created)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    stub = _Stub([
        AIMessage(content="", tool_calls=[{
            "name": "schedule_reminder",
            "args": {"title": "позвонить маме", "trigger_iso": "2031-03-15T09:00:00"},
            "id": "sr"}]),
        AIMessage(content="Готово."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="audit3db-r", llm=stub, user_text="напомни позвонить маме в 9 утра",  # #288: время в тексте (гейт)
        inbound_message_id="audit3db-r-msg", channel="max")
    rows = _audit(db_session, u.tenant_id, "family_reminder")
    assert len(rows) == 1, f"одна react-audit на создание напоминания: {len(rows)}"
    cb = rows[0].caused_by or {}
    assert cb.get("source") == "react" and cb.get("tool_name") == "schedule_reminder", cb
    assert rows[0].action == "created"


@pytest.mark.asyncio
async def test_react_create_task_audit_matches_task_count(db_session):
    """3d-B инвариант: повтор add_task (дательная задача → semantic-дедуп) в одном ходу → число
    react-audit РОВНО = числу созданных задач (reuse не эмитит лишнего; каждое создание — одна audit).
    Дательная (есть scheduled_date), т.к. у задач БЕЗ даты семантического дедупа нет (датовые-only)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    add = {"name": "add_task", "args": {"title": "купить хлеб", "scheduled_date": "2031-03-15"}}
    stub = _Stub([
        AIMessage(content="", tool_calls=[{**add, "id": "a1"}]),
        AIMessage(content="", tool_calls=[{**add, "id": "a2"}]),  # та же задача+дата → semantic reuse
        AIMessage(content="Готово."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="audit3db-dup", llm=stub, user_text="добавь задачу купить хлеб на 15 марта",
        inbound_message_id="audit3db-dup-msg", channel="max")
    from sreda.db.models.tasks import Task
    tasks_rows = db_session.query(Task).filter(Task.tenant_id == u.tenant_id).all()
    audit_rows = _audit(db_session, u.tenant_id, "task")
    assert len(audit_rows) == len(tasks_rows) >= 1, (
        f"audit ({len(audit_rows)}) ≠ числу задач ({len(tasks_rows)}) — reuse эмитит лишнее ИЛИ дубль")
    assert all((r.caused_by or {}).get("source") == "react" and r.action == "created"
               for r in audit_rows), "все audit — react/created"


@pytest.mark.asyncio
async def test_react_create_shopping_react_tagged(db_session):
    """3d-B: покупки уже эмитили аудит — теперь с react-провенансом (caused_by.source=react)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    stub = _Stub([
        AIMessage(content="", tool_calls=[{
            "name": "add_shopping_items",
            "args": {"items": [{"title": "молоко"}]}, "id": "as"}]),
        AIMessage(content="Готово."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="audit3db-sh", llm=stub, user_text="купи молоко",
        inbound_message_id="audit3db-sh-msg", channel="max")
    rows = _audit(db_session, u.tenant_id, "shopping_list_item")
    assert len(rows) >= 1, "покупки пишут аудит"
    assert (rows[0].caused_by or {}).get("source") == "react", rows[0].caused_by


def test_create_task_audit_failure_rolls_back(db_session, monkeypatch):
    """Codex high R1 MINOR (3d-B): non-race сбой аудита на СОЗДАНИИ задачи → откат вставки (named-W):
    задача НЕ остаётся без аудита (SELECT+аудит+commit под одним rollback-guard'ом)."""
    from datetime import date

    from sreda.db.models.tasks import Task
    from sreda.services import audit_feed as _af
    from sreda.services.tasks import TaskService

    u = seed_telegram_user(db_session)
    db_session.commit()

    def _boom(*a, **k):
        raise ValueError("audit boom")

    monkeypatch.setattr(_af, "emit_event", _boom)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, op_id="ocreate_fail", tool="add_task")):
        with pytest.raises(ValueError):
            TaskService(db_session).add(
                tenant_id=u.tenant_id, user_id=u.user_id, title="купить хлеб",
                scheduled_date=date(2031, 3, 15))
    db_session.rollback()
    assert db_session.query(Task).filter(Task.tenant_id == u.tenant_id).count() == 0, (
        "вставка задачи откатилась при сбое аудита (named-W)")
