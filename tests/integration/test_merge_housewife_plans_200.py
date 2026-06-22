"""#200 Фаза 2 — DDL-unit тест миграции слияния free-тарифов (SQLite, реальный _apply).

Паттерн репо: схема через `Base.metadata.create_all` (SQLite), логику миграции зовём напрямую
через `_apply(conn)` (рефактор-хук), без alembic upgrade-head стека.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models import (  # noqa: F401 — регистрирует модели
    PaymentOrder,
    PaymentOrderItem,
    SubscriptionPlan,
    TenantSubscription,
)

NOW = datetime(2026, 6, 22, tzinfo=UTC)


def _load_apply():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations" / "versions" / "20260622_0063_merge_housewife_free_plans.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0063", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plan(plan_id: str, key: str):
    return SubscriptionPlan(
        id=plan_id, plan_key=key, feature_key="housewife_assistant",
        title=key, description="", price_rub=0, billing_period_days=30,
        is_public=True, is_active=True, sort_order=0,
    )


def _sub(sub_id: str, tenant: str, plan_id: str, *, status="active", gf=NOW):
    return TenantSubscription(
        id=sub_id, tenant_id=tenant, plan_id=plan_id,
        feature_key="housewife_assistant", status=status,
        starts_at=NOW - timedelta(days=5), active_until=None,
        cancel_at_period_end=False, quantity=1, next_cycle_quantity=1,
        grandfathered_at=gf,
    )


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def test_merge_migrates_subs_and_drops_unreferenced_plans(engine):
    mig = _load_apply()
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add_all([
        _plan("plan_housewife_base", "housewife_assistant_base"),
        _plan("plan_sreda_free", "sreda_free"),
        _plan("plan_housewife_grandfathered", "housewife_grandfathered"),
    ])
    for i in range(3):
        s.add(_sub(f"sub{i}", f"t{i}", "plan_housewife_base", gf=NOW))
    s.commit()
    s.close()

    with engine.begin() as conn:
        mig._apply(conn)

    s = Session()
    subs = s.query(TenantSubscription).all()
    # 3 подписки → sreda_free, grandfathered_at сохранён, reason проставлен
    assert {x.plan_id for x in subs} == {"plan_sreda_free"}
    assert all(x.grandfathered_at is not None for x in subs)
    assert all("Migration #200" in (x.grandfather_reason or "") for x in subs)
    assert all(x.feature_key == "housewife_assistant" for x in subs)
    # оба legacy-плана удалены (0 ссылок), остался только sreda_free
    assert {p.id for p in s.query(SubscriptionPlan).all()} == {"plan_sreda_free"}
    s.close()

    # идемпотентность: повторный прогон — без ошибок, без изменений
    with engine.begin() as conn:
        mig._apply(conn)
    s = Session()
    assert s.query(TenantSubscription).filter_by(plan_id="plan_sreda_free").count() == 3
    assert {p.id for p in s.query(SubscriptionPlan).all()} == {"plan_sreda_free"}
    s.close()


def test_referenced_plan_retired_not_deleted(engine):
    """Если на legacy-план есть ссылка (historic-подписка) → НЕ удаляем, ретайр (c-RET2)."""
    mig = _load_apply()
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add_all([
        _plan("plan_housewife_base", "housewife_assistant_base"),
        _plan("plan_sreda_free", "sreda_free"),
        _plan("plan_housewife_grandfathered", "housewife_grandfathered"),
    ])
    s.add(_sub("sub_active", "t1", "plan_housewife_base", gf=NOW))
    # cancelled historic sub на grandfathered-плане → ссылка → ретайр, не дроп
    s.add(_sub("sub_hist", "t2", "plan_housewife_grandfathered", status="cancelled", gf=NOW))
    s.commit()
    s.close()

    with engine.begin() as conn:
        mig._apply(conn)

    s = Session()
    plans = {p.id: p for p in s.query(SubscriptionPlan).all()}
    # base (0 ссылок) удалён; grandfathered (есть ссылка) ретайрен, не удалён
    assert "plan_housewife_base" not in plans
    assert "plan_housewife_grandfathered" in plans
    assert plans["plan_housewife_grandfathered"].is_active is False
    assert plans["plan_housewife_grandfathered"].is_public is False
    s.close()


# NB: abort-on-active-duplicate guard в миграции — defense-in-depth. Сценарий «2 active
# housewife у одного тенанта» БД-предотвращён partial-unique-индексом
# (ix tenant_id+feature_key WHERE status='active'), поэтому через ORM/raw его не засеять
# (INSERT падает на IntegrityError). Guard остаётся на случай будущего снятия индекса; тестом
# не воспроизводим (нечего сеять) — это и есть подтверждение, что аномалия невозможна.
