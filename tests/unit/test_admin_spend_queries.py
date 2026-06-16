"""#150 F2: оконные агрегации трат по моделям + покрытие.

Контракт окна: aware-UTC, полуоткрытое [start, end), календарь MSK; месяц —
календарной арифметикой; неделя — пн 00:00 MSK. Стоимость — через llm_pricing
(беспрайсовое → «—»); построчные аномалии (отрицательные токены) исключаются
из сумм и считаются отдельно (Codex-ревью: без взаимогашения в группе).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.admin.queries import get_spend_by_model, period_window_utc
from sreda.db.base import Base
from sreda.db.models.core import Tenant
from sreda.db.models.skill_platform import SkillAIExecution

UTC = timezone.utc
# Якорь: 16 июня 2026, вторник, 12:00 UTC (15:00 MSK).
ANCHOR = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(Tenant(id="t1", name="T"))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _exec(session, *, provider_key, model, prompt, completion, created):
    now = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
    session.add(SkillAIExecution(
        id=f"skai_{uuid4().hex[:16]}", run_id=f"r_{uuid4().hex[:8]}", attempt_id=None,
        tenant_id="t1", feature_key="housewife_assistant", task_type="llm_call",
        provider_key=provider_key, model=model, ai_schema_version=1, status="succeeded",
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=(prompt if prompt > 0 else 0) + (completion if completion > 0 else 0),
        credits_consumed=0, created_at=created, started_at=created, finished_at=now,
    ))


# --- period_window_utc -------------------------------------------------------


def test_window_day_half_open_aware_utc() -> None:
    start, end = period_window_utc("day", ANCHOR)
    assert start.tzinfo is not None and end.tzinfo is not None  # aware
    # день MSK 2026-06-16 = [2026-06-15 21:00 UTC, 2026-06-16 21:00 UTC)
    assert start == datetime(2026, 6, 15, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 6, 16, 21, 0, tzinfo=UTC)


def test_window_week_starts_monday_msk() -> None:
    start, end = period_window_utc("week", ANCHOR)
    # вторник 16-е → неделя с пн 15-го 00:00 MSK = 14-го 21:00 UTC; +7 дней.
    assert start == datetime(2026, 6, 14, 21, 0, tzinfo=UTC)
    assert (end - start).days == 7


def test_window_month_calendar_arithmetic() -> None:
    start, end = period_window_utc("month", ANCHOR)
    # июнь MSK = [2026-06-01 00:00 MSK, 2026-07-01 00:00 MSK)
    assert start == datetime(2026, 5, 31, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 6, 30, 21, 0, tzinfo=UTC)


def test_window_month_december_rolls_to_next_year() -> None:
    dec = datetime(2026, 12, 16, 12, 0, tzinfo=UTC)
    start, end = period_window_utc("month", dec)
    assert end == datetime(2026, 12, 31, 21, 0, tzinfo=UTC)  # 2027-01-01 00:00 MSK


# --- get_spend_by_model ------------------------------------------------------


def test_spend_priced_unpriced_anomaly(session) -> None:
    in_win = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)  # внутри дня/недели/месяца
    # Mercury (priced), Gemini (priced), MiMo (unpriced), аномалия (negative).
    _exec(session, provider_key="inception-mercury2", model="mercury-2",
          prompt=1_000_000, completion=0, created=in_win)
    _exec(session, provider_key="openrouter-gemini-2.5-flash-lite",
          model="google/gemini-2.5-flash-lite", prompt=1000, completion=100, created=in_win)
    _exec(session, provider_key="mimo-v2.5-pro", model="mimo-v2.5-pro",
          prompt=500, completion=50, created=in_win)
    _exec(session, provider_key="inception-mercury2", model="mercury-2",
          prompt=-5, completion=10, created=in_win)  # аномалия
    # вне окна (прошлый месяц) — не должно попасть
    _exec(session, provider_key="inception-mercury2", model="mercury-2",
          prompt=999, completion=999, created=datetime(2026, 5, 10, 10, 0, tzinfo=UTC))
    session.commit()

    rep = get_spend_by_model(session, "month", ANCHOR)
    by = {(r.provider_key, r.model): r for r in rep.rows}

    # Mercury priced (аномальная строка исключена из токенов, но в окне)
    merc = by[("inception-mercury2", "mercury-2")]
    assert merc.priced and merc.est_usd is not None
    assert merc.est_usd < merc.upper_usd
    assert merc.prompt_tokens == 1_000_000  # отрицательная -5 НЕ вошла
    # Gemini priced
    gem = by[("openrouter-gemini-2.5-flash-lite", "google/gemini-2.5-flash-lite")]
    assert gem.priced and gem.est_usd == Decimal("0.00014")
    # MiMo unpriced → «—»
    mimo = by[("mimo-v2.5-pro", "mimo-v2.5-pro")]
    assert not mimo.priced and mimo.est_usd is None

    # Покрытие + аномалии
    assert rep.anomaly_count == 1
    assert ("mimo-v2.5-pro", "mimo-v2.5-pro") in rep.unpriced_models
    assert rep.priced_subtotal_usd == merc.est_usd + gem.est_usd
    assert rep.coverage_calls_pct is not None
    # прошлый месяц не попал
    assert all(r.prompt_tokens != 999 for r in rep.rows)


def test_budget_row_usd_and_coverage(session) -> None:
    from datetime import date, timedelta

    from sreda.admin.queries import get_budget_summary_for_day
    from sreda.db.models.billing import SubscriptionPlan, TenantSubscription

    session.add(SubscriptionPlan(
        id="plan_x", plan_key="hw_base", feature_key="housewife_assistant",
        title="HW", description="", price_rub=500, credits_monthly_quota=1_000_000,
    ))
    _now = datetime.now(UTC)
    session.add(TenantSubscription(
        id="sub_x", tenant_id="t1", plan_id="plan_x", feature_key="housewife_assistant",
        status="active", starts_at=_now - timedelta(days=10),
        active_until=_now + timedelta(days=10),
    ))
    in_win = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
    _exec(session, provider_key="inception-mercury2", model="mercury-2",
          prompt=40000, completion=600, created=in_win)          # priced
    _exec(session, provider_key="mimo-v2.5-pro", model="mimo-v2.5-pro",
          prompt=500, completion=50, created=in_win)             # unpriced
    session.commit()

    rows = get_budget_summary_for_day(session, date(2026, 6, 16))
    assert len(rows) == 1
    r = rows[0]
    assert r.est_usd is not None and r.est_usd > 0          # Mercury priced
    assert r.cost_coverage_pct == 50                        # 1 priced из 2 вызовов
