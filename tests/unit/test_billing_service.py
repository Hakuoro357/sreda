from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.billing import PaymentOrder, PaymentOrderItem, TenantBillingCycle, TenantSubscription
from sreda.db.models.connect import TenantEDSAccount
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
    assert summary.next_amount_rub == 5980


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
    assert summary.next_amount_rub == 2990


def test_subscriptions_message_shows_resume_button_after_base_cancel_requested() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.cancel_base_at_period_end("tenant_1")

    _, reply_markup = service.build_subscriptions_message("tenant_1")

    button_texts = [button["text"] for row in reply_markup["inline_keyboard"] for button in row]
    assert "Продлевать EDS" in button_texts
    assert "Отменить подписку на EDS" not in button_texts


def test_resume_base_renewal_restores_next_cycle() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.cancel_base_at_period_end("tenant_1")

    result = service.resume_base_renewal("tenant_1")

    subscription = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_BASE)
    assert "Продление EDS Monitor снова включено." in result.message_text
    assert subscription is not None
    assert subscription.cancel_at_period_end is False
    assert subscription.next_cycle_quantity == 1
    assert subscription.status == "active"


def test_status_message_hides_connect_button_when_all_slots_are_occupied() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    session.add(
        TenantEDSAccount(
            id="teds_1",
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            assistant_id="assistant_1",
            account_index="1",
            account_role="primary",
            status="active",
            login_masked="5047***341",
        )
    )
    session.commit()

    _, reply_markup = service.build_status_message("tenant_1")

    button_texts = [button["text"] for row in reply_markup["inline_keyboard"] for button in row]
    assert "Подключить EDS" not in button_texts
    assert button_texts == ["Подписки"]


def test_status_message_contains_only_subscriptions_button() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))

    _, reply_markup = service.build_status_message("tenant_1")

    button_texts = [button["text"] for row in reply_markup["inline_keyboard"] for button in row]
    assert button_texts == ["Подписки"]


def test_status_message_shows_connected_accounts_and_free_slots() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    session.add(
        TenantEDSAccount(
            id="teds_status_1",
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            assistant_id="assistant_1",
            account_index="1",
            account_role="primary",
            status="active",
            login_masked="5047***341",
        )
    )
    session.commit()

    text, _ = service.build_status_message("tenant_1")

    assert "Кабинеты EDS:" in text
    assert "подключено: 1 из 2" in text
    assert "свободно для подключения: 1" in text
    assert "5047***341" in text
    assert "не подключен" not in text


def test_remove_extra_account_requests_explicit_choice_when_all_slots_are_occupied() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    session.add_all(
        [
            TenantEDSAccount(
                id="teds_primary",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="1",
                account_role="primary",
                status="active",
                login_masked="5047***341",
            ),
            TenantEDSAccount(
                id="teds_extra",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="2",
                account_role="extra",
                status="active",
                login_masked="7000***002",
            ),
        ]
    )
    session.commit()

    result = service.remove_extra_account_at_period_end("tenant_1")
    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    button_texts = [button["text"] for row in result.reply_markup["inline_keyboard"] for button in row]

    assert "Выбери, какой кабинет не продлевать" in result.message_text
    assert extra is not None and extra.next_cycle_quantity == 1
    assert "7000***002" in button_texts
    assert "Назад" in button_texts


def test_schedule_connected_extra_account_cancel_marks_specific_account() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    session.add_all(
        [
            TenantEDSAccount(
                id="teds_primary",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="1",
                account_role="primary",
                status="active",
                login_masked="5047***341",
            ),
            TenantEDSAccount(
                id="teds_extra",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="2",
                account_role="extra",
                status="active",
                login_masked="7000***002",
            ),
        ]
    )
    session.commit()

    result = service.schedule_connected_eds_account_cancel("tenant_1", "teds_extra")
    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    tenant_account = session.get(TenantEDSAccount, "teds_extra")

    assert "7000***002" in result.message_text
    assert extra is not None and extra.next_cycle_quantity == 0
    assert tenant_account is not None and tenant_account.status == "scheduled_for_disconnect"


def test_renew_cycle_expires_selected_extra_account_after_connected_cancel() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    session.add_all(
        [
            TenantEDSAccount(
                id="teds_primary",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="1",
                account_role="primary",
                status="active",
                login_masked="5047***341",
            ),
            TenantEDSAccount(
                id="teds_extra",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="2",
                account_role="extra",
                status="active",
                login_masked="7000***002",
            ),
        ]
    )
    session.commit()
    service.schedule_connected_eds_account_cancel("tenant_1", "teds_extra")

    service.renew_cycle("tenant_1", now=datetime(2026, 3, 10, 12, 0, tzinfo=UTC))

    tenant_account = session.get(TenantEDSAccount, "teds_extra")
    summary = service.get_summary("tenant_1")

    assert tenant_account is not None and tenant_account.status == "expired"
    assert summary.allowed_count == 1
    assert summary.connected_count == 1
    assert summary.free_count == 0


def test_restore_extra_account_slot_restores_next_cycle_quantity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    service.remove_extra_account_at_period_end("tenant_1")

    result = service.restore_extra_account_slot("tenant_1")
    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    summary = service.get_summary("tenant_1")

    assert "снова будет продлен" in result.message_text
    assert extra is not None and extra.next_cycle_quantity == 1
    assert summary.next_amount_rub == 5980


def test_restore_connected_extra_account_cancel_reactivates_selected_account() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    session.add_all(
        [
            TenantEDSAccount(
                id="teds_primary",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="1",
                account_role="primary",
                status="active",
                login_masked="5047***341",
            ),
            TenantEDSAccount(
                id="teds_extra",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                assistant_id="assistant_1",
                account_index="2",
                account_role="extra",
                status="active",
                login_masked="7000***002",
            ),
        ]
    )
    session.commit()
    service.schedule_connected_eds_account_cancel("tenant_1", "teds_extra")

    result = service.restore_connected_eds_account_cancel("tenant_1", "teds_extra")
    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    tenant_account = session.get(TenantEDSAccount, "teds_extra")
    summary = service.get_summary("tenant_1")

    assert "7000***002" in result.message_text
    assert extra is not None and extra.next_cycle_quantity == 1
    assert tenant_account is not None and tenant_account.status == "active"
    assert summary.next_amount_rub == 5980


def test_subscriptions_message_shows_restore_button_for_removed_empty_slot() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    service.remove_extra_account_at_period_end("tenant_1")

    _, reply_markup = service.build_subscriptions_message("tenant_1")
    button_texts = [button["text"] for row in reply_markup["inline_keyboard"] for button in row]

    assert "Вернуть кабинет" in button_texts


def test_subscriptions_message_uses_new_eds_labels_and_actions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))

    _, reply_markup = service.build_subscriptions_message("tenant_1")
    button_texts = [button["text"] for row in reply_markup["inline_keyboard"] for button in row]

    assert "Отменить подписку на EDS" in button_texts
    assert "Добавить подписку на EDS" in button_texts
    assert "Добавить кабинет" in button_texts


def test_subscriptions_message_hides_add_cabinet_when_no_free_paid_slots() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    session.add(
        TenantEDSAccount(
            id="teds_primary",
            tenant_id="tenant_1",
            workspace_id="workspace_1",
            assistant_id="assistant_1",
            account_index="1",
            account_role="primary",
            status="active",
            login_masked="5047***341",
        )
    )
    session.commit()

    _, reply_markup = service.build_subscriptions_message("tenant_1")
    button_texts = [button["text"] for row in reply_markup["inline_keyboard"] for button in row]

    assert "Добавить подписку на EDS" in button_texts
    assert "Добавить кабинет" not in button_texts


def test_summary_counts_base_amount_even_if_base_marked_not_to_renew() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    service.cancel_base_at_period_end("tenant_1")

    summary = service.get_summary("tenant_1")

    assert summary.next_amount_rub == 5980


def test_renew_cycle_renews_active_base_even_if_marked_not_to_renew() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _seed_tenant_bundle(session)
    service = BillingService(session)

    service.start_base_subscription("tenant_1", now=datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
    service.add_extra_eds_account("tenant_1", now=datetime(2026, 3, 2, 12, 0, tzinfo=UTC))
    service.cancel_base_at_period_end("tenant_1")

    result = service.renew_cycle("tenant_1", now=datetime(2026, 3, 10, 12, 0, tzinfo=UTC))

    base = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_BASE)
    extra = service._get_subscription("tenant_1", PLAN_EDS_MONITOR_EXTRA)
    summary = service.get_summary("tenant_1")

    assert "Подписка продлена." in result.message_text
    assert base is not None and base.quantity == 1 and base.status == "active"
    assert extra is not None and extra.quantity == 1 and extra.status == "active"
    assert summary.next_amount_rub == 5980


def _seed_tenant_bundle(session) -> None:
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000001"))
    session.add(Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Среда"))
    session.add(TenantFeature(id="tenant_1:core_assistant", tenant_id="tenant_1", feature_key="core_assistant", enabled=True))
    session.commit()
