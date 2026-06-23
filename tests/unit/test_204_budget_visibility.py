"""#204 V1 — voice spend is VISIBLE in /admin/budget under the carrier.

Adversarial-приёмка (2026-06-23) нашла пробел ПОКРЫТИЯ: существующие
``test_voice_transcription.py`` / ``test_max_voice.py`` проверяют, что
``record_api_usage`` пишется под ``feature_key=housewife_assistant``
(carrier, Решение 1 #204), но НЕ зовут ``get_budget_summary`` — поэтому
главный наблюдаемый исход «голос виден в общем бюджете админки» не
закреплён named-тестом.

Этот тест замыкает контур end-to-end на РЕАЛЬНОМ ``get_budget_summary``
(``admin/queries.py``): тенант с active housewife-подпиской (sreda_free,
quota=None) + одна voice-строка расхода, записанная РОВНО как это делает
transcribe (``record_api_usage(feature_key="housewife_assistant",
task_type="speech_recognition", credits_consumed=1)``) → в сводке бюджета
РОВНО одна строка carrier'а, отражающая voice-расход.

Сверенные сигнатуры (читал источник, не угадывал):
  * ``BudgetService.record_api_usage(*, tenant_id, feature_key,
    provider_key, task_type, credits_consumed, run_id=None) -> int``
    (``services/budget.py:178``).
  * ``get_budget_summary(session) -> list[BudgetRow]``
    (``admin/queries.py:195``) — итерирует подписки со ``status=='active'``,
    джойнит план за ``feature_key``, агрегирует ``SkillAIExecution`` по
    ``(tenant_id, feature_key)`` в окне периода. Строка существует ТОЛЬКО
    при active-подписке с этим feature_key — поэтому тест не вакуумный
    (без подписки строки бы не было; см. ``test_..._no_active_carrier_...``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.admin.queries import get_budget_summary
from sreda.db.base import Base
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.core import Tenant, User
from sreda.services.budget import BudgetService

_CARRIER = "housewife_assistant"


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Tenant One"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _seed_carrier_plan(session, *, credits_monthly_quota: int | None = None):
    """sreda_free housewife carrier: quota=None (unmetered) by default, как у
    реального free-тенанта (voice бесплатен, но виден в бюджете)."""
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key="sreda_free",
        feature_key=_CARRIER,
        title="Помощник домохозяйки (free)",
        description="",
        price_rub=0,
        credits_monthly_quota=credits_monthly_quota,
    )
    session.add(plan)
    session.flush()
    return plan


def _seed_active_subscription(session, *, plan, tenant_id: str = "t1"):
    _now = datetime.now(timezone.utc)
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        starts_at=_now - timedelta(days=15),
        active_until=_now + timedelta(days=15),
    )
    session.add(sub)
    session.flush()
    return sub


def _record_voice_usage(session, *, tenant_id: str = "t1"):
    """Записать voice-расход РОВНО как ``_maybe_transcribe_voice`` / MAX-twin:
    те же kwargs, что в проде (``telegram_bot.py:393`` / ``max_inbound.py:848``)."""
    BudgetService(session).record_api_usage(
        tenant_id=tenant_id,
        feature_key=_CARRIER,
        provider_key="yandex",
        task_type="speech_recognition",
        credits_consumed=1,
    )


def test_voice_spend_visible_in_admin_budget_under_carrier(session):
    """V1: active housewife carrier + одна voice-строка → ``get_budget_summary``
    показывает РОВНО одну строку под ``feature_key=housewife_assistant`` с
    учтённым расходом (виден в общем бюджете админки)."""
    plan = _seed_carrier_plan(session)
    _seed_active_subscription(session, plan=plan)
    _record_voice_usage(session)
    session.commit()

    rows = get_budget_summary(session)

    carrier_rows = [r for r in rows if r.feature_key == _CARRIER]
    assert len(carrier_rows) == 1, (
        f"ожидалась ровно 1 строка carrier'а в бюджете, получено {len(carrier_rows)}"
    )
    row = carrier_rows[0]
    assert row.tenant_id == "t1"
    assert row.plan_title == "Помощник домохозяйки (free)"
    # Voice-строка реально учтена в агрегате (1 вызов; sreda_free → quota=None
    # → credits_used отражает расход, usage_pct=None т.к. квоты нет).
    assert row.total_calls == 1
    assert row.credits_used == 1
    assert row.credits_quota is None


def test_voice_spend_aggregates_multiple_calls(session):
    """V1 (дополнение): N voice-вызовов под carrier → одна строка с
    ``total_calls == N`` (агрегат по (tenant, feature_key), не дробится)."""
    plan = _seed_carrier_plan(session)
    _seed_active_subscription(session, plan=plan)
    for _ in range(3):
        _record_voice_usage(session)
    session.commit()

    rows = get_budget_summary(session)
    carrier_rows = [r for r in rows if r.feature_key == _CARRIER]
    assert len(carrier_rows) == 1
    assert carrier_rows[0].total_calls == 3
    assert carrier_rows[0].credits_used == 3


def test_no_active_carrier_no_budget_row_non_vacuity(session):
    """V1 не-вакуумность: УБРАТЬ active housewife-подписку (расход остаётся) →
    в сводке НЕТ строки carrier'а. Доказывает, что строка в предыдущих тестах
    обусловлена именно active-подпиской, а не просто наличием ``SkillAIExecution``
    (``get_budget_summary`` итерирует от active-подписок, не от расхода)."""
    # Расход есть, но подписки НЕТ.
    _record_voice_usage(session)
    session.commit()

    rows = get_budget_summary(session)
    assert [r for r in rows if r.feature_key == _CARRIER] == []
