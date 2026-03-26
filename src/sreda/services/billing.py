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
REMOVE_EDS_ACCOUNT_SELECT_PREFIX = "billing:remove_eds_account:select:"
RESTORE_EDS_ACCOUNT_CALLBACK = "billing:restore_eds_account"
RESTORE_EDS_ACCOUNT_SELECT_PREFIX = "billing:restore_eds_account:select:"
CANCEL_BASE_CALLBACK = "billing:cancel_plan:eds_monitor_base"
RESUME_BASE_CALLBACK = "billing:resume_plan:eds_monitor_base"

PLAN_EDS_MONITOR_BASE = "eds_monitor_base"
PLAN_EDS_MONITOR_EXTRA = "eds_monitor_extra_account"
OCCUPIED_ACCOUNT_STATUSES = {
    "pending_verification",
    "active",
    "auth_failed",
    "scheduled_for_disconnect",
}
CONNECTED_ACCOUNT_STATUSES = {
    "active",
    "scheduled_for_disconnect",
}


@dataclass(frozen=True, slots=True)
class ConnectedEDSAccountSummary:
    tenant_eds_account_id: str
    account_role: str
    login_masked: str
    status: str
    scheduled_for_disconnect: bool = False


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
    base_next_cycle_quantity: int
    allowed_count: int
    next_allowed_count: int
    connected_count: int
    free_count: int
    connected_accounts: list[ConnectedEDSAccountSummary]


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
        price_rub=2990,
        sort_order=10,
    ),
    PlanSeed(
        id="plan_eds_monitor_extra_account",
        plan_key=PLAN_EDS_MONITOR_EXTRA,
        feature_key="eds_monitor",
        title="Доп. кабинет EDS",
        description="Дополнительный кабинет для EDS Monitor.",
        price_rub=2990,
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
            "- помогать с подключением EDS."
        )
        return text, _inline_keyboard(
            [
                [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
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
        if (
            base_active
            and base_subscription is not None
            and base_subscription.quantity > 0
            and base_next_quantity <= 0
        ):
            base_next_quantity = base_subscription.quantity
        extra_quantity = extra_subscription.quantity if extra_subscription and extra_subscription.quantity > 0 else 0
        extra_next_quantity = self._get_next_cycle_quantity(extra_subscription)
        allowed_count = (1 if base_active else 0) + extra_quantity
        next_allowed_count = (1 if base_next_quantity > 0 else 0) + extra_next_quantity
        occupied_accounts = self._list_occupied_accounts(tenant_id) if allowed_count > 0 else []
        connected_accounts = self._build_connected_account_summaries(occupied_accounts)
        connected_count = len(connected_accounts)
        free_count = max(allowed_count - len(occupied_accounts), 0)

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
            base_next_cycle_quantity=base_next_quantity,
            allowed_count=allowed_count,
            next_allowed_count=next_allowed_count,
            connected_count=connected_count,
            free_count=free_count,
            connected_accounts=connected_accounts,
        )

    def build_status_message(self, tenant_id: str) -> tuple[str, dict]:
        summary = self.get_summary(tenant_id)
        active_lines: list[str] = []
        if summary.base_active and summary.base_active_until:
            active_lines.append(f"- EDS Monitor — активно до {_format_date(summary.base_active_until)}")
        if summary.extra_quantity > 0 and summary.extra_active_until:
            active_lines.append(
                f"- Доп. кабинеты EDS — {summary.extra_quantity} шт., активно до {_format_date(summary.extra_active_until)}"
            )
        if not active_lines:
            active_lines.append("- нет")

        due_text = _format_date(summary.next_payment_due_at) if summary.next_payment_due_at else "не назначен"
        connected_account_lines = [
            f"- {account.login_masked}{' (не продлевать)' if account.scheduled_for_disconnect else ''}"
            for account in summary.connected_accounts
        ]
        eds_lines: list[str] = []
        eds_lines.extend(connected_account_lines)
        if summary.allowed_count > 0 and summary.connected_count < summary.allowed_count:
            eds_lines.append(
                f"- подключено кабинетов: {summary.connected_count} из {summary.allowed_count}"
            )

        text = (
            "Мой статус\n\n"
            f"Следующий платеж: {due_text}\n"
            f"Сумма к оплате: {summary.next_amount_rub} ₽\n\n"
            "Активные подписки:\n"
            f"{chr(10).join(active_lines)}"
        )
        if summary.allowed_count > 0 or summary.connected_accounts:
            text += "\n\nКабинеты EDS:"
            if eds_lines:
                text += f"\n{chr(10).join(eds_lines)}"

        buttons: list[list[dict]] = [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
        return text, _inline_keyboard(buttons)

    def build_subscriptions_message(self, tenant_id: str) -> tuple[str, dict]:
        summary = self.get_summary(tenant_id)
        base_plan = self._get_plan(PLAN_EDS_MONITOR_BASE)
        extra_plan = self._get_plan(PLAN_EDS_MONITOR_EXTRA)
        next_cycle_free_slots = self._get_free_slots_for_next_cycle(tenant_id)

        active_lines: list[str] = []
        if summary.base_active and summary.base_active_until:
            active_lines.append(
                f"- {base_plan.title} — {base_plan.price_rub} ₽ / 30 дней, активно до {_format_date(summary.base_active_until)}"
            )
        if summary.extra_quantity > 0 and summary.extra_active_until:
            active_lines.append(
                f"- {extra_plan.title} — {summary.extra_quantity} × {extra_plan.price_rub} ₽ / 30 дней, активно до {_format_date(summary.extra_active_until)}"
            )

        if active_lines:
            active_block = "Подключенные:\n" + "\n".join(active_lines)
        else:
            active_block = "Подключенных подписок пока нет."

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
            buttons.append([{"text": "Добавить подписку на EDS", "callback_data": ADD_EDS_ACCOUNT_CALLBACK}])
            if summary.free_count > 0:
                buttons.append([{"text": "Подключить ЛК EDS", "callback_data": "onboarding:connect_eds"}])
            for account in summary.connected_accounts:
                if account.scheduled_for_disconnect:
                    continue
                buttons.append(
                    [
                        {
                            "text": f"Убрать {account.login_masked}",
                            "callback_data": f"{REMOVE_EDS_ACCOUNT_SELECT_PREFIX}{account.tenant_eds_account_id}",
                        }
                    ]
                )
            if next_cycle_free_slots > 0:
                buttons.append([{"text": "Убрать свободную подписку на EDS", "callback_data": REMOVE_EDS_ACCOUNT_CALLBACK}])
            buttons.extend(self._build_restore_rows(tenant_id, summary))
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
                    [{"text": "Подключить ЛК EDS", "callback_data": "onboarding:connect_eds"}],
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
                    [{"text": "Подключить ЛК EDS", "callback_data": "onboarding:connect_eds"}],
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
            if (
                plan.plan_key == PLAN_EDS_MONITOR_BASE
                and subscription.quantity > 0
                and subscription.active_until
                and _coerce_utc(subscription.active_until) > current_time
                and next_quantity <= 0
            ):
                next_quantity = subscription.quantity
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
        renewable_subscriptions = {item[0] for item in renewable_items}
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
            if subscription in renewable_subscriptions:
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
        self._apply_tenant_eds_account_renewal_state(tenant_id)
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

    def resume_base_renewal(self, tenant_id: str) -> SubscriptionActionResult:
        subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if subscription is None or subscription.quantity <= 0 or subscription.active_until is None:
            return SubscriptionActionResult(
                message_text="Подписка EDS Monitor сейчас не активна.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )

        subscription.cancel_at_period_end = False
        subscription.next_cycle_quantity = max(subscription.quantity, 1)
        subscription.status = "active"
        subscription.updated_at = _utcnow()
        self.session.commit()
        return SubscriptionActionResult(
            message_text=(
                "Продление EDS Monitor снова включено.\n\n"
                f"Подписка активна до: {_format_date(subscription.active_until)}\n"
                "Она будет продлена в следующий платежный цикл."
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def remove_extra_account_at_period_end(self, tenant_id: str) -> SubscriptionActionResult:
        summary = self.get_summary(tenant_id)
        if summary.allowed_count <= 0:
            return SubscriptionActionResult(
                message_text="Сейчас нет подписок EDS, которые можно убрать.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        next_cycle_free_slots = self._get_free_slots_for_next_cycle(tenant_id)
        if next_cycle_free_slots <= 0:
            return SubscriptionActionResult(
                message_text="Сейчас нет свободной подписки EDS, которую можно убрать.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        self._decrement_next_cycle_free_slot(tenant_id)
        self.session.commit()
        next_allowed_count = self._get_next_allowed_count(tenant_id)
        active_until = self._get_active_until_for_eds(tenant_id)

        return SubscriptionActionResult(
            message_text=(
                "Свободная подписка на EDS не будет продлеваться в следующий период.\n\n"
                f"До {_format_date(active_until)} текущая емкость подписки сохранится.\n"
                f"После этой даты число доступных кабинетов уменьшится до {next_allowed_count}."
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def schedule_connected_eds_account_cancel(
        self,
        tenant_id: str,
        tenant_eds_account_id: str,
    ) -> SubscriptionActionResult:
        tenant_account = self.session.get(TenantEDSAccount, tenant_eds_account_id)
        if tenant_account is None or tenant_account.tenant_id != tenant_id:
            return SubscriptionActionResult(
                message_text="Не удалось найти выбранный кабинет.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        if tenant_account.status not in CONNECTED_ACCOUNT_STATUSES:
            return SubscriptionActionResult(
                message_text="Выбранный кабинет уже не активен.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        if tenant_account.status == "scheduled_for_disconnect":
            return SubscriptionActionResult(
                message_text=(
                    f"Кабинет {tenant_account.login_masked} уже помечен как не продленный на следующий период."
                ),
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        decremented_plan_key = self._decrement_next_cycle_slot_for_account(tenant_account)
        if decremented_plan_key is None:
            return SubscriptionActionResult(
                message_text="Сейчас этот кабинет нельзя снять с продления.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        tenant_account.status = "scheduled_for_disconnect"
        tenant_account.updated_at = _utcnow()
        self.session.commit()

        next_allowed_count = self._get_next_allowed_count(tenant_id)
        active_until = self._get_active_until_for_eds(tenant_id)

        return SubscriptionActionResult(
            message_text=(
                f"Кабинет {tenant_account.login_masked} не будет продлеваться на следующий период.\n\n"
                f"До {_format_date(active_until)} он останется активным.\n"
                f"После этой даты число доступных кабинетов уменьшится до {next_allowed_count}."
            ),
            reply_markup=_inline_keyboard(
                [
                    [
                        {
                            "text": f"Вернуть {tenant_account.login_masked}",
                            "callback_data": f"{RESTORE_EDS_ACCOUNT_SELECT_PREFIX}{tenant_account.id}",
                        }
                    ],
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def restore_extra_account_slot(self, tenant_id: str) -> SubscriptionActionResult:
        if self._get_removed_free_slot_count(tenant_id) <= 0:
            return SubscriptionActionResult(
                message_text="Сейчас нет свободной подписки EDS, которую можно вернуть.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        self._restore_next_cycle_free_slot(tenant_id)
        self.session.commit()

        summary = self.get_summary(tenant_id)
        return SubscriptionActionResult(
            message_text=(
                "Свободная подписка на EDS снова будет продлена на следующий период.\n\n"
                f"Следующий платеж: {_format_date(summary.next_payment_due_at)}\n"
                f"Сумма следующего платежа: {summary.next_amount_rub} ₽"
            ),
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def restore_connected_eds_account_cancel(
        self,
        tenant_id: str,
        tenant_eds_account_id: str,
    ) -> SubscriptionActionResult:
        tenant_account = self.session.get(TenantEDSAccount, tenant_eds_account_id)
        if tenant_account is None or tenant_account.tenant_id != tenant_id:
            return SubscriptionActionResult(
                message_text="Не удалось найти выбранный кабинет.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        if tenant_account.status != "scheduled_for_disconnect":
            return SubscriptionActionResult(
                message_text=f"Кабинет {tenant_account.login_masked} сейчас не помечен как отмененный.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        if not self._restore_next_cycle_slot_for_account(tenant_account):
            return SubscriptionActionResult(
                message_text=f"Кабинет {tenant_account.login_masked} сейчас нельзя вернуть.",
                reply_markup=_inline_keyboard(
                    [
                        [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                        [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    ]
                ),
            )

        tenant_account.status = "active"
        tenant_account.updated_at = _utcnow()
        self.session.commit()

        summary = self.get_summary(tenant_id)
        return SubscriptionActionResult(
            message_text=(
                f"Кабинет {tenant_account.login_masked} снова будет продлен на следующий период.\n\n"
                f"Следующий платеж: {_format_date(summary.next_payment_due_at)}\n"
                f"Сумма следующего платежа: {summary.next_amount_rub} ₽"
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

    def _list_occupied_accounts(self, tenant_id: str) -> list[TenantEDSAccount]:
        return (
            self.session.query(TenantEDSAccount)
            .filter(
                TenantEDSAccount.tenant_id == tenant_id,
                TenantEDSAccount.status.in_(tuple(OCCUPIED_ACCOUNT_STATUSES)),
            )
            .order_by(TenantEDSAccount.account_index.asc(), TenantEDSAccount.created_at.asc())
            .all()
        )

    def _build_connected_account_summaries(
        self,
        tenant_accounts: list[TenantEDSAccount],
    ) -> list[ConnectedEDSAccountSummary]:
        items: list[ConnectedEDSAccountSummary] = []
        for account in tenant_accounts:
            if account.status not in CONNECTED_ACCOUNT_STATUSES:
                continue
            items.append(
                ConnectedEDSAccountSummary(
                    tenant_eds_account_id=account.id,
                    account_role=account.account_role,
                    login_masked=account.login_masked,
                    status=account.status,
                    scheduled_for_disconnect=account.status == "scheduled_for_disconnect",
                )
            )
        return items

    def _build_restore_rows(self, tenant_id: str, summary: BillingSummary) -> list[list[dict]]:
        rows: list[list[dict]] = []
        scheduled_accounts = [account for account in summary.connected_accounts if account.scheduled_for_disconnect]
        if scheduled_accounts:
            for account in scheduled_accounts:
                rows.append(
                    [
                        {
                            "text": f"Вернуть {account.login_masked}",
                            "callback_data": f"{RESTORE_EDS_ACCOUNT_SELECT_PREFIX}{account.tenant_eds_account_id}",
                        }
                    ]
                )
        if self._get_removed_free_slot_count(tenant_id) > 0:
            rows.append([{"text": "Вернуть свободную подписку на EDS", "callback_data": RESTORE_EDS_ACCOUNT_CALLBACK}])
        return rows

    def _get_next_allowed_count(self, tenant_id: str) -> int:
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        base_next = self._get_next_cycle_quantity(base_subscription)
        extra_next = self._get_next_cycle_quantity(extra_subscription)
        return (1 if base_next > 0 else 0) + extra_next

    def _get_active_until_for_eds(self, tenant_id: str) -> datetime | None:
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if base_subscription is not None and base_subscription.active_until is not None:
            return base_subscription.active_until
        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        return extra_subscription.active_until if extra_subscription is not None else None

    def _get_unscheduled_occupied_count(self, tenant_id: str) -> int:
        return sum(
            1
            for account in self._list_occupied_accounts(tenant_id)
            if account.status != "scheduled_for_disconnect"
        )

    def _get_free_slots_for_next_cycle(self, tenant_id: str) -> int:
        next_allowed_count = self._get_next_allowed_count(tenant_id)
        return max(next_allowed_count - self._get_unscheduled_occupied_count(tenant_id), 0)

    def _get_removed_free_slot_count(self, tenant_id: str) -> int:
        summary = self.get_summary(tenant_id)
        scheduled_connected_count = sum(
            1 for account in summary.connected_accounts if account.scheduled_for_disconnect
        )
        current_available_for_next = summary.allowed_count - scheduled_connected_count
        return max(current_available_for_next - summary.next_allowed_count, 0)

    def _decrement_next_cycle_free_slot(self, tenant_id: str) -> bool:
        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        if extra_subscription is not None and self._get_next_cycle_quantity(extra_subscription) > 0:
            extra_subscription.next_cycle_quantity = max(self._get_next_cycle_quantity(extra_subscription) - 1, 0)
            extra_subscription.status = (
                "scheduled_for_cancel" if extra_subscription.next_cycle_quantity == 0 else "active"
            )
            extra_subscription.updated_at = _utcnow()
            return True

        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if base_subscription is not None and self._get_next_cycle_quantity(base_subscription) > 0:
            base_subscription.next_cycle_quantity = max(self._get_next_cycle_quantity(base_subscription) - 1, 0)
            base_subscription.cancel_at_period_end = base_subscription.next_cycle_quantity == 0
            base_subscription.status = (
                "scheduled_for_cancel" if base_subscription.next_cycle_quantity == 0 else "active"
            )
            base_subscription.updated_at = _utcnow()
            return True
        return False

    def _restore_next_cycle_free_slot(self, tenant_id: str) -> bool:
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)
        if (
            base_subscription is not None
            and base_subscription.quantity > 0
            and self._get_next_cycle_quantity(base_subscription) < base_subscription.quantity
        ):
            base_subscription.next_cycle_quantity = min(
                self._get_next_cycle_quantity(base_subscription) + 1,
                base_subscription.quantity,
            )
            base_subscription.cancel_at_period_end = False
            base_subscription.status = "active"
            base_subscription.updated_at = _utcnow()
            return True

        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        if (
            extra_subscription is not None
            and extra_subscription.quantity > 0
            and self._get_next_cycle_quantity(extra_subscription) < extra_subscription.quantity
        ):
            extra_subscription.next_cycle_quantity = min(
                self._get_next_cycle_quantity(extra_subscription) + 1,
                extra_subscription.quantity,
            )
            extra_subscription.status = "active"
            extra_subscription.updated_at = _utcnow()
            return True
        return False

    def _decrement_next_cycle_slot_for_account(self, tenant_account: TenantEDSAccount) -> str | None:
        tenant_id = tenant_account.tenant_id
        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)

        if tenant_account.account_role == "extra":
            if extra_subscription is None or self._get_next_cycle_quantity(extra_subscription) <= 0:
                return None
            extra_subscription.next_cycle_quantity = max(self._get_next_cycle_quantity(extra_subscription) - 1, 0)
            extra_subscription.status = (
                "scheduled_for_cancel" if extra_subscription.next_cycle_quantity == 0 else "active"
            )
            extra_subscription.updated_at = _utcnow()
            return PLAN_EDS_MONITOR_EXTRA

        if extra_subscription is not None and self._get_next_cycle_quantity(extra_subscription) > 0:
            extra_subscription.next_cycle_quantity = max(self._get_next_cycle_quantity(extra_subscription) - 1, 0)
            extra_subscription.status = (
                "scheduled_for_cancel" if extra_subscription.next_cycle_quantity == 0 else "active"
            )
            extra_subscription.updated_at = _utcnow()
            return PLAN_EDS_MONITOR_EXTRA

        if base_subscription is not None and self._get_next_cycle_quantity(base_subscription) > 0:
            base_subscription.next_cycle_quantity = max(self._get_next_cycle_quantity(base_subscription) - 1, 0)
            base_subscription.cancel_at_period_end = base_subscription.next_cycle_quantity == 0
            base_subscription.status = (
                "scheduled_for_cancel" if base_subscription.next_cycle_quantity == 0 else "active"
            )
            base_subscription.updated_at = _utcnow()
            return PLAN_EDS_MONITOR_BASE
        return None

    def _restore_next_cycle_slot_for_account(self, tenant_account: TenantEDSAccount) -> bool:
        tenant_id = tenant_account.tenant_id
        extra_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_EXTRA)
        base_subscription = self._get_subscription(tenant_id, PLAN_EDS_MONITOR_BASE)

        if tenant_account.account_role == "extra":
            if extra_subscription is None or extra_subscription.quantity <= 0:
                return False
            extra_subscription.next_cycle_quantity = min(
                self._get_next_cycle_quantity(extra_subscription) + 1,
                extra_subscription.quantity,
            )
            extra_subscription.status = "active"
            extra_subscription.updated_at = _utcnow()
            return True

        if (
            base_subscription is not None
            and base_subscription.quantity > 0
            and self._get_next_cycle_quantity(base_subscription) < base_subscription.quantity
        ):
            base_subscription.next_cycle_quantity = min(
                self._get_next_cycle_quantity(base_subscription) + 1,
                base_subscription.quantity,
            )
            base_subscription.cancel_at_period_end = False
            base_subscription.status = "active"
            base_subscription.updated_at = _utcnow()
            return True

        if (
            extra_subscription is not None
            and extra_subscription.quantity > 0
            and self._get_next_cycle_quantity(extra_subscription) < extra_subscription.quantity
        ):
            extra_subscription.next_cycle_quantity = min(
                self._get_next_cycle_quantity(extra_subscription) + 1,
                extra_subscription.quantity,
            )
            extra_subscription.status = "active"
            extra_subscription.updated_at = _utcnow()
            return True
        return False

    def _apply_tenant_eds_account_renewal_state(self, tenant_id: str) -> None:
        scheduled_accounts = (
            self.session.query(TenantEDSAccount)
            .filter(
                TenantEDSAccount.tenant_id == tenant_id,
                TenantEDSAccount.status == "scheduled_for_disconnect",
            )
            .order_by(TenantEDSAccount.account_index.asc(), TenantEDSAccount.created_at.asc())
            .all()
        )
        if not scheduled_accounts:
            return
        now = _utcnow()
        for account in scheduled_accounts:
            account.status = "expired"
            account.updated_at = now

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
        if cycle is not None:
            self.session.flush()
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
