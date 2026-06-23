"""#204 Фаза 2 — закрыть ВСЕ пути реактивации мёртвого плана voice_transcription_base.

Прод-состояние (миграция 0018): план ``voice_transcription_base`` помечен
``is_active=False`` (tombstone). Эта фаза деплоится ПЕРВОЙ (до Ф3-чистки данных),
чтобы реактивация была невозможна к моменту снятия 2 легаси-подписок.

Покрытие (falsifiable, не вакуумные — каждый тест проверяет ОТСУТСТВИЕ active
voice-подписки после попытки реактивации):

1. ``start_simple_subscription("voice_transcription_base")`` (is_active=False)
   → reject (DeprecatedPlanError), active voice-sub НЕ создан.
3. callback-граф ``execute_subscription_connect_voice`` → start_voice_subscription
   на deprecated → НЕ создаёт active voice-подписку.
4. ``build_subscriptions_message`` для inactive voice-плана → НЕТ connect-блока/кнопки.
5. cancelled voice-sub с ``next_cycle_quantity=1`` → renew_cycle НЕ продлевает.
6. inactive voice-план + active legacy voice-sub → ``cancel_voice_subscription``
   РЕАЛЬНО ставит cancelled/quantity=0 (cancel НЕ сломан tombstone-фильтром).
+ Регрессия: активный план (housewife sreda_free) подписка/продление НЕ сломаны.

Endpoint POST /subscribe → 400 ``deprecated_plan`` живёт в test_miniapp_api.py
(``test_subscribe_voice_transcription``, переписан с 200 на 400 — тест 2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.billing import (
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.db.models.core import Assistant, Tenant, User, Workspace
from sreda.services.billing import (
    PLAN_VOICE_TRANSCRIPTION,
    BillingService,
    DeprecatedPlanError,
)

TENANT = "tenant_204"
WORKSPACE = "ws_204"
ASSISTANT = "asst_204"
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _plan(
    *,
    id: str,
    plan_key: str,
    feature_key: str,
    title: str,
    price_rub: int,
    is_active: bool = True,
) -> SubscriptionPlan:
    return SubscriptionPlan(
        id=id,
        plan_key=plan_key,
        feature_key=feature_key,
        title=title,
        description=f"{title} plan",
        price_rub=price_rub,
        billing_period_days=30,
        is_public=True,
        is_active=is_active,
        sort_order=10,
    )


def _seed_identity(session) -> None:
    session.add(Tenant(id=TENANT, name="T204", approved_at=NOW - timedelta(days=30)))
    session.add(Workspace(id=WORKSPACE, tenant_id=TENANT, name="WS"))
    session.add(User(id="user_204", tenant_id=TENANT, telegram_account_id="40921122"))
    session.add(
        Assistant(id=ASSISTANT, tenant_id=TENANT, workspace_id=WORKSPACE, name="A")
    )


def _seed_dead_voice_plan(session) -> None:
    """Voice plan in production tombstone state: is_active=False."""
    session.add(
        _plan(
            id="plan_voice",
            plan_key=PLAN_VOICE_TRANSCRIPTION,
            feature_key="voice_transcription",
            title="Распознавание голоса",
            price_rub=0,
            is_active=False,
        )
    )


def _active_voice_count(session) -> int:
    """Active voice subscriptions for the tenant (the thing reactivation creates)."""
    return (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == TENANT,
            TenantSubscription.feature_key == "voice_transcription",
            TenantSubscription.status == "active",
        )
        .count()
    )


# ---------------------------------------------------------------------------
# Test 1 — start_simple_subscription rejects the dead plan
# ---------------------------------------------------------------------------


def test_start_simple_subscription_rejects_dead_voice_plan(session) -> None:
    """(1) start_simple_subscription on the is_active=False voice plan rejects
    (DeprecatedPlanError) and creates NO active voice subscription."""
    _seed_identity(session)
    _seed_dead_voice_plan(session)
    session.commit()

    with pytest.raises(DeprecatedPlanError):
        BillingService(session).start_simple_subscription(
            TENANT, PLAN_VOICE_TRANSCRIPTION, now=NOW
        )
    session.expire_all()
    assert _active_voice_count(session) == 0


def test_start_simple_subscription_allows_active_plan_regression(session) -> None:
    """Regression: an active plan (sreda_free housewife) still subscribes —
    the deny is scoped to is_active=False, not all plans."""
    _seed_identity(session)
    session.add(
        _plan(
            id="plan_free",
            plan_key="sreda_free",
            feature_key="housewife_assistant",
            title="Среда Free",
            price_rub=0,
            is_active=True,
        )
    )
    session.commit()

    result = BillingService(session).start_simple_subscription(
        TENANT, "sreda_free", now=NOW
    )
    session.expire_all()
    assert "подключено" in result.message_text.lower()
    active = (
        session.query(TenantSubscription)
        .filter(
            TenantSubscription.tenant_id == TENANT,
            TenantSubscription.feature_key == "housewife_assistant",
            TenantSubscription.status == "active",
        )
        .count()
    )
    assert active == 1


# ---------------------------------------------------------------------------
# Test 3 — callback graph (connect_voice handler) cannot reactivate
# ---------------------------------------------------------------------------


def test_connect_voice_callback_does_not_reactivate(session) -> None:
    """(3) The connect-voice callback handler routes to start_voice_subscription;
    on the dead plan it must NOT create an active voice subscription."""
    from sreda.runtime.dispatcher import ActionEnvelope
    from sreda.runtime.handlers import execute_subscription_connect_voice

    _seed_identity(session)
    _seed_dead_voice_plan(session)
    session.commit()

    envelope = ActionEnvelope(
        action_type="subscription.connect_voice",
        tenant_id=TENANT,
        workspace_id=WORKSPACE,
        assistant_id=ASSISTANT,
        user_id="user_204",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_callback",
        source_value="subscription.connect_voice",
        params={},
    )

    # Must not raise (callback path stays graceful — run lands COMPLETED, not
    # failed) AND must not create an active voice subscription.
    execute_subscription_connect_voice(session, envelope, {})
    session.expire_all()
    assert _active_voice_count(session) == 0


def test_start_voice_subscription_does_not_reactivate_dead_plan(session) -> None:
    """Direct service call: start_voice_subscription on the dead plan creates
    NO active voice subscription (the activation is stubbed)."""
    _seed_identity(session)
    _seed_dead_voice_plan(session)
    session.commit()

    BillingService(session).start_voice_subscription(TENANT, now=NOW)
    session.expire_all()
    assert _active_voice_count(session) == 0


# ---------------------------------------------------------------------------
# Test 4 — legacy subscriptions UI hides the connect button for dead plan
# ---------------------------------------------------------------------------


def test_build_subscriptions_message_hides_connect_for_dead_plan(session) -> None:
    """(4) build_subscriptions_message for an is_active=False voice plan shows
    NO connect button/block (no CONNECT_VOICE_CALLBACK in the markup)."""
    from sreda.services.billing import CONNECT_VOICE_CALLBACK

    _seed_identity(session)
    _seed_dead_voice_plan(session)
    session.commit()

    text, markup = BillingService(session).build_subscriptions_message(TENANT, now=NOW)

    callbacks = [
        btn.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for btn in row
    ]
    assert CONNECT_VOICE_CALLBACK not in callbacks
    assert "Подключить Распознавание голоса" not in text


def test_build_subscriptions_message_shows_connect_for_active_plan(session) -> None:
    """Regression: an active (is_active=True) voice plan still surfaces the
    connect button — the hide is scoped to the tombstone."""
    from sreda.services.billing import CONNECT_VOICE_CALLBACK

    _seed_identity(session)
    session.add(
        _plan(
            id="plan_voice",
            plan_key=PLAN_VOICE_TRANSCRIPTION,
            feature_key="voice_transcription",
            title="Распознавание голоса",
            price_rub=0,
            is_active=True,
        )
    )
    session.commit()

    _text, markup = BillingService(session).build_subscriptions_message(TENANT, now=NOW)
    callbacks = [
        btn.get("callback_data")
        for row in markup.get("inline_keyboard", [])
        for btn in row
    ]
    assert CONNECT_VOICE_CALLBACK in callbacks


# ---------------------------------------------------------------------------
# Test 5 — renew_cycle skips a deprecated plan (no reactivation via renewal)
# ---------------------------------------------------------------------------


def test_renew_cycle_skips_dead_voice_plan(session) -> None:
    """(5) A cancelled voice-sub with next_cycle_quantity=1 on the is_active=False
    plan is NOT renewed by renew_cycle (status stays non-active)."""
    _seed_identity(session)
    _seed_dead_voice_plan(session)
    # Legacy cancelled voice subscription whose next_cycle_quantity would
    # otherwise make renew_cycle flip it back to active.
    session.add(
        TenantSubscription(
            id="sub_voice_dead",
            tenant_id=TENANT,
            plan_id="plan_voice",
            feature_key="voice_transcription",
            status="cancelled",
            starts_at=NOW - timedelta(days=40),
            active_until=NOW - timedelta(days=1),
            cancel_at_period_end=True,
            quantity=0,
            next_cycle_quantity=1,
        )
    )
    session.add(
        TenantBillingCycle(
            id="cycle_204",
            tenant_id=TENANT,
            billing_anchor_at=NOW - timedelta(days=30),
            next_payment_due_at=NOW - timedelta(days=1),
            currency="RUB",
            status="active",
        )
    )
    session.commit()

    BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()

    sub = session.get(TenantSubscription, "sub_voice_dead")
    assert sub.status != "active"
    assert _active_voice_count(session) == 0


def test_renew_cycle_leaves_dead_voice_row_fully_untouched_alongside_live_sub(
    session,
) -> None:
    """(5b, c-010 pin) Tenant with BOTH a live renewable sub (active plan,
    next_cycle_quantity>0) AND a dead voice-sub (cancelled, plan is_active=False,
    next_cycle_quantity>0). After renew_cycle: the live sub renews as usual AND
    the dead voice-row is COMPLETELY untouched — status / quantity /
    next_cycle_quantity / active_until / updated_at all unchanged.

    This proves the dead row enters NEITHER the renewable partition NOR the
    implicit expire-loop (the c-010 lesson: the expire-loop only iterates
    ``subscriptions``, which the is_active filter already excludes the dead row
    from). Test 5 above only covers the renewable path for a tenant with no
    live sub; this adds the case where a live sub exists, so the expire-loop is
    actually exercised. NOT vacuous: reverting the ``and row[1].is_active`` skip
    in renew_cycle's partition lets the dead row fall into the expire-loop,
    which flips its status to ``expired`` and zeroes quantity/next_cycle_quantity
    and bumps updated_at → the untouched-asserts go red.
    """
    _seed_identity(session)
    _seed_dead_voice_plan(session)
    # (a) live renewable sub on an ACTIVE plan
    session.add(
        _plan(
            id="plan_voice_live",
            plan_key="voice_live",
            feature_key="voice_live",
            title="Голос (живой)",
            price_rub=299,
            is_active=True,
        )
    )
    session.add(
        TenantSubscription(
            id="sub_live",
            tenant_id=TENANT,
            plan_id="plan_voice_live",
            feature_key="voice_live",
            status="active",
            starts_at=NOW - timedelta(days=40),
            active_until=NOW - timedelta(days=1),
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    # (b) dead voice-sub on the tombstone plan, with a sentinel updated_at far
    # from `now` so any mutation by renew_cycle is detectable.
    dead_updated_at = NOW - timedelta(days=99)
    session.add(
        TenantSubscription(
            id="sub_voice_dead",
            tenant_id=TENANT,
            plan_id="plan_voice",
            feature_key="voice_transcription",
            status="cancelled",
            starts_at=NOW - timedelta(days=40),
            active_until=NOW - timedelta(days=1),
            cancel_at_period_end=True,
            quantity=0,
            next_cycle_quantity=1,
            updated_at=dead_updated_at,
        )
    )
    session.add(
        TenantBillingCycle(
            id="cycle_204c",
            tenant_id=TENANT,
            billing_anchor_at=NOW - timedelta(days=30),
            next_payment_due_at=NOW - timedelta(days=1),
            currency="RUB",
            status="active",
        )
    )
    session.commit()

    # Snapshot the dead row BEFORE renewal.
    dead_before = session.get(TenantSubscription, "sub_voice_dead")
    before_status = dead_before.status
    before_quantity = dead_before.quantity
    before_next = dead_before.next_cycle_quantity
    before_active_until = dead_before.active_until
    before_updated_at = dead_before.updated_at
    live_before_until = session.get(TenantSubscription, "sub_live").active_until

    BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()

    # Live sub renewed as usual.
    live_after = session.get(TenantSubscription, "sub_live")
    assert live_after.status == "active"
    assert live_after.active_until > live_before_until

    # Dead voice-row fully untouched — neither renewable nor expire-loop touched
    # it.
    dead_after = session.get(TenantSubscription, "sub_voice_dead")
    assert dead_after.status == "cancelled" == before_status
    assert dead_after.quantity == 0 == before_quantity
    assert dead_after.next_cycle_quantity == 1 == before_next
    assert dead_after.active_until == before_active_until
    # updated_at unchanged. SQLite drops tzinfo on read-back, so compare the
    # DB-read snapshot directly (both naive) and pin the value to the sentinel
    # (naive form) — renew_cycle must not have bumped it.
    assert dead_after.updated_at == before_updated_at
    assert dead_after.updated_at.replace(tzinfo=None) == dead_updated_at.replace(
        tzinfo=None
    )


def test_renew_cycle_leaves_dead_voice_row_untouched_in_expire_branch(
    session,
) -> None:
    """(5c, c-010 EXPIRE-loop pin) Realistic post-Ф3 dead voice-row: cancelled,
    next_cycle_quantity=0 — the EXPIRE-branch candidate (next_quantity<=0). A live
    renewable sub is present so renew_cycle actually reaches its expire-loop; the
    is_active filter must keep the dead row OUT of it so it stays ``cancelled``
    (NOT flipped to ``expired``). Complements 5b (which covers the renewable path,
    next_cycle_quantity>0). NOT vacuous: reverting ``and row[1].is_active`` lets the
    dead row (next_quantity<=0) fall into the expire branch → status ``expired`` +
    bumped updated_at → asserts go red.
    """
    _seed_identity(session)
    _seed_dead_voice_plan(session)
    # (a) live renewable sub on an ACTIVE plan — drives the loop past renewable
    # into the expire branch.
    session.add(
        _plan(
            id="plan_voice_live",
            plan_key="voice_live",
            feature_key="voice_live",
            title="Голос (живой)",
            price_rub=299,
            is_active=True,
        )
    )
    session.add(
        TenantSubscription(
            id="sub_live",
            tenant_id=TENANT,
            plan_id="plan_voice_live",
            feature_key="voice_live",
            status="active",
            starts_at=NOW - timedelta(days=40),
            active_until=NOW - timedelta(days=1),
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    # (b) dead voice-sub: cancelled, next_cycle_quantity=0 → EXPIRE-branch candidate
    # (this is the realistic state AFTER Ф3 cancels it).
    dead_updated_at = NOW - timedelta(days=99)
    session.add(
        TenantSubscription(
            id="sub_voice_dead",
            tenant_id=TENANT,
            plan_id="plan_voice",
            feature_key="voice_transcription",
            status="cancelled",
            starts_at=NOW - timedelta(days=40),
            active_until=NOW - timedelta(days=1),
            cancel_at_period_end=True,
            quantity=0,
            next_cycle_quantity=0,
            updated_at=dead_updated_at,
        )
    )
    session.add(
        TenantBillingCycle(
            id="cycle_204d",
            tenant_id=TENANT,
            billing_anchor_at=NOW - timedelta(days=30),
            next_payment_due_at=NOW - timedelta(days=1),
            currency="RUB",
            status="active",
        )
    )
    session.commit()

    before_updated_at = session.get(
        TenantSubscription, "sub_voice_dead"
    ).updated_at

    BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()

    # Dead voice-row NOT flipped to 'expired' by the expire-loop (kept out by the
    # is_active partition filter).
    dead_after = session.get(TenantSubscription, "sub_voice_dead")
    assert dead_after.status == "cancelled"
    assert dead_after.next_cycle_quantity == 0
    assert dead_after.updated_at == before_updated_at


def test_renew_cycle_renews_active_plan_regression(session) -> None:
    """Regression: renew_cycle still renews a subscription on an active plan
    (the is_active skip does not block live plans)."""
    _seed_identity(session)
    session.add(
        _plan(
            id="plan_voice_live",
            plan_key="voice_live",
            feature_key="voice_live",
            title="Голос (живой)",
            price_rub=299,
            is_active=True,
        )
    )
    session.add(
        TenantSubscription(
            id="sub_live",
            tenant_id=TENANT,
            plan_id="plan_voice_live",
            feature_key="voice_live",
            status="active",
            starts_at=NOW - timedelta(days=40),
            active_until=NOW - timedelta(days=1),
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    session.add(
        TenantBillingCycle(
            id="cycle_204b",
            tenant_id=TENANT,
            billing_anchor_at=NOW - timedelta(days=30),
            next_payment_due_at=NOW - timedelta(days=1),
            currency="RUB",
            status="active",
        )
    )
    session.commit()

    before = session.get(TenantSubscription, "sub_live").active_until
    BillingService(session).renew_cycle(TENANT, now=NOW)
    session.expire_all()
    after = session.get(TenantSubscription, "sub_live")
    assert after.status == "active"
    assert after.active_until > before


# ---------------------------------------------------------------------------
# Test 6 — cancel_voice_subscription stays functional on the tombstone plan
# ---------------------------------------------------------------------------


def test_cancel_voice_subscription_works_on_dead_plan(session) -> None:
    """(6) inactive voice plan + active legacy voice-sub → cancel_voice_subscription
    REALLY sets cancelled/quantity=0 (proves cancel is NOT broken by the
    tombstone filter — Ф3 mirrors this path)."""
    _seed_identity(session)
    _seed_dead_voice_plan(session)
    session.add(
        TenantSubscription(
            id="sub_voice_active",
            tenant_id=TENANT,
            plan_id="plan_voice",
            feature_key="voice_transcription",
            status="active",
            starts_at=NOW - timedelta(days=5),
            active_until=NOW + timedelta(days=36500),
            cancel_at_period_end=False,
            quantity=1,
            next_cycle_quantity=1,
        )
    )
    session.commit()

    result = BillingService(session).cancel_voice_subscription(TENANT)
    session.expire_all()

    sub = session.get(TenantSubscription, "sub_voice_active")
    assert sub.status == "cancelled"
    assert sub.quantity == 0
    assert sub.next_cycle_quantity == 0
    assert sub.cancel_at_period_end is True
    assert "отключено" in result.message_text.lower()
