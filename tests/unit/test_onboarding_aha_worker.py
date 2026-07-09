"""Smoke-тесты OnboardingAhaWorker (#138 Ф2 — воркер сам ведёт seam-сессии).

Раньше у воркера НЕ было юнит-теста (пробел). Рефактор #138 Ф2 меняет структуру
(privileged-скан свежеодобренных → per-tenant tenant_session → aha2 outbox + sentinel,
пер-тенантный commit) — эти тесты верифицируют путь end-to-end + фиксируют инвариант
идемпотентности (sentinel) и новый инвариант «no-diet sentinel персистит всегда».
Через фикстуру ``worker_db`` (коммитящая файловая SQLite + патч шва _factory_for).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sreda.db.models.core import OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyMember, FamilyReminder
from sreda.workers.onboarding_aha_worker import OnboardingAhaWorker

# 11 UTC — внутри окна отправки (10-13 UTC); approved 24ч назад — в [20ч, 48ч].
_NOW = datetime(2026, 5, 1, 11, 0, tzinfo=UTC)


def _seed(session, *, tenant_id: str, notes: str, tg: str) -> None:
    session.add(Tenant(id=tenant_id, name="Aha", approved_at=_NOW - timedelta(hours=24)))
    session.add(Workspace(id=f"ws_{tenant_id}", tenant_id=tenant_id, name="WS"))
    session.add(User(id=f"u_{tenant_id}", tenant_id=tenant_id, telegram_account_id=tg))
    session.add(FamilyMember(
        id=f"fm_{tenant_id}", tenant_id=tenant_id, user_id=f"u_{tenant_id}",
        name="Маша", role="self", notes=notes,
    ))
    session.commit()


def test_aha2_fires_for_diet_tenant_and_is_idempotent(worker_db) -> None:
    _seed(worker_db, tenant_id="t_aha", notes="аллергия на глютен", tg="900")

    sent = asyncio.run(OnboardingAhaWorker().process_pending(now=_NOW))

    worker_db.expire_all()
    assert sent == 1
    outboxes = worker_db.query(OutboxMessage).all()
    assert len(outboxes) == 1
    assert "меню" in outboxes[0].payload_json
    sentinel = (
        worker_db.query(FamilyReminder)
        .filter(FamilyReminder.source_memo == "aha2:t_aha")
        .first()
    )
    assert sentinel is not None
    assert sentinel.status == "fired"

    # Второй тик — sentinel блокирует, новых outbox нет (идемпотентность).
    sent2 = asyncio.run(OnboardingAhaWorker().process_pending(now=_NOW))
    worker_db.expire_all()
    assert sent2 == 0
    assert worker_db.query(OutboxMessage).count() == 1


def test_aha2_no_diet_creates_sentinel_without_outbox(worker_db) -> None:
    """No-diet тенант: outbox НЕ шлётся, но sentinel создаётся (не перепроверять).
    #138 Ф2 инвариант: пер-тенантный commit → sentinel персистит ВСЕГДА (старый код
    коммитил только if sent>0 — no-diet sentinel терялся, если в тике не было отправок)."""
    _seed(worker_db, tenant_id="t_nodiet", notes="любит футбол", tg="901")

    sent = asyncio.run(OnboardingAhaWorker().process_pending(now=_NOW))

    worker_db.expire_all()
    assert sent == 0
    assert worker_db.query(OutboxMessage).count() == 0
    sentinel = (
        worker_db.query(FamilyReminder)
        .filter(FamilyReminder.source_memo == "aha2:t_nodiet")
        .first()
    )
    assert sentinel is not None  # ключевой инвариант рефактора


def test_aha2_outside_send_window_does_nothing(worker_db) -> None:
    """Вне окна 10-13 UTC — воркер не сканирует и ничего не пишет."""
    _seed(worker_db, tenant_id="t_win", notes="диета кето", tg="902")
    off_window = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)  # 3 UTC — вне окна

    sent = asyncio.run(OnboardingAhaWorker().process_pending(now=off_window))

    worker_db.expire_all()
    assert sent == 0
    assert worker_db.query(OutboxMessage).count() == 0
    assert worker_db.query(FamilyReminder).count() == 0  # даже sentinel не создан
