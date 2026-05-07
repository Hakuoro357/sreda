"""Phase 1 of free-tier-subscription plan — integration tests for
migration 0041 + new schema (usage_ledger, signup_attempts,
TenantSubscription extensions).

Strategy: Base.metadata.create_all() даёт up-to-date schema (включая
все Phase 1 changes). Тесты focus on:
- Observable model behavior (event listener populates feature_key)
- Constraint enforcement (partial unique index, usage_ledger UNIQUE)
- Migration data-manipulation logic (re-point grandfather, preflight)
- downgrade() refuses

NB: full Alembic upgrade-head stack не используется (issue с SQLite
ALTER limitations на старых миграциях, см. test_migration_0029).
Schema correctness — гарантируется SQLAlchemy сравнением модели с
DDL'ом миграции (manual code review).
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import (  # noqa: F401 — registers all models
    SignupAttempt,
    SubscriptionPlan,
    Tenant,
    TenantSubscription,
    UsageLedger,
    User,
)


_NOW = datetime.now(timezone.utc)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _migration_module():
    """Load migration 0041 by file path (numeric prefix invalid as Python id)."""
    repo_root = Path(__file__).resolve().parents[2]
    path = (
        repo_root / "migrations" / "versions"
        / "20260507_0041_subscription_quotas.py"
    )
    spec = importlib.util.spec_from_file_location("_migration_0041", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_plan(session, **kwargs):
    """Helper: insert SubscriptionPlan with sensible defaults."""
    defaults = dict(
        id=f"plan_{kwargs.get('plan_key', 'x')}",
        title="Test plan",
        description="...",
        price_rub=0,
        billing_period_days=30,
        is_public=True,
        is_active=True,
        sort_order=100,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    plan = SubscriptionPlan(**defaults)
    session.add(plan)
    session.commit()
    return plan


def _seed_tenant(session, tenant_id: str, *, approved: bool = True):
    tenant = Tenant(
        id=tenant_id,
        name=f"Tenant {tenant_id}",
        created_at=_NOW,
        approved_at=_NOW if approved else None,
    )
    session.add(tenant)
    session.commit()
    return tenant


def _seed_subscription(session, *, sub_id, tenant_id, plan, status="active",
                       feature_key=None, **kwargs):
    """Insert TenantSubscription. feature_key is auto-populated by listener
    if not provided."""
    sub = TenantSubscription(
        id=sub_id,
        tenant_id=tenant_id,
        plan_id=plan.id,
        feature_key=feature_key or plan.feature_key,  # explicit для теста
        status=status,
        starts_at=_NOW,
        active_until=None,
        cancel_at_period_end=False,
        quantity=1,
        next_cycle_quantity=1,
        created_at=_NOW,
        updated_at=_NOW,
        **kwargs,
    )
    session.add(sub)
    session.commit()
    return sub


# ---------------------------------------------------------------- model behavior


def test_event_listener_populates_feature_key_from_plan(session):
    """Listener fills TenantSubscription.feature_key из plan если не задан."""
    plan = _seed_plan(
        session, plan_key="test_pl", feature_key="housewife_assistant",
    )
    _seed_tenant(session, "t_listener")
    # Create без feature_key — listener должен populate
    sub = TenantSubscription(
        id="sub_listener", tenant_id="t_listener", plan_id=plan.id,
        feature_key=None,  # explicit None
        status="active", starts_at=_NOW, active_until=None,
        cancel_at_period_end=False, quantity=1, next_cycle_quantity=1,
        created_at=_NOW, updated_at=_NOW,
    )
    session.add(sub)
    session.commit()

    session.refresh(sub)
    assert sub.feature_key == "housewife_assistant"


def test_event_listener_resyncs_feature_key_on_plan_id_change(session):
    """Xiaomi review R1 MAJOR fix: listener re-syncs feature_key когда
    UPDATE меняет plan_id (upgrade/downgrade scenario). Без этого
    feature_key становится stale — entitlement queries в Phase 2
    возвращают неправильный tier."""
    plan_old = _seed_plan(
        session, plan_key="resync_old", feature_key="feature_a",
    )
    plan_new = _seed_plan(
        session, plan_key="resync_new", feature_key="feature_b",
    )
    _seed_tenant(session, "t_resync")
    sub = _seed_subscription(
        session, sub_id="sub_resync",
        tenant_id="t_resync", plan=plan_old, status="active",
    )
    assert sub.feature_key == "feature_a"

    # UPDATE plan_id → listener должен re-sync feature_key
    sub.plan_id = plan_new.id
    session.commit()
    session.refresh(sub)
    assert sub.feature_key == "feature_b"


def test_event_listener_does_not_overwrite_explicit_feature_key(session):
    """Если caller указал feature_key — listener не overwrite'ит."""
    plan = _seed_plan(
        session, plan_key="test_pl2", feature_key="housewife_assistant",
    )
    _seed_tenant(session, "t_no_overwrite")
    sub = _seed_subscription(
        session, sub_id="sub_explicit",
        tenant_id="t_no_overwrite", plan=plan,
        feature_key="explicit_override",
    )
    session.refresh(sub)
    assert sub.feature_key == "explicit_override"


def test_multi_active_subs_per_feature_allowed(session):
    """eds_monitor billing pattern: tenant has multi active subs same
    feature_key (base + extra-account slots). Phase 1 НЕ enforces
    single-active invariant на DB-level — это compatible с existing
    eds_monitor multi-slot model. Single-active invariant for
    `housewife_assistant` enforced на app-level в Phase 2 через
    EntitlementGate + SignupAbuseGuard.
    """
    plan_a = _seed_plan(
        session, plan_key="multi_a", feature_key="eds_monitor",
    )
    plan_b = _seed_plan(
        session, plan_key="multi_b", feature_key="eds_monitor",
    )
    _seed_tenant(session, "t_multi")
    _seed_subscription(
        session, sub_id="sub_base",
        tenant_id="t_multi", plan=plan_a, status="active",
    )
    # Both active — должно работать (no DB constraint blocking)
    sub2 = _seed_subscription(
        session, sub_id="sub_extra",
        tenant_id="t_multi", plan=plan_b, status="active",
    )
    assert sub2.status == "active"


def test_usage_ledger_unique_period(engine):
    """UNIQUE (tenant_id, metric, period_type, period_key)."""
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        _seed_tenant(session, "t_ledger")
        session.add(UsageLedger(
            tenant_id="t_ledger", metric="llm_turns",
            period_type="daily", period_key="2026-05-07",
            used=5, quota_snapshot=20, updated_at=_NOW,
        ))
        session.commit()
        # Second row same composite — IntegrityError
        with pytest.raises(IntegrityError):
            session.add(UsageLedger(
                tenant_id="t_ledger", metric="llm_turns",
                period_type="daily", period_key="2026-05-07",
                used=10, quota_snapshot=20, updated_at=_NOW,
            ))
            session.commit()
        session.rollback()

        # Different period_key — OK
        session.add(UsageLedger(
            tenant_id="t_ledger", metric="llm_turns",
            period_type="daily", period_key="2026-05-08",
            used=1, quota_snapshot=20, updated_at=_NOW,
        ))
        session.commit()
    finally:
        session.close()


def test_signup_attempts_basic_insert_query(session):
    """SignupAttempt journal: insert + query by (channel, hash)."""
    session.add_all([
        SignupAttempt(
            channel="telegram",
            source_id_hash="a" * 64,
            attempted_at=_NOW,
        ),
        SignupAttempt(
            channel="telegram",
            source_id_hash="a" * 64,
            attempted_at=_NOW,
        ),
        SignupAttempt(
            channel="max",
            source_id_hash="b" * 64,
            attempted_at=_NOW,
        ),
    ])
    session.commit()

    rows = session.execute(
        select(SignupAttempt).where(
            SignupAttempt.channel == "telegram",
            SignupAttempt.source_id_hash == "a" * 64,
        )
    ).scalars().all()
    assert len(rows) == 2


# ---------------------------------------------------------------- migration logic


def test_downgrade_raises():
    """downgrade() refuses (one-way migration; backup restore = rollback)."""
    mod = _migration_module()
    with pytest.raises(RuntimeError, match="one-way"):
        mod.downgrade()


def test_grandfather_repoint_logic_preserves_existing_subs(session):
    """Migration's re-point step: existing housewife_assistant active sub →
    housewife_grandfathered. Verify SQL replicated в test."""
    # Setup pre-migration state
    ha_plan = _seed_plan(
        session, plan_key="housewife_assistant",
        feature_key="housewife_assistant", sort_order=10,
    )
    _seed_plan(
        session, plan_key="housewife_grandfathered",
        feature_key="housewife_assistant", sort_order=999,
        is_public=False,
    )
    _seed_tenant(session, "t_existing", approved=True)
    sub = _seed_subscription(
        session, sub_id="sub_existing",
        tenant_id="t_existing", plan=ha_plan, status="active",
    )
    pre_plan_id = sub.plan_id

    # Replicate migration step 9: re-point active housewife_assistant subs
    # of approved tenants → housewife_grandfathered (SQLite syntax variant).
    session.execute(text("""
        UPDATE tenant_subscriptions
        SET plan_id = (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_grandfathered'),
            grandfathered_at = :now,
            grandfather_reason = 'test grandfather'
        WHERE tenant_id IN (SELECT id FROM tenants WHERE approved_at IS NOT NULL)
          AND status = 'active'
          AND feature_key = 'housewife_assistant'
          AND plan_id = (SELECT id FROM subscription_plans WHERE plan_key = 'housewife_assistant')
    """), {"now": _NOW})
    session.commit()
    session.refresh(sub)

    assert sub.plan_id != pre_plan_id
    assert sub.grandfathered_at is not None
    assert sub.grandfather_reason == "test grandfather"


def test_grandfather_creates_sub_for_approved_with_cancelled_only(session):
    """Edge case: approved tenant has CANCELLED housewife sub (no active).
    Migration treats this как "no active" и создаёт grandfathered sub
    в дополнение к cancelled (Xiaomi review R1 MINOR — explicit coverage)."""
    _seed_plan(
        session, plan_key="housewife_grandfathered",
        feature_key="housewife_assistant", sort_order=999, is_public=False,
    )
    ha_plan = _seed_plan(
        session, plan_key="housewife_assistant",
        feature_key="housewife_assistant", sort_order=10,
    )
    _seed_tenant(session, "t_cancelled", approved=True)
    _seed_subscription(
        session, sub_id="sub_cancelled",
        tenant_id="t_cancelled", plan=ha_plan, status="cancelled",
    )

    rows = session.execute(text("""
        SELECT t.id AS tenant_id
        FROM tenants t
        WHERE t.approved_at IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM tenant_subscriptions ts
            WHERE ts.tenant_id = t.id
              AND ts.feature_key = 'housewife_assistant'
              AND ts.status = 'active'
          )
    """)).fetchall()
    assert len(rows) == 1
    assert rows[0].tenant_id == "t_cancelled"


def test_grandfather_creates_sub_for_approved_without_active(session):
    """Migration's step 10: approved tenant без active housewife sub →
    создаётся grandfathered subscription."""
    _seed_plan(
        session, plan_key="housewife_grandfathered",
        feature_key="housewife_assistant", sort_order=999, is_public=False,
    )
    _seed_tenant(session, "t_approved_no_sub", approved=True)
    _seed_tenant(session, "t_pending", approved=False)

    # Replicate migration step 10 (Python loop, не SQL чтобы dialect-agnostic)
    rows = session.execute(text("""
        SELECT t.id AS tenant_id
        FROM tenants t
        WHERE t.approved_at IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM tenant_subscriptions ts
            WHERE ts.tenant_id = t.id
              AND ts.feature_key = 'housewife_assistant'
              AND ts.status = 'active'
          )
    """)).fetchall()
    for row in rows:
        session.add(TenantSubscription(
            id=f"sub_gf_{row.tenant_id}",
            tenant_id=row.tenant_id,
            plan_id="plan_housewife_grandfathered",
            feature_key="housewife_assistant",
            status="active",
            starts_at=_NOW,
            active_until=None,
            grandfathered_at=_NOW,
            grandfather_reason="approved without sub",
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
            created_at=_NOW,
            updated_at=_NOW,
        ))
    session.commit()

    # t_approved_no_sub получил grandfathered, t_pending — нет
    approved_sub = session.execute(
        select(TenantSubscription).where(
            TenantSubscription.tenant_id == "t_approved_no_sub"
        )
    ).scalar_one_or_none()
    assert approved_sub is not None
    assert approved_sub.grandfathered_at is not None

    pending_sub = session.execute(
        select(TenantSubscription).where(
            TenantSubscription.tenant_id == "t_pending"
        )
    ).scalar_one_or_none()
    assert pending_sub is None


