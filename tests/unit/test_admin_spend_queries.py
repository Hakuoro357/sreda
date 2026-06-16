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

from sreda.admin.queries import SpendReport, get_spend_by_model, period_window_utc
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


def test_get_users_page_paginates(session) -> None:
    from sreda.admin.queries import get_users_page
    from sreda.db.models.core import User

    for i in range(3):
        session.add(Tenant(id=f"tt{i}", name=f"T{i}"))
        session.add(User(id=f"u{i}", tenant_id=f"tt{i}", telegram_account_id=str(100 + i)))
    session.commit()

    p1 = get_users_page(session, page=1, per_page=2)
    assert p1.per_page == 2 and p1.total == 3 and p1.total_pages == 2
    assert len(p1.rows) == 2
    assert len(get_users_page(session, page=2, per_page=2).rows) == 1


def test_tenant_spend_detail_filters_tenant(session) -> None:
    from sreda.admin.queries import get_tenant_spend_detail

    in_win = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
    _exec(session, provider_key="inception-mercury2", model="mercury-2",
          prompt=1000, completion=10, created=in_win)              # t1
    session.add(Tenant(id="t2", name="T2"))
    session.add(SkillAIExecution(
        id="skai_t2", run_id="r_t2", attempt_id=None, tenant_id="t2",
        feature_key="housewife_assistant", task_type="llm_call",
        provider_key="inception-mercury2", model="mercury-2", ai_schema_version=1,
        status="succeeded", prompt_tokens=5000, completion_tokens=99,
        total_tokens=5099, credits_consumed=0,
        created_at=in_win, started_at=in_win, finished_at=in_win))
    session.commit()

    detail = get_tenant_spend_detail(session, "t1", anchor=ANCHOR)
    assert detail.tenant_id == "t1"
    rows = detail.month.rows
    assert any(r.prompt_tokens == 1000 for r in rows)       # t1 есть
    assert all(r.prompt_tokens != 5000 for r in rows)       # t2 НЕ протёк


def test_tenant_detail_template_renders() -> None:
    from sreda.admin.queries import TenantSpendDetail
    from sreda.admin.routes import templates

    empty = SpendReport(
        period="day", start_utc=ANCHOR, end_utc=ANCHOR, rows=[],
        priced_subtotal_usd=Decimal("0"), upper_subtotal_usd=Decimal("0"),
        unpriced_models=[], unpriced_calls=0, unpriced_tokens=0,
        coverage_calls_pct=None, coverage_tokens_pct=None, anomaly_count=0)
    detail = TenantSpendDetail(
        tenant_id="t1", tenant_name="Tenant One", day=empty, week=empty, month=empty)
    html = templates.env.get_template("tenant_detail.html").render(
        token="t", section="users", detail=detail)
    assert "Tenant One" in html
    assert "тенанта/аккаунта" in html        # подпись «не пользователя»
    assert "llm-calls" in html


def test_cost_volume_summary(session) -> None:
    from sreda.admin.queries import get_cost_volume_summary

    cv = get_cost_volume_summary(session, anchor=ANCHOR)
    assert set(cv) == {"day", "week", "month"}
    assert all(isinstance(cv[p], SpendReport) for p in cv)


def test_top_tenants_by_spend(session) -> None:
    from sreda.admin.queries import get_top_tenants_by_spend

    in_win = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
    _exec(session, provider_key="inception-mercury2", model="mercury-2",
          prompt=2000, completion=50, created=in_win)             # t1 priced
    session.add(Tenant(id="t2", name="T2"))
    session.add(SkillAIExecution(
        id="skai_u", run_id="r_u", attempt_id=None, tenant_id="t2",
        feature_key="housewife_assistant", task_type="llm_call",
        provider_key="mimo-v2.5-pro", model="mimo-v2.5-pro", ai_schema_version=1,
        status="succeeded", prompt_tokens=9000, completion_tokens=100,
        total_tokens=9100, credits_consumed=0,
        created_at=in_win, started_at=in_win, finished_at=in_win))
    session.commit()

    top = get_top_tenants_by_spend(session, "month", anchor=ANCHOR)
    assert "t1" in [t[0] for t in top.by_spend]          # priced Mercury
    assert "t2" in [t[0] for t in top.by_unpriced]       # unpriced MiMo


def test_dialogue_health_returns_counts(session) -> None:
    from sreda.admin.queries import get_dialogue_health

    h = get_dialogue_health(session)
    assert h is None or h.turns_total == 0   # пустая БД → нули (или None)


def test_dashboard_template_renders() -> None:
    from sreda.admin.queries import TopTenants
    from sreda.admin.routes import templates
    from sreda.workers.reliability_report import DayCounts

    empty = SpendReport(
        period="day", start_utc=ANCHOR, end_utc=ANCHOR, rows=[],
        priced_subtotal_usd=Decimal("0"), upper_subtotal_usd=Decimal("0"),
        unpriced_models=[], unpriced_calls=0, unpriced_tokens=0,
        coverage_calls_pct=None, coverage_tokens_pct=None, anomaly_count=0)
    html = templates.env.get_template("dashboard.html").render(
        token="t", section="dashboard",
        cost={"day": empty, "week": empty, "month": empty},
        health=DayCounts(5, 1, 0, 0, 2),
        top=TopTenants(
            by_spend=[("t1", "Tenant1", Decimal("0.05"), 10)],
            by_unpriced=[("t2", "Tenant2", 9000)]),
        balances=[])
    assert "Дашборд" in html
    assert "Затраты и объём" in html
    assert "Здоровье диалога" in html
    assert "Tenant1" in html and "Tenant2" in html
    assert "всего проблем" in html
