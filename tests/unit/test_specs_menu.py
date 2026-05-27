"""Integration tests for the menu family ToolSpec instances
(Sub-A4 phase 4 — Plan-Execute Epic).

Mirrors the shopping/reminders/recipes patterns (all R5-R7=NSC).
Coverage:
- All 5 MENU_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: menu-family entries exact match
- Per-tool: input_model rejects extra keys, parsers produce
  output_model on canonical "ok:..." strings
- Tight MenuPlanId / MenuItemId aliases
- WeekStartIso shape regex
- recipe_id XOR free_text in cells, etc.
- Sentinel boundary regression
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    ClearMenuOk,
    GenerateShoppingFromMenuOk,
    ListMenuEmpty,
    ListMenuOk,
    PARSERS,
    PlanWeekMenuOk,
    UpdateMenuItemClearedOrNotFound,
    UpdateMenuItemUpdated,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
    validate_tool_registry_quality,
)
from sreda.services.tool_schemas.specs_menu import (
    MENU_SPECS,
    ClearMenuInput,
    GenerateShoppingFromMenuInput,
    ListMenuInput,
    MenuCellInput,
    PlanWeekMenuInput,
    UpdateMenuItemInput,
)

# Real-shape IDs.
MENU_A = "menu_aaaaaaaaaaaaaaaaaaaaaaaa"
MENU_B = "menu_bbbbbbbbbbbbbbbbbbbbbbbb"
MPI_A = "mpi_aaaaaaaaaaaaaaaaaaaaaaaa"
MPI_B = "mpi_bbbbbbbbbbbbbbbbbbbbbbbb"
REC_A = "rec_aaaaaaaaaaaaaaaaaaaaaaaa"


def test_menu_specs_count_is_five() -> None:
    assert len(MENU_SPECS) == 5


def test_menu_specs_pass_strict_quality_lint() -> None:
    violations = validate_tool_registry_quality(MENU_SPECS, strict=True)
    assert violations == [], (
        f"Strict quality lint surfaced {len(violations)} violation(s): "
        f"{[(v.tool_name, v.code, v.message[:80]) for v in violations]}"
    )


def test_menu_specs_pass_assert_production_gate() -> None:
    """Codex Sub-A4 menu R1 MAJOR #2 closure: with the
    ``migrated_specs`` API extension on
    ``validate_mutex_note_references``, ``assert_production_registry_quality``
    now treats MIGRATED_TOOL_SPECS as the «known available» set when
    linting a per-family subset. Cross-family references (menu →
    search_recipes/add_shopping_items) no longer false-positive."""
    assert_production_registry_quality(MENU_SPECS)


def test_menu_specs_no_intra_family_unmigrated_references() -> None:
    """Linter scoped to MENU_SPECS — should only fire on references
    to MENU-family tools that aren't yet in MENU_SPECS (currently
    none — all 5 menu tools are migrated together). Cross-family
    references are LEGITIMATE here and tested separately at the
    aggregate level."""
    from sreda.services.tool_schemas.registry_quality import (
        validate_mutex_note_references,
    )
    # Build a manifest of ONLY menu-family entries so the linter can't
    # false-positive on cross-family references that the menu tools
    # legitimately need (search_recipes, add_shopping_items).
    menu_only_manifest = {
        name: family
        for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "menu"
    }
    violations = validate_mutex_note_references(
        MENU_SPECS, manifest=menu_only_manifest
    )
    assert violations == []


def test_menu_specs_match_tool_family_manifest() -> None:
    manifest_menu = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "menu"
    }
    spec_names = {s.name for s in MENU_SPECS}
    assert spec_names == manifest_menu, (
        f"Mismatch.\nIn manifest only: {manifest_menu - spec_names}\n"
        f"In specs only: {spec_names - manifest_menu}"
    )


def test_menu_spec_names_are_unique() -> None:
    names = [s.name for s in MENU_SPECS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Input model rejection
# ---------------------------------------------------------------------------


def test_plan_week_menu_input_accepts_minimal() -> None:
    parsed = PlanWeekMenuInput.model_validate({
        "week_start": "2026-05-25",
        "days": [{"day_of_week": 0, "meals": {}}],
    })
    assert parsed.week_start == "2026-05-25"


def test_plan_week_menu_input_rejects_bad_iso() -> None:
    with pytest.raises(ValidationError):
        PlanWeekMenuInput.model_validate({
            "week_start": "tomorrow",
            "days": [{"day_of_week": 0, "meals": {}}],
        })


def test_plan_week_menu_input_rejects_empty_days() -> None:
    with pytest.raises(ValidationError):
        PlanWeekMenuInput.model_validate({
            "week_start": "2026-05-25",
            "days": [],
        })


def test_plan_week_menu_input_rejects_over_7_days() -> None:
    with pytest.raises(ValidationError):
        PlanWeekMenuInput.model_validate({
            "week_start": "2026-05-25",
            "days": [{"day_of_week": i % 7, "meals": {}} for i in range(8)],
        })


def test_menu_cell_input_accepts_recipe_id() -> None:
    parsed = MenuCellInput.model_validate({"recipe_id": REC_A})
    assert parsed.recipe_id == REC_A


def test_menu_cell_input_accepts_free_text() -> None:
    parsed = MenuCellInput.model_validate({"free_text": "овсянка"})
    assert parsed.free_text == "овсянка"


def test_menu_cell_input_rejects_blank_free_text() -> None:
    with pytest.raises(ValidationError):
        MenuCellInput.model_validate({"free_text": "   "})


def test_menu_cell_input_rejects_bad_recipe_id() -> None:
    with pytest.raises(ValidationError):
        MenuCellInput.model_validate({"recipe_id": "rec_1"})


def test_menu_cell_input_rejects_both_recipe_id_and_free_text() -> None:
    """Codex Sub-A4 menu R1 MAJOR #4: XOR — both set means silent
    drop of free_text. Schema-time rejection."""
    with pytest.raises(ValidationError) as exc:
        MenuCellInput.model_validate({
            "recipe_id": REC_A,
            "free_text": "овсянка",
        })
    assert "EITHER recipe_id OR free_text" in str(exc.value)


def test_menu_cell_input_accepts_both_none_as_clear() -> None:
    """Both ``None`` (no notes either) is a legit explicit-clear cell."""
    parsed = MenuCellInput.model_validate({})
    assert parsed.recipe_id is None
    assert parsed.free_text is None
    assert parsed.notes is None


def test_menu_cell_input_rejects_notes_only_cell() -> None:
    """Codex Sub-A4 menu R2 MAJOR #4: notes-only cell would clear
    the meal AND silently drop the notes — planner thinks it added
    a hint, actually erased the slot. Reject."""
    with pytest.raises(ValidationError) as exc:
        MenuCellInput.model_validate({
            "notes": "после тренировки",
        })
    assert "notes-only cell" in str(exc.value)


def test_menu_cell_input_accepts_notes_with_recipe_id() -> None:
    """Notes attached to a real cell are fine."""
    parsed = MenuCellInput.model_validate({
        "recipe_id": REC_A,
        "notes": "после тренировки",
    })
    assert parsed.notes == "после тренировки"


def test_menu_cell_input_accepts_notes_with_free_text() -> None:
    """Notes attached to free_text are fine."""
    parsed = MenuCellInput.model_validate({
        "free_text": "овсянка",
        "notes": "с ягодами",
    })
    assert parsed.free_text == "овсянка"
    assert parsed.notes == "с ягодами"


def test_update_menu_item_input_accepts_real_ids() -> None:
    parsed = UpdateMenuItemInput.model_validate({
        "plan_id": MENU_A,
        "day_of_week": 2,
        "meal_type": "dinner",
        "recipe_id": REC_A,
    })
    assert parsed.plan_id == MENU_A
    assert parsed.meal_type == "dinner"


def test_update_menu_item_input_rejects_bad_plan_id() -> None:
    with pytest.raises(ValidationError):
        UpdateMenuItemInput.model_validate({
            "plan_id": "menu_short",
            "day_of_week": 0,
            "meal_type": "breakfast",
        })


def test_update_menu_item_input_rejects_bad_day_of_week() -> None:
    with pytest.raises(ValidationError):
        UpdateMenuItemInput.model_validate({
            "plan_id": MENU_A,
            "day_of_week": 7,  # 0-6 only
            "meal_type": "breakfast",
        })


def test_update_menu_item_input_rejects_unknown_meal_type() -> None:
    with pytest.raises(ValidationError):
        UpdateMenuItemInput.model_validate({
            "plan_id": MENU_A,
            "day_of_week": 0,
            "meal_type": "elevenses",
        })


def test_update_menu_item_input_rejects_both_recipe_id_and_free_text() -> None:
    """Codex Sub-A4 menu R1 MAJOR #4: same XOR rule as MenuCellInput."""
    with pytest.raises(ValidationError) as exc:
        UpdateMenuItemInput.model_validate({
            "plan_id": MENU_A,
            "day_of_week": 0,
            "meal_type": "breakfast",
            "recipe_id": REC_A,
            "free_text": "омлет",
        })
    assert "EITHER recipe_id OR free_text" in str(exc.value)


def test_update_menu_item_input_accepts_both_none_as_clear() -> None:
    """(None, None, None) is the explicit clear shape — runtime
    treats it as «erase this cell» (housewife_chat_tools.py:1336)."""
    parsed = UpdateMenuItemInput.model_validate({
        "plan_id": MENU_A,
        "day_of_week": 3,
        "meal_type": "dinner",
    })
    assert parsed.recipe_id is None
    assert parsed.free_text is None
    assert parsed.notes is None


def test_update_menu_item_input_rejects_notes_only() -> None:
    """Codex Sub-A4 menu R2 MAJOR #4: notes-only update would clear
    the cell AND silently drop the notes. Reject at schema time."""
    with pytest.raises(ValidationError) as exc:
        UpdateMenuItemInput.model_validate({
            "plan_id": MENU_A,
            "day_of_week": 3,
            "meal_type": "dinner",
            "notes": "лёгкий ужин",
        })
    assert "notes-only update" in str(exc.value)


def test_update_menu_item_input_accepts_notes_with_recipe_id() -> None:
    """Notes attached to a real recipe_id are fine."""
    parsed = UpdateMenuItemInput.model_validate({
        "plan_id": MENU_A,
        "day_of_week": 3,
        "meal_type": "dinner",
        "recipe_id": REC_A,
        "notes": "лёгкий ужин",
    })
    assert parsed.recipe_id == REC_A
    assert parsed.notes == "лёгкий ужин"


def test_list_menu_input_accepts_none_week() -> None:
    parsed = ListMenuInput.model_validate({})
    assert parsed.week_start is None


def test_list_menu_input_accepts_iso_week() -> None:
    parsed = ListMenuInput.model_validate({"week_start": "2026-05-25"})
    assert parsed.week_start == "2026-05-25"


def test_generate_shopping_from_menu_input_accepts_real_id() -> None:
    parsed = GenerateShoppingFromMenuInput.model_validate({"plan_id": MENU_A})
    assert parsed.plan_id == MENU_A


def test_generate_shopping_from_menu_input_rejects_short_id() -> None:
    with pytest.raises(ValidationError):
        GenerateShoppingFromMenuInput.model_validate({"plan_id": "menu_1"})


def test_clear_menu_input_accepts_iso() -> None:
    parsed = ClearMenuInput.model_validate({"week_start": "2026-05-25"})
    assert parsed.week_start == "2026-05-25"


def test_clear_menu_input_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        ClearMenuInput.model_validate({
            "week_start": "2026-05-25",
            "confirm": True,
        })


# ---------------------------------------------------------------------------
# Parser → output_model
# ---------------------------------------------------------------------------


def test_plan_week_menu_parser_returns_plan_created() -> None:
    parsed = parse_tool_output(
        "plan_week_menu", f"ok:plan_created:{MENU_A}:2026-05-25"
    )
    assert isinstance(parsed, PlanWeekMenuOk)
    assert parsed.menu_id == MENU_A
    assert parsed.week_start_iso == "2026-05-25"


def test_plan_week_menu_parser_returns_sentinel_for_malformed_id() -> None:
    parsed = parse_tool_output(
        "plan_week_menu", "ok:plan_created:menu_short:2026-05-25"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_update_menu_item_parser_returns_updated() -> None:
    parsed = parse_tool_output("update_menu_item", f"ok:updated:{MPI_A}")
    assert isinstance(parsed, UpdateMenuItemUpdated)
    assert parsed.item_id == MPI_A


def test_update_menu_item_parser_returns_cleared_or_not_found() -> None:
    parsed = parse_tool_output("update_menu_item", "ok:cleared_or_not_found")
    assert isinstance(parsed, UpdateMenuItemClearedOrNotFound)


def test_update_menu_item_parser_returns_sentinel_for_malformed_id() -> None:
    parsed = parse_tool_output("update_menu_item", "ok:updated:mpi_short")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_list_menu_parser_returns_empty() -> None:
    parsed = parse_tool_output("list_menu", "no menu plan for that week")
    assert isinstance(parsed, ListMenuEmpty)


def test_list_menu_parser_returns_ok() -> None:
    raw = (
        f"menu_id: {MENU_A}\n"
        "week_start: 2026-05-25\n"
        "\n"
        "Monday:\n"
        "  breakfast: овсянка\n"
    )
    parsed = parse_tool_output("list_menu", raw)
    assert isinstance(parsed, ListMenuOk)
    assert parsed.menu_id == MENU_A
    assert parsed.week_start_iso == "2026-05-25"


def test_list_menu_parser_returns_sentinel_for_malformed_id() -> None:
    raw = (
        "menu_id: menu_short\n"
        "week_start: 2026-05-25\n"
    )
    parsed = parse_tool_output("list_menu", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_generate_shopping_from_menu_parser_returns_generated() -> None:
    parsed = parse_tool_output(
        "generate_shopping_from_menu", "ok:generated:7:eaters=4"
    )
    assert isinstance(parsed, GenerateShoppingFromMenuOk)
    assert parsed.generated_count == 7
    assert parsed.eaters == 4


def test_generate_shopping_from_menu_parser_eaters_none_path() -> None:
    """Codex Sub-A4 menu R1 MAJOR #1: runtime emits ``ok:generated:0``
    (without ``:eaters=E``) for the free_text-only / unknown-plan
    early-return path (housewife_chat_tools.py:1490). Parser maps
    the missing eaters segment to ``eaters=None`` so composer can
    disambiguate from the «scaled but zero ingredients» case
    (which carries an explicit eaters >= 1)."""
    parsed = parse_tool_output(
        "generate_shopping_from_menu", "ok:generated:0"
    )
    assert isinstance(parsed, GenerateShoppingFromMenuOk)
    assert parsed.generated_count == 0
    assert parsed.eaters is None


def test_generate_shopping_from_menu_parser_rejects_eaters_zero() -> None:
    """Runtime ``count_eaters`` minimum is 1 — ``ok:generated:N:eaters=0``
    is malformed legacy output. Parser fail-closes via sentinel."""
    parsed = parse_tool_output(
        "generate_shopping_from_menu", "ok:generated:5:eaters=0"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_generate_shopping_from_menu_ok_rejects_nonzero_without_eaters() -> None:
    """Codex Sub-A4 menu R2 MAJOR #2: invariant ``eaters is None ⟺
    generated_count == 0``. Non-zero counts MUST carry an eaters
    value — the early-return path that emits ``ok:generated:0``
    without ``:eaters=E`` is the ONLY valid eaters=None case."""
    with pytest.raises(ValidationError) as exc:
        GenerateShoppingFromMenuOk.model_validate({
            "status": "generated",
            "generated_count": 5,
            "eaters": None,
        })
    assert "eaters is None" in str(exc.value)


def test_generate_shopping_from_menu_ok_allows_zero_with_eaters() -> None:
    """``generated_count == 0 && eaters > 0`` is allowed — represents
    «scaled correctly but recipes have no shoppable ingredients».
    Only ``generated_count > 0 && eaters is None`` is the forbidden
    shape."""
    parsed = GenerateShoppingFromMenuOk.model_validate({
        "status": "generated",
        "generated_count": 0,
        "eaters": 3,
    })
    assert parsed.generated_count == 0
    assert parsed.eaters == 3


def test_generate_shopping_from_menu_ok_allows_zero_with_none() -> None:
    """``generated_count == 0 && eaters is None`` — the early-return
    path that emits ``ok:generated:0``."""
    parsed = GenerateShoppingFromMenuOk.model_validate({
        "status": "generated",
        "generated_count": 0,
        "eaters": None,
    })
    assert parsed.generated_count == 0
    assert parsed.eaters is None


def test_clear_menu_parser_returns_cleared() -> None:
    parsed = parse_tool_output("clear_menu", "ok:cleared:1")
    assert isinstance(parsed, ClearMenuOk)
    assert parsed.cleared_count == 1


# ---------------------------------------------------------------------------
# Parser/output_model compatibility
# ---------------------------------------------------------------------------


_PARSER_HAPPY_PATH = {
    "plan_week_menu": f"ok:plan_created:{MENU_A}:2026-05-25",
    "update_menu_item": f"ok:updated:{MPI_A}",
    "list_menu": "no menu plan for that week",
    "generate_shopping_from_menu": "ok:generated:7:eaters=4",
    "clear_menu": "ok:cleared:1",
}


@pytest.mark.parametrize("spec", MENU_SPECS, ids=lambda s: s.name)
def test_every_menu_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS


@pytest.mark.parametrize("spec", MENU_SPECS, ids=lambda s: s.name)
def test_parser_output_validates_against_spec_output_model(spec) -> None:
    raw = _PARSER_HAPPY_PATH[spec.name]
    parsed = parse_tool_output(spec.name, raw)
    TypeAdapter(spec.output_model).validate_python(parsed.model_dump())


def test_sentinel_is_not_valid_against_any_menu_output_model() -> None:
    sentinel = parse_tool_output("plan_week_menu", "totally unparseable")
    assert isinstance(sentinel, ToolOutputContractViolation)
    sentinel_dump = sentinel.model_dump()
    for spec in MENU_SPECS:
        with pytest.raises(ValidationError):
            TypeAdapter(spec.output_model).validate_python(sentinel_dump)


def test_menu_family_header_lint_clean_in_full_registry() -> None:
    """Cross-family lint at the aggregate level. Menu specs legitimately
    name `search_recipes` / `add_shopping_items` in mutex_notes — those
    are migrated (recipes/shopping families). At the FULL migrated-set
    level, the linter sees them and doesn't flag. This test exercises
    the production invocation."""
    from sreda.services.tool_schemas.registry_quality import (
        validate_mutex_note_references,
    )
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    violations = validate_mutex_note_references(
        MIGRATED_TOOL_SPECS, manifest=TOOL_FAMILY_MANIFEST
    )
    # Filter to violations originating in menu-family specs only.
    menu_spec_names = {s.name for s in MENU_SPECS}
    menu_violations = [
        v for v in violations
        if v.tool_name in menu_spec_names
        and v.code in (
            "mutex_note_references_unmigrated_tool",
            "family_header_references_unmigrated_tool",
        )
    ]
    assert not menu_violations, (
        f"menu specs reference unmigrated tools at aggregate level: "
        f"{[(v.tool_name, v.field_path, v.message[:60]) for v in menu_violations]}"
    )


def test_migrated_tool_specs_aggregate_includes_menu() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    menu_names = {s.name for s in MENU_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert menu_names.issubset(migrated_names)


def test_migrated_tool_specs_pass_strict_with_menu() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
