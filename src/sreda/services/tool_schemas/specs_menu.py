"""ToolSpec instances for the MENU family (Sub-A4 phase 4).

5 tools migrated: ``plan_week_menu``, ``update_menu_item``,
``list_menu``, ``generate_shopping_from_menu``, ``clear_menu``.

Sources of truth:
- Tool signatures: ``services/housewife_chat_tools.py:1224`` (plan),
  ``:1324`` (update), ``:1372`` (list), ``:1444`` (gen), ``:2239`` (clear).
- Output schemas: ``services/tool_schemas/housewife.py`` — Plan,
  Update (with cleared_or_not_found variant), List (with empty),
  GenerateShopping, ClearMenu.
- ID factories: ``housewife_menu.py:138`` (``menu_<24 hex>``),
  ``:164,182,243`` (``mpi_<24 hex>`` for cells).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import (
    MenuPlanId,
    RecipeId,
)
from sreda.services.tool_schemas.housewife import (
    ClearMenuOutput,
    GenerateShoppingFromMenuOutput,
    ListMenuOutput,
    PlanWeekMenuOutput,
    UpdateMenuItemOutput,
)


# ---------------------------------------------------------------------------
# Menu-specific aliases
# ---------------------------------------------------------------------------


WeekStartIso = Annotated[
    str,
    # YYYY-MM-DD shape. Service normalises ANY day-of-week in the
    # target week to its Monday, so we accept any ISO date and let
    # the runtime do the snap-to-Monday.
    StringConstraints(
        strip_whitespace=True,
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]
"""ISO calendar date ``YYYY-MM-DD``. ``plan_week_menu`` /
``update_menu_item`` / ``list_menu`` / ``clear_menu`` accept ANY day
within the target week — runtime snaps to Monday. We validate shape
at planner time; the snap-to-Monday stays runtime business."""


DayOfWeek = Annotated[int, Field(ge=0, le=6)]
"""0=Mon, 1=Tue, ..., 6=Sun (housewife_chat_tools.py:1267)."""


MealType = Literal["breakfast", "lunch", "dinner", "snack"]
"""4-meal grid per day. Runtime rejects unknown meal types."""


FreeText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Free-form meal description when there's no saved recipe to
reference. 500-char cap is generous («гречка с тушёнкой и салатом
из помидоров и огурцов») without ballooning the plan payload."""


CellNotes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Optional per-cell note. Short — it's a hint, not a recipe body."""


WeekNotes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Optional week-level note attached to the menu plan."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class MenuCellInput(BaseModel):
    """One cell in the weekly grid — meal type × day. EITHER
    ``recipe_id`` OR ``free_text``, not both (docstring contract at
    housewife_chat_tools.py:1268-1269 — runtime drops free_text when
    recipe_id is set, but we enforce exclusivity at planner time)."""

    model_config = ConfigDict(extra="forbid")
    recipe_id: RecipeId | None = None
    free_text: FreeText | None = None
    notes: CellNotes | None = None


class MenuDayInput(BaseModel):
    """One day in the week. ``day_of_week`` ∈ [0, 6]; ``meals`` maps
    meal_type → MenuCellInput. Empty meal keys allowed — omitting
    «snack» is the default."""

    model_config = ConfigDict(extra="forbid")
    day_of_week: DayOfWeek
    meals: dict[MealType, MenuCellInput] = Field(default_factory=dict)


class PlanWeekMenuInput(BaseModel):
    """Create (or REPLACE) the weekly menu.

    Heavy composite call — planner generates up to 28 cells (7×4) in
    one shot. Runtime overwrites any existing plan for ``week_start``
    completely (docstring contract).
    """

    model_config = ConfigDict(extra="forbid")
    week_start: WeekStartIso
    days: list[MenuDayInput] = Field(min_length=1, max_length=7)
    notes: WeekNotes | None = None


class UpdateMenuItemInput(BaseModel):
    """Point-edit a single cell. ``recipe_id`` / ``free_text`` both
    ``None`` clears the cell (housewife_chat_tools.py:1336)."""

    model_config = ConfigDict(extra="forbid")
    plan_id: MenuPlanId
    day_of_week: DayOfWeek
    meal_type: MealType
    recipe_id: RecipeId | None = None
    free_text: FreeText | None = None
    notes: CellNotes | None = None


class ListMenuInput(BaseModel):
    """Fetch the weekly menu. ``week_start`` optional — None returns
    the user's most recent menu across all weeks."""

    model_config = ConfigDict(extra="forbid")
    week_start: WeekStartIso | None = None


class GenerateShoppingFromMenuInput(BaseModel):
    """Pull all ingredients from a menu plan's recipes into the
    shopping list, scaled by family-eaters count."""

    model_config = ConfigDict(extra="forbid")
    plan_id: MenuPlanId


class ClearMenuInput(BaseModel):
    """Delete the weekly menu plan."""

    model_config = ConfigDict(extra="forbid")
    week_start: WeekStartIso


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


PLAN_WEEK_MENU_SPEC = ToolSpec(
    name="plan_week_menu",
    description=(
        "Создать (или ПЕРЕЗАПИСАТЬ) недельное меню юзера: 7 дней × до 4 "
        "приёмов пищи (breakfast/lunch/dinner/snack). ПЕРЕД вызовом "
        "поищи в книге рецептов чтобы ≥50% ячеек ссылались на recipe_id "
        "(а не free_text). Если уже есть план на week_start — полностью "
        "перезапишется. Для точечной правки одной ячейки — "
        "update_menu_item."
    ),
    family="menu",
    effect="write",
    read_domains=["recipes"],
    write_domains=["menu"],
    input_model=PlanWeekMenuInput,
    output_model=PlanWeekMenuOutput,
    trigger_examples=[
        "составь меню на неделю",
        "запиши меню на эту неделю",
        "запланируй что готовить на 7 дней",
        "сделай план питания на неделю",
    ],
    mutex_notes=[
        "ПЕРЕЗАПИСЫВАЕТ всю неделю. Для одной ячейки — update_menu_item, не plan_week_menu с одним днём.",
        "Перед вызовом — search_recipes('') чтобы видеть книгу и ссылаться на recipe_id.",
        "Для генерации списка покупок из меню — generate_shopping_from_menu (после plan_week_menu).",
    ],
    timeout_seconds=30,
    side_effect_class="transactional_write",
)


UPDATE_MENU_ITEM_SPEC = ToolSpec(
    name="update_menu_item",
    description=(
        "Обновить одну ячейку в существующем меню — точечная правка "
        "вида «замени ужин в среду на пасту». Если ячейки нет — "
        "создаст её. Оба recipe_id и free_text None очищают ячейку."
    ),
    family="menu",
    effect="write",
    read_domains=[],
    write_domains=["menu"],
    input_model=UpdateMenuItemInput,
    output_model=UpdateMenuItemOutput,
    trigger_examples=[
        "замени ужин в среду на пасту",
        "добавь перекус в пятницу",
        "убери завтрак во вторник",
        "поменяй обед в субботу",
    ],
    mutex_notes=[
        "Используй для ОДНОЙ ячейки. Для перепланирования всей недели — plan_week_menu (он ПЕРЕЗАПИСЫВАЕТ).",
        "plan_id берётся из list_menu или из ok:plan_created при создании.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


LIST_MENU_SPEC = ToolSpec(
    name="list_menu",
    description=(
        "Показать недельное меню юзера. Если week_start не указан — "
        "вернёт самый свежий план юзера. Возвращает плоский текст с "
        "menu_id и днями для дальнейшей точечной правки."
    ),
    family="menu",
    effect="read",
    read_domains=["menu"],
    write_domains=[],
    input_model=ListMenuInput,
    output_model=ListMenuOutput,
    trigger_examples=[
        "покажи моё меню на неделю",
        "что у меня в плане на неделю",
        "распиши меню",
        "какой у меня план питания",
    ],
    mutex_notes=[
        "Возвращает МЕНЮ, не книгу рецептов. Для рецептов — search_recipes из группы РЕЦЕПТЫ.",
    ],
    timeout_seconds=10,
    side_effect_class="read_only",
)


GENERATE_SHOPPING_FROM_MENU_SPEC = ToolSpec(
    name="generate_shopping_from_menu",
    description=(
        "Извлечь все ингредиенты рецептов недельного меню в список "
        "покупок, масштабированные под количество едоков семьи "
        "(eaters / recipe.servings). Использует source_recipe_id "
        "для будущей связки «купил для X»."
    ),
    family="menu",
    effect="write",
    read_domains=["menu", "recipes", "household"],
    write_domains=["shopping"],
    input_model=GenerateShoppingFromMenuInput,
    output_model=GenerateShoppingFromMenuOutput,
    trigger_examples=[
        "собери список покупок из меню",
        "добавь ингредиенты меню в покупки",
        "что покупать на неделю по меню",
        "сгенерируй покупки из плана меню",
    ],
    mutex_notes=[
        "Использует ТОЛЬКО рецепты с recipe_id в ячейках; free_text-ячейки игнорирует.",
        "Для добавления одной строки в покупки — add_shopping_items из группы ПОКУПКИ.",
    ],
    timeout_seconds=30,
    side_effect_class="transactional_write",
)


CLEAR_MENU_SPEC = ToolSpec(
    name="clear_menu",
    description=(
        "Удалить недельное меню юзера на указанную неделю. После "
        "list_menu вернёт пусто для этой недели. Используй когда "
        "юзер просит «убери меню», «отмени план на неделю»."
    ),
    family="menu",
    effect="write",
    read_domains=[],
    write_domains=["menu"],
    input_model=ClearMenuInput,
    output_model=ClearMenuOutput,
    trigger_examples=[
        "убери меню на эту неделю",
        "отмени план питания",
        "очисти меню",
        "удали недельный план",
    ],
    mutex_notes=[
        "Удаляет ВЕСЬ план недели. Для одной ячейки — update_menu_item с recipe_id=None+free_text=None (очищает ячейку).",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


MENU_SPECS: list[ToolSpec] = [
    PLAN_WEEK_MENU_SPEC,
    UPDATE_MENU_ITEM_SPEC,
    LIST_MENU_SPEC,
    GENERATE_SHOPPING_FROM_MENU_SPEC,
    CLEAR_MENU_SPEC,
]


__all__ = [
    "CLEAR_MENU_SPEC",
    "CellNotes",
    "ClearMenuInput",
    "DayOfWeek",
    "FreeText",
    "GENERATE_SHOPPING_FROM_MENU_SPEC",
    "GenerateShoppingFromMenuInput",
    "LIST_MENU_SPEC",
    "ListMenuInput",
    "MEAL_TYPE_LITERAL",
    "MENU_SPECS",
    "MealType",
    "MenuCellInput",
    "MenuDayInput",
    "PLAN_WEEK_MENU_SPEC",
    "PlanWeekMenuInput",
    "UPDATE_MENU_ITEM_SPEC",
    "UpdateMenuItemInput",
    "WeekNotes",
    "WeekStartIso",
]


# Re-export for tests/legibility; MealType is already a Literal[].
MEAL_TYPE_LITERAL = MealType
