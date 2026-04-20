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

    def aggregate_ingredients_for_shopping(
        self, *, tenant_id: str, user_id: str, plan_id: str
    ) -> list[AggregatedIngredient]:
        """Flatten all ingredients from all recipes referenced by a
        plan's items. One output row per (recipe × ingredient) pair —
        no dedup, no scaling; the user (or a later enhancement) handles
        "нужно 3 л молока всего" math themselves.

        Free-text cells contribute nothing — they have no structured
        ingredients. The user can manually add stuff for those.

        Cross-tenant safe: if the plan isn't owned by (tenant, user),
        returns empty list.
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
            self.session.query(RecipeIngredient, Recipe.id.label("rid"))
            .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
            .filter(
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
                Recipe.id.in_(recipe_ids),
            )
            .order_by(Recipe.id, RecipeIngredient.sort_order)
            .all()
        )

        out: list[AggregatedIngredient] = []
        for ing, rid in rows:
            out.append(
                AggregatedIngredient(
                    title=ing.title,
                    quantity_text=ing.quantity_text,
                    is_optional=ing.is_optional,
                    source_recipe_id=rid,
                )
            )
        return out

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
