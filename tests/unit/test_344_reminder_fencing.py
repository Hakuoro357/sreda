"""#344 F5 (reminder) — fencing: повторная проверка due/status ПОСЛЕ scan.

Аудит #336 F5(а): reminder-воркер снимает id в scan-сессии
(``due_now``, FOR UPDATE SKIP LOCKED держится ТОЛЬКО в этой сессии), затем
re-fetch'ит под tenant_session и НЕ перепроверяет ``status=='pending'`` /
``next_trigger_at<=now`` (только late-grace). Если другой воркер (или прошлый
тик) advance'нул reminder МЕЖДУ scan и действием — этот поток фаерит повторно
и преждевременно advance'ит.

Фикс: после ``s.get(reminder)`` перепроверить status/due; advance'нутый →
skip без fire/advance.

Модель отказа (M1 R1): целимся в FENCING, не в delivery-dedup. Двойную ДОСТАВКУ
того же fire уже держит idempotency_key + partial-unique. Тут — что воркер не
трогает reminder, который стал НЕ-due между scan и действием.

Харнесс: ``worker_db`` (коммитящая SQLite + шов ``_factory_for``). «Другой
воркер» моделируется патчем ``due_now``, который возвращает pre-advance снимок
id, а фактическую строку advance'ит в отдельной коммитящей сессии.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyReminder
import sreda.db.session as _dbs
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker


def _seed_base(session) -> None:
    session.add(Tenant(id="tenant_1", name="Test"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Default"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    session.commit()


def test_reminder_advanced_between_scan_and_action_not_refired(worker_db, monkeypatch):
    """RED→GREEN: reminder, advance'нутый другим воркером в FUTURE между scan и
    ``s.get``, НЕ фаерится повторно (нет outbox-строки, escalation не bump'ится
    снова). На текущем коде late-grace видит future-триггер как «не просрочено»
    → фаерит → outbox-строка появляется (двойной fire)."""
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    rem = svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Купить молоко", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()
    rid = rem.id

    def fake_due_now(self, *, now=None, limit=100):
        # «Другой воркер» уже отработал этот fire: advance next_trigger_at в
        # будущее (эскалация) + bump escalation, коммит в отдельной сессии.
        # Наш scan-снимок был снят ДО этого — возвращаем id как есть.
        s2 = _dbs._factory_for(None)()
        try:
            r2 = s2.get(FamilyReminder, rid)
            r2.next_trigger_at = now + timedelta(hours=1)
            r2.escalation_count = 1
            s2.commit()
        finally:
            s2.close()
        return [SimpleNamespace(id=rid, tenant_id="tenant_1")]

    monkeypatch.setattr(HousewifeReminderService, "due_now", fake_due_now)

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert worker_db.query(OutboxMessage).count() == 0, (
        "fencing: advance'нутый в future reminder не должен фаериться повторно"
    )
    assert fired == 0
    # escalation_count не bump'нулся дальше 1 (mark_fired НЕ вызывался вторично)
    assert worker_db.get(FamilyReminder, rid).escalation_count == 1


def test_reminder_marked_fired_between_scan_and_action_not_refired(worker_db, monkeypatch):
    """Fencing по STATUS: one-shot reminder, который другой воркер финализировал
    (status='fired', next_trigger_at=None) между scan и действием, НЕ фаерится
    повторно."""
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    rem = svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Разовое", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()
    rid = rem.id

    def fake_due_now(self, *, now=None, limit=100):
        s2 = _dbs._factory_for(None)()
        try:
            r2 = s2.get(FamilyReminder, rid)
            r2.status = "fired"
            r2.next_trigger_at = None
            s2.commit()
        finally:
            s2.close()
        return [SimpleNamespace(id=rid, tenant_id="tenant_1")]

    monkeypatch.setattr(HousewifeReminderService, "due_now", fake_due_now)

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert worker_db.query(OutboxMessage).count() == 0, (
        "fencing: уже-fired reminder не должен фаериться повторно"
    )
    assert fired == 0


def test_genuinely_due_reminder_still_fires(worker_db, monkeypatch):
    """НЕ-ВАКУУМНОСТЬ: reminder, который ПО-ПРЕЖНЕМУ due к моменту действия
    (никто не advance'нул), фаерится нормально — fencing не ломает happy-path."""
    _seed_base(worker_db)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Всё ещё due", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert fired == 1
    assert worker_db.query(OutboxMessage).count() == 1
