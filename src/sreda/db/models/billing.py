from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    price_rub: Mapped[int] = mapped_column(Integer)
    billing_period_days: Mapped[int] = mapped_column(Integer, default=30)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    # Per-billing-cycle credit quota (Phase 4.5). NULL = unmetered plan
    # (legacy). Each LLM call attributed to this plan's feature_key
    # consumes credits per the MiMo rate formula; when the cumulative
    # usage in the current period reaches this number, the skill's
    # LLM-backed features fall back to "quota exhausted".
    credits_monthly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class TenantBillingCycle(Base):
    __tablename__ = "tenant_billing_cycles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    billing_anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_payment_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class TenantSubscription(Base):
    __tablename__ = "tenant_subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("subscription_plans.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_payment", index=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    next_cycle_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_payment_order_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_orders.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    billing_cycle_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenant_billing_cycles.id"),
        nullable=True,
        index=True,
    )
    provider_key: Mapped[str] = mapped_column(String(32), index=True)
    order_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    amount_rub: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class PaymentOrderItem(Base):
    __tablename__ = "payment_order_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_order_id: Mapped[str] = mapped_column(ForeignKey("payment_orders.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("subscription_plans.id"), index=True)
    amount_rub: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calculation_type: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
