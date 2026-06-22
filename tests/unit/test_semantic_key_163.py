"""#163 Фаза 2а/2b — time-aware semantic_key пишется на пути создания ТОЛЬКО через ReAct (ctx).

Контракт: одинаковое название + РАЗНОЕ время → РАЗНЫЙ ключ; пустой extra не меняет хеш (обратная
совместимость shopping). Scope (план #163 + контракт #162): хеш/дедуп — только на ctx-пути; легаси
(ctx=None) НЕ трогаем (его «без ctx = без дедупа» проверяет test_react_loop_*_idempotency).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.operation_id import compute_normalized_title_hash
from sreda.services.tasks import TaskService
from tests.unit.conftest import seed_telegram_user


def _ctx(tenant_id, user_id, eid, *, tool="t"):
    """Свежий ReAct-контекст (разные execution_id → разные operation_id; semantic_key решает дедуп)."""
    return ToolRuntimeContext(operation_id=f"op_{eid}", execution_id=eid, step_id="s",
                              tool_name=tool, tenant_id=tenant_id, user_id=user_id)


def test_hash_extra_time_aware_and_backcompat_163():
    """extra меняет хеш; пустой extra = как без него (shopping не сдвигается)."""
    no_extra = compute_normalized_title_hash("молоко", entity_type="x", tenant_id="t", user_id="u")
    empty_extra = compute_normalized_title_hash(
        "молоко", entity_type="x", tenant_id="t", user_id="u", extra="")
    t9 = compute_normalized_title_hash(
        "лекарство", entity_type="reminder", tenant_id="t", user_id="u", extra="09:00")
    t21 = compute_normalized_title_hash(
        "лекарство", entity_type="reminder", tenant_id="t", user_id="u", extra="21:00")
    assert no_extra == empty_extra, "пустой extra не должен менять хеш (обратная совместимость)"
    assert t9 and t21 and t9 != t21, "разное время → разный semantic-ключ"


def test_reminder_schedule_writes_time_aware_hash_163(db_session):
    """ReAct-путь: schedule пишет semantic_key; разное время → разный ключ; то же → reuse."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)

    def sched(when, eid):
        with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, eid)):
            return svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="лекарство",
                                trigger_at=when)

    r1 = sched(datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc), "e1")
    r2 = sched(datetime(2030, 1, 1, 21, 0, tzinfo=timezone.utc), "e2")
    assert r1.normalized_title_hash and r2.normalized_title_hash
    assert r1.normalized_title_hash != r2.normalized_title_hash, "разное время → разный ключ"
    # то же название+время (иной ctx/op_id) → semantic_key тот же → reuse существующей
    r3 = sched(datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc), "e3")
    assert r3.id == r1.id, "то же название+время → reuse (та же строка)"


def test_task_add_writes_hash_on_ctx_path_163(db_session):
    """ReAct-путь: add(дательная) пишет semantic_key; разная дата → разный ключ."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)

    def add(d, eid):
        with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, eid)):
            return svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="полить цветы",
                           scheduled_date=d)

    t1 = add(date(2030, 6, 20), "e1")
    t2 = add(date(2030, 6, 21), "e2")
    assert t1.normalized_title_hash, "дательная задача (ctx) должна писать semantic_key"
    assert t1.normalized_title_hash != t2.normalized_title_hash, "разная дата → разный ключ"
