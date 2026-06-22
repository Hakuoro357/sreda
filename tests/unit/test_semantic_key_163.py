"""#163 Фаза 2а — time-aware semantic_key пишется на пути создания напоминаний/задач.

Контракт: одинаковое название + РАЗНОЕ время → РАЗНЫЙ ключ (не схлопываем «лекарство 9:00» и
«21:00»); пустой extra не меняет хеш (обратная совместимость shopping). Уникальность (индекс) — 2в.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.operation_id import compute_normalized_title_hash
from sreda.services.tasks import TaskService
from tests.unit.conftest import seed_telegram_user


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
    """schedule пишет semantic_key; одинаковое название + разное время → разный ключ."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = HousewifeReminderService(db_session)
    r1 = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="лекарство",
                      trigger_at=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))
    r2 = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="лекарство",
                      trigger_at=datetime(2030, 1, 1, 21, 0, tzinfo=timezone.utc))
    assert r1.normalized_title_hash, "напоминание должно писать semantic_key"
    assert r2.normalized_title_hash
    assert r1.normalized_title_hash != r2.normalized_title_hash, "разное время → разный ключ"
    # тот же повтор (название+время) → тот же ключ
    r3 = svc.schedule(tenant_id=u.tenant_id, user_id=u.user_id, title="лекарство",
                      trigger_at=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert r3.normalized_title_hash == r1.normalized_title_hash, "то же название+время → тот же ключ"


def test_task_add_writes_hash_163(db_session):
    """add пишет semantic_key; разная дата → разный ключ."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    svc = TaskService(db_session)
    t1 = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="полить цветы",
                 scheduled_date=date(2030, 6, 20))
    t2 = svc.add(tenant_id=u.tenant_id, user_id=u.user_id, title="полить цветы",
                 scheduled_date=date(2030, 6, 21))
    assert t1.normalized_title_hash, "задача должна писать semantic_key"
    assert t1.normalized_title_hash != t2.normalized_title_hash, "разная дата → разный ключ"
