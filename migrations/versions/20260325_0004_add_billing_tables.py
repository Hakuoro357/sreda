"""add billing tables

Revision ID: 20260325_0004
Revises: 20260325_0003
Create Date: 2026-03-25 20:35:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260325_0004"
down_revision = "20260325_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("plan_key", sa.String(length=64), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("price_rub", sa.Integer(), nullable=False),
        sa.Column("billing_period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscription_plans_plan_key", "subscription_plans", ["plan_key"], unique=True)
    op.create_index("ix_subscription_plans_feature_key", "subscription_plans", ["feature_key"])

    op.create_table(
        "tenant_billing_cycles",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("billing_anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_payment_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="RUB"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_billing_cycles_tenant_id", "tenant_billing_cycles", ["tenant_id"])
    op.create_index(
        "ix_tenant_billing_cycles_next_payment_due_at",
        "tenant_billing_cycles",
        ["next_payment_due_at"],
    )
    op.create_index("ix_tenant_billing_cycles_status", "tenant_billing_cycles", ["status"])

    op.create_table(
        "payment_orders",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "billing_cycle_id",
            sa.String(length=64),
            sa.ForeignKey("tenant_billing_cycles.id"),
            nullable=True,
        ),
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("order_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="created"),
        sa.Column("amount_rub", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True),
        sa.Column("provider_invoice_url", sa.Text(), nullable=True),
        sa.Column("provider_payload_json", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_payment_orders_tenant_id", "payment_orders", ["tenant_id"])
    op.create_index("ix_payment_orders_billing_cycle_id", "payment_orders", ["billing_cycle_id"])
    op.create_index("ix_payment_orders_provider_key", "payment_orders", ["provider_key"])
    op.create_index("ix_payment_orders_order_type", "payment_orders", ["order_type"])
    op.create_index("ix_payment_orders_status", "payment_orders", ["status"])

    op.create_table(
        "tenant_subscriptions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("plan_id", sa.String(length=64), sa.ForeignKey("subscription_plans.id"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_payment"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_cycle_quantity", sa.Integer(), nullable=True),
        sa.Column(
            "last_payment_order_id",
            sa.String(length=64),
            sa.ForeignKey("payment_orders.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenant_subscriptions_tenant_id", "tenant_subscriptions", ["tenant_id"])
    op.create_index("ix_tenant_subscriptions_plan_id", "tenant_subscriptions", ["plan_id"])
    op.create_index("ix_tenant_subscriptions_status", "tenant_subscriptions", ["status"])
    op.create_index(
        "ix_tenant_subscriptions_last_payment_order_id",
        "tenant_subscriptions",
        ["last_payment_order_id"],
    )

    op.create_table(
        "payment_order_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "payment_order_id",
            sa.String(length=64),
            sa.ForeignKey("payment_orders.id"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.String(length=64), sa.ForeignKey("subscription_plans.id"), nullable=False),
        sa.Column("amount_rub", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculation_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_payment_order_items_payment_order_id", "payment_order_items", ["payment_order_id"])
    op.create_index("ix_payment_order_items_plan_id", "payment_order_items", ["plan_id"])
    op.create_index(
        "ix_payment_order_items_calculation_type",
        "payment_order_items",
        ["calculation_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_payment_order_items_calculation_type", table_name="payment_order_items")
    op.drop_index("ix_payment_order_items_plan_id", table_name="payment_order_items")
    op.drop_index("ix_payment_order_items_payment_order_id", table_name="payment_order_items")
    op.drop_table("payment_order_items")

    op.drop_index("ix_tenant_subscriptions_last_payment_order_id", table_name="tenant_subscriptions")
    op.drop_index("ix_tenant_subscriptions_status", table_name="tenant_subscriptions")
    op.drop_index("ix_tenant_subscriptions_plan_id", table_name="tenant_subscriptions")
    op.drop_index("ix_tenant_subscriptions_tenant_id", table_name="tenant_subscriptions")
    op.drop_table("tenant_subscriptions")

    op.drop_index("ix_payment_orders_status", table_name="payment_orders")
    op.drop_index("ix_payment_orders_order_type", table_name="payment_orders")
    op.drop_index("ix_payment_orders_provider_key", table_name="payment_orders")
    op.drop_index("ix_payment_orders_billing_cycle_id", table_name="payment_orders")
    op.drop_index("ix_payment_orders_tenant_id", table_name="payment_orders")
    op.drop_table("payment_orders")

    op.drop_index("ix_tenant_billing_cycles_status", table_name="tenant_billing_cycles")
    op.drop_index("ix_tenant_billing_cycles_next_payment_due_at", table_name="tenant_billing_cycles")
    op.drop_index("ix_tenant_billing_cycles_tenant_id", table_name="tenant_billing_cycles")
    op.drop_table("tenant_billing_cycles")

    op.drop_index("ix_subscription_plans_feature_key", table_name="subscription_plans")
    op.drop_index("ix_subscription_plans_plan_key", table_name="subscription_plans")
    op.drop_table("subscription_plans")
