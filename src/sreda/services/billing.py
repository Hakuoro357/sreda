from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.billing import (
    PaymentOrder,
    PaymentOrderItem,
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.db.models.connect import TenantEDSAccount
from sreda.db.models.core import TenantFeature

STATUS_CALLBACK = "billing:status"
SUBSCRIPTIONS_CALLBACK = "billing:subscriptions"
RENEW_CALLBACK = "billing:renew"
CONNECT_BASE_CALLBACK = "billing:connect_plan:eds_monitor_base"
ADD_EDS_ACCOUNT_CALLBACK = "billing:add_eds_account"
REMOVE_EDS_ACCOUNT_CALLBACK = "billing:remove_eds_account"
CANCEL_BASE_CALLBACK = "billing:cancel_plan:eds_monitor_base"

PLAN_EDS_MONITOR_BASE = "eds_monitor_base"
PLAN_EDS_MONITOR_EXTRA = "eds_monitor_extra_account"


@dataclass(frozen=True, slots=True)
class PlanSeed:
    id: str
    plan_key: str
    feature_key: str
    title: str
    description: str
    price_rub: int
    billing_period_days: int = 30
    is_public: bool = True
    is_active: bool = True
    sort_order: int = 100


@dataclass(slots=True)
class BillingSummary:
    tenant_id: str
    next_payment_due_at: datetime | None
    next_amount_rub: int
    base_active: bool
    base_active_until: datetime | None
    base_cancel_at_period_end: bool
    extra_quantity: int
    extra_next_cycle_quantity: int
    extra_active_until: datetime | None
    allowed_count: int
    connected_count: int


@dataclass(slots=True)
class SubscriptionActionResult:
    message_text: str
    reply_markup: dict


PLAN_SEEDS: tuple[PlanSeed, ...] = (
    PlanSeed(
        id="plan_eds_monitor_base",
        plan_key=PLAN_EDS_MONITOR_BASE,
        feature_key="eds_monitor",
        title="EDS Monitor",
        description="Базовая подписка на мониторинг EDS. Включает 1 кабинет.",
        price_rub=990,
        sort_order=10,
    ),
    PlanSeed(
        id="plan_eds_monitor_extra_account",
        plan_key=PLAN_EDS_MONITOR_EXTRA,
        feature_key="eds_monitor",
        title="Доп. кабинет EDS",
        description="Дополнительный кабинет для EDS Monitor.",
        price_rub=290,
        sort_order=20,
    ),
)


class BillingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_default_plans(self) -> None:
        for seed in PLAN_SEEDS:
            plan = (
                self.session.query(SubscriptionPlan)
                .filter(SubscriptionPlan.plan_key == seed.plan_key)
                .one_or_none()
            )
            if plan is None:
                self.session.add(
                    SubscriptionPlan(
                        id=seed.id,
                        plan_key=seed.plan_key,
                        feature_key=seed.feature_key,
                        title=seed.title,
                        description=seed.description,
                        price_rub=seed.price_rub,
                        billing_period_days=seed.billing_period_days,
                        is_public=seed.is_public,
                        is_active=seed.is_active,
                        sort_order=seed.sort_order,
                    )
                )
                continue
            plan.title = seed.title
            plan.description = seed.description
            plan.price_rub = seed.price_rub
            plan.billing_period_days = seed.billing_period_days
            plan.is_public = seed.is_public
            plan.is_active = seed.is_active
            plan.sort_order = seed.sort_order
            plan.updated_at = _utcnow()
        self.session.flush()

    def build_help_message(self) -> tuple[str, dict]:
        text = (
            "Я Среда.\n\n"
            "Сейчас я умею:\n"
            "- показывать статус аккаунта и подписок;\n"
            "- подключать и продлевать подписки;\n"
            "- показывать последние события по EDS;\n"
            "- помогать с подключением EDS."
        )
        return text, _inline_keyboard(
            [
                [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                [{"text": "Последние события", "callback_data": "events:latest"}],
            ]
        )

    def get_summary(self, tenant_id: str) -> BillingSummary:
        self.ensure_default_plans()
        now = _utcnow()
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        cycle = self._get_cycle(tenant_id)

        base_active = bool(
            base_subscription
            and base_subscription.quantity > 0
            and base_subscription.active_until
            and _coerce_utc(base_subscription.active_until) > now
            and base_subscription.status in {"active", "scheduled_for_cancel"}
        )
        base_next_quantity = self._get_next_cycle_quantity(base_subscription)
        extra_quantity = extra_subscription.quantity if extra_subscription and extra_subscription.quantity > 0 else 0
        extra_next_quantity = self._get_next_cycle_quantity(extra_subscription)
        allowed_count = (1 if base_active else 0) + extra_quantity
        connected_count = (
            self.session.query(TenantEDSAccount)
            .filter(
                TenantEDSAccount.tenant_id == tenant_id,
                TenantEDSAccount.status.in_(["pending_verification", "active"]),
            )
            .count()
            if allowed_count > 0
            else 0
        )

        next_amount_rub = 0
        if cycle is not None:
            if base_next_quantity > 0:
                next_amount_rub += self._get_plan(PLAN_EDS_MONITOR_BASE).price_rub
            if extra_next_quantity > 0:
                next_amount_rub += self._get_plan(PLAN_EDS_MONITOR_EXTRA).price_rub * extra_next_quantity

        return BillingSummary(
            tenant_id=tenant_id,
            next_payment_due_at=cycle.next_payment_due_at if cycle else None,
            next_amount_rub=next_amount_rub,
            base_active=base_active,
            base_active_until=base_subscription.active_until if base_subscription else None,
            base_cancel_at_period_end=bool(base_subscription and base_subscription.cancel_at_period_end),
            extra_quantity=extra_quantity,
            extra_next_cycle_quantity=extra_next_quantity,
            extra_active_until=extra_subscription.active_until if extra_subscription else None,
            allowed_count=allowed_count,
            connected_count=connected_count,
        )

    def build_status_message(self, tenant_id: str) -> tuple[str, dict]:
        summary = self.get_summary(tenant_id)
        active_lines: list[str] = []
        if summary.base_active and summary.base_active_until:
            suffix = " (не продлевать)" if summary.base_cancel_at_period_end else ""
            active_lines.append(
                f"- EDS Monitor — активно до {_format_date(summary.base_active_until)}{suffix}"
            )
        if summary.extra_quantity > 0 and summary.extra_active_until:
            active_lines.append(
                f"- Доп. кабинеты EDS — {summary.extra_quantity} шт., активно до {_format_date(summary.extra_active_until)}"
            )
        if not active_lines:
            active_lines.append("- нет")

        due_text = _format_date(summary.next_payment_due_at) if summary.next_payment_due_at else "не назначен"
        text = (
            "Мой статус\n\n"
            f"Следующий платеж: {due_text}\n"
            f"Сумма к оплате: {summary.next_amount_rub} ₽\n\n"
            "Активные подписки:\n"
            f"{chr(10).join(active_lines)}\n\n"
            "EDS:\n"
            f"- {'подключен' if summary.connected_count > 0 else 'не подключен'}\n"
            f"- кабинетов подключено: {summary.connected_count} из {summary.allowed_count}"
        )

        buttons: list[list[dict]] = [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
        if summary.next_payment_due_at is not None and summary.next_amount_rub > 0:
            buttons.append([{"text": "Продлить", "callback_data": RENEW_CALLBACK}])
        if summary.base_active:
            buttons.append([{"text": "Подключить EDS", "callback_data": "onboarding:connect_eds"}])
        return text, _inline_keyboard(buttons)

    def build_subscriptions_message(self, tenant_id: str) -> tuple[str, dict]:
        summary = self.get_summary(tenant_id)
        base_plan = self._get_plan(PLAN_EDS_MONITOR_BASE)
        extra_plan = self._get_plan(PLAN_EDS_MONITOR_EXTRA)

        active_lines: list[str] = []
        if summary.base_active and summary.base_active_until:
            suffix = " (не продлевать)" if summary.base_cancel_at_period_end else ""
            active_lines.append(
                f"- {base_plan.title} — {base_plan.price_rub} ₽ / 30 дней, активно до {_format_date(summary.base_active_until)}{suffix}"
            )
        if summary.extra_quantity > 0 and summary.extra_active_until:
            active_lines.append(
                f"- {extra_plan.title} — {summary.extra_quantity} × {extra_plan.price_rub} ₽ / 30 дней, активно до {_format_date(summary.extra_active_until)}"
            )

        if active_lines:
            active_block = "Активные:\n" + "\n".join(active_lines)
        else:
            active_block = "Активных подписок пока нет."

        available_lines = []
        if not summary.base_active:
            available_lines.append(f"- {base_plan.title} — {base_plan.price_rub} ₽ / 30 дней")
        elif summary.base_active:
            available_lines.append(f"- {extra_plan.title} — {extra_plan.price_rub} ₽ / 30 дней")
        available_block = "Доступные:\n" + ("\n".join(available_lines) if available_lines else "- нет")

        text = f"Подписки\n\n{active_block}\n\n{available_block}"

        buttons: list[list[dict]] = []
        if not summary.base_active:
            buttons.append([{"text": "Подключить EDS Monitor", "callback_data": CONNECT_BASE_CALLBACK}])
        else:
            buttons.append([{"text": "Не продлевать EDS", "callback_data": CANCEL_BASE_CALLBACK}])
            buttons.append([{"text": "Добавить кабинет", "callback_data": ADD_EDS_ACCOUNT_CALLBACK}])
            if summary.extra_next_cycle_quantity > 0:
                buttons.append([{"text": "Убрать кабинет", "callback_data": REMOVE_EDS_ACCOUNT_CALLBACK}])
        buttons.append([{"text": "Мой статус", "callback_data": STATUS_CALLBACK}])
        return text, _inline_keyboard(buttons)

    def start_base_subscription(self, tenant_id: str, *, now: datetime | None = None) -> SubscriptionActionResult:
        self.ensure_default_plans()
        current_time = _utcnow(now)
        existing = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if self._is_subscription_active(existing, current_time):
            return SubscriptionActionResult(
                message_text=(
                    f"Подписка уже активна до {_format_date(existing.active_until)}."
                    if existing and existing.active_until
                    else "Подписка уже активна."
                ),
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                        [{"text": "Не продлевать", "callback_data": CANCEL_BASE_CALLBACK}],
                    ]
                ),
            )

        cycle = self._get_cycle(tenant_id)
        plan = self._get_plan(PLAN_EDS_MONITOR_BASE)
        if cycle is None or cycle.status == "expired" or cycle.next_payment_due_at <= current_time:
            cycle = TenantBillingCycle(
                id=f"cycle_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                billing_anchor_at=current_time,
                next_payment_due_at=current_time + timedelta(days=plan.billing_period_days),
                currency="RUB",
                status="active",
            )
            self.session.add(cycle)
            order_type = "initial_purchase"
            amount_rub = plan.price_rub
            period_start = current_time
            period_end = cycle.next_payment_due_at
            calculation_type = "full_cycle"
        else:
            order_type = "proration_purchase"
            amount_rub = _calculate_proration(plan.price_rub, cycle.next_payment_due_at, current_time)
            period_start = current_time
            period_end = cycle.next_payment_due_at
            calculation_type = "proration"

        order = self._create_paid_stub_order(
            tenant_id=tenant_id,
            cycle=cycle,
            order_type=order_type,
            amount_rub=amount_rub,
            description=f"Подключение {plan.title}",
        )
        self.session.add(
            PaymentOrderItem(
                id=f"poi_{uuid4().hex[:24]}",
                payment_order_id=order.id,
                plan_id=plan.id,
                amount_rub=amount_rub,
                quantity=1,
                period_start=period_start,
                period_end=period_end,
                calculation_type=calculation_type,
            )
        )

        if existing is None:
            existing = TenantSubscription(
                id=f"sub_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                plan_id=plan.id,
            )
            self.session.add(existing)

        existing.status = "active"
        existing.starts_at = current_time
        existing.active_until = cycle.next_payment_due_at
        existing.cancel_at_period_end = False
        existing.quantity = 1
        existing.next_cycle_quantity = 1
        existing.last_payment_order_id = order.id
        existing.updated_at = current_time

        self._ensure_feature_enabled(tenant_id, "eds_monitor", True)
        self.session.commit()

        summary = self.get_summary(tenant_id)
        return SubscriptionActionResult(
            message_text=(
                "Подписка EDS Monitor подключена.\n\n"
                f"Активно до: {_format_date(existing.active_until)}\n"
                f"Следующий платеж: {_format_date(summary.next_payment_due_at)}\n"
                f"Сумма следующего платежа: {summary.next_amount_rub} ₽"
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подключить EDS", "callback_data": "onboarding:connect_eds"}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                ]
            ),
        )

    def add_extra_eds_account(self, tenant_id: str, *, now: datetime | None = None) -> SubscriptionActionResult:
        self.ensure_default_plans()
        current_time = _utcnow(now)
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if not self._is_subscription_active(base_subscription, current_time):
            return SubscriptionActionResult(
                message_text=(
                    "Сначала подключи EDS Monitor, а потом можно будет добавить еще один кабинет."
                ),
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подключить", "callback_data": CONNECT_BASE_CALLBACK}],
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    ]
                ),
            )

        cycle = self._get_cycle(tenant_id)
        assert cycle is not None
        plan = self._get_plan(PLAN_EDS_MONITOR_EXTRA)
        amount_rub = _calculate_proration(plan.price_rub, cycle.next_payment_due_at, current_time)

        order = self._create_paid_stub_order(
            tenant_id=tenant_id,
            cycle=cycle,
            order_type="proration_purchase",
            amount_rub=amount_rub,
            description="Подключение дополнительного кабинета EDS",
        )
        self.session.add(
            PaymentOrderItem(
                id=f"poi_{uuid4().hex[:24]}",
                payment_order_id=order.id,
                plan_id=plan.id,
                amount_rub=amount_rub,
                quantity=1,
                period_start=current_time,
                period_end=cycle.next_payment_due_at,
                calculation_type="proration",
            )
        )

        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        if extra_subscription is None:
            next_quantity = 0
            extra_subscription = TenantSubscription(
                id=f"sub_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                plan_id=plan.id,
            )
            self.session.add(extra_subscription)
            current_quantity = 0
        else:
            current_quantity = extra_subscription.quantity
            next_quantity = self._get_next_cycle_quantity(extra_subscription)
        extra_subscription.status = "active"
        extra_subscription.starts_at = extra_subscription.starts_at or current_time
        extra_subscription.active_until = cycle.next_payment_due_at
        extra_subscription.cancel_at_period_end = False
        extra_subscription.quantity = current_quantity + 1
        extra_subscription.next_cycle_quantity = max(next_quantity, current_quantity) + 1
        extra_subscription.last_payment_order_id = order.id
        extra_subscription.updated_at = current_time

        self.session.commit()
        summary = self.get_summary(tenant_id)
        return SubscriptionActionResult(
            message_text=(
                "Дополнительный кабинет EDS подключен.\n\n"
                f"Теперь доступно кабинетов: {summary.allowed_count}\n"
                f"Следующий платеж: {_format_date(summary.next_payment_due_at)}\n"
                f"Сумма следующего платежа: {summary.next_amount_rub} ₽"
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подключить EDS", "callback_data": "onboarding:connect_eds"}],
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def renew_cycle(self, tenant_id: str, *, now: datetime | None = None) -> SubscriptionActionResult:
        self.ensure_default_plans()
        current_time = _utcnow(now)
        cycle = self._get_cycle(tenant_id)
        if cycle is None:
            return SubscriptionActionResult(
                message_text="Пока нечего продлевать.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )

        subscriptions = (
            self.session.query(TenantSubscription)
            .filter(TenantSubscription.tenant_id == tenant_id)
            .all()
        )
        renewable_items: list[tuple[TenantSubscription, SubscriptionPlan, int]] = []
        total_amount_rub = 0
        for subscription in subscriptions:
            plan = self.session.get(SubscriptionPlan, subscription.plan_id)
            if plan is None:
                continue
            next_quantity = self._get_next_cycle_quantity(subscription)
            if next_quantity <= 0:
                continue
            renewable_items.append((subscription, plan, next_quantity))
            total_amount_rub += plan.price_rub * next_quantity

        if not renewable_items:
            return SubscriptionActionResult(
                message_text="На следующий период нет подписок для продления.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )

        new_due_date = cycle.next_payment_due_at + timedelta(days=30)
        order = self._create_paid_stub_order(
            tenant_id=tenant_id,
            cycle=cycle,
            order_type="cycle_renewal",
            amount_rub=total_amount_rub,
            description="Продление подписок",
        )
        for subscription, plan, quantity in renewable_items:
            self.session.add(
                PaymentOrderItem(
                    id=f"poi_{uuid4().hex[:24]}",
                    payment_order_id=order.id,
                    plan_id=plan.id,
                    amount_rub=plan.price_rub * quantity,
                    quantity=quantity,
                    period_start=cycle.next_payment_due_at,
                    period_end=new_due_date,
                    calculation_type="full_cycle",
                )
            )
            subscription.status = "active"
            subscription.active_until = new_due_date
            subscription.quantity = quantity
            subscription.next_cycle_quantity = quantity
            subscription.cancel_at_period_end = False
            subscription.last_payment_order_id = order.id
            subscription.updated_at = current_time

        for subscription in subscriptions:
            if subscription in [item[0] for item in renewable_items]:
                continue
            subscription.status = "expired"
            subscription.quantity = 0
            subscription.next_cycle_quantity = 0
            subscription.updated_at = current_time

        cycle.billing_anchor_at = cycle.next_payment_due_at
        cycle.next_payment_due_at = new_due_date
        cycle.status = "active"
        cycle.updated_at = current_time

        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        self._ensure_feature_enabled(
            tenant_id,
            "eds_monitor",
            bool(base_subscription and base_subscription.quantity > 0 and base_subscription.status == "active"),
        )
        self.session.commit()

        summary = self.get_summary(tenant_id)
        return SubscriptionActionResult(
            message_text=(
                "Подписка продлена.\n\n"
                f"Следующий платеж: {_format_date(summary.next_payment_due_at)}\n"
                f"Сумма следующего платежа: {summary.next_amount_rub} ₽"
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                ]
            ),
        )

    def cancel_base_at_period_end(self, tenant_id: str) -> SubscriptionActionResult:
        subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if subscription is None or subscription.quantity <= 0 or subscription.active_until is None:
            return SubscriptionActionResult(
                message_text="Подписка EDS Monitor сейчас не активна.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )
        subscription.cancel_at_period_end = True
        subscription.next_cycle_quantity = 0
        subscription.status = "scheduled_for_cancel"
        subscription.updated_at = _utcnow()
        self.session.commit()
        return SubscriptionActionResult(
            message_text=(
                "Подписка не будет продлеваться дальше.\n\n"
                f"Подписка будет активна до: {_format_date(subscription.active_until)}\n"
                "После этой даты она завершится, если ее не продлить вручную."
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def remove_extra_account_at_period_end(self, tenant_id: str) -> SubscriptionActionResult:
        subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        if subscription is None or subscription.quantity <= 0 or subscription.active_until is None:
            return SubscriptionActionResult(
                message_text="Сейчас нет дополнительных кабинетов, которые можно убрать.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        next_quantity = max(self._get_next_cycle_quantity(subscription) - 1, 0)
        subscription.next_cycle_quantity = next_quantity
        subscription.status = "scheduled_for_cancel" if next_quantity == 0 else "active"
        subscription.updated_at = _utcnow()
        self.session.commit()

        next_allowed_count = 0
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if base_subscription and self._get_next_cycle_quantity(base_subscription) > 0:
            next_allowed_count += 1
        next_allowed_count += next_quantity

        return SubscriptionActionResult(
            message_text=(
                "Дополнительный кабинет не будет продлеваться в следующий период.\n\n"
                f"До {_format_date(subscription.active_until)} он останется активным.\n"
                f"После этой даты число доступных кабинетов уменьшится до {next_allowed_count}."
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def _get_cycle(self, tenant_id: str) -> TenantBillingCycle | None:
        return (
            self.session.query(TenantBillingCycle)
            .filter(TenantBillingCycle.tenant_id == tenant_id)
            .order_by(TenantBillingCycle.created_at.desc())
            .first()
        )

    def _get_subscription(self, tenant_id: str, plan_key: str) -> TenantSubscription | None:
        plan = self._get_plan(plan_key)
        return (
            self.session.query(TenantSubscription)
            .filter(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.plan_id == plan.id,
            )
            .one_or_none()
        )

    def _get_plan(self, plan_key: str) -> SubscriptionPlan:
        plan = (
            self.session.query(SubscriptionPlan)
            .filter(SubscriptionPlan.plan_key == plan_key)
            .one()
        )
        return plan

    def _create_paid_stub_order(
        self,
        *,
        tenant_id: str,
        cycle: TenantBillingCycle | None,
        order_type: str,
        amount_rub: int,
        description: str,
    ) -> PaymentOrder:
        now = _utcnow()
        order = PaymentOrder(
            id=f"po_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            billing_cycle_id=cycle.id if cycle else None,
            provider_key="stub",
            order_type=order_type,
            status="paid",
            amount_rub=amount_rub,
            description=description,
            provider_payload_json='{"provider":"stub","mode":"auto_paid"}',
            paid_at=now,
        )
        self.session.add(order)
        return order

    def _ensure_feature_enabled(self, tenant_id: str, feature_key: str, enabled: bool) -> None:
        feature = (
            self.session.query(TenantFeature)
            .filter(
                TenantFeature.tenant_id == tenant_id,
                TenantFeature.feature_key == feature_key,
            )
            .one_or_none()
        )
        if feature is None:
            feature = TenantFeature(
                id=f"{tenant_id}:{feature_key}",
                tenant_id=tenant_id,
                feature_key=feature_key,
                enabled=enabled,
            )
            self.session.add(feature)
            return
        feature.enabled = enabled

    @staticmethod
    def _is_subscription_active(subscription: TenantSubscription | None, now: datetime) -> bool:
        return bool(
            subscription
            and subscription.quantity > 0
            and subscription.active_until
            and _coerce_utc(subscription.active_until) > now
            and subscription.status in {"active", "scheduled_for_cancel"}
        )

    @staticmethod
    def _get_next_cycle_quantity(subscription: TenantSubscription | None) -> int:
        if subscription is None:
            return 0
        if subscription.next_cycle_quantity is not None:
            return max(subscription.next_cycle_quantity, 0)
        return max(subscription.quantity, 0)


def _inline_keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def _utcnow(value: datetime | None = None) -> datetime:
    if value is not None:
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _format_date(value: datetime | None) -> str:
    if value is None:
        return "не назначен"
    return _coerce_utc(value).strftime("%d.%m.%Y")


def _calculate_proration(price_rub: int, next_payment_due_at: datetime, now: datetime) -> int:
    due = _utcnow(next_payment_due_at)
    current = _utcnow(now)
    if due <= current:
        return 0
    remaining_days = max(1, ceil((due - current).total_seconds() / 86400))
    return round(price_rub * remaining_days / 30)


def _coerce_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
