"""Boundary-contract tests for ``executor_contract.dispatch_typed_output``
(Codex Sub-A4 R2/R3 MAJOR #5).

These tests pin the ORDER in which the executor must process a raw
tool ``str`` output:

  1. Parse
  2. If sentinel → raise ``PlannerGapError`` (executor writes planner_gaps)
  3. Else → ``TypeAdapter(output_model).validate_python(...)`` (defensive)
  4. Return validated instance

A future executor that accidentally swaps the order — validating before
checking for the sentinel — would surface ``ToolOutputContractViolation``
as a generic ``ValidationError`` instead of the intended fail-closed
``contract_violation`` ``planner_gaps`` path. These tests break in that
case.
"""

from __future__ import annotations

import pytest

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.executor_contract import (
    PlannerGapError,
    dispatch_typed_output,
)
from sreda.services.tool_schemas.specs_shopping import (
    ADD_SHOPPING_ITEMS_SPEC,
    UPDATE_SHOPPING_ITEM_SPEC,
)


SH_VALID = "sh_aaaaaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# STEP 2 — sentinel path: unparseable raw output → PlannerGapError
# ---------------------------------------------------------------------------


def test_sentinel_path_raises_planner_gap_error() -> None:
    """Unknown raw → parser returns ``ToolOutputContractViolation`` →
    ``dispatch_typed_output`` MUST raise ``PlannerGapError`` and NOT
    fall through to ``TypeAdapter`` validation."""
    with pytest.raises(PlannerGapError) as exc_info:
        dispatch_typed_output(
            "update_shopping_item",
            "totally unparseable raw output",
            UPDATE_SHOPPING_ITEM_SPEC.output_model,
        )
    sentinel = exc_info.value.sentinel
    assert isinstance(sentinel, ToolOutputContractViolation)
    assert sentinel.tool_name == "update_shopping_item"
    assert sentinel.raw_output == "totally unparseable raw output"


def test_sentinel_path_carries_raw_for_planner_gaps() -> None:
    """Executor needs the raw output verbatim to record a useful
    ``planner_gaps`` row (so GEPA can later train against it)."""
    raw = "ok:unknown_status_we_havent_seen_before"
    with pytest.raises(PlannerGapError) as exc_info:
        dispatch_typed_output(
            "update_shopping_item",
            raw,
            UPDATE_SHOPPING_ITEM_SPEC.output_model,
        )
    assert exc_info.value.sentinel.raw_output == raw


def test_sentinel_path_for_malformed_id_in_legacy_output() -> None:
    """Codex R2 MAJOR #4 + R3 boundary: tight ID pattern rejects
    legacy malformed output; parser produces sentinel; executor
    raises PlannerGapError rather than letting bad id reach planner."""
    with pytest.raises(PlannerGapError):
        # Parser regex matches ``ok:updated:<id>`` but the id fails
        # the tight ``^sh_[0-9a-f]{24}$`` constraint → sentinel.
        dispatch_typed_output(
            "update_shopping_item",
            "ok:updated:sh_garbage",
            UPDATE_SHOPPING_ITEM_SPEC.output_model,
        )


def test_sentinel_path_for_add_with_malformed_ids() -> None:
    """Same fail-closed contract for ``add_shopping_items`` when legacy
    output carries short / non-hex IDs."""
    with pytest.raises(PlannerGapError):
        dispatch_typed_output(
            "add_shopping_items",
            "ok:added:2:ids=[sh_1,sh_2]",
            ADD_SHOPPING_ITEMS_SPEC.output_model,
        )


# ---------------------------------------------------------------------------
# STEP 3 — happy path: parser succeeds → TypeAdapter validates → return
# ---------------------------------------------------------------------------


def test_happy_path_returns_validated_output() -> None:
    """Well-formed raw → parser produces typed object → output_model
    validates → result returned for planner to branch on."""
    result = dispatch_typed_output(
        "update_shopping_item",
        f"ok:updated:{SH_VALID}",
        UPDATE_SHOPPING_ITEM_SPEC.output_model,
    )
    # TypeAdapter returns the same shape after validation.
    assert result.status == "updated"
    assert result.item_id == SH_VALID


def test_happy_path_error_variant_routes_through_union() -> None:
    """``error: item not found`` parses to a ``HousewifeToolError``
    variant. The discriminator union accepts both ``"updated"`` and
    ``"error"`` for ``update_shopping_item``."""
    result = dispatch_typed_output(
        "update_shopping_item",
        "error: item 'sh_42' not found",
        UPDATE_SHOPPING_ITEM_SPEC.output_model,
    )
    assert result.status == "error"
    assert result.error_code == "item_not_found"


# ---------------------------------------------------------------------------
# STEP 1 — unknown tool name → sentinel from parse_tool_output → PlannerGapError
# ---------------------------------------------------------------------------


def test_unknown_tool_name_raises_planner_gap_error() -> None:
    """A tool name without a registered parser also surfaces as
    sentinel (parser registry says «I don't know how to decode this»),
    which the executor handles via the same fail-closed path."""
    with pytest.raises(PlannerGapError):
        dispatch_typed_output(
            "tool_with_no_parser_registered",
            "anything",
            UPDATE_SHOPPING_ITEM_SPEC.output_model,  # output_model irrelevant
        )


# ---------------------------------------------------------------------------
# Ordering proof — what would break if a wrapper swapped steps 2 and 3
# ---------------------------------------------------------------------------


def test_ordering_invariant_sentinel_before_validation() -> None:
    """If a future wrapper accidentally swaps the order — validates
    BEFORE checking for sentinel — the failure mode changes from
    ``PlannerGapError`` (fail-closed planner gap path) to a generic
    ``ValidationError`` (which the executor handles as a different
    failure type, NOT contract_violation).

    Proof: the sentinel is NOT a valid member of any output_model
    union (sentinel-boundary contract from R2). So if validation
    runs first, it raises ``ValidationError``. The
    ``dispatch_typed_output`` contract demands sentinel check first
    so the caller sees ``PlannerGapError`` instead."""
    with pytest.raises(PlannerGapError):
        # NOT ValidationError — that would mean ordering was swapped.
        dispatch_typed_output(
            "update_shopping_item",
            "this raw output is unparseable by any known pattern",
            UPDATE_SHOPPING_ITEM_SPEC.output_model,
        )
