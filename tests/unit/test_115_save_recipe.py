"""#115 Ф3 vertical slice — save_recipe returns the recipe NAME (red-before-impl).

Proves the full pipeline for the simplest migrated tool:
service str (okv2 with title) → parse_tool_ok → SaveRecipeOk(+display_summary @computed_field)
→ dispatch_typed_output round-trip (computed_field + extra='forbid') → presenter shows the NAME.
Legacy positional `ok:saved:rec_x` still parses (title=None) — legacy_compat=yes.
"""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.executor_contract import dispatch_typed_output
from sreda.services.tool_schemas.housewife import (
    SaveRecipeOk,
    SaveRecipeOutput,
    parse_save_recipe,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

REC = "rec_" + "a" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


def test_parse_okv2_carries_name():
    raw = encode_tool_ok("saved", {"recipe_id": REC, "title": "Борщ"})
    parsed = parse_save_recipe(raw)
    assert isinstance(parsed, SaveRecipeOk)
    assert parsed.status == "saved"
    assert parsed.recipe_id == REC
    assert parsed.title == "Борщ"  # the NAME, not just the id (the #74 deliverable)


def test_display_summary_shows_name_not_id():
    raw = encode_tool_ok("saved", {"recipe_id": REC, "title": "Борщ"})
    parsed = parse_save_recipe(raw)
    summary = parsed.display_summary
    assert "Борщ" in summary
    assert REC not in summary  # raw id never leaks


def test_duplicate_status_label():
    raw = encode_tool_ok("duplicate", {"recipe_id": REC, "title": "Борщ"})
    parsed = parse_save_recipe(raw)
    assert parsed.status == "duplicate"
    assert "Борщ" in parsed.display_summary


def test_dispatch_round_trip_with_computed_field():
    # The foundation fix: computed_field + extra='forbid' must survive the
    # defensive model_dump→TypeAdapter revalidation in dispatch_typed_output.
    raw = encode_tool_ok("saved", {"recipe_id": REC, "title": "Плов"})
    model = dispatch_typed_output("save_recipe", raw, SaveRecipeOutput)
    assert isinstance(model, SaveRecipeOk)
    assert model.title == "Плов"
    assert "Плов" in model.display_summary
    # the dict the executor stores for the presenter carries the computed field
    assert "Плов" in model.model_dump()["display_summary"]


def test_presenter_renders_name_via_display_field():
    raw = encode_tool_ok("saved", {"recipe_id": REC, "title": "Окрошка"})
    parsed = parse_save_recipe(raw)
    text = render_display_text("save_recipe", parsed.model_dump(), domain_status="saved")
    assert "Окрошка" in text
    assert REC not in text


def test_legacy_positional_still_parses_without_name():
    # historical/replay output (no name) — legacy_compat=yes
    parsed = parse_save_recipe(f"ok:saved:{REC}")
    assert isinstance(parsed, SaveRecipeOk)
    assert parsed.title is None
    assert parsed.display_summary == "Готово."


def test_malformed_okv2_is_contract_violation():
    # bad payload → fail-closed sentinel (executor → planner_gap → unknown_outcome)
    parsed = parse_save_recipe("okv2:saved:{not json")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_okv2_name_with_separators_survives():
    raw = encode_tool_ok("saved", {"recipe_id": REC, "title": "Салат: оливье, праздничный"})
    parsed = parse_save_recipe(raw)
    assert parsed.title == "Салат: оливье, праздничный"
    assert "Салат: оливье, праздничный" in parsed.display_summary
