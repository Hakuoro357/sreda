"""Фаза 0 #162 — идемпотентность создания напоминаний в ReAct-пути.

Чеклист приёмки #162:
  п.1 — повтор создания ВНУТРИ хода (тот же ctx) → ровно ОДНА строка.
  п.2 — тот же title, ДРУГОЕ время → ДВЕ строки (время входит в logical_key,
        иначе «лекарство в 9:00» и «в 21:00» молча схлопнулись бы — R2 CRITICAL).

Эталон ветки ctx — services/housewife_shopping.py::add_items (Sub-A12 Phase E):
  ctx is None → легаси байт-в-байт; ctx is not None → operation_id (с ВРЕМЕНЕМ в
  logical_key) + INSERT ON CONFLICT DO NOTHING + SELECT существующей строки.

RED-before-impl: сейчас schedule() — плоский add+commit без operation_id, поэтому
повтор создаёт ВТОРУЮ строку → test_replay падает. Зелёным станет после ctx-ветки.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sreda.db.models.housewife import FamilyReminder
from sreda.runtime.planner.tool_runtime import (
    ToolRuntimeContext,
    bind_tool_runtime,
)
from sreda.services.housewife_reminders import HousewifeReminderService
from tests.unit.conftest import seed_telegram_user


def _ctx(*, tenant_id: str, user_id: str, step_id: str = "step_1") -> ToolRuntimeContext:
    # execution_id = детерминированный непрозрачный id из turn_key (sha1(turn_key)
    # в реальном пути); в тесте фиксируем напрямую — важна СТАБИЛЬНОСТЬ между
    # вызовами одного хода.
    return ToolRuntimeContext(
        operation_id="op_step_level_test",  # per-step id (для emit_event; в срезе не зовём)
        execution_id="exec_react_test",
        step_id=step_id,
        tool_name="schedule_reminder",
        tenant_id=tenant_id,
        user_id=user_id,
        turn_key="react:max:tenant_1:msg_1",
    )


def test_schedule_reminder_ctx_replay_single_row(db_session):
    """п.1: повтор того же tool_call внутри хода (тот же ctx, тот же title+время)
    → РОВНО одна строка."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)
    when = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)
    ctx = _ctx(tenant_id=u.tenant_id, user_id=u.user_id)

    with bind_tool_runtime(ctx):
        svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="разминка", trigger_at=when)
    with bind_tool_runtime(ctx):  # resume/крэш-ретрай внутри хода — тот же ctx
        svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="разминка", trigger_at=when)

    rows = (
        db_session.query(FamilyReminder)
        .filter_by(tenant_id=u.tenant_id, user_id=u.user_id)
        .all()
    )
    assert len(rows) == 1, f"ожидали 1 строку (идемпотентно), получили {len(rows)}"


def test_schedule_reminder_same_title_different_time_two_rows(db_session):
    """п.2: тот же ctx-шаг и title, но ДРУГОЕ время → ДВЕ строки (время в
    logical_key). Если ключ был бы title-only — второй вызов схлопнулся бы в один."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)
    t1 = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)
    ctx = _ctx(tenant_id=u.tenant_id, user_id=u.user_id)  # один и тот же step_id

    with bind_tool_runtime(ctx):
        svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="разминка", trigger_at=t1)
    with bind_tool_runtime(ctx):
        svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="разминка", trigger_at=t2)

    rows = (
        db_session.query(FamilyReminder)
        .filter_by(tenant_id=u.tenant_id, user_id=u.user_id)
        .all()
    )
    assert len(rows) == 2, f"разное время — ожидали 2 строки, получили {len(rows)}"


def test_schedule_reminder_legacy_no_ctx_unchanged(db_session):
    """п.11: без ctx (легаси-путь) поведение прежнее — каждый вызов создаёт строку."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)
    when = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)

    # без bind_tool_runtime: current_tool_runtime() is None
    svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="разминка", trigger_at=when)
    svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="разминка", trigger_at=when)

    rows = (
        db_session.query(FamilyReminder)
        .filter_by(tenant_id=u.tenant_id, user_id=u.user_id)
        .all()
    )
    assert len(rows) == 2, "легаси-путь без ctx — без дедупа, 2 вызова = 2 строки"
