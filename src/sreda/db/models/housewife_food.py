"""Housewife assistant — food/shopping domain models.

Five tables supporting:
  * ShoppingListItem — per-user list, one global per user, grouped by
    ``category`` in the UI.
  * Recipe + RecipeIngredient — recipe book with provenance (``source``
    enum: user_dictated / ai_generated / web_found / upgraded_from_menu).
  * MenuPlan + MenuPlanItem — weekly menu grids (21 cells: 7 days × 3
    meals), each cell points to a recipe or holds free_text.

Sensitive fields (item/recipe titles, instructions, free_text notes)
use ``EncryptedString`` — ciphertext at rest, plaintext via ORM. Short
enum-ish columns (status, category, meal_type) stay plaintext since
they're used in queries and filters.

Migration: ``20260421_0019_add_food_shopping_tables.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sreda.db.base import Base
from sreda.db.types import EncryptedString


# Category taxonomy for shopping list grouping. Small, fixed set — the
# LLM classifies new items into one of these on add. "другое" is the
# fall-through; the UI renders it at the bottom.
SHOPPING_CATEGORIES = (
    "молочные",
    "мясо_рыба",
    "овощи_фрукты",
    "хлеб",
    "бакалея",
    "напитки",
    "готовое",
    "замороженное",
    "бытовая_химия",
    "другое",
)

SHOPPING_STATUSES = ("pending", "bought", "cancelled")

# Recipe provenance — affects the source badge shown in Mini App.
RECIPE_SOURCES = (
    "user_dictated",      # 📝 user narrated it in chat
    "ai_generated",       # 🤖 LLM invented it
    "web_found",          # 🌐 scraped from a URL
    "upgraded_from_menu", # 📅 promoted from a free-text menu cell
)

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

MENU_PLAN_STATUSES = ("draft", "active", "archived")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ShoppingListItem
# ---------------------------------------------------------------------------


class ShoppingListItem(Base):
    """One row per item the user wants to buy.

    Lifecycle: ``pending`` → ``bought`` (checked off) or ``cancelled``
    (removed without buying). Mini App only shows ``pending``; the
    others stick around in the DB for history / analytics.
    """

    __tablename__ = "shopping_list_items"
    __table_args__ = (
        Index(
            "ix_shopping_list_items_tenant_user_status",
            "tenant_id",
            "user_id",
            "status",
        ),
        Index("ix_shopping_list_items_source_recipe", "source_recipe_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    # EncryptedString: "молоко", "батон нарезной"
    title: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    # Free-form: "2 шт", "500 г", "1 л". Plaintext — treated as opaque
    # label, no math on it in v1.
    quantity_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # When auto-added via generate_shopping_from_menu, points at the
    # recipe that owns this ingredient. NULL for manual additions.
    source_recipe_id: Mapped[str | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True
    )

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Recipe + Ingredients
# ---------------------------------------------------------------------------


class Recipe(Base):
    """One stored recipe. Ingredient rows in ``recipe_ingredients``
    (cascade-deleted with the parent).

    ``source`` tracks where the recipe came from so the Mini App can
    show a provenance badge (📝 you / 🤖 AI / 🌐 web / 📅 menu upgrade).
    """

    __tablename__ = "recipes"
    __table_args__ = (
        Index("ix_recipes_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )

    # EncryptedString: all three may contain identifying content.
    title: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    description: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    instructions_md: Mapped[str | None] = mapped_column(
        EncryptedString(), nullable=True
    )

    servings: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # Nutrition estimates per SERVING (not per whole recipe). LLM fills
    # at save_recipe time; all nullable so legacy/partial data survives.
    # Accuracy: LLM text-generation level, ~±20% typical — good enough
    # for a household planner, not a dietetics clinic.
    calories_per_serving: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    protein_per_serving: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    fat_per_serving: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    carbs_per_serving: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Populated when ``source == "web_found"``; fetch_url origin.
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON-encoded list[str] of tags. Plaintext — used in search.
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        "RecipeIngredient",
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeIngredient.sort_order",
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"
    __table_args__ = (
        Index("ix_recipe_ingredients_recipe", "recipe_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    )
    # EncryptedString: "картошка", "куриное филе".
    title: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    quantity_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    recipe: Mapped[Recipe] = relationship("Recipe", back_populates="ingredients")


# ---------------------------------------------------------------------------
# MenuPlan + MenuPlanItem
# ---------------------------------------------------------------------------


class MenuPlan(Base):
    """One weekly menu. ``week_start_date`` always a Monday (enforced
    in the service). Items in ``menu_plan_items`` — typically 21 rows
    (7 days × 3 meals) though breakfast/lunch/dinner can be skipped per
    day and ``snack`` is optional."""

    __tablename__ = "menu_plans"
    __table_args__ = (
        Index(
            "ix_menu_plans_tenant_user_week",
            "tenant_id",
            "user_id",
            "week_start_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    week_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    items: Mapped[list["MenuPlanItem"]] = relationship(
        "MenuPlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="(MenuPlanItem.day_of_week, MenuPlanItem.meal_type)",
    )


class MenuPlanItem(Base):
    """One cell in the weekly grid: (day, meal_type) → recipe or free_text.

    ``recipe_id`` and ``free_text`` are mutually exclusive in practice —
    the service enforces. A row with both NULL is a placeholder slot.
    """

    __tablename__ = "menu_plan_items"
    __table_args__ = (
        Index(
            "ix_menu_plan_items_plan_day",
            "menu_plan_id",
            "day_of_week",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    menu_plan_id: Mapped[str] = mapped_column(
        ForeignKey("menu_plans.id", ondelete="CASCADE"), nullable=False
    )
    # ISO weekday: 0=Monday, 6=Sunday.
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    meal_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # One of these two is typically set. Both NULL = placeholder.
    recipe_id: Mapped[str | None] = mapped_column(
        ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True
    )
    free_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)

    plan: Mapped[MenuPlan] = relationship("MenuPlan", back_populates="items")
    recipe: Mapped[Recipe | None] = relationship("Recipe", foreign_keys=[recipe_id])
