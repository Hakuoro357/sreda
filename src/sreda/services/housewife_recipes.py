"""Housewife recipe book — CRUD over Recipe + RecipeIngredient.

Recipes carry a ``source`` enum distinguishing who authored them:
user_dictated / ai_generated / web_found / upgraded_from_menu. The
Mini App renders a different badge per source so the user can spot
at a glance whether a recipe came from them or was invented by the
bot.

Search: title and tag_json are fields the LLM / UI want to filter by,
but both are either encrypted (title) or stored as JSON text. For
pilot scale (handful of recipes per user) we load all owned rows and
filter in Python — avoids maintaining a plaintext mirror column that
would defeat the encryption. Upgrade path to a real search index is
listed in the module's non-goals.

Ingredients are modelled as a separate table with cascade-on-delete,
so deleting a recipe cleans them up automatically. The service always
commits both recipe + ingredients in one transaction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, joinedload

from sreda.db.models.housewife_food import (
    RECIPE_SOURCES,
    Recipe,
    RecipeIngredient,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_TITLE_WS_RE = re.compile(r"\s+")


def _normalise_title(title: str) -> str:
    """Canonical form for exact-title dedup.

    Lowercased, stripped, internal whitespace collapsed. "Плов с курицей"
    and "  ПЛОВ  с  курицей  " normalise to the same key.
    Semantic near-duplicates ("Пельмени домашние" vs "Пельмени домашние
    со сметаной") are deliberately NOT deduped — that needs fuzzy or
    embedding match, out of scope for v1.2.
    """
    return _TITLE_WS_RE.sub(" ", (title or "").strip().lower())


@dataclass(slots=True)
class IngredientInput:
    """Normalised ingredient payload for ``save_recipe``."""

    title: str
    quantity_text: str | None = None
    is_optional: bool = False


@dataclass(slots=True)
class SaveRecipesBatchResult:
    """Return shape for ``save_recipes_batch`` that exposes how many
    items were newly inserted vs short-circuited as duplicates of an
    existing recipe. Stage 6 (v1.2) — production saw the book grow
    copies of the same dish after tool-budget restarts."""

    created: list[Recipe] = field(default_factory=list)
    skipped_existing: list[Recipe] = field(default_factory=list)


class HousewifeRecipeService:
    """Recipe book facade scoped by (tenant_id, user_id).

    All writes commit inside the service so LLM tool-loop semantics
    stay predictable — one successful ``save_recipe`` is durable even
    if a later tool call in the same turn errors out.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_recipe(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str,
        ingredients: list[IngredientInput] | list[dict[str, Any]],
        instructions_md: str | None = None,
        servings: int = 2,
        source: str = "user_dictated",
        source_url: str | None = None,
        tags: list[str] | None = None,
        calories_per_serving: float | None = None,
        protein_per_serving: float | None = None,
        fat_per_serving: float | None = None,
        carbs_per_serving: float | None = None,
    ) -> tuple[Recipe, bool]:
        """Insert a new recipe row + its ingredient rows atomically,
        unless a recipe with the same (tenant, user, normalised-title)
        already exists. Returns ``(recipe, is_new)`` — ``is_new=False``
        means we short-circuited on a duplicate and the returned row
        is the PRE-EXISTING one (nothing was inserted).

        ``source`` must be one of RECIPE_SOURCES (enforced). ``title``
        is required and non-empty. Ingredients normalise empty titles
        out — a recipe with zero ingredients is still legal (a free-
        form instructions-only recipe) but we never add empty ones.

        Dedup (Stage 6) compares by ``_normalise_title``: lowercased,
        whitespace-collapsed. Real semantic near-duplicates ("Пельмени
        домашние" vs "Пельмени домашние со сметаной") still slip
        through — fuzzy match is v2+.

        Nutrition args are all optional per-serving floats. LLM fills
        at save time with best-effort estimates from its food knowledge
        (~±20% accuracy). Negative values / NaN are coerced to None."""
        title_clean = (title or "").strip()
        if not title_clean:
            raise ValueError("title required")
        if source not in RECIPE_SOURCES:
            raise ValueError(f"unknown source: {source!r}")
        servings = max(1, int(servings or 1))

        # Dedup against existing book — exact-title match (normalised).
        existing_index = self._existing_by_normalised_title(tenant_id, user_id)
        existing = existing_index.get(_normalise_title(title_clean))
        if existing is not None:
            logger.info(
                "save_recipe: dedup hit tenant=%s user=%s title=%r → existing id=%s",
                tenant_id, user_id, title_clean, existing.id,
            )
            return existing, False

        normalised_ings = _normalise_ingredients(ingredients)

        recipe = Recipe(
            id=f"rec_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            title=title_clean[:500],
            description=None,
            instructions_md=(instructions_md or None),
            servings=servings,
            calories_per_serving=_clean_nutrient(calories_per_serving),
            protein_per_serving=_clean_nutrient(protein_per_serving),
            fat_per_serving=_clean_nutrient(fat_per_serving),
            carbs_per_serving=_clean_nutrient(carbs_per_serving),
            source=source,
            source_url=(source_url or None),
            tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.session.add(recipe)
        # Flush so recipe.id is assignable as FK even before commit.
        self.session.flush()

        for idx, ing in enumerate(normalised_ings):
            self.session.add(
                RecipeIngredient(
                    id=f"ring_{uuid4().hex[:20]}",
                    recipe_id=recipe.id,
                    title=ing.title,
                    quantity_text=ing.quantity_text,
                    is_optional=ing.is_optional,
                    sort_order=idx,
                )
            )
        self.session.commit()
        return recipe, True

    def save_recipes_batch(
        self,
        *,
        tenant_id: str,
        user_id: str,
        recipes: list[dict[str, Any]],
    ) -> SaveRecipesBatchResult:
        """Batch version of ``save_recipe`` — one commit for all inputs.

        Same per-recipe semantics (required title, ingredient
        normalisation, source enum validation). Items that fail
        validation are skipped silently — the whole batch shouldn't
        die because one entry has an empty title.

        Dedup (Stage 6): two passes.
          1. Collapse duplicate titles WITHIN the input batch — if
             the LLM passed two "борщ"s, only the first is considered.
          2. Check each survivor against the existing book via
             ``_normalise_title``. Matches are reported in
             ``skipped_existing`` (not re-inserted).

        Used by the LLM when the user asks for many recipes in one go
        ("сохрани 18 рецептов из книги"). Keeps turn budget under
        control by collapsing what would be N tool calls into one,
        and now survives tool-budget restarts without producing
        duplicate rows for recipes that were already saved.
        """
        result = SaveRecipesBatchResult()
        existing_index = self._existing_by_normalised_title(tenant_id, user_id)
        seen_in_batch: set[str] = set()

        for raw in recipes or []:
            if not isinstance(raw, dict):
                continue
            title = (raw.get("title") or "").strip()
            if not title:
                continue
            source = raw.get("source") or "user_dictated"
            if source not in RECIPE_SOURCES:
                continue
            servings = max(1, int(raw.get("servings") or 2))

            normal_key = _normalise_title(title)

            # Within-batch dedup
            if normal_key in seen_in_batch:
                logger.info(
                    "save_recipes_batch: in-batch duplicate title=%r skipped",
                    title,
                )
                continue
            seen_in_batch.add(normal_key)

            # Against-DB dedup
            existing = existing_index.get(normal_key)
            if existing is not None:
                logger.info(
                    "save_recipes_batch: dedup hit tenant=%s user=%s "
                    "title=%r → existing id=%s",
                    tenant_id, user_id, title, existing.id,
                )
                result.skipped_existing.append(existing)
                continue

            normalised_ings = _normalise_ingredients(raw.get("ingredients"))

            tags = raw.get("tags") or None
            tags_json = None
            if tags:
                try:
                    tags_json = json.dumps(
                        [str(t) for t in tags], ensure_ascii=False
                    )
                except (TypeError, ValueError):
                    tags_json = None

            recipe = Recipe(
                id=f"rec_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                user_id=user_id,
                title=title[:500],
                description=None,
                instructions_md=(raw.get("instructions_md") or None),
                servings=servings,
                calories_per_serving=_clean_nutrient(raw.get("calories_per_serving")),
                protein_per_serving=_clean_nutrient(raw.get("protein_per_serving")),
                fat_per_serving=_clean_nutrient(raw.get("fat_per_serving")),
                carbs_per_serving=_clean_nutrient(raw.get("carbs_per_serving")),
                source=source,
                source_url=(raw.get("source_url") or None),
                tags_json=tags_json,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            self.session.add(recipe)
            self.session.flush()

            for idx, ing in enumerate(normalised_ings):
                self.session.add(
                    RecipeIngredient(
                        id=f"ring_{uuid4().hex[:20]}",
                        recipe_id=recipe.id,
                        title=ing.title,
                        quantity_text=ing.quantity_text,
                        is_optional=ing.is_optional,
                        sort_order=idx,
                    )
                )
            result.created.append(recipe)
            # Keep index current so later items in this batch see the
            # newly-inserted row (further in-batch dup protection).
            existing_index[normal_key] = recipe

        if result.created:
            self.session.commit()
        return result

    def _existing_by_normalised_title(
        self, tenant_id: str, user_id: str
    ) -> dict[str, Recipe]:
        """Load all owned recipes and index them by normalised title.

        Titles are EncryptedString, so ORM-level decryption happens on
        attribute access — we can't do SQL LIKE. At pilot scale this
        is fine (< 100 recipes per user); if it ever gets hot we'll
        add a plaintext ``title_normal`` mirror column + a trigram
        index.
        """
        rows = (
            self.session.query(Recipe)
            .filter(
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
            )
            .all()
        )
        out: dict[str, Recipe] = {}
        for r in rows:
            key = _normalise_title(r.title)
            # First-wins: if two older rows already share a normalised
            # title (legacy data), the oldest is kept as the dedup target.
            out.setdefault(key, r)
        return out

    def delete_recipe(
        self, *, tenant_id: str, user_id: str, recipe_id: str
    ) -> bool:
        """Delete a recipe (and cascading ingredients). Returns True if
        a row was actually removed; False if it didn't exist or didn't
        belong to the caller (cross-tenant safety)."""
        row = (
            self.session.query(Recipe)
            .filter(
                Recipe.id == recipe_id,
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
            )
            .one_or_none()
        )
        if row is None:
            return False
        self.session.delete(row)
        self.session.commit()
        return True

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_recipe(
        self, *, tenant_id: str, user_id: str, recipe_id: str
    ) -> Recipe | None:
        """Fetch a single recipe with ingredients eagerly loaded.

        Cross-tenant safe — returns None if the recipe id exists but
        belongs to someone else."""
        return (
            self.session.query(Recipe)
            .options(joinedload(Recipe.ingredients))
            .filter(
                Recipe.id == recipe_id,
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
            )
            .one_or_none()
        )

    def list_recipes(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str | None = None,
    ) -> list[Recipe]:
        """All recipes for (tenant, user), most-recent first. Optional
        ``query`` filters by title substring (case-insensitive) AND by
        any tag match — title is encrypted so we load everything owned
        and filter in Python, which is fine at pilot scale (< 100
        recipes per user).

        The load is cheap: 1 query + decryption of title strings. No
        ingredients loaded here — ``get_recipe`` for the detail page."""
        base = (
            self.session.query(Recipe)
            .filter(
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
            )
            .order_by(Recipe.created_at.desc())
        )
        rows = base.all()
        if not query:
            return rows
        q_lower = query.strip().lower()
        if not q_lower:
            return rows

        out: list[Recipe] = []
        for row in rows:
            haystack_parts = [row.title.lower()]
            if row.tags_json:
                try:
                    for tag in json.loads(row.tags_json) or []:
                        if isinstance(tag, str):
                            haystack_parts.append(tag.lower())
                except (json.JSONDecodeError, TypeError):
                    pass
            if any(q_lower in part for part in haystack_parts):
                out.append(row)
        return out

    def search_recipes(
        self, *, tenant_id: str, user_id: str, query: str
    ) -> list[Recipe]:
        """Alias for ``list_recipes(query=...)`` — clearer intent when
        called from LLM tool that specifically wants filtered results."""
        return self.list_recipes(
            tenant_id=tenant_id, user_id=user_id, query=query
        )

    def count_recipes(self, *, tenant_id: str, user_id: str) -> int:
        """Cheap counter for the Mini App dashboard tile."""
        return (
            self.session.query(Recipe)
            .filter(
                Recipe.tenant_id == tenant_id,
                Recipe.user_id == user_id,
            )
            .count()
        )


def _clean_nutrient(value: Any) -> float | None:
    """Coerce an LLM-supplied nutrient value to a non-negative float or
    None. Handles strings ("450"), negatives (→ None), and non-numeric
    junk (→ None) gracefully."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0 or f != f:  # negative or NaN
        return None
    return f


def _normalise_ingredients(
    raw: list[IngredientInput] | list[dict[str, Any]] | None,
) -> list[IngredientInput]:
    if not raw:
        return []
    out: list[IngredientInput] = []
    for item in raw:
        if isinstance(item, IngredientInput):
            if item.title and item.title.strip():
                out.append(
                    IngredientInput(
                        title=item.title.strip()[:300],
                        quantity_text=(item.quantity_text or "").strip()[:64]
                        or None,
                        is_optional=bool(item.is_optional),
                    )
                )
            continue
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            IngredientInput(
                title=title[:300],
                quantity_text=(item.get("quantity_text") or "").strip()[:64]
                or None,
                is_optional=bool(item.get("is_optional") or False),
            )
        )
    return out
