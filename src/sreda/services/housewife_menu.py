"""Weekly menu planning — MenuPlan + MenuPlanItem CRUD.

One plan per ISO week (Monday-anchored), one item per (day × meal).
The LLM composes the whole grid in a single ``plan_week_menu`` call;
the service just persists what it gets, enforces tenant/user scoping,
and exposes a couple of niche helpers for auto-gen and cell-updates.

Key design choices:

* **Week anchor is a Monday.** The service coerces whatever date the
  LLM passed into the start of its ISO week. Callers don't have to
  worry about off-by-one timezone issues.
* **Duplicate plans for the same week** are replaced — the user saying
  "составь меню заново" shouldn't leave the old grid hanging. The
  service deletes and re-creates atomically.
* **recipe_id vs free_text are mutually exclusive** in practice but
  NOT in schema — we accept rows with both null (placeholder) and
  rows with one or the other. The service silently drops items that
  have neither.
* **Ingredient aggregation** (``aggregate_ingredients_for_shopping``)
  preserves per-recipe attribution via ``source_recipe_id`` so the
  shopping list can show "куплено для борща" in future UX.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from sreda.db.models.housewife_food import (
    MEAL_TYPES,
    MenuPlan,
    MenuPlanItem,
    Recipe,
    RecipeIngredient,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_monday(d: date | str) -> date:
    """Normalise any date (or ISO string) to the Monday of its ISO week.

    ``d.weekday()`` — 0=Monday … 6=Sunday. Always subtract back to Monday."""
    if isinstance(d, str):
        parsed = date.fromisoformat(d)
    else:
        parsed = d
    return parsed - timedelta(days=parsed.weekday())


@dataclass(slots=True)
class MenuCellInput:
    """Normalised single-cell payload. Either ``recipe_id`` xor
    ``free_text`` gets stored; both None → slot skipped."""

    day_of_week: int  # 0=Mon … 6=Sun
    meal_type: str    # breakfast | lunch | dinner | snack
    recipe_id: str | None = None
    free_text: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class AggregatedIngredient:
    """Shape returned by ``aggregate_ingredients_for_shopping``."""

    title: str
    quantity_text: str | None
    is_optional: bool
    source_recipe_id: str


class HousewifeMenuService:
    """Weekly menu CRUD, scoped by (tenant, user)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def plan_week(
        self,
        *,
        tenant_id: str,
        user_id: str,
        week_start: date | str,
        cells: list[MenuCellInput] | list[dict[str, Any]],
        notes: str | None = None,
    ) -> MenuPlan:
        """Create (or replace) the menu for the given ISO week.

        Idempotent by ``(tenant, user, week_start)``: a prior plan for
        the same week gets deleted, then a fresh one is inserted with
        the new cells. Callers don't need to call ``clear_menu`` first.
        """
        start = _coerce_monday(week_start)
        normalised_cells = _normalise_cells(cells)

        # Delete any prior plan for this week — cascade wipes items.
        for prior in self._find_plans(tenant_id, user_id, start):
            self.session.delete(prior)

        plan = MenuPlan(
            id=f"menu_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            week_start_date=start,
            notes=notes,
            status="active",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.session.add(plan)
        self.session.flush()

        for cell in normalised_cells:
            # Slot with nothing in it → skip. We don't insert placeholder
            # rows; the UI renders empty grid cells for missing (day,
            # meal_type) combinations.
            if cell.recipe_id is None and not (cell.free_text or "").strip():
                continue
            self.session.add(
                MenuPlanItem(
                    id=f"mpi_{uuid4().hex[:24]}",
                    menu_plan_id=plan.id,
                    day_of_week=cell.day_of_week,
                    meal_type=cell.meal_type,
                    recipe_id=cell.recipe_id,
                    free_text=(cell.free_text or None),
                    notes=(cell.notes or None),
                )
            )

        self.session.commit()
        return plan

    def update_item(
        self,
        *,
        tenant_id: str,
        user_id: str,
        plan_id: str,
        day_of_week: int,
        meal_type: str,
        recipe_id: str | None = None,
        free_text: str | None = None,
        notes: str | None = None,
    ) -> MenuPlanItem | None:
        """Replace one cell in a menu. Creates the item if no row
        currently exists at that (day, meal_type); otherwise overwrites
        recipe_id / free_text / notes in place.

        Returns the stored item, or None if the plan wasn't found /
        cross-tenant. Passing all three nullable fields as None/empty
        effectively deletes the cell (no insert).
        """
        plan = self._get_plan(tenant_id, user_id, plan_id)
        if plan is None:
            return None
        if meal_type not in MEAL_TYPES:
            raise ValueError(f"unknown meal_type: {meal_type!r}")
        if not 0 <= day_of_week <= 6:
            raise ValueError(f"day_of_week must be 0-6, got {day_of_week}")

        existing = (
            self.session.query(MenuPlanItem)
            .filter(
                MenuPlanItem.menu_plan_id == plan.id,
                MenuPlanItem.day_of_week == day_of_week,
                MenuPlanItem.meal_type == meal_type,
            )
            .one_or_none()
        )

        text_empty = not (free_text or "").strip()
        if recipe_id is None and text_empty:
            # Clearing the cell.
            if existing is not None:
                self.session.delete(existing)
                self.session.commit()
            return None

        if existing is None:
            item = MenuPlanItem(
                id=f"mpi_{uuid4().hex[:24]}",
                menu_plan_id=plan.id,
                day_of_week=day_of_week,
                meal_type=meal_type,
                recipe_id=recipe_id,
                free_text=(free_text or None),
                notes=(notes or None),
            )
            self.session.add(item)
        else:
            existing.recipe_id = recipe_id
            existing.free_text = (free_text or None)
            existing.notes = (notes or None)
            item = existing
        plan.updated_at = _utcnow()
        self.session.commit()
        return item

    def clear_menu(
        self, *, tenant_id: str, user_id: str, week_start: date | str
    ) -> int:
        """Delete all plans for (user, week). Returns count removed."""
        start = _coerce_monday(week_start)
        count = 0
        for plan in self._find_plans(tenant_id, user_id, start):
            self.session.delete(plan)
            count += 1
        if count:
            self.session.commit()
        return count

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_plan_for_week(
        self, *, tenant_id: str, user_id: str, week_start: date | str
    ) -> MenuPlan | None:
        """Fetch a specific week's plan with items + linked recipes
        eagerly loaded. ``None`` if no plan for that week yet."""
        start = _coerce_monday(week_start)
        return (
            self.session.query(MenuPlan)
            .options(
                joinedload(MenuPlan.items).joinedload(MenuPlanItem.recipe)
            )
            .filter(
                MenuPlan.tenant_id == tenant_id,
                MenuPlan.user_id == user_id,
                MenuPlan.week_start_date == start,
            )
            .order_by(MenuPlan.created_at.desc())
            .first()
        )

    def list_user_plans(
        self, *, tenant_id: str, user_id: str
    ) -> list[MenuPlan]:
        """Most recent plans first. Doesn't eager-load items — keep it
        cheap; the list view only needs week_start + created_at."""
        return (
            self.session.query(MenuPlan)
            .filter(
                MenuPlan.tenant_id == tenant_id,
                MenuPlan.user_id == user_id,
            )
            .order_by(MenuPlan.week_start_date.desc())
            .all()
        )

    # ------------------------------------------------------------------
    # Aggregation — used by Stage 6 auto-gen
    # ------------------------------------------------------------------

    def aggregate_ingredients_for_day(
        self,
        *,
        tenant_id: str,
        user_id: str,
        plan_id: str,
        day_of_week: int,
        eaters_count: int = 1,
    ) -> list[AggregatedIngredient]:
        """Same as ``aggregate_ingredients_for_shopping`` but restricted
        to a single day of the plan. Backs the per-day "В список
        покупок" button on the Mini App menu screen — sometimes the
        user only wants shopping for today/tomorrow instead of the
        whole week.
        """
        if not 0 <= int(day_of_week) <= 6:
            raise ValueError(f"day_of_week must be 0-6, got {day_of_week}")
        plan = self._get_plan(tenant_id, user_id, plan_id)
        if plan is None:
            return []

        recipe_ids = {
            item.recipe_id
            for item in plan.items
            if item.recipe_id is not None and item.day_of_week == day_of_week
        }
        if not recipe_ids:
            return []

        rows = (
            self.session.query(RecipeIngredient, Recipe)
            .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
            .filter(
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
                Recipe.id.in_(recipe_ids),
            )
            .order_by(Recipe.id, RecipeIngredient.sort_order)
            .all()
        )

        eaters = max(1, int(eaters_count or 1))
        out: list[AggregatedIngredient] = []
        for ing, recipe in rows:
            servings = max(1, int(recipe.servings or 1))
            factor = max(1, math.ceil(eaters / servings))
            out.append(
                AggregatedIngredient(
                    title=ing.title,
                    quantity_text=_scale_quantity(ing.quantity_text, factor),
                    is_optional=ing.is_optional,
                    source_recipe_id=recipe.id,
                )
            )
        return out

    def aggregate_ingredients_for_shopping(
        self,
        *,
        tenant_id: str,
        user_id: str,
        plan_id: str,
        eaters_count: int = 1,
    ) -> list[AggregatedIngredient]:
        """Flatten all ingredients from recipes referenced by a plan's
        items. One output row per (recipe × ingredient) — no dedup
        (semantic "молоко 500мл + молоко 200мл = 700мл" math is v2+).

        ``eaters_count`` scales quantities by
        ``factor = ceil(eaters / recipe.servings)``. So a 2-serving
        recipe with a family of 4 cooks twice → ingredients × 2. Pass
        ``HousewifeFamilyService.count_eaters(...)`` to get the right
        number. Default 1 keeps the old behaviour (no scaling) for
        callers that don't know about family.

        Numeric quantities get parsed + multiplied ("500 г" → "1000 г").
        Non-numeric or unparseable quantities ("по вкусу", "2-3 шт")
        get an ``×N`` prefix when factor > 1, so the user sees what
        was supposed to scale.

        Free-text menu cells contribute nothing — they have no
        structured ingredients.

        Cross-tenant safe: if the plan isn't owned by (tenant, user),
        returns an empty list.
        """
        plan = self._get_plan(tenant_id, user_id, plan_id)
        if plan is None:
            return []

        # Collect distinct recipe ids referenced by this plan's items.
        recipe_ids = {
            item.recipe_id
            for item in plan.items
            if item.recipe_id is not None
        }
        if not recipe_ids:
            return []

        rows = (
            self.session.query(RecipeIngredient, Recipe)
            .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
            .filter(
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
                Recipe.id.in_(recipe_ids),
            )
            .order_by(Recipe.id, RecipeIngredient.sort_order)
            .all()
        )

        eaters = max(1, int(eaters_count or 1))
        out: list[AggregatedIngredient] = []
        for ing, recipe in rows:
            servings = max(1, int(recipe.servings or 1))
            factor = max(1, math.ceil(eaters / servings))
            out.append(
                AggregatedIngredient(
                    title=ing.title,
                    quantity_text=_scale_quantity(ing.quantity_text, factor),
                    is_optional=ing.is_optional,
                    source_recipe_id=recipe.id,
                )
            )
        return out

    def get_cell(
        self,
        *,
        tenant_id: str,
        user_id: str,
        plan_id: str,
        day_of_week: int,
        meal_type: str,
    ) -> MenuPlanItem | None:
        """Fetch one cell by (plan, day, meal_type). Cross-tenant safe —
        returns None if plan isn't owned."""
        plan = self._get_plan(tenant_id, user_id, plan_id)
        if plan is None:
            return None
        for item in plan.items:
            if item.day_of_week == day_of_week and item.meal_type == meal_type:
                return item
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_plans(
        self, tenant_id: str, user_id: str, start: date
    ) -> list[MenuPlan]:
        return (
            self.session.query(MenuPlan)
            .filter(
                MenuPlan.tenant_id == tenant_id,
                MenuPlan.user_id == user_id,
                MenuPlan.week_start_date == start,
            )
            .all()
        )

    def _get_plan(
        self, tenant_id: str, user_id: str, plan_id: str
    ) -> MenuPlan | None:
        return (
            self.session.query(MenuPlan)
            .options(
                joinedload(MenuPlan.items).joinedload(MenuPlanItem.recipe)
            )
            .filter(
                MenuPlan.id == plan_id,
                MenuPlan.tenant_id == tenant_id,
                MenuPlan.user_id == user_id,
            )
            .one_or_none()
        )


def _normalise_cells(
    raw: Iterable[MenuCellInput | dict[str, Any]] | None,
) -> list[MenuCellInput]:
    if not raw:
        return []
    out: list[MenuCellInput] = []
    for item in raw:
        if isinstance(item, MenuCellInput):
            out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        try:
            day = int(item["day_of_week"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= day <= 6:
            continue
        meal = str(item.get("meal_type") or "").strip()
        if meal not in MEAL_TYPES:
            continue
        out.append(
            MenuCellInput(
                day_of_week=day,
                meal_type=meal,
                recipe_id=(item.get("recipe_id") or None),
                free_text=(item.get("free_text") or None),
                notes=(item.get("notes") or None),
            )
        )
    return out


# Matches a quantity starting with a number (int / decimal using . or ,)
# optionally followed by a simple unit like "г", "мл", "шт", "кг".
# Ranges ("2-3"), fractions ("1/2") and free text ("по вкусу") do not
# match — they fall through to the ``×N`` prefix branch.
_QTY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([A-Za-zА-Яа-яЁё\s.]*?)\s*$")


def _scale_quantity(text: str | None, factor: int) -> str | None:
    """Multiply a free-form quantity string by ``factor``.

    "500 г" + factor=2 → "1000 г". "0.5 л" + factor=3 → "1.5 л"
    (comma decimals normalised). Unparseable quantities like
    "по вкусу" or "2-3 шт" get an ``×N `` prefix when factor > 1 so
    the user sees that they were meant to scale; factor == 1 returns
    the input verbatim.

    ``None`` quantity_text passes through unchanged — ingredient
    rows sometimes have no quantity at all.
    """
    if text is None:
        return None
    if factor <= 1:
        return text
    match = _QTY_RE.match(text)
    if match is None:
        return f"×{factor} {text}"
    num_raw, unit_raw = match.group(1), (match.group(2) or "").strip()
    try:
        num = float(num_raw.replace(",", "."))
    except ValueError:
        return f"×{factor} {text}"
    scaled = num * factor
    if scaled.is_integer():
        num_out = str(int(scaled))
    else:
        num_out = f"{scaled:.3f}".rstrip("0").rstrip(".")
    return f"{num_out} {unit_raw}".strip() if unit_raw else num_out
