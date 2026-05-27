"""Integration tests for the recipes family ToolSpec instances
(Sub-A4 phase 3 — Plan-Execute Epic).

Mirrors the shopping + reminders patterns (both R7=NSC). Coverage:
- All 5 RECIPES_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: recipes-family entries in TOOL_FAMILY_MANIFEST
  match the migrated specs exactly (Codex R1 MAJOR #6 removed
  ``get_recipe_any_source`` from both the manifest and this list
  until the runtime function ships)
- Per-tool: input_model rejects extra keys, parsers produce
  output_model on canonical "ok:..." strings
- Tight RecipeId aliases
- Sentinel boundary regression
- recipe_not_found stable code
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    DeleteRecipeOk,
    HousewifeToolError,
    PARSERS,
    SaveRecipeOk,
    SaveRecipesBatchOk,
    SearchRecipesEmpty,
    SearchRecipesList,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
    validate_tool_registry_quality,
)
from sreda.services.tool_schemas.specs_recipes import (
    RECIPES_SPECS,
    DeleteRecipeInput,
    GetRecipeInput,
    SaveRecipeInput,
    SaveRecipesBatchInput,
    SearchRecipesInput,
)

# Real-shape IDs matching f"rec_{uuid4().hex[:24]}".
REC_A = "rec_aaaaaaaaaaaaaaaaaaaaaaaa"
REC_B = "rec_bbbbbbbbbbbbbbbbbbbbbbbb"
REC_C = "rec_cccccccccccccccccccccccc"


# ---------------------------------------------------------------------------
# Registry-level invariants
# ---------------------------------------------------------------------------


def test_recipes_specs_count_is_five() -> None:
    """5 migrated. ``get_recipe_any_source`` ships in a future sub-issue."""
    assert len(RECIPES_SPECS) == 5


def test_recipes_specs_all_pass_strict_quality_lint() -> None:
    violations = validate_tool_registry_quality(RECIPES_SPECS, strict=True)
    assert violations == [], (
        f"Strict quality lint surfaced {len(violations)} violation(s): "
        f"{[(v.tool_name, v.code, v.message[:80]) for v in violations]}"
    )


def test_recipes_specs_pass_assert_production_gate() -> None:
    assert_production_registry_quality(RECIPES_SPECS)


def test_recipes_specs_match_tool_family_manifest() -> None:
    """Every TOOL_FAMILY_MANIFEST entry mapped to 'recipes' must have
    a corresponding ToolSpec with matching ``spec.family``. Codex
    Sub-A4 recipes R2 closure: ``get_recipe_any_source`` was removed
    from the manifest in R1; cross-check is now exact (no test
    exclusion). When the tool ships, the same commit re-adds it to
    both the manifest and RECIPES_SPECS."""
    manifest_recipes = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "recipes"
    }
    spec_names = {s.name for s in RECIPES_SPECS}
    assert spec_names == manifest_recipes, (
        f"Mismatch.\nIn manifest only: {manifest_recipes - spec_names}\n"
        f"In specs only: {spec_names - manifest_recipes}"
    )
    for spec in RECIPES_SPECS:
        expected = TOOL_FAMILY_MANIFEST[spec.name]
        assert spec.family == expected


def test_recipes_spec_names_are_unique() -> None:
    names = [s.name for s in RECIPES_SPECS]
    assert len(names) == len(set(names))


def test_recipes_write_tools_declare_recipes_write_domain() -> None:
    for spec in RECIPES_SPECS:
        if spec.effect == "write":
            assert "recipes" in spec.write_domains


# ---------------------------------------------------------------------------
# Per-tool input model rejection of extra keys / bad shapes
# ---------------------------------------------------------------------------


_VALID_RECIPE = {
    "title": "Борщ",
    "ingredients": [{"title": "свёкла", "quantity_text": "2 шт"}],
    "instructions_md": "1. Сварить.",
    "servings": 4,
    "source": "user_dictated",
}


def test_save_recipe_input_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({**_VALID_RECIPE, "hallucinated": "v"})


def test_save_recipe_input_accepts_minimal() -> None:
    parsed = SaveRecipeInput.model_validate(_VALID_RECIPE)
    assert parsed.title == "Борщ"
    assert parsed.source == "user_dictated"


def test_save_recipe_input_rejects_unknown_source() -> None:
    """RecipeSource is a Literal — schema rejects values runtime would silently drop."""
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({**_VALID_RECIPE, "source": "made_up"})


def test_save_recipe_input_accepts_empty_ingredients() -> None:
    """Codex R1 MAJOR #3: runtime explicitly allows recipes with zero
    structured ingredients («a free-form instructions-only recipe» —
    housewife_recipes.py:160-163). Schema should match — previously
    rejected at min_length=1."""
    parsed = SaveRecipeInput.model_validate({**_VALID_RECIPE, "ingredients": []})
    assert parsed.ingredients == []


def test_save_recipe_input_rejects_zero_servings() -> None:
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({**_VALID_RECIPE, "servings": 0})


def test_save_recipe_input_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({**_VALID_RECIPE, "title": "   "})


def test_save_recipe_input_accepts_title_at_500() -> None:
    """Codex R1 MINOR: RecipeTitle bumped from 200 to 500 to match
    DB column (EncryptedString, no cap) — 200 was overly strict for
    web-imported titles."""
    parsed = SaveRecipeInput.model_validate({**_VALID_RECIPE, "title": "x" * 500})
    assert len(parsed.title) == 500


def test_save_recipe_input_rejects_over_500_title() -> None:
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({**_VALID_RECIPE, "title": "x" * 501})


def test_save_recipe_input_rejects_too_many_tags() -> None:
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({
            **_VALID_RECIPE,
            "tags": [f"tag{i}" for i in range(11)],
        })


def test_save_recipe_input_rejects_web_found_without_url() -> None:
    """Codex R1 MAJOR #2: source=web_found requires source_url."""
    with pytest.raises(ValidationError, match="requires source_url"):
        SaveRecipeInput.model_validate({
            **_VALID_RECIPE,
            "source": "web_found",
        })


def test_save_recipe_input_rejects_non_web_with_url() -> None:
    """source != web_found forbids source_url."""
    with pytest.raises(ValidationError, match="must NOT carry source_url"):
        SaveRecipeInput.model_validate({
            **_VALID_RECIPE,
            "source": "ai_generated",
            "source_url": "https://example.com/recipe/borscht",
        })


def test_save_recipe_input_rejects_web_found_with_malformed_url() -> None:
    """Codex R1 MAJOR #2: URL shape regex catches «не URL»."""
    with pytest.raises(ValidationError, match="valid http/https URL"):
        SaveRecipeInput.model_validate({
            **_VALID_RECIPE,
            "source": "web_found",
            "source_url": "yes please",
        })


def test_save_recipe_input_accepts_web_found_with_valid_url() -> None:
    parsed = SaveRecipeInput.model_validate({
        **_VALID_RECIPE,
        "source": "web_found",
        "source_url": "https://example.com/recipes/borscht",
    })
    assert parsed.source_url == "https://example.com/recipes/borscht"


def test_save_recipe_input_rejects_source_url_over_500() -> None:
    """Codex R1 MAJOR #1: SourceUrl caps at 500 (DB column constraint)."""
    long_url = "https://example.com/" + "x" * 500
    with pytest.raises(ValidationError):
        SaveRecipeInput.model_validate({
            **_VALID_RECIPE,
            "source": "web_found",
            "source_url": long_url,
        })


def test_save_recipes_batch_input_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        SaveRecipesBatchInput.model_validate({"recipes": []})


def test_save_recipes_batch_input_rejects_over_50() -> None:
    """Build 51 distinct recipes (titles differ) to avoid the dup guard
    firing first."""
    recipes = [
        {**_VALID_RECIPE, "title": f"Рецепт #{i}"} for i in range(51)
    ]
    with pytest.raises(ValidationError):
        SaveRecipesBatchInput.model_validate({"recipes": recipes})


def test_save_recipes_batch_input_accepts_max_50_distinct() -> None:
    """50 DISTINCT recipes (distinct titles) is the upper bound."""
    recipes = [
        {**_VALID_RECIPE, "title": f"Рецепт #{i}"} for i in range(50)
    ]
    parsed = SaveRecipesBatchInput.model_validate({"recipes": recipes})
    assert len(parsed.recipes) == 50


def test_save_recipes_batch_input_rejects_duplicate_normalized_titles() -> None:
    """Codex R1 MAJOR #4: runtime silently drops later duplicates and
    doesn't count them in skipped_as_duplicate. Schema rejects so the
    planner knows it sent dups."""
    recipes = [
        {**_VALID_RECIPE, "title": "Борщ"},
        {**_VALID_RECIPE, "title": "  БОРЩ  "},  # normalizes to same
    ]
    with pytest.raises(ValidationError):
        SaveRecipesBatchInput.model_validate({"recipes": recipes})


def test_save_recipes_batch_input_rejects_oversize_payload() -> None:
    """Codex R1 MAJOR #5: aggregate char budget — 50 recipes × 8000-char
    instructions = 400KB worst case. Cap at 200_000 chars."""
    huge_instructions = "x" * 8000
    recipes = [
        {**_VALID_RECIPE, "title": f"Рецепт #{i}", "instructions_md": huge_instructions}
        for i in range(50)
    ]
    with pytest.raises(ValidationError):
        SaveRecipesBatchInput.model_validate({"recipes": recipes})


def test_save_recipes_batch_input_accepts_normal_payload() -> None:
    """Realistic 20-recipe batch under the budget passes."""
    recipes = [
        {**_VALID_RECIPE, "title": f"Рецепт #{i}",
         "instructions_md": "1. Сварить.\n2. Подать."}
        for i in range(20)
    ]
    parsed = SaveRecipesBatchInput.model_validate({"recipes": recipes})
    assert len(parsed.recipes) == 20


def test_search_recipes_input_accepts_empty_query() -> None:
    """Empty query returns ALL recipes — runtime contract requires this."""
    parsed = SearchRecipesInput.model_validate({"query": ""})
    assert parsed.query == ""


def test_search_recipes_input_rejects_long_query() -> None:
    with pytest.raises(ValidationError):
        SearchRecipesInput.model_validate({"query": "x" * 201})


def test_get_recipe_input_accepts_real_shape_id() -> None:
    parsed = GetRecipeInput.model_validate({"recipe_id": REC_A})
    assert parsed.recipe_id == REC_A


def test_get_recipe_input_rejects_short_id() -> None:
    """Tight ``^rec_[0-9a-f]{24}$`` rejects ``rec_1``."""
    with pytest.raises(ValidationError):
        GetRecipeInput.model_validate({"recipe_id": "rec_1"})


def test_get_recipe_input_rejects_typo_id() -> None:
    with pytest.raises(ValidationError):
        GetRecipeInput.model_validate({"recipe_id": "rec-abc"})


def test_delete_recipe_input_accepts_real_shape_id() -> None:
    parsed = DeleteRecipeInput.model_validate({"recipe_id": REC_A})
    assert parsed.recipe_id == REC_A


def test_delete_recipe_input_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        DeleteRecipeInput.model_validate({
            "recipe_id": REC_A,
            "confirm": True,
        })


# ---------------------------------------------------------------------------
# End-to-end parser → output_model variants
# ---------------------------------------------------------------------------


def test_save_recipe_parser_returns_saved() -> None:
    parsed = parse_tool_output("save_recipe", f"ok:saved:{REC_A}")
    assert isinstance(parsed, SaveRecipeOk)
    assert parsed.status == "saved"
    assert parsed.recipe_id == REC_A


def test_save_recipe_parser_returns_duplicate() -> None:
    parsed = parse_tool_output("save_recipe", f"ok:duplicate:{REC_A}")
    assert isinstance(parsed, SaveRecipeOk)
    assert parsed.status == "duplicate"


def test_save_recipe_parser_returns_sentinel_for_malformed_id() -> None:
    parsed = parse_tool_output("save_recipe", "ok:saved:rec_garbage")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_save_recipes_batch_parser_returns_batch_saved() -> None:
    raw = f"ok:batch_saved:2:skipped_as_duplicate:1:ids=[{REC_A},{REC_B}]"
    parsed = parse_tool_output("save_recipes_batch", raw)
    assert isinstance(parsed, SaveRecipesBatchOk)
    assert parsed.created_count == 2
    assert parsed.skipped_as_duplicate == 1
    assert parsed.recipe_ids == [REC_A, REC_B]


def test_save_recipes_batch_parser_returns_zero_created() -> None:
    """All-duplicates case — no ids group emitted."""
    parsed = parse_tool_output(
        "save_recipes_batch", "ok:batch_saved:0:skipped_as_duplicate:5"
    )
    assert isinstance(parsed, SaveRecipesBatchOk)
    assert parsed.created_count == 0
    assert parsed.skipped_as_duplicate == 5
    assert parsed.recipe_ids == []


def test_save_recipes_batch_parser_rejects_zero_count_with_ids() -> None:
    """Internally-inconsistent — N=0 should NOT have ids."""
    parsed = parse_tool_output(
        "save_recipes_batch",
        f"ok:batch_saved:0:skipped_as_duplicate:5:ids=[{REC_A}]",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_save_recipes_batch_parser_count_mismatch_rejects() -> None:
    """N != len(ids) — fail-closed via the @model_validator on
    SaveRecipesBatchOk."""
    parsed = parse_tool_output(
        "save_recipes_batch",
        f"ok:batch_saved:3:skipped_as_duplicate:0:ids=[{REC_A},{REC_B}]",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_search_recipes_parser_returns_empty() -> None:
    parsed = parse_tool_output("search_recipes", "no recipes found")
    assert isinstance(parsed, SearchRecipesEmpty)


def test_search_recipes_parser_returns_list() -> None:
    raw = (
        "2 recipe(s):\n"
        f"  [{REC_A}] 📝 Борщ tags=[суп]\n"
        f"  [{REC_B}] 🤖 Сырники"
    )
    parsed = parse_tool_output("search_recipes", raw)
    assert isinstance(parsed, SearchRecipesList)
    assert len(parsed.items) == 2
    assert parsed.items[0].recipe_id == REC_A
    assert "Борщ" in parsed.items[0].raw_line


def test_search_recipes_parser_returns_sentinel_for_malformed_id() -> None:
    raw = (
        "1 recipe(s):\n"
        "  [rec_garbage] 📝 Борщ"
    )
    parsed = parse_tool_output("search_recipes", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_delete_recipe_parser_returns_deleted() -> None:
    parsed = parse_tool_output("delete_recipe", "ok:deleted")
    assert isinstance(parsed, DeleteRecipeOk)


def test_delete_recipe_parser_returns_sentinel_for_extra_payload() -> None:
    parsed = parse_tool_output("delete_recipe", "ok:deleted:rec_xxx")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Stable error code regression
# ---------------------------------------------------------------------------


def test_get_recipe_not_found_is_stable() -> None:
    """``error: recipe 'rec_42' not found`` and ``error: recipe 'rec_7'
    not found`` must produce the SAME error_code so planner can branch
    deterministically. The recipe pattern in _STABLE_ERROR_PATTERNS
    overrides the default `error: recipe ...` dynamic-code derivation."""
    a = parse_tool_output("get_recipe", "error: recipe 'rec_42' not found")
    b = parse_tool_output("get_recipe", "error: recipe 'rec_7' not found")
    assert isinstance(a, HousewifeToolError)
    assert isinstance(b, HousewifeToolError)
    assert a.error_code == "recipe_not_found"
    assert b.error_code == "recipe_not_found"


def test_delete_recipe_not_found_is_stable() -> None:
    a = parse_tool_output("delete_recipe", "error: recipe 'rec_42' not found")
    b = parse_tool_output("delete_recipe", "error: recipe 'rec_7' not found")
    assert a.error_code == "recipe_not_found"
    assert b.error_code == "recipe_not_found"


# ---------------------------------------------------------------------------
# Parser/output_model compatibility for every recipes spec
# ---------------------------------------------------------------------------


_PARSER_HAPPY_PATH = {
    "save_recipe": f"ok:saved:{REC_A}",
    "save_recipes_batch": f"ok:batch_saved:1:skipped_as_duplicate:0:ids=[{REC_A}]",
    "search_recipes": "no recipes found",
    "get_recipe": "Борщ (on 4 servings, source=user_dictated)\ningredients:\n  - свёкла — 2 шт",
    "delete_recipe": "ok:deleted",
}


@pytest.mark.parametrize("spec", RECIPES_SPECS, ids=lambda s: s.name)
def test_every_recipes_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS, (
        f"Tool {spec.name!r} has a ToolSpec but no parser in PARSERS."
    )


@pytest.mark.parametrize("spec", RECIPES_SPECS, ids=lambda s: s.name)
def test_parser_output_validates_against_spec_output_model(spec) -> None:
    raw = _PARSER_HAPPY_PATH[spec.name]
    parsed = parse_tool_output(spec.name, raw)
    TypeAdapter(spec.output_model).validate_python(parsed.model_dump())


# ---------------------------------------------------------------------------
# Sentinel boundary regression — sentinel must NOT validate against any
# recipes output_model union.
# ---------------------------------------------------------------------------


def test_sentinel_is_not_valid_against_any_recipes_output_model() -> None:
    sentinel = parse_tool_output("save_recipe", "totally unparseable")
    assert isinstance(sentinel, ToolOutputContractViolation)
    sentinel_dump = sentinel.model_dump()
    for spec in RECIPES_SPECS:
        with pytest.raises(ValidationError):
            TypeAdapter(spec.output_model).validate_python(sentinel_dump)


# ---------------------------------------------------------------------------
# Family-header lint clean for current recipes header
# ---------------------------------------------------------------------------


def test_recipes_family_header_lint_clean() -> None:
    """Recipes family header doesn't name any unmigrated tool. The
    family-level mutex-note linter should produce no violations."""
    from sreda.services.tool_schemas.registry_quality import (
        validate_mutex_note_references,
    )
    violations = validate_mutex_note_references(
        RECIPES_SPECS, manifest=TOOL_FAMILY_MANIFEST
    )
    bad = [v for v in violations if v.code in (
        "mutex_note_references_unmigrated_tool",
        "family_header_references_unmigrated_tool",
    )]
    assert not bad, (
        f"recipes family/specs reference unmigrated tools: "
        f"{[(v.tool_name, v.field_path, v.message[:60]) for v in bad]}"
    )


# ---------------------------------------------------------------------------
# Aggregator coverage
# ---------------------------------------------------------------------------


def test_migrated_tool_specs_aggregate_includes_recipes() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    recipes_names = {s.name for s in RECIPES_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert recipes_names.issubset(migrated_names)


def test_migrated_tool_specs_pass_strict_production_quality_with_recipes() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
