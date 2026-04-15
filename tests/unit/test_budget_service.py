"""Phase 4.5b: BudgetService — quota status + usage recording."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.core import Tenant, User
from sreda.db.models.skill_platform import SkillAIExecution
from sreda.services.budget import BudgetService


def _utc(y, mo, d, h=0) -> datetime:
    return datetime(y, mo, d, h, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _seed_plan(
    session,
    *,
    plan_key: str,
    feature_key: str,
    credits_monthly_quota: int | None,
) -> SubscriptionPlan:
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=plan_key,
        feature_key=feature_key,
        title=plan_key,
        description="",
        price_rub=500,
        credits_monthly_quota=credits_monthly_quota,
    )
    session.add(plan)
    session.flush()
    return plan


def _seed_subscription(
    session,
    *,
    plan: SubscriptionPlan,
    tenant_id: str = "t1",
    starts_at: datetime | None = None,
    active_until: datetime | None = None,
    status: str = "active",
) -> TenantSubscription:
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        plan_id=plan.id,
        status=status,
        starts_at=starts_at or _utc(2026, 4, 1),
        active_until=active_until or _utc(2026, 5, 1),
    )
    session.add(sub)
    session.flush()
    return sub


# ---------------------------------------------------------------------------
# Quota status
# ---------------------------------------------------------------------------


def test_no_subscription_is_not_subscribed(session):
    svc = BudgetService(session)
    status = svc.get_quota_status("t1", "eds_monitor")
    assert not status.is_subscribed
    assert status.is_exhausted  # functionally off
    assert svc.has_quota("t1", "eds_monitor") is False


def test_active_subscription_with_quota_reports_zero_used(session):
    plan = _seed_plan(
        session, plan_key="eds_basic", feature_key="eds_monitor",
        credits_monthly_quota=1_000_000,
    )
    _seed_subscription(session, plan=plan)
    session.commit()

    svc = BudgetService(session)
    status = svc.get_quota_status("t1", "eds_monitor")
    assert status.is_subscribed
    assert not status.is_exhausted
    assert status.credits_used == 0
    assert status.credits_quota == 1_000_000


def test_expired_subscription_is_not_active(session):
    plan = _seed_plan(
        session, plan_key="eds_basic", feature_key="eds_monitor",
        credits_monthly_quota=1_000_000,
    )
    _seed_subscription(
        session, plan=plan,
        starts_at=_utc(2026, 1, 1), active_until=_utc(2026, 2, 1),
    )
    session.commit()

    svc = BudgetService(session)
    # April 15 2026 is well past Feb 1 expiry
    status = svc.get_quota_status("t1", "eds_monitor")
    assert not status.is_subscribed


def test_unmetered_plan_never_exhausted(session):
    plan = _seed_plan(
        session, plan_key="eds_unmetered", feature_key="eds_monitor",
        credits_monthly_quota=None,
    )
    _seed_subscription(session, plan=plan)
    session.commit()

    svc = BudgetService(session)
    # Record a huge usage — doesn't matter for unmetered plans
    svc.record_llm_usage(
        tenant_id="t1", feature_key="eds_monitor",
        model="mimo-v2-pro", prompt_tokens=1_000_000, completion_tokens=1_000_000,
        run_id=f"run_{uuid4().hex[:8]}",
    )
    session.commit()
    status = svc.get_quota_status("t1", "eds_monitor")
    assert status.is_subscribed
    assert not status.is_exhausted
    assert status.credits_quota is None


def test_exhausted_quota_blocks_further_use(session):
    plan = _seed_plan(
        session, plan_key="eds_basic", feature_key="eds_monitor",
        credits_monthly_quota=1_000,
    )
    _seed_subscription(session, plan=plan)
    session.commit()

    svc = BudgetService(session)
    svc.record_llm_usage(
        tenant_id="t1", feature_key="eds_monitor",
        model="mimo-v2-pro", prompt_tokens=300, completion_tokens=200,
        run_id=f"run_{uuid4().hex[:8]}",
    )
    session.commit()
    # 300+200 = 500 tokens × 2 (pro rate) = 1000 credits → exhausted
    status = svc.get_quota_status("t1", "eds_monitor")
    assert status.credits_used == 1000
    assert status.is_exhausted
    assert svc.has_quota("t1", "eds_monitor") is False


def test_usage_scoped_per_feature_key(session):
    eds_plan = _seed_plan(
        session, plan_key="eds_basic", feature_key="eds_monitor",
        credits_monthly_quota=1000,
    )
    _seed_subscription(session, plan=eds_plan)
    stub_plan = _seed_plan(
        session, plan_key="stub_basic", feature_key="stub_skill",
        credits_monthly_quota=1000,
    )
    _seed_subscription(session, plan=stub_plan)
    session.commit()

    svc = BudgetService(session)
    # Blow EDS budget entirely
    svc.record_llm_usage(
        tenant_id="t1", feature_key="eds_monitor",
        model="mimo-v2-omni", prompt_tokens=1000, completion_tokens=0,
        run_id="run_a",
    )
    session.commit()
    assert svc.has_quota("t1", "eds_monitor") is False
    # Stub skill should still be fine
    assert svc.has_quota("t1", "stub_skill") is True


def test_usage_outside_period_not_counted(session):
    plan = _seed_plan(
        session, plan_key="eds_basic", feature_key="eds_monitor",
        credits_monthly_quota=1000,
    )
    # Period: Apr 1 → May 1
    _seed_subscription(session, plan=plan)
    session.commit()

    svc = BudgetService(session)
    # Record usage with a manually back-dated created_at (simulating a
    # row from a previous billing period that was retained through
    # cleanup)
    old_row = SkillAIExecution(
        id=f"skai_{uuid4().hex[:8]}",
        run_id="run_old",
        attempt_id=None,
        tenant_id="t1",
        feature_key="eds_monitor",
        task_type="llm_call",
        provider_key="mimo",
        model="mimo-v2-pro",
        ai_schema_version=1,
        status="succeeded",
        prompt_tokens=500,
        completion_tokens=500,
        total_tokens=1000,
        credits_consumed=5000,  # deliberately huge
        created_at=_utc(2026, 3, 15),  # BEFORE period_start
    )
    session.add(old_row)
    session.commit()

    status = svc.get_quota_status("t1", "eds_monitor")
    assert status.credits_used == 0  # old usage ignored
    assert not status.is_exhausted


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def test_record_writes_row_with_correct_credits(session):
    svc = BudgetService(session)
    credits = svc.record_llm_usage(
        tenant_id="t1", feature_key="eds_monitor",
        model="mimo-v2-pro",
        prompt_tokens=200, completion_tokens=100,
        run_id="run_x", attempt_id="att_x",
        task_type="decide_to_speak",
    )
    session.commit()
    # 300 tokens × 2 = 600 credits
    assert credits == 600

    rows = session.query(SkillAIExecution).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.feature_key == "eds_monitor"
    assert r.model == "mimo-v2-pro"
    assert r.prompt_tokens == 200
    assert r.completion_tokens == 100
    assert r.credits_consumed == 600
    assert r.task_type == "decide_to_speak"
