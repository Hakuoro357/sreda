"""Integration tests for the shopping family ToolSpec instances
(Sub-A4 — Plan-Execute Epic).

Coverage:
- All 7 SHOPPING_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: every shopping-family entry in
  TOOL_FAMILY_MANIFEST has a matching ToolSpec
- Per-tool: input_model rejects extra keys, parser produces
  output_model on the canonical "ok:..." string
- Codex Sub-A4 R2: tight ID aliases, string caps match runtime,
  update quantity_text="" accepted, output ID fail-closed,
  cannot_parse_trigger_iso stable, sentinel boundary regression.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    AddShoppingItemsAdded,
    ClearBoughtShoppingOk,
    HousewifeToolError,
    ListShoppingEmpty,
    MarkShoppingBoughtOk,
    PARSERS,
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
    SHOPPING_SPECS,
    UPDATE_SHOPPING_ITEM_SPEC,
    AddShoppingItemsInput,
    ClearBoughtShoppingInput,
    ListShoppingInput,
    MarkShoppingBoughtInput,
    UpdateShoppingItemInput,
    UpdateShoppingItemsCategoryInput,
)

# ---------------------------------------------------------------------------
# Real-shape test IDs. Codex R2 MAJOR #1 — must match
# ``^sh_[0-9a-f]{24}$`` etc. exactly (the runtime emits
# ``f"sh_{uuid4().hex[:24]}"``). Lowercase hex only.
# ---------------------------------------------------------------------------

SH_A = "sh_aaaaaaaaaaaaaaaaaaaaaaaa"
SH_B = "sh_bbbbbbbbbbbbbbbbbbbbbbbb"
SH_C = "sh_cccccccccccccccccccccccc"
REM_A = "rem_dddddddddddddddddddddddd"


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
    a corresponding ToolSpec WITH matching ``spec.family``, and vice
    versa (Codex Sub-A4 R1 MAJOR #3 — name-only comparison let a
    misclassified family slip through previously)."""
    manifest_shopping = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "shopping"
    }
    spec_names = {s.name for s in SHOPPING_SPECS}
    assert spec_names == manifest_shopping, (
        f"Mismatch.\nIn manifest only: {manifest_shopping - spec_names}\n"
        f"In specs only: {spec_names - manifest_shopping}"
    )
    # Cross-check: every spec's family field equals manifest's mapping.
    for spec in SHOPPING_SPECS:
        expected = TOOL_FAMILY_MANIFEST[spec.name]
        assert spec.family == expected, (
            f"{spec.name}: spec.family={spec.family!r} but manifest "
            f"says {expected!r}. Renderer would misgroup this tool."
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


def test_add_shopping_items_input_rejects_quantity_over_64() -> None:
    """Codex R3 MAJOR #1: ``ShoppingItemInput.quantity_text`` was
    previously typed as ``ShoppingTitle`` (500-char cap in JSON schema)
    with a ``model_validator`` enforcing 64. Field-level
    ``AddQuantityText`` puts the 64 cap in the JSON schema where the
    planner's refs-present validation path can see it."""
    with pytest.raises(ValidationError):
        AddShoppingItemsInput.model_validate({
            "items": [{"title": "молоко", "quantity_text": "x" * 65}]
        })


def test_add_shopping_items_input_rejects_blank_quantity() -> None:
    """Codex R3 MAJOR #1: ``AddQuantityText`` requires non-blank on
    add — empty/whitespace would be silently dropped at runtime
    (``housewife_shopping.py:253`` strips before saving, ``or None``
    branch nulls empty)."""
    with pytest.raises(ValidationError):
        AddShoppingItemsInput.model_validate({
            "items": [{"title": "молоко", "quantity_text": ""}]
        })


def test_add_shopping_items_input_accepts_quantity_at_boundary() -> None:
    """64-char quantity at exact runtime cap is accepted."""
    parsed = AddShoppingItemsInput.model_validate({
        "items": [{"title": "молоко", "quantity_text": "x" * 64}]
    })
    assert len(parsed.items[0].quantity_text) == 64


def test_add_shopping_items_input_rejects_empty_items_list() -> None:
    """Plan validator catches `items=[]` at planning time, but the
    pydantic model also rejects it — defense in depth."""
    with pytest.raises(ValidationError):
        AddShoppingItemsInput.model_validate({"items": []})


def test_mark_shopping_bought_input_rejects_empty_ids() -> None:
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({"item_ids": []})


def test_mark_shopping_bought_input_rejects_id_without_sh_prefix() -> None:
    """Codex R1 MAJOR #2: ``ShoppingItemId`` enforces ``sh_…`` prefix."""
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({"item_ids": ["42"]})


def test_mark_shopping_bought_input_rejects_blank_id() -> None:
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({"item_ids": [""]})


def test_mark_shopping_bought_input_rejects_whitespace_only_id() -> None:
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({"item_ids": ["   "]})


def test_mark_shopping_bought_input_accepts_real_shape_id() -> None:
    """Codex R2 MAJOR #1: tight ``^sh_[0-9a-f]{24}$`` must accept the
    real runtime shape produced by ``f"sh_{uuid4().hex[:24]}"``."""
    parsed = MarkShoppingBoughtInput.model_validate({"item_ids": [SH_A, SH_B]})
    assert parsed.item_ids == [SH_A, SH_B]


def test_mark_shopping_bought_input_rejects_short_id() -> None:
    """Codex R2 MAJOR #1: ``sh_1`` no longer accepted (was accepted by
    the loose ``^sh_\\S+$`` pattern in R1)."""
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({"item_ids": ["sh_1"]})


def test_mark_shopping_bought_input_rejects_uppercase_hex() -> None:
    """uuid4().hex emits lowercase only — uppercase is a planner typo."""
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({
            "item_ids": ["sh_AAAAAAAAAAAAAAAAAAAAAAAA"]
        })


def test_mark_shopping_bought_input_rejects_non_hex_chars() -> None:
    """Non-hex char in suffix — would never come from uuid4().hex."""
    with pytest.raises(ValidationError):
        MarkShoppingBoughtInput.model_validate({
            "item_ids": ["sh_zzzzzzzzzzzzzzzzzzzzzzzz"]
        })


def test_update_shopping_item_input_requires_at_least_one_mutable_field() -> None:
    """Codex R1 MAJOR #2: ``{"item_id": "sh_<id>"}`` with no
    title/quantity/category is a no-op call — rejected by model_validator."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({"item_id": SH_A})


def test_update_shopping_item_input_rejects_explicit_null_title() -> None:
    """Codex R3 MAJOR #2: explicit ``"title": null`` was previously
    accepted because R2's ``model_fields_set`` check counted explicit
    null as «field provided». Runtime line 396-397 short-circuits on
    ``title is None`` — null is a silent no-op for the runtime, so the
    schema must also reject it as a not-actually-provided contribution
    to the mutable-fields requirement."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": SH_A,
            "title": None,
        })


def test_update_shopping_item_input_rejects_explicit_null_quantity_text() -> None:
    """Codex R3 MAJOR #2: ``"quantity_text": null`` is a no-op at runtime
    (housewife_shopping.py:400 ``if quantity_text is not None`` skips
    the assignment block). Must reject as a no-op contribution; empty
    string is different (means «clear»)."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": SH_A,
            "quantity_text": None,
        })


def test_update_shopping_item_input_rejects_all_explicit_nulls() -> None:
    """Codex R3 MAJOR #2: every mutable field as explicit null →
    nothing actually mutated at runtime → reject."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": SH_A,
            "title": None,
            "quantity_text": None,
            "category": None,
        })


def test_update_shopping_item_input_accepts_empty_qty_with_null_title() -> None:
    """Codex R3 MAJOR #2: as long as ONE mutable field is actually
    provided (non-null OR the empty-string clear-intent), the call is
    valid even if the others are explicit null."""
    parsed = UpdateShoppingItemInput.model_validate({
        "item_id": SH_A,
        "title": None,
        "quantity_text": "",
        "category": None,
    })
    assert parsed.quantity_text == ""
    assert parsed.title is None
    assert parsed.category is None


def test_update_shopping_item_input_accepts_title_only() -> None:
    parsed = UpdateShoppingItemInput.model_validate({
        "item_id": SH_A,
        "title": "новый хлеб",
    })
    assert parsed.title == "новый хлеб"
    assert parsed.quantity_text is None


def test_update_shopping_item_input_rejects_typo_id() -> None:
    """``item_id="sh42"`` without underscore should fail the pattern."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": "sh42",  # missing underscore
            "title": "хлеб",
        })


def test_update_shopping_item_input_accepts_empty_quantity_text_for_clear() -> None:
    """Codex R2 MAJOR #3: ``quantity_text=""`` is a legitimate intent
    («убери количество у молока») — runtime ``housewife_shopping.py:401-402``
    maps empty string to ``None`` (clears the quantity). Was rejected
    by R1's ``ShortStr | None`` (min_length=1)."""
    parsed = UpdateShoppingItemInput.model_validate({
        "item_id": SH_A,
        "quantity_text": "",
    })
    assert parsed.quantity_text == ""


def test_update_shopping_item_input_rejects_empty_only_call() -> None:
    """Codex R2 MAJOR #3 corner: empty quantity_text DOES count as a
    mutable field (it's a real clear intent). But submitting NOTHING
    still fails — the model_validator switched to ``model_fields_set``
    semantics, so missing fields are different from explicit empty."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({"item_id": SH_A})


def test_update_shopping_item_input_accepts_long_title_up_to_500() -> None:
    """Codex R2 MAJOR #2: ``ShoppingTitle`` cap matches runtime
    ``title[:500]`` (housewife_shopping.py:252). Was capped at 200 by
    ``ShortStr`` — silently truncated long titles."""
    long_title = "a" * 500
    parsed = UpdateShoppingItemInput.model_validate({
        "item_id": SH_A,
        "title": long_title,
    })
    assert len(parsed.title) == 500


def test_update_shopping_item_input_rejects_title_over_500() -> None:
    """Codex R2 MAJOR #2: just past the runtime cap should fail
    validation rather than silently truncate downstream."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": SH_A,
            "title": "a" * 501,
        })


def test_update_shopping_item_input_rejects_quantity_over_64() -> None:
    """Codex R2 MAJOR #2: ``QuantityText`` cap matches runtime
    ``quantity_text=...[:64]`` (housewife_shopping.py:253)."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": SH_A,
            "quantity_text": "x" * 65,
        })


def test_update_shopping_item_input_rejects_category_over_64() -> None:
    """Codex R2 MAJOR #2: ``CategoryName`` cap matches runtime
    ``_normalize_category`` returning ``[:64]`` (housewife_shopping.py:96)."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemInput.model_validate({
            "item_id": SH_A,
            "category": "x" * 65,
        })


def test_update_shopping_items_category_input_requires_category() -> None:
    with pytest.raises(ValidationError):
        UpdateShoppingItemsCategoryInput.model_validate({
            "item_ids": [SH_A, SH_B],
        })


def test_update_shopping_items_category_input_rejects_single_id() -> None:
    """Codex R1 MINOR #8: bulk requires ≥2 ids — single-id reassignment
    should use ``update_shopping_item`` (more specific return shape)."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemsCategoryInput.model_validate({
            "item_ids": [SH_A],
            "category": "молочные",
        })


def test_update_shopping_items_category_input_rejects_long_category() -> None:
    """Codex R2 MAJOR #2: bulk category was ``NonBlankStr`` (uncapped) —
    now uses ``CategoryName`` (≤64) to match runtime."""
    with pytest.raises(ValidationError):
        UpdateShoppingItemsCategoryInput.model_validate({
            "item_ids": [SH_A, SH_B],
            "category": "x" * 65,
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
        "add_shopping_items", f"ok:added:2:ids=[{SH_A},{SH_B}]"
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
    parsed = parse_tool_output("update_shopping_item", f"ok:updated:{SH_A}")
    assert isinstance(parsed, UpdateShoppingItemOk)
    assert parsed.item_id == SH_A


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


# ---------------------------------------------------------------------------
# Codex Sub-A4 R1 CRITICAL — stable error codes for dynamic-message errors
# ---------------------------------------------------------------------------


def test_update_shopping_item_not_found_has_stable_error_code() -> None:
    """``error: item 'sh_42' not found`` and ``error: item 'sh_7' not
    found`` must produce the SAME error_code so the planner can branch
    on it deterministically (Codex R1 CRITICAL)."""
    a = parse_tool_output("update_shopping_item", "error: item 'sh_42' not found")
    b = parse_tool_output("update_shopping_item", "error: item 'sh_7' not found")
    assert isinstance(a, HousewifeToolError)
    assert isinstance(b, HousewifeToolError)
    assert a.error_code == "item_not_found"
    assert b.error_code == "item_not_found"


# ---------------------------------------------------------------------------
# Codex Sub-A4 R2 MAJOR #6 — cannot_parse_trigger_iso stable code
# ---------------------------------------------------------------------------


def test_schedule_reminder_cannot_parse_trigger_iso_has_stable_code() -> None:
    """``error: cannot parse trigger_iso='завтра'`` and
    ``error: cannot parse trigger_iso='вчера'`` must produce the SAME
    error_code so reminder-parsing failures branch deterministically
    (Codex R2 MAJOR #6 — was value-dependent like
    ``cannot_parse_trigger_iso='завтра'``)."""
    a = parse_tool_output(
        "schedule_reminder", "error: cannot parse trigger_iso='завтра'"
    )
    b = parse_tool_output(
        "schedule_reminder", "error: cannot parse trigger_iso='вчера'"
    )
    assert isinstance(a, HousewifeToolError)
    assert isinstance(b, HousewifeToolError)
    assert a.error_code == "cannot_parse_trigger_iso"
    assert b.error_code == "cannot_parse_trigger_iso"


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #5 — parser output validates against spec.output_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", SHOPPING_SPECS, ids=lambda s: s.name)
def test_every_shopping_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS, (
        f"Tool {spec.name!r} has a ToolSpec but no parser in PARSERS. "
        f"Executor wouldn't know how to typed-decode its raw output."
    )


_PARSER_HAPPY_PATH = {
    "add_shopping_items": f"ok:added:1:ids=[{SH_A}]",
    "mark_shopping_bought": "ok:bought:3",
    "remove_shopping_items": "ok:removed:2",
    "update_shopping_item": f"ok:updated:{SH_A}",
    "update_shopping_items_category": "ok:updated:5",
    "list_shopping": "no shopping items",
    "clear_bought_shopping": "ok:cleared:10",
}


@pytest.mark.parametrize("spec", SHOPPING_SPECS, ids=lambda s: s.name)
def test_parser_output_validates_against_spec_output_model(spec) -> None:
    """Parser-typed output for each tool must be a valid instance of
    that tool's discriminator union — catches drift between parser
    branches and spec.output_model variants (Codex R1 MAJOR #5)."""
    raw = _PARSER_HAPPY_PATH[spec.name]
    parsed = parse_tool_output(spec.name, raw)
    # Dump-then-validate via the spec's output_model TypeAdapter —
    # exercises the discriminator routing end-to-end.
    TypeAdapter(spec.output_model).validate_python(parsed.model_dump())


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #4 — output IDs are also tightened; malformed legacy
# output returns ToolOutputContractViolation (fail-closed)
# ---------------------------------------------------------------------------


def test_update_shopping_item_parser_returns_sentinel_for_malformed_id() -> None:
    """``ok:updated:sh_garbage`` matches the parser regex but fails the
    tight ``ShoppingItemId`` constraint — must fall through to the
    sentinel rather than emit a bad id to the planner."""
    parsed = parse_tool_output("update_shopping_item", "ok:updated:sh_garbage")
    assert isinstance(parsed, ToolOutputContractViolation)
    assert parsed.tool_name == "update_shopping_item"


def test_add_shopping_items_parser_returns_sentinel_for_malformed_ids() -> None:
    """``ok:added:2:ids=[sh_1,sh_2]`` was R1's happy-path test fixture —
    R2 tightening rejects it because the suffixes are not 24-hex."""
    parsed = parse_tool_output(
        "add_shopping_items", "ok:added:2:ids=[sh_1,sh_2]"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_add_shopping_items_parser_rejects_zero_count_with_ids() -> None:
    """Codex R3 MINOR: ``ok:added:0:ids=[sh_x]`` is internally
    inconsistent (count says nothing added, ids claims a row was
    created). Symmetric with the count/id mismatch guard on the
    count>0 path — fail-closed via sentinel rather than silently
    treat as ``empty``."""
    parsed = parse_tool_output(
        "add_shopping_items", f"ok:added:0:ids=[{SH_A}]"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_add_shopping_items_parser_accepts_zero_count_no_ids() -> None:
    """The all-duplicate case ``ok:added:0`` (no ids group) is the
    legitimate empty variant — still returns ``AddShoppingItemsEmpty``."""
    from sreda.services.tool_schemas.housewife import AddShoppingItemsEmpty
    parsed = parse_tool_output("add_shopping_items", "ok:added:0")
    assert isinstance(parsed, AddShoppingItemsEmpty)


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #5 — ToolOutputContractViolation boundary regression
# The sentinel is intentionally NOT in any output_model union. Executor
# MUST catch it before output_model validation. This test pins that
# contract so a future wrapper can't silently accept the sentinel.
# ---------------------------------------------------------------------------


def test_sentinel_is_not_valid_against_any_shopping_output_model() -> None:
    """``ToolOutputContractViolation`` must FAIL ``TypeAdapter`` validation
    against every shopping ``output_model`` — proves the executor must
    catch the sentinel BEFORE output_model validation (Codex R2 MAJOR #5).
    If a future wrapper accidentally lets it through, this test breaks."""
    sentinel = parse_tool_output("update_shopping_item", "totally unparseable")
    assert isinstance(sentinel, ToolOutputContractViolation)
    sentinel_dump = sentinel.model_dump()
    for spec in SHOPPING_SPECS:
        with pytest.raises(ValidationError):
            TypeAdapter(spec.output_model).validate_python(sentinel_dump)


# ---------------------------------------------------------------------------
# Codex R1 MINOR #9 — error-path tests for new parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name,raw,expected_code", [
    ("mark_shopping_bought", "error: empty item_ids", "empty_item_ids"),
    ("remove_shopping_items", "error: no user_id context", "no_user_id_context"),
    ("update_shopping_item", "error: item 'sh_42' not found", "item_not_found"),
    ("update_shopping_items_category", "error: empty item_ids", "empty_item_ids"),
    ("clear_bought_shopping", "error: internal", "internal"),
])
def test_new_parsers_error_paths(tool_name, raw, expected_code) -> None:
    parsed = parse_tool_output(tool_name, raw)
    assert isinstance(parsed, HousewifeToolError), (
        f"Expected HousewifeToolError; got {type(parsed).__name__}"
    )
    assert parsed.error_code == expected_code


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #4 — central registry aggregator
# ---------------------------------------------------------------------------


def test_migrated_tool_specs_aggregate_includes_shopping() -> None:
    """The central aggregator must include every shopping spec — Sub-A4
    phases 2-7 will append the other families."""
    from sreda.services.tool_schemas.specs import (
        ALL_TOOL_SPECS,
        MIGRATED_TOOL_SPECS,
    )
    shopping_names = {s.name for s in SHOPPING_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert shopping_names.issubset(migrated_names), (
        f"Missing from MIGRATED_TOOL_SPECS: "
        f"{shopping_names - migrated_names}"
    )
    assert ALL_TOOL_SPECS == MIGRATED_TOOL_SPECS  # alias for now


def test_migrated_tool_specs_pass_strict_production_quality() -> None:
    """Full aggregate passes the CI acceptance gate."""
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #1 — refs-aware no-op rejector via
# ``ToolSpec.required_any_non_null_args`` + ``.validate_args_satisfy_required_any``
# Closes the gap where the planner's refs-present validation skips
# input_model's @model_validator (Codex Sub-A-77 R6 MAJOR #1).
# ---------------------------------------------------------------------------


def test_update_shopping_item_spec_declares_required_any_non_null() -> None:
    """The spec encodes which fields the planner's static validator
    must check, independent of the input_model's model_validator."""
    assert UPDATE_SHOPPING_ITEM_SPEC.required_any_non_null_args == [
        "title",
        "quantity_text",
        "category",
    ]


def test_validate_args_satisfy_required_any_rejects_refs_only_no_mutable() -> None:
    """Codex R4 MAJOR #1: the common ``update_shopping_item`` flow uses
    refs for ``item_id`` (from ``list_shopping``). Without any mutable
    field, the call is a silent no-op — must be rejected even when
    the input_model's @model_validator is skipped on the refs path."""
    with pytest.raises(ValueError, match="at least one of"):
        UPDATE_SHOPPING_ITEM_SPEC.validate_args_satisfy_required_any(
            {"item_id": "${s1.items[0].item_id}"}
        )


def test_validate_args_satisfy_required_any_accepts_ref_for_mutable_field() -> None:
    """A ref to a mutable field is non-null-by-shape (planner trusts
    upstream output schemas). Should pass even though we can't yet
    see the resolved value."""
    UPDATE_SHOPPING_ITEM_SPEC.validate_args_satisfy_required_any(
        {"item_id": SH_A, "title": "${s2.recipe.title}"}
    )  # no raise


def test_validate_args_satisfy_required_any_accepts_literal_mutable() -> None:
    """At least one mutable field with a literal non-null value
    satisfies the requirement."""
    UPDATE_SHOPPING_ITEM_SPEC.validate_args_satisfy_required_any(
        {"item_id": SH_A, "title": "новое название"}
    )  # no raise


def test_validate_args_satisfy_required_any_accepts_empty_quantity() -> None:
    """Empty string is non-null — it's the «clear quantity» intent for
    shopping. Must satisfy the «at least one provided» requirement."""
    UPDATE_SHOPPING_ITEM_SPEC.validate_args_satisfy_required_any(
        {"item_id": SH_A, "quantity_text": ""}
    )  # no raise


def test_validate_args_satisfy_required_any_rejects_explicit_null_only() -> None:
    """All explicit nulls is the same as «nothing provided» — runtime
    short-circuits on ``X is not None`` for every mutable field."""
    with pytest.raises(ValueError):
        UPDATE_SHOPPING_ITEM_SPEC.validate_args_satisfy_required_any(
            {
                "item_id": SH_A,
                "title": None,
                "quantity_text": None,
                "category": None,
            }
        )


def test_validate_args_satisfy_required_any_noop_when_field_unset() -> None:
    """Tools without the field (e.g. ``add_shopping_items``) do NOT
    enforce this rule — their input_model already requires a non-empty
    items list."""
    ADD_SHOPPING_ITEMS_SPEC.validate_args_satisfy_required_any(
        {"items": []}
    )  # no raise — separate concern handled by input_model min_length


def test_tool_spec_rejects_required_any_non_null_args_with_unknown_field() -> None:
    """Codex R4 MAJOR #1 construction-time guard: names in
    ``required_any_non_null_args`` must exist on ``input_model``. A
    typo here would silently let no-op calls through the planner's
    static validator."""
    with pytest.raises(ValidationError):
        # Take the canonical spec but try to require a name that
        # doesn't exist on UpdateShoppingItemInput.
        UPDATE_SHOPPING_ITEM_SPEC.model_copy(
            update={
                "required_any_non_null_args": [
                    "title",
                    "missing_typo_field",
                ]
            }
        ).model_validate(
            UPDATE_SHOPPING_ITEM_SPEC.model_copy(
                update={
                    "required_any_non_null_args": [
                        "title",
                        "missing_typo_field",
                    ]
                }
            ).model_dump()
        )


def test_tool_spec_rejects_required_any_non_null_args_empty_list() -> None:
    """Empty list is meaningless («accept everything as no-op») —
    construction-time reject so the field's intent is preserved."""
    with pytest.raises(ValidationError):
        UPDATE_SHOPPING_ITEM_SPEC.model_validate(
            UPDATE_SHOPPING_ITEM_SPEC.model_copy(
                update={"required_any_non_null_args": []}
            ).model_dump()
        )


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #2 — process_output() as canonical executor entry point
# Phase B's executor MUST call this method (not reimplement the
# parse→sentinel→validate ordering).
# ---------------------------------------------------------------------------


def test_tool_spec_process_output_delegates_to_dispatch_typed_output() -> None:
    """``ToolSpec.process_output(raw)`` is the canonical Phase-B entry
    point. Internally delegates to ``executor_contract.dispatch_typed_output``
    so the boundary contract has a single source of truth — any future
    code that goes through ``spec.process_output(...)`` inherits the
    correct parse→sentinel→validate ordering automatically."""
    result = UPDATE_SHOPPING_ITEM_SPEC.process_output(f"ok:updated:{SH_A}")
    assert result.status == "updated"
    assert result.item_id == SH_A


def test_tool_spec_process_output_raises_planner_gap_on_sentinel() -> None:
    """Unparseable raw output surfaces as ``PlannerGapError``, NOT as
    a generic ``ValidationError`` — proves Phase B can rely on
    catching this specific exception type to record
    ``planner_gaps(gap_type='contract_violation')``."""
    from sreda.services.tool_schemas.executor_contract import PlannerGapError
    with pytest.raises(PlannerGapError):
        UPDATE_SHOPPING_ITEM_SPEC.process_output("totally unparseable")


def test_dispatch_typed_output_exposed_at_package_level() -> None:
    """Codex R4 MAJOR #2: ``dispatch_typed_output`` and
    ``PlannerGapError`` are part of the public ``tool_schemas`` API
    so Phase B imports them directly instead of reimplementing the
    ordering. The boundary tests then exercise the actual production
    code path."""
    from sreda.services import tool_schemas
    assert hasattr(tool_schemas, "dispatch_typed_output")
    assert hasattr(tool_schemas, "PlannerGapError")
