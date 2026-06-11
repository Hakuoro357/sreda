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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import (
    IsoDateStr,
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


WeekStartIso = IsoDateStr
"""ISO calendar date ``YYYY-MM-DD`` for the target week. ``plan_week_menu``
/ ``update_menu_item`` / ``list_menu`` / ``clear_menu`` accept ANY
day within the target week — runtime ``_coerce_monday`` snaps to
Monday.

Codex Sub-A4 menu R1 MAJOR #6: previously a local regex-only alias
that accepted impossible dates like ``2026-02-31``. Now uses the
shared ``IsoDateStr`` from common.py which adds an
``AfterValidator(date.fromisoformat)`` to catch semantic errors at
planner-input time."""


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
    """One cell in the weekly grid — meal type × day.

    Codex Sub-A4 menu R1 MAJOR #4: EITHER ``recipe_id`` OR
    ``free_text``, NOT both. Runtime silently drops free_text when
    recipe_id is set (housewife_chat_tools.py:1268-1269) — the
    silent-drop hides the planner's confusion, so we reject at
    schema time. Both ``None`` is allowed (explicit clear; same
    semantics as ``UpdateMenuItemInput`` with both None).

    Codex Sub-A4 menu R2 MAJOR #4: ``notes`` is a per-cell hint
    that hangs off ``recipe_id``/``free_text`` — runtime stores it
    on the MenuPlanItem row and only when the cell has content.
    ``recipe_id=None + free_text=None + notes='X'`` would clear the
    cell AND silently drop the notes; the planner could think it
    added a note when it actually erased the meal. Reject the
    notes-only shape — planner must either attach notes to a
    recipe/free_text cell or use the (None, None, None) explicit-
    clear shape with no notes.
    """

    model_config = ConfigDict(extra="forbid")
    recipe_id: RecipeId | None = None
    free_text: FreeText | None = None
    notes: CellNotes | None = None

    @model_validator(mode="after")
    def _validate_recipe_xor_free_text(self) -> "MenuCellInput":
        if self.recipe_id is not None and self.free_text is not None:
            raise ValueError(
                "MenuCellInput accepts EITHER recipe_id OR free_text, "
                "not both. Runtime would silently drop free_text and "
                "the planner would think the free_text version was "
                "stored. Pick one and omit the other."
            )
        return self

    @model_validator(mode="after")
    def _reject_notes_only_cell(self) -> "MenuCellInput":
        if (
            self.recipe_id is None
            and self.free_text is None
            and self.notes is not None
        ):
            raise ValueError(
                "MenuCellInput rejects a notes-only cell "
                "(recipe_id=None + free_text=None + notes=...). The "
                "runtime treats (recipe_id=None, free_text=None) as "
                "an explicit clear and silently drops the notes — "
                "the planner could think it added a hint when it "
                "actually erased the meal. Either attach notes to "
                "a real recipe_id/free_text, or omit notes entirely "
                "for the clear case."
            )
        return self


class MenuDayInput(BaseModel):
    """One day in the week. ``day_of_week`` ∈ [0, 6]; ``meals`` maps
    meal_type → MenuCellInput. Empty meal keys allowed — omitting
    «snack» is the default."""

    model_config = ConfigDict(extra="forbid")
    day_of_week: DayOfWeek
    meals: dict[MealType, MenuCellInput] = Field(default_factory=dict)


class PlanWeekMenuInput(BaseModel):
    """Upsert the weekly menu — preserve-merge semantics.

    Codex Sub-A4 menu R1 MAJOR #3: the chat_tools docstring said
    «ПОЛНОСТЬЮ ПЕРЕЗАПИСЫВАЕТ» but the underlying service
    (``HousewifeMenuService.plan_week`` at housewife_menu.py:105-122)
    actually preserves unspecified slots — partial payloads only
    overwrite the slots they mention. Aligned wording with runtime.

    Codex Sub-A4 menu R1 MAJOR #5: ``day_of_week`` must be unique
    across the days list — duplicates would create double
    MenuPlanItems (no DB unique constraint on (plan, day, meal)).
    """

    model_config = ConfigDict(extra="forbid")
    week_start: WeekStartIso
    days: list[MenuDayInput] = Field(min_length=1, max_length=7)
    notes: WeekNotes | None = None

    @model_validator(mode="after")
    def _validate_unique_day_of_week(self) -> "PlanWeekMenuInput":
        seen: dict[int, int] = {}
        for idx, day in enumerate(self.days):
            if day.day_of_week in seen:
                first = seen[day.day_of_week]
                raise ValueError(
                    f"days[{idx}].day_of_week={day.day_of_week} duplicates "
                    f"days[{first}]. Each day of the week may appear at "
                    f"most once — runtime would create duplicate "
                    f"MenuPlanItems and break list/render."
                )
            seen[day.day_of_week] = idx
        return self


class UpdateMenuItemInput(BaseModel):
    """Point-edit a single cell.

    Codex Sub-A4 menu R1 MAJOR #4: same recipe_id XOR free_text
    rule as ``MenuCellInput``. Both ``None`` clears the cell
    (housewife_chat_tools.py:1336) — that's the only legit «both
    absent» case.

    Codex Sub-A4 menu R2 MAJOR #4: same notes-only rejection as
    ``MenuCellInput`` — (None, None, notes) is the silent-drop
    trap. Either notes hang off a real cell, or the cell is being
    cleared with no notes.
    """

    model_config = ConfigDict(extra="forbid")
    plan_id: MenuPlanId
    day_of_week: DayOfWeek
    meal_type: MealType
    recipe_id: RecipeId | None = None
    free_text: FreeText | None = None
    notes: CellNotes | None = None

    @model_validator(mode="after")
    def _validate_recipe_xor_free_text(self) -> "UpdateMenuItemInput":
        if self.recipe_id is not None and self.free_text is not None:
            raise ValueError(
                "UpdateMenuItemInput accepts EITHER recipe_id OR "
                "free_text, not both. Runtime would silently drop "
                "free_text. Pick one (or both None to clear the cell)."
            )
        return self

    @model_validator(mode="after")
    def _reject_notes_only_update(self) -> "UpdateMenuItemInput":
        if (
            self.recipe_id is None
            and self.free_text is None
            and self.notes is not None
        ):
            raise ValueError(
                "UpdateMenuItemInput rejects a notes-only update "
                "(recipe_id=None + free_text=None + notes=...). The "
                "runtime treats (recipe_id=None, free_text=None) as "
                "an explicit clear of the cell and silently drops "
                "the notes — the planner could think it added a "
                "hint when it actually erased the meal. Either "
                "attach notes to a real recipe_id/free_text, or "
                "omit notes for the clear case."
            )
        return self


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
        "Создать или ОБНОВИТЬ недельное меню юзера: 7 дней × до 4 "
        "приёмов пищи (breakfast/lunch/dinner/snack). PRESERVE-MERGE: "
        "ячейки, которые ты ПЕРЕДАЁШЬ, переписываются; ячейки, которые "
        "ты НЕ передаёшь, остаются как были. Для полной очистки старого "
        "меню сначала clear_menu, потом plan_week_menu. ПЕРЕД вызовом "
        "поищи в книге рецептов чтобы ≥50% ячеек ссылались на recipe_id."
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
        "PRESERVE-MERGE: переданные ячейки перезаписываются, остальные сохраняются. Для одной ячейки — update_menu_item. Для полной очистки — clear_menu, затем заново plan_week_menu.",
        "Перед вызовом — search_recipes('') чтобы видеть книгу и ссылаться на recipe_id.",
        "Для генерации покупок из меню — generate_shopping_from_menu.",
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
        "Используй для ОДНОЙ ячейки. Для пере-планирования сразу нескольких ячеек — plan_week_menu (он preserve-merge, не перезаписывает неотправленные слоты).",
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
    # #128: ужато без потери исходов
    description=(
        "Извлечь ингредиенты рецептов недельного меню в покупки, "
        "масштабируя под едоков (eaters / recipe.servings). Исходы: "
        "(1) ok:generated:N:eaters=E; (2) ok:plan_no_recipes — все "
        "ячейки free_text («в меню нет сохранённых рецептов»); "
        "(3) error:plan_not_found — нет такого плана у юзера "
        "(предложи list_menu)."
    ),
    family="menu",
    effect="write",
    read_domains=["menu", "recipes", "household"],
    write_domains=["shopping"],
    input_model=GenerateShoppingFromMenuInput,
    output_model=GenerateShoppingFromMenuOutput,
    # #29: "plan_no_recipes" (menu has no recipes) generates nothing → excluded.
    # NB: "generated" still over-reports the generated_count=0 sub-case (menu
    # has recipes but yielded 0 new shopping rows) — that's the SAFE direction
    # (false positive → extra recovery verification) and isn't expressible with
    # status-only metadata (would need count-aware classification).
    committed_statuses=frozenset({"generated"}),
    trigger_examples=[
        "собери список покупок из меню",
        "добавь ингредиенты меню в покупки",
        "что покупать на неделю по меню",
        "сгенерируй покупки из плана меню",
    ],
    mutex_notes=[
        "Использует ТОЛЬКО рецепты с recipe_id в ячейках; free_text-ячейки игнорирует.",
        "Три исхода: ok:generated:N:eaters=E (норма) / ok:plan_no_recipes (план free_text-only) / error:plan_not_found (нет такого плана).",
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
