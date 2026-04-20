"""add food/shopping tables for housewife v1.1

Five tables supporting the housewife "food" block:

  * ``shopping_list_items`` — per-user shopping list (one global list,
    grouped visually by ``category`` in the Mini App).
  * ``recipes`` — recipe book rows with provenance via ``source``
    (user_dictated / ai_generated / web_found / upgraded_from_menu).
  * ``recipe_ingredients`` — per-recipe ingredient rows (FK).
  * ``menu_plans`` — weekly menu container (one per week_start_date
    per user).
  * ``menu_plan_items`` — 21 cells per plan (7 days × 3 meals by
    default), pointing to a recipe_id or free_text.

No subscription_plans seed here — the housewife plan is already active
from migration 0017.

Revision ID: 20260421_0019
Revises: 20260418_0018
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0019"
down_revision = "20260418_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- shopping_list_items ----------
    op.create_table(
        "shopping_list_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # title is EncryptedString at the ORM layer (AES-GCM envelope
        # stored here as Text). See db/types.py.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("quantity_text", sa.String(length=64), nullable=True),
        # enum-like short string. See housewife_food module for values.
        sa.Column("category", sa.String(length=32), nullable=False),
        # pending | bought | cancelled
        sa.Column("status", sa.String(length=16), nullable=False),
        # When this item was auto-added from a menu's recipe (aggregation).
        # NULL for manually added items.
        sa.Column(
            "source_recipe_id",
            sa.String(length=64),
            sa.ForeignKey("recipes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_shopping_list_items_tenant_user_status",
        "shopping_list_items",
        ["tenant_id", "user_id", "status"],
    )
    op.create_index(
        "ix_shopping_list_items_source_recipe",
        "shopping_list_items",
        ["source_recipe_id"],
    )

    # ---------- recipes ----------
    op.create_table(
        "recipes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # Encrypted at ORM layer: title, description, instructions_md.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions_md", sa.Text(), nullable=True),
        sa.Column("servings", sa.Integer(), nullable=False),
        # user_dictated | ai_generated | web_found | upgraded_from_menu
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        # JSON-encoded list[str] of short tags ("суп", "быстрое").
        sa.Column("tags_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_recipes_tenant_user", "recipes", ["tenant_id", "user_id"]
    )

    # ---------- recipe_ingredients ----------
    op.create_table(
        "recipe_ingredients",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "recipe_id",
            sa.String(length=64),
            sa.ForeignKey("recipes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Encrypted at ORM layer: title.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("quantity_text", sa.String(length=64), nullable=True),
        sa.Column("is_optional", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_recipe_ingredients_recipe", "recipe_ingredients", ["recipe_id"]
    )

    # ---------- menu_plans ----------
    op.create_table(
        "menu_plans",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # Always a Monday (ISO week-start). Service enforces.
        sa.Column("week_start_date", sa.Date(), nullable=False),
        # Encrypted at ORM layer.
        sa.Column("notes", sa.Text(), nullable=True),
        # draft | active | archived
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_menu_plans_tenant_user_week",
        "menu_plans",
        ["tenant_id", "user_id", "week_start_date"],
    )

    # ---------- menu_plan_items ----------
    op.create_table(
        "menu_plan_items",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "menu_plan_id",
            sa.String(length=64),
            sa.ForeignKey("menu_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ISO weekday: 0=Monday … 6=Sunday.
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        # breakfast | lunch | dinner | snack
        sa.Column("meal_type", sa.String(length=16), nullable=False),
        # Exactly one of recipe_id / free_text is expected to be set;
        # service-level validation. Both NULL = placeholder slot.
        sa.Column(
            "recipe_id",
            sa.String(length=64),
            sa.ForeignKey("recipes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Encrypted at ORM layer: free_text + notes.
        sa.Column("free_text", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_menu_plan_items_plan_day",
        "menu_plan_items",
        ["menu_plan_id", "day_of_week"],
    )


def downgrade() -> None:
    op.drop_index("ix_menu_plan_items_plan_day", table_name="menu_plan_items")
    op.drop_table("menu_plan_items")

    op.drop_index("ix_menu_plans_tenant_user_week", table_name="menu_plans")
    op.drop_table("menu_plans")

    op.drop_index("ix_recipe_ingredients_recipe", table_name="recipe_ingredients")
    op.drop_table("recipe_ingredients")

    op.drop_index("ix_recipes_tenant_user", table_name="recipes")
    op.drop_table("recipes")

    op.drop_index(
        "ix_shopping_list_items_source_recipe", table_name="shopping_list_items"
    )
    op.drop_index(
        "ix_shopping_list_items_tenant_user_status", table_name="shopping_list_items"
    )
    op.drop_table("shopping_list_items")
