from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.billing import PaymentOrder, PaymentOrderItem, TenantBillingCycle, TenantSubscription
from sreda.db.models.core import Assistant, Tenant, TenantFeature, User, Workspace
from sreda.services.billing import BillingService, PLAN_EDS_MONITOR_BASE, PLAN_EDS_MONITOR_EXTRA


def test_start_base_subscription_creates_cycle_subscription_and_payment_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    result = service.start_base_subscription(
        "tenant_1",
        now=datetime(2026, 3, 25, 12, 0, tzinfo=UTC),
    )

    cycle = session.query(TenantBillingCycle).one()
    subscription = session.query(TenantSubscription).one()
    order = session.query(PaymentOrder).one()
    item = session.query(PaymentOrderItem).one()
    feature = (
        session.query(TenantFeature)
        .filter(TenantFeature.tenant_id == "tenant_1", TenantFeature.feature_key == "eds_monitor")
        .one()
    )

    assert "Подписка EDS Monitor подключена." in result.message_text
    assert cycle.status == "active"
    assert subscription.status == "active"
    assert subscription.quantity == 1
    assert subscription.next_cycle_quantity == 1
    assert order.provider_key == "stub"
    assert order.order_type == "initial_purchase"
    assert item.quantity == 1
    assert feature.enabled is True


def test_add_extra_eds_account_increases_quantity_and_next_amount() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 25, 12, 0, tzinfo=UTC))
    result = service.add_extra_eds_account(
        "tenant_1",
        now=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
    )

    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    summary = service.get_summary("tenant_1")

    assert "Дополнительный кабинет EDS подключен." in result.message_text
    assert extra is not None
    assert extra.quantity == 1
    assert extra.next_cycle_quantity == 1
    assert summary.allowed_count == 2
    assert summary.next_amount_rub == 1280


def test_renew_cycle_respects_scheduled_cancel_for_extra_account() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    service.remove_extra_account_at_period_end("tenant_1")

    result = service.renew_cycle("tenant_1", now=datetime(2026, 3, 10, 12, 0, tzinfo=UTC))

    base = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_BASE)
    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    summary = service.get_summary("tenant_1")

    assert "Подписка продлена." in result.message_text
    assert base is not None and base.quantity == 1 and base.status == "active"
    assert extra is not None and extra.quantity == 0 and extra.status == "expired"
    assert summary.next_amount_rub == 990


def _seed_tenant_bundle(session) -> None:
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000001"))
    session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Среда"))
    session.add(TenantFeature(id="tenant_1:core_assistant", tenant_id="tenant_1", feature_key="core_assistant", enabled=True))
    session.commit()
