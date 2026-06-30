"""#163 Фаза 2b (named-Y) — partial-UNIQUE индекс блокирует второй *pending* дубль.

Инвариант: два pending напоминания/задачи с одним semantic_key
(tenant + user|tenant-wide + normalized_title_hash) невозможны на уровне БД.
- `COALESCE(user_id,'__tenant_wide__')` ловит и ОБЩЕСЕМЕЙНЫЕ (NULL user) — без него
  два NULL-user дубля проскользнули бы (NULL ≠ NULL в UNIQUE).
- Партиал `WHERE status='pending' AND normalized_title_hash IS NOT NULL`: не-pending и
  не-бэкфилленные (NULL hash) строки — ВНЕ индекса (дубли среди них допустимы).

red-before: без индекса второй insert проходит → тест падает; индекс делает его IntegrityError.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.tasks import Task
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from tests.unit.conftest import seed_telegram_user


def _ctx(tenant_id, user_id, eid):
    """Свежий ReAct-контекст (дедуп/хеш — только на ctx-пути; разные execution_id → разные op_id)."""
    return ToolRuntimeContext(operation_id=f"op_{eid}", execution_id=eid, step_id="s",
                              tool_name="t", tenant_id=tenant_id, user_id=user_id)


def _rem(tenant, user, nhash, status="pending"):
    return FamilyReminder(
        id=f"rem_{uuid4().hex[:20]}", tenant_id=tenant, user_id=user, title="лекарство",
        trigger_at=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc), status=status,
        bot_key="sreda", normalized_title_hash=nhash)


def _task(tenant, user, nhash, status="pending"):
    now = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    return Task(
        id=f"task_{uuid4().hex[:20]}", tenant_id=tenant, user_id=user, title="полить цветы",
        scheduled_date=date(2030, 6, 20), status=status, created_at=now, updated_at=now,
        normalized_title_hash=nhash)


def test_pending_semantic_unique_blocks_personal_dup_163(db_session):
    u = seed_telegram_user(db_session)
    db_session.commit()
    db_session.add(_rem(u.tenant_id, u.user_id, "H1"))
    db_session.commit()
    db_session.add(_rem(u.tenant_id, u.user_id, "H1"))  # тот же ключ, pending
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_pending_semantic_unique_blocks_tenant_wide_dup_163(db_session):
    """COALESCE-заглушка: два общесемейных (NULL user) с одним hash → 2-й заблокирован."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    db_session.add(_rem(u.tenant_id, None, "H2"))
    db_session.commit()
    db_session.add(_rem(u.tenant_id, None, "H2"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_pending_semantic_unique_allows_non_pending_and_null_hash_163(db_session):
    """Партиал: не-pending и NULL-hash (не бэкфилленные) — вне индекса, дубли допустимы."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    # тот же hash, но один fired (вне индекса) + один pending → не конфликтует
    db_session.add(_rem(u.tenant_id, u.user_id, "H3", status="fired"))
    db_session.commit()
    db_session.add(_rem(u.tenant_id, u.user_id, "H3", status="pending"))
    db_session.commit()
    # два NULL-hash pending → вне индекса (IS NOT NULL) → допустимо
    db_session.add(_rem(u.tenant_id, u.user_id, None))
    db_session.commit()
    db_session.add(_rem(u.tenant_id, u.user_id, None))
    db_session.commit()


def test_pending_semantic_unique_blocks_task_dup_163(db_session):
    """Тот же инвариант на tasks_items."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    db_session.add(_task(u.tenant_id, u.user_id, "TH1"))
    db_session.commit()
    db_session.add(_task(u.tenant_id, u.user_id, "TH1"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_schedule_reuses_existing_on_dup_163(db_session):
    """Сервис (named-Y, ctx-путь): повтор того же напоминания (иной ctx/op_id, тот же ключ) →
    reuse (та же строка, НЕ падаем, НЕ дубль). Легаси-путь (без ctx) дедупа не имеет — см. #162."""
    from sreda.services.housewife_reminders import HousewifeReminderService

    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)
    when = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e1")):
        r1 = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="лекарство", trigger_at=when)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e2")):
        r2 = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="лекарство", trigger_at=when)
    assert r1.id == r2.id, "повтор → та же строка (reuse)"
    n = db_session.query(FamilyReminder).filter_by(tenant_id=u.tenant_id).count()
    assert n == 1, f"должна остаться 1 строка, не дубль: {n}"


def test_add_dated_task_reuses_but_dateless_does_not_163(db_session):
    """Сервис (ctx-путь): повтор ДАТЕЛЬНОЙ задачи → reuse; БЕЗ даты → НЕ дедупим (две отдельные)."""
    from sreda.services.tasks import TaskService

    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e1")):
        t1 = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="дантист",
                     scheduled_date=date(2030, 6, 20))
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e2")):
        t2 = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="дантист",
                     scheduled_date=date(2030, 6, 20))
    assert t1.id == t2.id, "дательный повтор → reuse (та же задача)"
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e3")):
        d1 = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="купить хлеб")
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e4")):
        d2 = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="купить хлеб")
    assert d1.id != d2.id, "без даты → две отдельные задачи (не дедупим — решение Бориса)"


def test_time_only_task_not_deduped_163(db_session):
    """Codex high R1 MAJOR: задача со ВРЕМЕНЕМ но БЕЗ даты — НЕ дедупим (только «с датой»)."""
    from datetime import time as _time

    from sreda.services.tasks import TaskService

    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e1")):
        a = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="позвонить",
                    time_start=_time(9, 0))
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e2")):
        b = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="позвонить",
                    time_start=_time(9, 0))
    assert a.normalized_title_hash is None, "время-без-даты → hash не пишется"
    assert a.id != b.id, "время-без-даты → не дедупим (две отдельные)"


def test_punctuation_title_reminder_not_deduped_163(db_session):
    """Субагент R1 MINOR: вырожденное название (пунктуация → пустой хеш) → hash=None, НЕ дедупим."""
    from sreda.services.housewife_reminders import HousewifeReminderService

    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)
    when = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e1")):
        a = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="...", trigger_at=when)
    with bind_tool_runtime(_ctx(u.tenant_id, u.user_id, "e2")):
        b = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="...", trigger_at=when)
    assert a.normalized_title_hash is None, "пунктуация → пустой хеш → None (вне индекса)"
    assert a.id != b.id, "вырожденное название → не дедупим"
