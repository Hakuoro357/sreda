"""R1-фиксы аудита 2026-07-18, область W5 (money-path).

Покрывает находки decision-log R1:

- C7 billing.renew_cycle — mixed-period: каждая позиция получает СВОЁ окно
     (anchor + plan.billing_period_days), НЕ max по всем. 7-дневный план
     больше не получает 30 дней за одну оплату. Цикл next-due = самое раннее.

(M9/M12/M21 — добавляются по мере имплементации соответствующих фиксов.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
import sreda.db.models  # noqa: F401
from sreda.db.models import Tenant, User
from sreda.db.models.billing import (
    PaymentOrderItem,
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.services.billing import BillingService


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _seed_plan(session, *, plan_key, feature_key, price_rub, billing_period_days):
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}", plan_key=plan_key, feature_key=feature_key,
        title=plan_key, description="", price_rub=price_rub,
        billing_period_days=billing_period_days,
    )
    session.add(plan)
    session.flush()
    return plan


def _seed_sub(session, plan):
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}", tenant_id="t1", plan_id=plan.id,
        feature_key=plan.feature_key, status="active", quantity=1,
        next_cycle_quantity=1,
    )
    session.add(sub)
    session.flush()
    return sub


def test_c7_mixed_period_renewal_uses_per_plan_window(session) -> None:
    """7-дневный и 30-дневный планы продлеваются ВМЕСТЕ. Каждый получает своё
    окно (7 и 30 дней), а НЕ max(30) на оба. Цикл next-due = самое раннее (7)."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan7 = _seed_plan(
        session, plan_key="weekly", feature_key="feat_weekly",
        price_rub=70, billing_period_days=7,
    )
    plan30 = _seed_plan(
        session, plan_key="monthly", feature_key="feat_monthly",
        price_rub=100, billing_period_days=30,
    )
    sub7 = _seed_sub(session, plan7)
    sub30 = _seed_sub(session, plan30)
    cycle = TenantBillingCycle(
        id=f"cyc_{uuid4().hex[:16]}", tenant_id="t1",
        billing_anchor_at=now, next_payment_due_at=now, status="active",
    )
    session.add(cycle)
    session.commit()

    BillingService(session).renew_cycle("t1", now=now)

    session.refresh(sub7)
    session.refresh(sub30)
    session.refresh(cycle)
    # Каждая подписка — СВОЁ окно (не раздутое до max).
    assert _aware(sub7.active_until) == now + timedelta(days=7)
    assert _aware(sub30.active_until) == now + timedelta(days=30)
    # Цикл платит снова, когда истекает ПЕРВАЯ подписка (7 дней).
    assert _aware(cycle.next_payment_due_at) == now + timedelta(days=7)

    # PaymentOrderItem'ы несут поплановый period_end.
    items = {
        session.get(SubscriptionPlan, i.plan_id).billing_period_days: i
        for i in session.query(PaymentOrderItem).all()
    }
    assert _aware(items[7].period_end) == now + timedelta(days=7)
    assert _aware(items[30].period_end) == now + timedelta(days=30)
    # amount поплановый (не смешан).
    assert items[7].amount_rub == 70
    assert items[30].amount_rub == 100
