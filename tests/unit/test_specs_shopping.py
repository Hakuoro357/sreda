"""Integration tests for the shopping family ToolSpec instances
(Sub-A4 — Plan-Execute Epic).

Coverage:
- All 7 SHOPPING_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: every shopping-family entry in
  TOOL_FAMILY_MANIFEST has a matching ToolSpec
- Per-tool: input_model rejects extra keys, parser produces
  output_model on the canonical "ok:..." string
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    AddShoppingItemsAdded,
    ClearBoughtShoppingOk,
    ListShoppingEmpty,
    MarkShoppingBoughtOk,
    RemoveShoppingItemsOk,
    UpdateShoppingItemOk,
    UpdateShoppingItemsCategoryOk,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
    validate_tool_registry_quality,
)
from sreda.services.tool_schemas.specs_shopping import (
    ADD_SHOPPING_ITEMS_SPEC,
    CLEAR_BOUGHT_SHOPPING_SPEC,
    LIST_SHOPPING_SPEC,
    MARK_SHOPPING_BOUGHT_SPEC,
    REMOVE_SHOPPING_ITEMS_SPEC,
    SHOPPING_SPECS,
    UPDATE_SHOPPING_ITEM_SPEC,
    UPDATE_SHOPPING_ITEMS_CATEGORY_SPEC,
    AddShoppingItemsInput,
    ClearBoughtShoppingInput,
    ListShoppingInput,
    MarkShoppingBoughtInput,
    RemoveShoppingItemsInput,
    UpdateShoppingItemInput,
    UpdateShoppingItemsCategoryInput,
)


# ---------------------------------------------------------------------------
# Registry-level invariants
# ---------------------------------------------------------------------------


def test_shopping_specs_count_is_seven() -> None:
    assert len(SHOPPING_SPECS) == 7


def test_shopping_specs_all_pass_strict_quality_lint() -> None:
    violations = validate_tool_registry_quality(SHOPPING_SPECS, strict=True)
    assert violations == [], (
        f"Strict quality lint surfaced {len(violations)} violation(s): "
        f"{[(v.tool_name, v.code, v.message[:80]) for v in violations]}"
    )


def test_shopping_specs_pass_assert_production_gate() -> None:
    """The CI acceptance gate raises if anything's off."""
    assert_production_registry_quality(SHOPPING_SPECS)


def test_shopping_specs_match_tool_family_manifest() -> None:
    """Every TOOL_FAMILY_MANIFEST entry mapped to 'shopping' must have
    a corresponding ToolSpec, and vice versa."""
    manifest_shopping = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "shopping"
    }
    spec_names = {s.name for s in SHOPPING_SPECS}
    assert spec_names == manifest_shopping, (
        f"Mismatch.\nIn manifest only: {manifest_shopping - spec_names}\n"
        f"In specs only: {spec_names - manifest_shopping}"
    )


def test_shopping_spec_names_are_unique() -> None:
    names = [s.name for s in SHOPPING_SPECS]
    assert len(names) == len(set(names))


def test_shopping_write_tools_declare_shopping_write_domain() -> None:
    """Every write tool in the family must declare ``shopping`` in
    write_domains — scheduler conflict detection invariant."""
    for spec in SHOPPING_SPECS:
        if spec.effect == "write":
            assert "shopping" in spec.write_domains, (
                f"{spec.name}: effect=write but 'shopping' not in "
                f"write_domains={spec.write_domains!r}"
            )


# ---------------------------------------------------------------------------
# Per-tool input model rejection of extra keys
# ---------------------------------------------------------------------------


def test_add_shopping_items_input_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        AddShoppingItemsInput.model_validate({
            "items": [{"title": "молоко"}],
            "hallucinated_extra": "value",
        })


def test_add_shopping_items_input_accepts_minimal_item() -> None:
    parsed = AddShoppingItemsInput.model_validate({
        "items": [{"title": "молоко"}]
    })
    assert parsed.items[0].title == "молоко"
    assert parsed.items[0].quantity_text is None
    assert parsed.items[0].category is None


def test_add_shopping_items_input_rejects_empty_items_list() -> None:
    """Plan validator catches `items=[]` at planning time, but the
    pydantic model also rejects it — defense in depth."""
    with pytest.raises(ValidationError):
        AddShoppingItemsInput.model_validate({"items": []})


def test_mark_shopping_bought_input_rejects_empty_ids() -> None:
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({"item_ids": []})


def test_update_shopping_item_input_accepts_partial_update() -> None:
    """Only item_id required; other fields default to None for
    «leave field unchanged» semantics in the tool implementation."""
    parsed = UpdateShoppingItemInput.model_validate({"item_id": "sh_42"})
    assert parsed.title is None
    assert parsed.quantity_text is None
    assert parsed.category is None


def test_update_shopping_items_category_input_requires_category() -> None:
    with pytest.raises(ValidationError):
        UpdateShoppingItemsCategoryInput.model_validate({
            "item_ids": ["sh_1", "sh_2"],
        })


def test_list_shopping_input_accepts_empty_dict() -> None:
    parsed = ListShoppingInput.model_validate({})
    assert isinstance(parsed, ListShoppingInput)


def test_list_shopping_input_rejects_any_arg() -> None:
    with pytest.raises(ValidationError):
        ListShoppingInput.model_validate({"unexpected": "value"})


def test_clear_bought_shopping_input_accepts_empty_dict() -> None:
    parsed = ClearBoughtShoppingInput.model_validate({})
    assert isinstance(parsed, ClearBoughtShoppingInput)


# ---------------------------------------------------------------------------
# End-to-end: parser returns the spec's output_model variant
# ---------------------------------------------------------------------------


def test_add_shopping_items_parser_returns_added_variant() -> None:
    parsed = parse_tool_output(
        "add_shopping_items", "ok:added:2:ids=[sh_1,sh_2]"
    )
    assert isinstance(parsed, AddShoppingItemsAdded)
    assert parsed.added_count == 2


def test_mark_shopping_bought_parser_returns_bought_variant() -> None:
    parsed = parse_tool_output("mark_shopping_bought", "ok:bought:3")
    assert isinstance(parsed, MarkShoppingBoughtOk)
    assert parsed.bought_count == 3


def test_remove_shopping_items_parser_returns_removed_variant() -> None:
    parsed = parse_tool_output("remove_shopping_items", "ok:removed:2")
    assert isinstance(parsed, RemoveShoppingItemsOk)
    assert parsed.removed_count == 2


def test_update_shopping_item_parser_returns_updated_variant() -> None:
    parsed = parse_tool_output("update_shopping_item", "ok:updated:sh_42")
    assert isinstance(parsed, UpdateShoppingItemOk)
    assert parsed.item_id == "sh_42"


def test_update_shopping_items_category_parser_returns_category_variant() -> None:
    parsed = parse_tool_output(
        "update_shopping_items_category", "ok:updated:5"
    )
    assert isinstance(parsed, UpdateShoppingItemsCategoryOk)
    assert parsed.updated_count == 5


def test_clear_bought_shopping_parser_returns_cleared_variant() -> None:
    parsed = parse_tool_output("clear_bought_shopping", "ok:cleared:10")
    assert isinstance(parsed, ClearBoughtShoppingOk)
    assert parsed.cleared_count == 10


def test_list_shopping_parser_returns_empty_variant() -> None:
    parsed = parse_tool_output("list_shopping", "no shopping items")
    assert isinstance(parsed, ListShoppingEmpty)
