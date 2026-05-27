"""ToolSpec instances for the RECIPES family (Sub-A4 phase 3).

5 tools migrated: ``save_recipe``, ``save_recipes_batch``,
``search_recipes``, ``get_recipe``, ``delete_recipe``.

``get_recipe_any_source`` is listed in TOOL_FAMILY_MANIFEST as a
future tool (architecture map TODO-2) but the runtime function does
not exist yet — it ships in a later sub-issue. The manifest cross-
check test for this family explicitly excludes it.

Sources of truth:
- Tool signatures: ``services/housewife_chat_tools.py:921`` (save),
  ``:1030`` (batch), ``:1102`` (search), ``:1156`` (get), ``:1193`` (delete).
- Output schemas: ``services/tool_schemas/housewife.py``
  ``SaveRecipeOutput`` / ``SaveRecipesBatchOutput`` /
  ``SearchRecipesOutput`` / ``GetRecipeOutput`` (already existed) /
  ``DeleteRecipeOutput``.
- ID factory: ``housewife_recipes.py:217,353`` —
  ``f"rec_{uuid4().hex[:24]}"``.
- Recipe-not-found stable code: ``_STABLE_ERROR_PATTERNS`` in
  housewife.py (added recipe-pattern alongside item/task/reminder/
  checklist).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.common import (
    NonBlankStr,
    RecipeId,
    ShortStr,
)
from sreda.services.tool_schemas.housewife import (
    DeleteRecipeOutput,
    GetRecipeOutput,
    SaveRecipeOutput,
    SaveRecipesBatchOutput,
    SearchRecipesOutput,
)


# ---------------------------------------------------------------------------
# Recipes-specific aliases — caps from the docstring contracts in
# housewife_chat_tools.py and the recipe_service implementation.
# ---------------------------------------------------------------------------


RecipeTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Recipe title. Service-layer caps via SQLAlchemy column length; the
200-char cap matches the «short name» convention in the docstring
contract and prevents accidentally serializing a paragraph as a title."""


InstructionsMd = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8000),
]
"""Recipe instructions in markdown. 8000-char cap is generous (a
multi-step recipe rarely exceeds 2000) but bounds the prompt size for
``save_recipes_batch`` calls that send many recipes."""


RecipeSource = Literal[
    "user_dictated", "ai_generated", "web_found", "upgraded_from_menu"
]
"""Origin badge — see ``housewife_chat_tools.py:979-985``. Runtime
silently skips items with unknown ``source`` in batch saves; we reject
at schema time."""


SearchQuery = Annotated[
    str,
    # ``search_recipes`` accepts an empty query (returns everything).
    # No min_length. Cap at 200 to bound payload.
    StringConstraints(strip_whitespace=True, max_length=200),
]


SourceUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=4, max_length=2048),
]
"""HTTPS source URL when source == 'web_found'. Cap at 2048 (typical
browser URL limit). Format not validated at schema — runtime does its
own httpx check."""


TagsList = Annotated[
    list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]],
    Field(max_length=10),
]
"""Up to 10 short tags per recipe. Each tag ≤40 chars non-blank."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class RecipeIngredientInput(BaseModel):
    """One ingredient row inside ``save_recipe.ingredients``."""

    model_config = ConfigDict(extra="forbid")
    title: ShortStr
    quantity_text: NonBlankStr | None = None
    is_optional: bool | None = None


class SaveRecipeInput(BaseModel):
    """Save a single recipe. Field caps per ``housewife_chat_tools.py:921``."""

    model_config = ConfigDict(extra="forbid")
    title: RecipeTitle
    ingredients: list[RecipeIngredientInput] = Field(min_length=1, max_length=100)
    instructions_md: InstructionsMd
    servings: int = Field(ge=1, le=100)
    source: RecipeSource
    source_url: SourceUrl | None = None
    tags: TagsList | None = None
    cooking_time_minutes: int | None = Field(default=None, ge=1, le=600)
    calories_per_serving: float | None = Field(default=None, ge=0, le=10000)
    protein_per_serving: float | None = Field(default=None, ge=0, le=1000)
    fat_per_serving: float | None = Field(default=None, ge=0, le=1000)
    carbs_per_serving: float | None = Field(default=None, ge=0, le=1000)


class SaveRecipesBatchInput(BaseModel):
    """Batch-save N recipes."""

    model_config = ConfigDict(extra="forbid")
    recipes: list[SaveRecipeInput] = Field(min_length=1, max_length=50)
    """Up to 50 recipes per batch — keeps the LLM tool-call payload
    bounded; for «save my whole book» flows the planner can chunk."""


class SearchRecipesInput(BaseModel):
    """Search the user's recipe book.

    Empty query returns ALL recipes (housewife_chat_tools.py:1116).
    Schema accepts empty string explicitly."""

    model_config = ConfigDict(extra="forbid")
    query: SearchQuery


class GetRecipeInput(BaseModel):
    """Get full recipe details by id."""

    model_config = ConfigDict(extra="forbid")
    recipe_id: RecipeId


class DeleteRecipeInput(BaseModel):
    """Delete a recipe by id. Cascades to ingredients."""

    model_config = ConfigDict(extra="forbid")
    recipe_id: RecipeId


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


SAVE_RECIPE_SPEC = ToolSpec(
    name="save_recipe",
    description=(
        "Сохранить ОДИН рецепт в книгу юзера. Для нескольких рецептов в "
        "одном ходе — save_recipes_batch (бережёт tool-budget). Title "
        "уникален per-user (case+whitespace-insensitive) — дубликаты "
        "возвращают ok:duplicate без вставки."
    ),
    family="recipes",
    effect="write",
    read_domains=[],
    write_domains=["recipes"],
    input_model=SaveRecipeInput,
    output_model=SaveRecipeOutput,
    trigger_examples=[
        "сохрани рецепт борща",
        "запиши этот рецепт в книгу",
        "добавь в книгу рецепт сырников",
        "сохрани вот этот рецепт",
    ],
    mutex_notes=[
        "Для batch-сохранения (>1 рецепт за раз) — save_recipes_batch, не save_recipe в цикле.",
        "Для редактирования существующего рецепта — delete_recipe + save_recipe (нет отдельного update).",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


SAVE_RECIPES_BATCH_SPEC = ToolSpec(
    name="save_recipes_batch",
    description=(
        "Сохранить НЕСКОЛЬКО рецептов одним вызовом. Используй когда "
        "юзер просит «запиши N рецептов», или когда после планирования "
        "меню (группа МЕНЮ) свободные блюда стоит превратить в "
        "структурированные рецепты. Дедупликация per-title."
    ),
    family="recipes",
    effect="write",
    read_domains=[],
    write_domains=["recipes"],
    input_model=SaveRecipesBatchInput,
    output_model=SaveRecipesBatchOutput,
    trigger_examples=[
        "сохрани 10 рецептов",
        "запиши все эти рецепты в книгу",
        "добавь рецепты в книгу батчем",
        "запиши сразу несколько рецептов",
    ],
    mutex_notes=[
        "Используй ТОЛЬКО для нескольких рецептов. Для одного — save_recipe.",
        "Перед батч-сохранением полезно search_recipes('') чтобы увидеть что уже в книге и не плодить дубли.",
    ],
    timeout_seconds=30,
    side_effect_class="transactional_write",
)


SEARCH_RECIPES_SPEC = ToolSpec(
    name="search_recipes",
    description=(
        "Найти рецепты в книге юзера по title или тегу. Возвращает ВСЮ "
        "книгу (не меню) — для меню используй группу МЕНЮ. Пустой "
        "query возвращает все рецепты в reverse-chronological order. "
        "Полезно перед планированием меню (группа МЕНЮ) чтобы видеть "
        "что уже есть."
    ),
    family="recipes",
    effect="read",
    read_domains=["recipes"],
    write_domains=[],
    input_model=SearchRecipesInput,
    output_model=SearchRecipesOutput,
    trigger_examples=[
        "найди рецепт борща",
        "покажи мои рецепты",
        "какие у меня есть супы в книге",
        "что в книге рецептов",
    ],
    mutex_notes=[
        "Возвращает книгу, НЕ меню. Для проверки «что в меню на день» — см. группу МЕНЮ.",
    ],
    timeout_seconds=10,
    side_effect_class="read_only",
)


GET_RECIPE_SPEC = ToolSpec(
    name="get_recipe",
    description=(
        "Получить полный текст рецепта по id из книги юзера: "
        "ингредиенты, шаги, порции. Используй когда нужны детали "
        "конкретного сохранённого рецепта (например, после "
        "search_recipes)."
    ),
    family="recipes",
    effect="read",
    read_domains=["recipes"],
    write_domains=[],
    input_model=GetRecipeInput,
    output_model=GetRecipeOutput,
    trigger_examples=[
        "покажи рецепт борща полностью",
        "как готовить вот этот рецепт",
        "дай мне полный текст рецепта",
        "распиши шаги по этому рецепту",
    ],
    mutex_notes=[
        # Codex Sub-A4 recipes phase: `get_recipe_any_source` is listed
        # in TOOL_FAMILY_MANIFEST as a future tool (architecture map
        # TODO-2). Naming it directly here would trigger the family-
        # header/mutex-note linter for unmigrated references. Softened
        # to intent-level until the tool ships.
        "Берёт ТОЛЬКО из личной книги юзера. Для поиска без сохранения / fallback на ллм — будет отдельный композитный tool в этой же группе РЕЦЕПТЫ (пока в дорожной карте).",
    ],
    timeout_seconds=10,
    side_effect_class="read_only",
)


DELETE_RECIPE_SPEC = ToolSpec(
    name="delete_recipe",
    description=(
        "Удалить рецепт из книги юзера. Каскад на ингредиенты. "
        "Используй ТОЛЬКО когда юзер явно просит убрать рецепт. Для "
        "редактирования — delete_recipe + save_recipe (отдельного "
        "update нет)."
    ),
    family="recipes",
    effect="write",
    read_domains=[],
    write_domains=["recipes"],
    input_model=DeleteRecipeInput,
    output_model=DeleteRecipeOutput,
    trigger_examples=[
        "удали рецепт борща из книги",
        "убери этот рецепт",
        "не нужен этот рецепт, удали",
        "выкинь из книги рецепт окрошки",
    ],
    mutex_notes=[
        "Используй для УДАЛЕНИЯ. Чтобы заменить — delete_recipe + save_recipe.",
    ],
    timeout_seconds=10,
    side_effect_class="transactional_write",
)


RECIPES_SPECS: list[ToolSpec] = [
    SAVE_RECIPE_SPEC,
    SAVE_RECIPES_BATCH_SPEC,
    SEARCH_RECIPES_SPEC,
    GET_RECIPE_SPEC,
    DELETE_RECIPE_SPEC,
]
"""5 migrated recipe specs. ``get_recipe_any_source`` from
TOOL_FAMILY_MANIFEST is intentionally absent — runtime function not
yet implemented (architecture map TODO-2). Add to this list when the
function ships."""


__all__ = [
    "DELETE_RECIPE_SPEC",
    "DeleteRecipeInput",
    "GET_RECIPE_SPEC",
    "GetRecipeInput",
    "InstructionsMd",
    "RECIPES_SPECS",
    "RecipeIngredientInput",
    "RecipeSource",
    "RecipeTitle",
    "SAVE_RECIPE_SPEC",
    "SAVE_RECIPES_BATCH_SPEC",
    "SEARCH_RECIPES_SPEC",
    "SaveRecipeInput",
    "SaveRecipesBatchInput",
    "SearchQuery",
    "SearchRecipesInput",
    "SourceUrl",
    "TagsList",
]
