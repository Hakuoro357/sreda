from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.billing import (
    PaymentOrder,
    PaymentOrderItem,
    SubscriptionPlan,
    TenantBillingCycle,
    TenantSubscription,
)
from sreda.db.models.core import TenantFeature, User
from sreda.domain.tenants.features import is_feature_disabled

logger = logging.getLogger(__name__)

# #181: text shown when a user reaches a retired skill through any old
# surface (callback, Mini App, legacy chat command). No reply_markup —
# the surface is a tombstone, there is no follow-up action.
DISABLED_FEATURE_MESSAGE = "Это умение больше не поддерживается."

STATUS_CALLBACK = "billing:status"
SUBSCRIPTIONS_CALLBACK = "billing:subscriptions"
RENEW_CALLBACK = "billing:renew"
# #181 Phase B: EDS Monitor is fully retired (engine, billing read-path and
# DB tables removed). The legacy EDS callback CONSTANTS below are kept ONLY so
# the dispatcher can still map old chat buttons (pre-migration history) to the
# tombstoned ``eds.*`` / ``subscription.*`` handlers, which answer "Это умение
# отключено." There is no EDS code path behind them anymore.
CONNECT_BASE_CALLBACK = "billing:connect_plan:eds_monitor_base"
ADD_EDS_ACCOUNT_CALLBACK = "billing:add_eds_account"
REMOVE_EDS_ACCOUNT_CALLBACK = "billing:remove_eds_account"
REMOVE_EDS_ACCOUNT_SELECT_PREFIX = "billing:remove_eds_account:select:"
RESTORE_EDS_ACCOUNT_CALLBACK = "billing:restore_eds_account"
RESTORE_EDS_ACCOUNT_SELECT_PREFIX = "billing:restore_eds_account:select:"
CANCEL_BASE_CALLBACK = "billing:cancel_plan:eds_monitor_base"
RESUME_BASE_CALLBACK = "billing:resume_plan:eds_monitor_base"

PLAN_VOICE_TRANSCRIPTION = "voice_transcription_base"
CONNECT_VOICE_CALLBACK = "billing:connect_plan:voice_transcription_base"
CANCEL_VOICE_CALLBACK = "billing:cancel_plan:voice_transcription_base"


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
class SubscriptionActionResult:
    message_text: str
    reply_markup: dict


# #181 Phase B: EDS PlanSeeds removed. No plans are auto-seeded by the engine
# anymore (voice / housewife plans are seeded by their own skill modules). The
# tuple stays so ``ensure_default_plans`` keeps its read-only no-op contract.
PLAN_SEEDS: tuple[PlanSeed, ...] = ()


class BillingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _eds_disabled_result() -> SubscriptionActionResult:
        """#181: no-op result for a retired skill. No DB write, no markup.

        Kept as a generic guard for the simple-subscription path: if a future
        skill is retired via ``is_feature_disabled`` its start/cancel calls
        short-circuit here instead of mutating rows."""
        return SubscriptionActionResult(
            message_text=DISABLED_FEATURE_MESSAGE,
            reply_markup={},
        )

    def ensure_default_plans(self) -> None:
        # 2026-06-04 (vex-assistant#99/#100): write ONLY when a plan is missing
        # or a field actually differs from the seed. This method runs on the
        # Mini App HOT READ paths (get_plans + get_summary via this service), and
        # the previous blind per-call UPDATE took a row lock on the shared plan
        # rows on EVERY open. Concurrent opens then serialized on those locks and
        # timed out (prod 2026-06-04: /plans + /summary returned 499 under the
        # migration broadcast burst). In steady state (plans already match the
        # seeds) this is now read-only — no UPDATE, no flush, no row lock.
        changed = False
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
                changed = True
                continue
            if (
                plan.title == seed.title
                and plan.description == seed.description
                and plan.price_rub == seed.price_rub
                and plan.billing_period_days == seed.billing_period_days
                and plan.is_public == seed.is_public
                and plan.is_active == seed.is_active
                and plan.sort_order == seed.sort_order
            ):
                continue  # unchanged — skip the UPDATE (and its row lock)
            plan.title = seed.title
            plan.description = seed.description
            plan.price_rub = seed.price_rub
            plan.billing_period_days = seed.billing_period_days
            plan.is_public = seed.is_public
            plan.is_active = seed.is_active
            plan.sort_order = seed.sort_order
            plan.updated_at = _utcnow()
            changed = True
        if changed:
            self.session.flush()

    def build_help_message(self) -> tuple[str, dict]:
        text = (
            "Я Среда.\n\n"
            "Сейчас я умею:\n"
            "- показывать статус аккаунта и подписок;\n"
            "- подключать и продлевать подписки."
        )
        return text, _inline_keyboard(
            [
                [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
            ]
        )

    def next_payment_for_display(
        self, tenant_id: str, *, now: datetime | None = None
    ) -> tuple[int, datetime | None]:
        """Compute the next renewal charge (amount, due date) across the
        tenant's renewable subscriptions.

        #181 Phase B: this replaces the EDS-centric ``get_summary`` figures.
        It sums every renewable subscription (voice / housewife / future
        simple skills) by joining ``TenantSubscription`` with its plan and
        using ``next_cycle_quantity``. Plans that map to a retired skill are
        skipped via ``is_feature_disabled`` (generic guard; currently a no-op).

        Returns ``(amount_rub, due_at)``. When nothing renews — no billing
        cycle, or only free renewals (e.g. the 0 ₽ housewife plan) — returns
        ``(0, None)`` so the caller drops the "Сумма к оплате" line. READ-ONLY:
        never mutates rows.
        """
        cycle = self._get_cycle(tenant_id)
        if cycle is None:
            return 0, None

        subscription_plan_rows: list[tuple[TenantSubscription, SubscriptionPlan]] = (
            self.session.query(TenantSubscription, SubscriptionPlan)
            .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
            .filter(TenantSubscription.tenant_id == tenant_id)
            .all()
        )
        amount_rub = 0
        for subscription, plan in subscription_plan_rows:
            if is_feature_disabled(plan.feature_key):
                continue
            next_quantity = self._get_next_cycle_quantity(subscription)
            if next_quantity <= 0:
                continue
            amount_rub += plan.price_rub * next_quantity

        if amount_rub <= 0:
            return 0, None
        return amount_rub, cycle.next_payment_due_at

    def build_status_message(self, tenant_id: str, *, now: datetime | None = None) -> tuple[str, dict]:
        # #181 Phase B: EDS Monitor is retired — no EDS active lines, no
        # "Кабинеты EDS" block. Active subscriptions and the payment block are
        # derived from the non-EDS subscription rows (voice / housewife / etc.).
        self.ensure_default_plans()
        current_time = _utcnow(now)
        active_lines = self._build_active_subscription_lines(tenant_id, now=current_time)
        if not active_lines:
            active_lines.append("- нет")

        display_amount, display_due = self.next_payment_for_display(tenant_id, now=current_time)

        if display_due is None:
            # Nothing to renew → omit the payment lines completely.
            payment_block = ""
        else:
            payment_block = (
                f"Следующий платеж: {_format_date(display_due)}\n"
                f"Сумма к оплате: {display_amount} ₽\n\n"
            )

        text = (
            "Мой статус\n\n"
            f"{payment_block}"
            "Активные подписки:\n"
            f"{chr(10).join(active_lines)}"
        )

        buttons: list[list[dict]] = [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
        return text, _inline_keyboard(buttons)

    def build_subscriptions_message(
        self,
        tenant_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, dict]:
        """Render the /subscriptions message + markup.

        #181 Phase B: the EDS connect/add/remove/restore UI is gone. Only the
        non-EDS simple subscriptions (currently voice transcription) remain."""
        self.ensure_default_plans()
        current_time = _utcnow(now)

        # Voice transcription subscription state
        voice_plan = self._get_plan_optional(PLAN_VOICE_TRANSCRIPTION)
        voice_sub = self._get_subscription_optional(tenant_id, PLAN_VOICE_TRANSCRIPTION)
        voice_active = self._is_subscription_active(voice_sub, current_time) if voice_sub else False

        active_lines: list[str] = []
        if voice_active and voice_sub and voice_plan:
            price_label = f"{voice_plan.price_rub} ₽ / 30 дней" if voice_plan.price_rub > 0 else "бесплатно"
            active_lines.append(
                f"- {voice_plan.title} — {price_label}, активно до {_format_date(voice_sub.active_until)}"
            )

        if active_lines:
            active_block = "Подключенные:\n" + "\n".join(active_lines)
        else:
            active_block = "Подключенных подписок пока нет."

        available_lines = []
        if not voice_active and voice_plan:
            price_label = f"{voice_plan.price_rub} ₽ / 30 дней" if voice_plan.price_rub > 0 else "бесплатно"
            available_lines.append(f"- {voice_plan.title} — {price_label}")
        available_block = "Доступные:\n" + ("\n".join(available_lines) if available_lines else "- нет")

        text = f"Подписки\n\n{active_block}\n\n{available_block}"

        buttons: list[list[dict]] = []
        # Voice transcription toggle
        if voice_plan:
            if not voice_active:
                buttons.append([{"text": f"Подключить {voice_plan.title}", "callback_data": CONNECT_VOICE_CALLBACK}])
            else:
                buttons.append([{"text": f"Отключить {voice_plan.title}", "callback_data": CANCEL_VOICE_CALLBACK}])
        buttons.append([{"text": "Мой статус", "callback_data": STATUS_CALLBACK}])
        return text, _inline_keyboard(buttons)

    def _build_active_subscription_lines(
        self, tenant_id: str, *, now: datetime
    ) -> list[str]:
        """Active non-EDS subscription lines for the status message.

        #181 Phase B: currently only voice transcription has a status line;
        housewife is shown via the Mini App skill cards, not the chat status.
        """
        lines: list[str] = []
        voice_plan = self._get_plan_optional(PLAN_VOICE_TRANSCRIPTION)
        voice_sub = self._get_subscription_optional(tenant_id, PLAN_VOICE_TRANSCRIPTION)
        if voice_plan and voice_sub and self._is_subscription_active(voice_sub, now):
            lines.append(
                f"- {voice_plan.title} — активно до {_format_date(voice_sub.active_until)}"
            )
        return lines

    def renew_cycle(self, tenant_id: str, *, now: datetime | None = None) -> SubscriptionActionResult:
        self.ensure_default_plans()
        current_time = _utcnow(now)
        cycle = self._get_cycle(tenant_id)
        if cycle is None:
            return SubscriptionActionResult(
                message_text="Пока нечего продлевать.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )

        # Join subscriptions with their plans in a single round-trip so
        # the loop below no longer issues ``session.get(SubscriptionPlan,
        # subscription.plan_id)`` per row (N+1). An INNER JOIN matches
        # the old behavior: subscriptions whose plan row is missing were
        # silently skipped.
        subscription_plan_rows: list[tuple[TenantSubscription, SubscriptionPlan]] = (
            self.session.query(TenantSubscription, SubscriptionPlan)
            .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
            .filter(TenantSubscription.tenant_id == tenant_id)
            .all()
        )
        # #181 PARTITION: drop disabled-skill subscriptions from the source
        # iteration entirely (generic guard; currently a no-op). They must NOT
        # enter ``renewable_items`` NOR the implicit expire-loop below. Non-EDS
        # subs (voice/housewife) renew as usual.
        subscription_plan_rows = [
            row
            for row in subscription_plan_rows
            if not is_feature_disabled(row[1].feature_key)
        ]
        subscriptions = [row[0] for row in subscription_plan_rows]
        renewable_items: list[tuple[TenantSubscription, SubscriptionPlan, int]] = []
        total_amount_rub = 0
        for subscription, plan in subscription_plan_rows:
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

        self.session.commit()

        # #181 Phase B: success message reflects the post-renewal next charge,
        # computed across the non-EDS subscriptions just renewed. When nothing
        # remains to renew (due is None) the payment line is dropped.
        display_amount, display_due = self.next_payment_for_display(
            tenant_id, now=current_time
        )
        if display_due is None:
            payment_block = ""
        else:
            payment_block = (
                "\n\n"
                f"Следующий платеж: {_format_date(display_due)}\n"
                f"Сумма следующего платежа: {display_amount} ₽"
            )

        return SubscriptionActionResult(
            message_text=f"Подписка продлена.{payment_block}",
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                ]
            ),
        )

    def start_voice_subscription(self, tenant_id: str, *, now: datetime | None = None) -> SubscriptionActionResult:
        """Activate the free voice_transcription plan for a tenant."""
        current_time = _utcnow(now)
        plan = self._get_plan_optional(PLAN_VOICE_TRANSCRIPTION)
        if plan is None:
            return SubscriptionActionResult(
                message_text="План распознавания голоса не найден.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )
        sub = self._get_subscription_optional(tenant_id, PLAN_VOICE_TRANSCRIPTION)
        if sub is not None and self._is_subscription_active(sub, current_time):
            return SubscriptionActionResult(
                message_text="Распознавание голоса уже подключено.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )
        if sub is None:
            sub = TenantSubscription(
                id=f"sub_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                plan_id=plan.id,
            )
            self.session.add(sub)
        sub.status = "active"
        sub.starts_at = current_time
        # Free plans (price_rub == 0) are perpetual: active_until is set
        # ~100 years out so _is_subscription_active stays True forever and
        # no "cycle renewal" path ever surfaces a billing event for them.
        # This is the backend half of "бесплатный скил подключается
        # бессрочно"; the UI half is hiding the expiry line when price == 0.
        if plan.price_rub == 0:
            sub.active_until = current_time + timedelta(days=36500)
        else:
            sub.active_until = current_time + timedelta(days=plan.billing_period_days)
        sub.cancel_at_period_end = False
        sub.quantity = 1
        sub.next_cycle_quantity = 1
        sub.updated_at = current_time
        self._ensure_feature_enabled(tenant_id, "voice_transcription", True)
        self.session.commit()
        if plan.price_rub == 0:
            message_text = f"{plan.title} подключено."
        else:
            message_text = (
                f"{plan.title} подключено.\n\n"
                f"Активно до: {_format_date(sub.active_until)}"
            )
        return SubscriptionActionResult(
            message_text=message_text,
            reply_markup=_inline_keyboard([
                [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
            ]),
        )

    def cancel_voice_subscription(self, tenant_id: str) -> SubscriptionActionResult:
        """Deactivate voice_transcription for a tenant."""
        sub = self._get_subscription_optional(tenant_id, PLAN_VOICE_TRANSCRIPTION)
        if sub is None or sub.quantity <= 0:
            return SubscriptionActionResult(
                message_text="Распознавание голоса сейчас не активно.",
                reply_markup=_inline_keyboard([[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]),
            )
        sub.status = "cancelled"
        sub.quantity = 0
        sub.next_cycle_quantity = 0
        sub.cancel_at_period_end = True
        sub.updated_at = _utcnow()
        self._ensure_feature_enabled(tenant_id, "voice_transcription", False)
        self.session.commit()
        return SubscriptionActionResult(
            message_text="Распознавание голоса отключено.",
            reply_markup=_inline_keyboard([
                [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
            ]),
        )

    # ------------------------------------------------------------------
    # Simple subscription (one plan_key → one subscription → toggle)
    #
    # For any skill that doesn't need EDS-style base+extra aggregation.
    # Derives feature_key from the plan row itself, so adding a new
    # simple skill is:
    #   1. Seed SubscriptionPlan with feature_key
    #   2. Wire start/cancel_simple_subscription in the Mini App API
    # ``start_voice_subscription`` and ``cancel_voice_subscription`` stay
    # as focused aliases (used by callback handlers) but share identical
    # semantics with this path.
    # ------------------------------------------------------------------

    def start_simple_subscription(
        self,
        tenant_id: str,
        plan_key: str,
        *,
        now: datetime | None = None,
    ) -> SubscriptionActionResult:
        current_time = _utcnow(now)
        plan = self._get_plan_optional(plan_key)
        if plan is None:
            return SubscriptionActionResult(
                message_text=f"План {plan_key!r} не найден.",
                reply_markup=_inline_keyboard(
                    [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
                ),
            )
        # #181: cut ONLY a retired skill (by resolved feature_key) — voice /
        # housewife (other feature_key) keep flowing through this generic path.
        if is_feature_disabled(plan.feature_key):
            return self._eds_disabled_result()
        sub = self._get_subscription_optional(tenant_id, plan_key)
        if sub is not None and self._is_subscription_active(sub, current_time):
            return SubscriptionActionResult(
                message_text=f"{plan.title} уже подключено.",
                reply_markup=_inline_keyboard(
                    [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
                ),
            )
        if sub is None:
            sub = TenantSubscription(
                id=f"sub_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                plan_id=plan.id,
            )
            self.session.add(sub)
        sub.status = "active"
        sub.starts_at = current_time
        # Free plans are perpetual (~100 years out). See voice spec 57.
        if plan.price_rub == 0:
            sub.active_until = current_time + timedelta(days=36500)
        else:
            sub.active_until = current_time + timedelta(
                days=plan.billing_period_days
            )
        sub.cancel_at_period_end = False
        sub.quantity = 1
        sub.next_cycle_quantity = 1
        sub.updated_at = current_time
        self._ensure_feature_enabled(tenant_id, plan.feature_key, True)
        self._schedule_onboarding_if_needed(tenant_id, plan.feature_key)
        self.session.commit()
        if plan.price_rub == 0:
            message_text = f"{plan.title} подключено."
        else:
            message_text = (
                f"{plan.title} подключено.\n\n"
                f"Активно до: {_format_date(sub.active_until)}"
            )
        return SubscriptionActionResult(
            message_text=message_text,
            reply_markup=_inline_keyboard(
                [
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            ),
        )

    def cancel_simple_subscription(
        self, tenant_id: str, plan_key: str
    ) -> SubscriptionActionResult:
        plan = self._get_plan_optional(plan_key)
        if plan is None:
            return SubscriptionActionResult(
                message_text=f"План {plan_key!r} не найден.",
                reply_markup=_inline_keyboard(
                    [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
                ),
            )
        # #181: cut ONLY a retired skill (by resolved feature_key).
        if is_feature_disabled(plan.feature_key):
            return self._eds_disabled_result()
        sub = self._get_subscription_optional(tenant_id, plan_key)
        if sub is None or sub.quantity <= 0:
            return SubscriptionActionResult(
                message_text=f"{plan.title} сейчас не активно.",
                reply_markup=_inline_keyboard(
                    [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]
                ),
            )
        sub.status = "cancelled"
        sub.quantity = 0
        sub.next_cycle_quantity = 0
        sub.cancel_at_period_end = True
        sub.updated_at = _utcnow()
        self._ensure_feature_enabled(tenant_id, plan.feature_key, False)
        self.session.commit()
        return SubscriptionActionResult(
            message_text=f"{plan.title} отключено.",
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

    def _get_plan_optional(self, plan_key: str) -> SubscriptionPlan | None:
        return (
            self.session.query(SubscriptionPlan)
            .filter(SubscriptionPlan.plan_key == plan_key)
            .one_or_none()
        )

    def _get_subscription_optional(self, tenant_id: str, plan_key: str) -> TenantSubscription | None:
        plan = self._get_plan_optional(plan_key)
        if plan is None:
            return None
        return (
            self.session.query(TenantSubscription)
            .filter(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.plan_id == plan.id,
            )
            .one_or_none()
        )

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

    def _schedule_onboarding_if_needed(
        self, tenant_id: str, feature_key: str
    ) -> None:
        """For skills with a first-contact onboarding flow (currently only
        housewife_assistant), schedule a kickoff so the bot can proactively
        introduce itself ~5 minutes after subscription if the user doesn't
        write first. Idempotent — if onboarding is already in progress or
        complete, this is a no-op.

        We pick the tenant's first User row as the target. For the current
        single-user-per-Telegram-account model this is unambiguous; when
        the platform grows to multi-user tenants, the caller will need to
        pass user_id explicitly.
        """
        from sreda.services.housewife_onboarding import (
            HOUSEWIFE_FEATURE_KEY,
            HousewifeOnboardingService,
        )

        if feature_key != HOUSEWIFE_FEATURE_KEY:
            return
        user = (
            self.session.query(User)
            .filter(User.tenant_id == tenant_id)
            .order_by(User.id.asc())
            .first()
        )
        if user is None:
            # No user bound to this tenant yet — onboarding will be
            # initialised naturally on the user's first message.
            return
        try:
            HousewifeOnboardingService(self.session).schedule_kickoff(
                tenant_id=tenant_id,
                user_id=user.id,
                delay_minutes=5,
            )
        except Exception:  # noqa: BLE001 — onboarding is additive; never block a subscription
            logger.exception("failed to schedule housewife onboarding kickoff")

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


def _coerce_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
