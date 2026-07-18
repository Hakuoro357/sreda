"""Регрессионные тесты фиксов аудита 2026-07-18 (slug: billing).

Покрывает находки svc-billing (#1 гейт-окно, #2 starts_at при продлении,
#4 clamp продления к now, #5 async try_consume, #6 MSK-день free_tier,
#8 pricing-кэш, #7 provider_balances single-flight/метка) и planner-exec
#4 (симметричная naive/aware нормализация в planner.billing.expire).

Без сети и без Postgres: SQLite in-memory + monkeypatch.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sreda.db.models  # noqa: F401 — регистрирует все таблицы в metadata
import sreda.db.models.planner  # noqa: F401
from sreda.db.base import Base
from sreda.db.models.billing import (
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.db.models.core import Tenant, User
from sreda.services.billing import BillingService
from sreda.services.entitlement_gate import EntitlementGate
from sreda.services.free_tier import FREE_TIER_DAILY_LIMIT, FreeTierCounter
from sreda.services.usage_ledger import UsageLedgerService, msk_period_keys


def _aware(dt: datetime) -> datetime:
    """SQLite возвращает naive — приводим к aware UTC для сравнений."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
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


def _seed_plan(
    session,
    *,
    plan_key: str,
    feature_key: str,
    price_rub: int = 0,
    billing_period_days: int = 30,
) -> SubscriptionPlan:
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=plan_key,
        feature_key=feature_key,
        title=plan_key,
        description="",
        price_rub=price_rub,
        billing_period_days=billing_period_days,
    )
    session.add(plan)
    session.flush()
    return plan


def _seed_sub(
    session,
    plan: SubscriptionPlan,
    *,
    status: str = "active",
    starts_at: datetime | None = None,
    active_until: datetime | None = None,
    quantity: int = 1,
    next_cycle_quantity: int | None = 1,
) -> TenantSubscription:
    sub = TenantSubscription(
        id=f"sub_{uuid4().hex[:16]}",
        tenant_id="t1",
        plan_id=plan.id,
        feature_key=plan.feature_key,
        status=status,
        starts_at=starts_at,
        active_until=active_until,
        quantity=quantity,
        next_cycle_quantity=next_cycle_quantity,
    )
    session.add(sub)
    session.flush()
    return sub


# ---------------------------------------------------------------------------
# svc-billing #1: entitlement_gate — окно active_until + quantity
# ---------------------------------------------------------------------------


class TestEntitlementGateWindow:
    FEATURE = "housewife_assistant"

    def _gate(self, session) -> EntitlementGate:
        _seed_plan(session, plan_key="sreda_free", feature_key=self.FEATURE)
        return EntitlementGate(session)

    def test_perpetual_sub_allowed(self, session) -> None:
        gate = self._gate(session)
        plan = session.query(SubscriptionPlan).one()
        _seed_sub(session, plan, active_until=None)  # auto-grant контракт
        result = gate.check("t1")
        assert result.allowed is True
        assert result.plan_key == "sreda_free"

    def test_future_active_until_allowed(self, session) -> None:
        gate = self._gate(session)
        plan = session.query(SubscriptionPlan).one()
        _seed_sub(
            session, plan,
            active_until=datetime.now(timezone.utc) + timedelta(days=5),
        )
        assert gate.check("t1").allowed is True

    def test_expired_active_until_blocked(self, session) -> None:
        """Находка #1: status='active' + истёкший active_until = обход оплаты."""
        gate = self._gate(session)
        plan = session.query(SubscriptionPlan).one()
        _seed_sub(
            session, plan,
            active_until=datetime.now(timezone.utc) - timedelta(days=1),
        )
        result = gate.check("t1")
        assert result.allowed is False
        assert result.reason == "no_active_subscription"

    def test_zero_quantity_blocked(self, session) -> None:
        gate = self._gate(session)
        plan = session.query(SubscriptionPlan).one()
        _seed_sub(session, plan, active_until=None, quantity=0)
        assert gate.check("t1").allowed is False


# ---------------------------------------------------------------------------
# svc-billing #2/#4: renew_cycle — clamp к now, billing_period_days, starts_at
# ---------------------------------------------------------------------------


class TestRenewCycleAnchor:
    def test_overdue_renewal_clamps_to_now_and_moves_window(self, session) -> None:
        """Находка #4: просрочка >периода — active_until НЕ в прошлом.
        Находка #2: starts_at двигается вместе с окном квоты."""
        now = datetime.now(timezone.utc).replace(microsecond=0)
        plan = _seed_plan(
            session, plan_key="paid_skill", feature_key="paid_feature",
            price_rub=100, billing_period_days=45,
        )
        sub = _seed_sub(
            session, plan,
            starts_at=now - timedelta(days=80),
            active_until=now - timedelta(days=40),
        )
        cycle = TenantBillingCycle(
            id=f"cyc_{uuid4().hex[:16]}",
            tenant_id="t1",
            billing_anchor_at=now - timedelta(days=80),
            next_payment_due_at=now - timedelta(days=40),  # просрочен
            status="active",
        )
        session.add(cycle)
        session.commit()

        result = BillingService(session).renew_cycle("t1", now=now)

        assert result.message_text.startswith("Подписка продлена.")
        session.refresh(sub)
        session.refresh(cycle)
        # Clamp: окно стартует от now, не от просроченного due.
        assert _aware(sub.starts_at) == now
        # Период из плана (45 дней), не захардкоженные 30.
        assert _aware(sub.active_until) == now + timedelta(days=45)
        assert _aware(cycle.next_payment_due_at) == now + timedelta(days=45)
        assert sub.status == "active"


# ---------------------------------------------------------------------------
# svc-billing #3: свипер истёкших подписок
# ---------------------------------------------------------------------------


class TestSweepExpiredSubscriptions:
    def test_sweep_flips_only_expired(self, session) -> None:
        now = datetime.now(timezone.utc)
        plan_a = _seed_plan(session, plan_key="pa", feature_key="fa")
        plan_b = _seed_plan(session, plan_key="pb", feature_key="fb")
        plan_c = _seed_plan(session, plan_key="pc", feature_key="fc")
        expired = _seed_sub(
            session, plan_a, active_until=now - timedelta(hours=1))
        perpetual = _seed_sub(session, plan_b, active_until=None)
        future = _seed_sub(
            session, plan_c, active_until=now + timedelta(days=3))
        session.commit()

        flipped = BillingService(session).sweep_expired_subscriptions(now=now)

        assert flipped == 1
        session.refresh(expired)
        session.refresh(perpetual)
        session.refresh(future)
        assert expired.status == "expired"
        assert expired.quantity == 0
        assert expired.next_cycle_quantity == 0
        assert perpetual.status == "active"
        assert future.status == "active"


# ---------------------------------------------------------------------------
# svc-billing #9: start_simple_subscription — второй план той же фичи
# ---------------------------------------------------------------------------


class TestStartSimpleSubscriptionConflict:
    def test_other_active_plan_same_feature_friendly_answer(self, session) -> None:
        now = datetime.now(timezone.utc)
        plan_a = _seed_plan(session, plan_key="skill_a", feature_key="skill_f")
        _seed_plan(session, plan_key="skill_b", feature_key="skill_f")
        _seed_sub(session, plan_a, active_until=now + timedelta(days=10))
        session.commit()

        result = BillingService(session).start_simple_subscription(
            "t1", "skill_b", now=now)

        assert "другой тариф" in result.message_text
        # Второй active-строки не появилось — unique index не задет.
        actives = (
            session.query(TenantSubscription)
            .filter_by(tenant_id="t1", status="active")
            .all()
        )
        assert len(actives) == 1


# ---------------------------------------------------------------------------
# svc-billing #5: usage_ledger.try_consume_async — без блокировки event loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def ledger(session) -> UsageLedgerService:
    return UsageLedgerService(session.get_bind())


class TestTryConsumeAsync:
    PERIODS = [("daily", "2099-01-01", 10), ("monthly", "2099-01", 100)]

    @pytest.mark.asyncio
    async def test_consume_and_exhaust(self, ledger) -> None:
        ok = await ledger.try_consume_async("t1", "llm_turns", 1, self.PERIODS)
        assert ok is True
        # Исчерпываем дневную квоту.
        for _ in range(9):
            assert await ledger.try_consume_async(
                "t1", "llm_turns", 1, self.PERIODS) is True
        assert await ledger.try_consume_async(
            "t1", "llm_turns", 1, self.PERIODS) is False

    @pytest.mark.asyncio
    async def test_serialization_conflict_retries_with_asyncio_sleep(
        self, ledger, monkeypatch
    ) -> None:
        """Backoff — asyncio.sleep, не time.sleep: патчим asyncio.sleep и
        проверяем, что ретрай прошёл через него."""
        import sreda.services.usage_ledger as ul

        class _FakePgError(Exception):
            sqlstate = "40001"

        attempts = {"n": 0}
        real_attempt = ledger._consume_attempt

        def flaky(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise DBAPIError("INSERT ...", {}, _FakePgError())
            return real_attempt(*args, **kwargs)

        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(ledger, "_consume_attempt", flaky)
        monkeypatch.setattr(ul.asyncio, "sleep", fake_sleep)

        ok = await ledger.try_consume_async("t1", "llm_turns", 1, self.PERIODS)

        assert ok is True
        assert attempts["n"] == 2
        assert sleeps == [0.05]  # backoff первой попытки


# ---------------------------------------------------------------------------
# svc-billing #6: free_tier — MSK-день + refund
# ---------------------------------------------------------------------------


class TestFreeTierMskDay:
    def test_day_matches_msk_period_keys(self, session) -> None:
        counter = FreeTierCounter(session)
        count, over = counter.increment_and_check(tenant_id="t1", user_id="u1")
        assert (count, over) == (1, False)
        # Запись легла на МОСКОВСКУЮ дату, не UTC.
        from sreda.db.models.free_tier import FreeTierUsage

        row = session.query(FreeTierUsage).one()
        msk_day = date.fromisoformat(msk_period_keys()[0])
        assert row.day == msk_day

    def test_refund_undoes_increment(self, session) -> None:
        counter = FreeTierCounter(session)
        for _ in range(3):
            counter.increment_and_check(tenant_id="t1", user_id="u1")
        counter.refund(tenant_id="t1", user_id="u1")
        assert counter.usage_today(tenant_id="t1", user_id="u1") == 2
        # Полный refund до нуля — дальше floor at 0, не уходит в минус.
        counter.refund(tenant_id="t1", user_id="u1")
        counter.refund(tenant_id="t1", user_id="u1")
        counter.refund(tenant_id="t1", user_id="u1")
        assert counter.usage_today(tenant_id="t1", user_id="u1") == 0

    def test_limit_boundary(self, session) -> None:
        counter = FreeTierCounter(session)
        over = False
        for _ in range(FREE_TIER_DAILY_LIMIT):
            _, over = counter.increment_and_check(tenant_id="t1", user_id="u1")
        assert over is False
        _, over = counter.increment_and_check(tenant_id="t1", user_id="u1")
        assert over is True


# ---------------------------------------------------------------------------
# svc-billing #8: pricing — исключение не кэшируется, 0 ₽ сохраняется
# ---------------------------------------------------------------------------


class TestPricingCache:
    @pytest.fixture(autouse=True)
    def _clear_pricing_cache(self):
        from sreda.services import pricing

        pricing.invalidate_cache()
        yield
        pricing.invalidate_cache()

    def test_zero_price_preserved(self, session) -> None:
        from sreda.services.pricing import get_monthly_price_rub

        _seed_plan(session, plan_key="free_p", feature_key="feat_zero",
                   price_rub=0)
        session.commit()
        assert get_monthly_price_rub(session, feature_key="feat_zero") == 0

    def test_exception_not_cached(self, session, monkeypatch) -> None:
        from sreda.services import pricing

        _seed_plan(session, plan_key="paid_p", feature_key="feat_x",
                   price_rub=990)
        session.commit()

        real_query = session.query
        monkeypatch.setattr(
            session, "query",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        assert pricing.get_monthly_price_rub(
            session, feature_key="feat_x") is None
        # Кэш НЕ отравлен: после восстановления БД цена видна сразу.
        monkeypatch.setattr(session, "query", real_query)
        assert pricing.get_monthly_price_rub(
            session, feature_key="feat_x") == 990


# ---------------------------------------------------------------------------
# svc-billing #7: provider_balances — метка кэша после фетчей + single-flight
# ---------------------------------------------------------------------------


class TestProviderBalancesCache:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        from sreda.services import provider_balances as pb

        pb.invalidate_cache()
        self.pb = pb
        self.calls = {"n": 0}

        def fake_fetch(settings):
            self.calls["n"] += 1
            time.sleep(0.3)
            return pb.ProviderBalance(
                key="k", label="l", status="ok", headline="h")

        for name in ("_fetch_openrouter", "_fetch_inception", "_fetch_mimo",
                     "_fetch_groq", "_fetch_yandex"):
            monkeypatch.setattr(pb, name, fake_fetch)
        yield
        pb.invalidate_cache()

    def test_stamp_after_fetch_keeps_entry_fresh(self) -> None:
        """Фетчи заняли ~1.5с; метка ставится ПОСЛЕ них, поэтому немедленный
        второй вызов обязан попасть в кэш (раньше запись ложилась протухшей
        на длительность фетчей)."""
        first = self.pb.fetch_balances(object())
        n_after_first = self.calls["n"]
        second = self.pb.fetch_balances(object())
        assert second is first
        assert self.calls["n"] == n_after_first

    def test_single_flight_dedupes_parallel_refresh(self) -> None:
        results: list = []

        def worker():
            results.append(self.pb.fetch_balances(object()))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        time.sleep(0.05)  # t1 гарантированно захватывает _fetch_lock первым
        t2.start()
        t1.join()
        t2.join()

        assert len(results) == 2
        # Каждый фетч вызван ровно один раз — параллельный refresh не
        # продублировал сетевые пробы.
        assert self.calls["n"] == 5


# ---------------------------------------------------------------------------
# planner-exec #4: expire() — симметричная naive/aware нормализация
# ---------------------------------------------------------------------------


class TestPlannerExpireTzNormalization:
    def _stub_row(self, *, expires_at: datetime) -> SimpleNamespace:
        return SimpleNamespace(
            state="reserved", expires_at=expires_at, updated_at=None)

    def _run_expire(self, monkeypatch, row, now) -> None:
        import sreda.runtime.planner.billing as pb

        monkeypatch.setattr(pb, "_get_reservation_for_update", lambda s, i: row)
        fake_session = SimpleNamespace(flush=lambda: None)
        pb.expire(fake_session, llm_call_id="lc_x", now=now)

    def test_aware_expires_at_vs_naive_now_no_typeerror(self, monkeypatch) -> None:
        """Находка #4: раньше aware expires_at + naive now → TypeError."""
        row = self._stub_row(
            expires_at=datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc))
        # naive now ПОЗЖЕ expires_at → expire обязан пройти.
        self._run_expire(monkeypatch, row, now=datetime(2026, 6, 1, 14, 0))
        assert row.state == "expired"

    def test_aware_expires_at_vs_naive_now_not_lapsed(self, monkeypatch) -> None:
        row = self._stub_row(
            expires_at=datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="TTL has not lapsed"):
            self._run_expire(monkeypatch, row, now=datetime(2026, 6, 1, 12, 0))

    def test_naive_expires_at_vs_aware_now_still_works(self, monkeypatch) -> None:
        """Исходная ветка (SQLite roundtrip) не сломана."""
        row = self._stub_row(expires_at=datetime(2026, 6, 1, 13, 0))
        self._run_expire(
            monkeypatch, row,
            now=datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc))
        assert row.state == "expired"
