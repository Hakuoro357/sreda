"""ToolSpec instances for the RECIPES family (Sub-A4 phase 3).

5 tools migrated: ``save_recipe``, ``save_recipes_batch``,
``search_recipes``, ``get_recipe``, ``delete_recipe``.

``get_recipe_any_source`` is a future TODO-2 tool (architecture
map). Codex Sub-A4 recipes R1 MAJOR #6 removed it from
``TOOL_FAMILY_MANIFEST`` until the runtime function ships — the
recipes manifest cross-check is exact now (no test exclusion).
Restore the manifest entry + ToolSpec in the same commit that adds
the runtime function.

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

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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


_HTTPS_URL_RE = re.compile(r"^https?://[^\s/$.?#].\S*$", re.IGNORECASE)
"""Loose http/https URL shape. Runtime doesn't fetch — this only
catches obviously-not-a-URL strings like ``"yes"`` getting into
``source_url`` slot. dateutil's ``rrulestr`` analogue."""


def _normalize_title_for_dedup(title: str) -> str:
    """Mirror of ``housewife_recipes._normalise_title``: lowercase +
    whitespace-collapse. Used here so the batch validator catches the
    same duplicates the runtime would silently drop.

    Source: ``services/housewife_recipes.py`` (private; reimplemented
    here because import would create a cycle through the planner).
    """
    return " ".join(title.lower().split())


# ---------------------------------------------------------------------------
# Recipes-specific aliases — caps from the docstring contracts in
# housewife_chat_tools.py and the recipe_service implementation.
# ---------------------------------------------------------------------------


RecipeTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Recipe title. Codex Sub-A4 recipes R1 MINOR: bumped from 200 to 500
to match the DB column behaviour (``Recipe.title`` is EncryptedString
with no length cap; runtime is unbounded). 200 was overly strict —
some imported web-sourced recipe titles legitimately exceed it. 500
keeps the «short name» spirit of the docstring while accepting real-
world variability."""


InstructionsMd = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8000),
]
"""Recipe instructions in markdown. 8000-char cap is generous (a
multi-step recipe rarely exceeds 2000) but bounds individual recipe
size; aggregate batch payload is additionally bounded in
``SaveRecipesBatchInput`` (Codex R1 MAJOR #5)."""


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
    StringConstraints(strip_whitespace=True, min_length=10, max_length=500),
]
"""HTTPS source URL when source == 'web_found'. Codex Sub-A4 recipes
R1 MAJOR #1: capped at 500 to match ``Recipe.source_url: String(500)``
(``db/models/housewife_food.py:243``). Was 2048 — would generate
schema-valid payloads that fail/truncate at insert.

Format additionally validated by ``SaveRecipeInput.@model_validator``
to require ``https?://...`` shape AND that source_url is set IFF
source=='web_found' (Codex R1 MAJOR #2)."""


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
    """Save a single recipe. Field caps per ``housewife_chat_tools.py:921``
    and the underlying ``Recipe`` SQLAlchemy model.

    Codex Sub-A4 recipes R1:
    - MAJOR #2: source / source_url invariant enforced via
      ``@model_validator`` — source==web_found ⇔ source_url present
      with ``https?://...`` shape.
    - MAJOR #3: ``ingredients`` ``min_length`` relaxed from 1 to 0 —
      runtime (``housewife_recipes.py:160-163``) explicitly allows
      «a free-form instructions-only recipe» with zero structured
      ingredient rows.
    """

    model_config = ConfigDict(extra="forbid")
    title: RecipeTitle
    ingredients: list[RecipeIngredientInput] = Field(min_length=0, max_length=100)
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

    @model_validator(mode="after")
    def _validate_source_url_invariant(self) -> "SaveRecipeInput":
        """Codex R1 MAJOR #2: bind ``source`` and ``source_url`` together.

        Runtime stores the URL when source==web_found, but the schema
        previously accepted any combo (URL with source=ai_generated,
        web_found without URL, malformed URL string). Now:

        - source == "web_found" → source_url REQUIRED and ``https?://``
        - source != "web_found" → source_url FORBIDDEN (or explicit null)

        Strict at planner time so bad provenance metadata never reaches
        storage. Empty/blank source_url on web_found rejected too."""
        if self.source == "web_found":
            if self.source_url is None or not self.source_url.strip():
                raise ValueError(
                    "source='web_found' requires source_url with a "
                    "valid https?:// URL. Either omit source_url and "
                    "use a different source, or provide the origin URL."
                )
            if not _HTTPS_URL_RE.match(self.source_url):
                raise ValueError(
                    f"source_url={self.source_url!r} doesn't look like "
                    f"a valid http/https URL. Required when "
                    f"source='web_found'."
                )
        else:
            if self.source_url is not None:
                raise ValueError(
                    f"source={self.source!r} must NOT carry source_url "
                    f"(got {self.source_url!r}). source_url is only "
                    f"meaningful for source='web_found' — runtime "
                    f"would store stale/wrong provenance metadata "
                    f"otherwise."
                )
        return self


class SaveRecipesBatchInput(BaseModel):
    """Batch-save N recipes.

    Codex Sub-A4 recipes R1 MAJOR #4 + #5:
    - Duplicate normalized titles within ONE batch are now rejected
      at the schema level (runtime silently dropped them without
      counting in ``skipped_as_duplicate``, hiding work the user
      asked for).
    - Aggregate char budget across all recipes capped at 200_000
      to prevent a 50×8000-char-instructions explosion from blowing
      planner / tool-call payload size before validation can help.
    """

    model_config = ConfigDict(extra="forbid")
    recipes: list[SaveRecipeInput] = Field(min_length=1, max_length=50)
    """Up to 50 recipes per batch — keeps the LLM tool-call payload
    bounded; for «save my whole book» flows the planner can chunk."""

    @model_validator(mode="after")
    def _validate_batch_invariants(self) -> "SaveRecipesBatchInput":
        # Duplicate-title guard — same normalization runtime uses.
        # Mirrors ``housewife_recipes._normalise_title``.
        seen_titles: dict[str, int] = {}
        for idx, recipe in enumerate(self.recipes):
            normalized = _normalize_title_for_dedup(recipe.title)
            if normalized in seen_titles:
                first_idx = seen_titles[normalized]
                raise ValueError(
                    f"recipes[{idx}].title={recipe.title!r} duplicates "
                    f"recipes[{first_idx}] under case/whitespace "
                    f"normalization. Runtime would silently drop the "
                    f"later occurrence without counting it in "
                    f"skipped_as_duplicate — caller wouldn't see "
                    f"work was lost. Deduplicate before sending."
                )
            seen_titles[normalized] = idx

        # Aggregate char budget. Each recipe's instructions_md alone
        # can be 8000 chars; 50 recipes worst-case = 400_000 chars
        # before ingredients/tags (Python str ``len``, NOT UTF-8
        # bytes — Cyrillic doubles per char on serialization). Cap
        # at 200_000 chars total (≈4× a typical batch) so accidental
        # «save 50 enormous recipes» doesn't blow the LLM tool-call
        # payload.
        total_chars = sum(
            len(r.title)
            + len(r.instructions_md)
            + sum(
                len(i.title) + len(i.quantity_text or "")
                for i in r.ingredients
            )
            + (len(r.source_url) if r.source_url else 0)
            + sum(len(t) for t in (r.tags or []))
            for r in self.recipes
        )
        BATCH_CHAR_BUDGET = 200_000
        if total_chars > BATCH_CHAR_BUDGET:
            raise ValueError(
                f"batch aggregate size {total_chars} Python str chars "
                f"exceeds {BATCH_CHAR_BUDGET} char budget. NOTE: budget "
                f"is in characters, not UTF-8 bytes — Cyrillic doubles "
                f"per char on serialization. Chunk into smaller "
                f"batches (e.g. 10-20 recipes per call) — the LLM "
                f"tool-call has a payload ceiling."
            )
        return self


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
    # #29: "duplicate" (recipe already exists) inserts nothing.
    committed_statuses=frozenset({"saved"}),
    trigger_examples=[
        "сохрани рецепт борща",
        "запиши этот рецепт в книгу",
        "добавь в книгу рецепт сырников",
        "сохрани вот этот рецепт",
    ],
    mutex_notes=[
        "Для >1 рецепта — save_recipes_batch; для правки — delete_recipe + save_recipe.",
        # #146: юзер диктует рецепт без всех полей → НЕ уточняй, заполни сам.
        "Юзер дал не все поля → НЕ уточняй: servings=4, source=user_dictated, способ→instructions_md, продукты→ingredients (пусто ок). Подтверди через ${s1.title}, НЕ raw_text. Сохраняй сразу.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


SAVE_RECIPES_BATCH_SPEC = ToolSpec(
    name="save_recipes_batch",
    description=(
        "Сохранить НЕСКОЛЬКО рецептов одним вызовом (до 50): «запиши N "
        "рецептов» либо превратить блюда плана меню в структурированные "
        "рецепты. Дедуп per-title; дубли внутри батча схема отвергает."
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
        "Перед батчем полезно search_recipes('') — увидеть что уже в книге, не плодить дубли.",
        "При >50 рецептах или >200k символов — чанкуй на несколько вызовов (иначе схема отвергнет). Лимит — на символах, не байтах.",
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
        # Codex Sub-A4 recipes R1 MAJOR #6 + R4 cleanup: the future
        # ``get_recipe_any_source`` (architecture-map TODO-2) is NOT
        # in TOOL_FAMILY_MANIFEST until the runtime function ships.
        # Keep this mutex_note intent-level («отдельный композитный
        # tool в группе РЕЦЕПТЫ») without naming the tool — that
        # avoids steering the planner toward an unbuilt typed tool
        # AND keeps the registry-quality linter clean. When the tool
        # ships, restore the manifest entry, ToolSpec, AND the
        # explicit name here in one commit.
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
"""5 migrated recipe specs. ``get_recipe_any_source`` (architecture
map TODO-2) is intentionally absent from BOTH this list AND
``TOOL_FAMILY_MANIFEST`` — runtime function not yet implemented.
Add to both surfaces in the commit that ships the runtime."""


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
