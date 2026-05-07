from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    event,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, Session, mapped_column

from sreda.db.base import Base


# Cross-dialect JSONB: PG native, SQLite fallback to JSON (тесты).
_JSONB = postgresql.JSONB(astext_type=Text()).with_variant(JSON(), "sqlite")


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
    # NB: The plan called for partial unique index `(tenant_id, feature_key)
    # WHERE status='active'`, но это ломает existing eds_monitor multi-slot
    # billing pattern (base + extra-account subs share feature_key). Phase 1
    # ships без DB-level uniqueness — single-active-per-(tenant, feature)
    # invariant enforced на app-level в Phase 2 (EntitlementGate +
    # SignupAbuseGuard). Tracked as known TODO; full DB-level enforcement
    # требует refactor of eds_monitor billing model (separate sprint).

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("subscription_plans.id"), index=True)
    # Denormalized from plan. NOT NULL after migration 0041 backfill.
    # ORM event listener `_sync_subscription_feature_key` (см. ниже)
    # populates на INSERT (если feature_key falsy) и re-syncs на
    # UPDATE при изменении plan_id. Index используется Phase 2
    # entitlement queries — см. EntitlementGate.
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
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
    # Grandfather metadata — set on existing housewife_assistant subscribers
    # by migration 0041, или admin-grant later. NOT NULL grandfathered_at →
    # tenant exempt from quota enforcement (Phase 2) and future billing.
    grandfathered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    grandfather_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional per-tenant quota override JSONB. Schema (Phase 2):
    # {"llm_turns_daily": 100, "llm_turns_monthly": 1000, ...} — overrides
    # plan defaults. Если NULL — plan defaults применяются.
    quota_overrides_json: Mapped[dict[str, Any] | None] = mapped_column(
        _JSONB, nullable=True,
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


# ---------------------------------------------------------- ORM event listener
# Sync TenantSubscription.feature_key from linked SubscriptionPlan.
#
# Phase 1 introduces denormalized feature_key column (used by Phase 2
# entitlement queries). Listener fires on flush:
# - INSERT new sub без feature_key → populate из plan.
# - UPDATE existing sub с modified plan_id → re-sync feature_key
#   (Xiaomi MAJOR review fix: stale feature_key after plan upgrade
#   downgrade ломает entitlement lookups).
# - Explicit feature_key uже задан и plan_id не менялся → не overwrite.

from sqlalchemy import inspect as sa_inspect


@event.listens_for(Session, "before_flush")
def _sync_subscription_feature_key(session, flush_context, instances) -> None:
    pending: list[TenantSubscription] = []

    # NEW objects with missing feature_key
    for obj in session.new:
        if isinstance(obj, TenantSubscription) and not obj.feature_key:
            pending.append(obj)

    # DIRTY objects whose plan_id changed → re-sync
    for obj in session.dirty:
        if not isinstance(obj, TenantSubscription):
            continue
        attrs = sa_inspect(obj).attrs
        plan_history = attrs.plan_id.history
        if plan_history.has_changes():
            pending.append(obj)

    if not pending:
        return
    plan_ids = {obj.plan_id for obj in pending if obj.plan_id}
    if not plan_ids:
        return
    rows = session.execute(
        SubscriptionPlan.__table__.select().where(SubscriptionPlan.id.in_(plan_ids))
    ).fetchall()
    feature_by_plan = {row.id: row.feature_key for row in rows}
    for obj in pending:
        feat = feature_by_plan.get(obj.plan_id)
        if feat:
            obj.feature_key = feat
