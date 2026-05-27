"""Plan-time and execute-time argument validation tests
(Codex Sub-A4 R5 MAJOR #1 + #3).

These tests pin two boundaries:

- **Plan time**: ``validate_action_args(spec, raw_args)`` is the
  canonical entry point Phase B's planner uses to validate action
  args. It calls ``spec.validate_args_satisfy_required_any`` AND
  defers field-level checks for refs-present args. Locks in the
  contract so a future planner-validator regression breaks tests.

- **Execute time**: ``spec.validate_args_at_execute_time(resolved_args)``
  is the canonical entry point Phase B's executor uses AFTER refs
  resolve. Runs full ``input_model.model_validate`` so a ref that
  resolves to ``None`` triggers the model_validator's no-op
  rejector (Codex R5 MAJOR #3 — closes the deferred no-op risk
  from accepting refs as «non-null-by-shape» at plan time).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sreda.services.tool_schemas.plan_validator import (
    is_ref,
    validate_action_args,
)
from sreda.services.tool_schemas.specs_shopping import (
    ADD_SHOPPING_ITEMS_SPEC,
    UPDATE_SHOPPING_ITEM_SPEC,
)


SH_VALID = "sh_aaaaaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# is_ref helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("${s1.items[0].item_id}", True),
    ("${result.title}", True),
    ("${s1.id}", True),
    ("sh_aaaaaaaaaaaaaaaaaaaaaaaa", False),
    ("", False),
    (None, False),
    (42, False),
    ([], False),
    ("$not_a_ref", False),
    ("${not_closed", False),
    ("not_open}", False),
])
def test_is_ref(value, expected) -> None:
    assert is_ref(value) is expected


# ---------------------------------------------------------------------------
# validate_action_args — plan-time boundary (Codex R5 MAJOR #1)
# ---------------------------------------------------------------------------


def test_validate_action_args_rejects_refs_only_no_mutable() -> None:
    """The classic deferred no-op: planner emits
    ``update_shopping_item(item_id="${s1.id}")`` with NO mutable
    fields. Without this guard, plan validation passes (model_validator
    is skipped on refs path) and execute fires a silent no-op.

    Codex R5 MAJOR #1 — this MUST reject at plan time."""
    with pytest.raises(ValueError, match="at least one of"):
        validate_action_args(
            UPDATE_SHOPPING_ITEM_SPEC,
            {"item_id": "${s1.items[0].item_id}"},
        )


def test_validate_action_args_accepts_ref_for_mutable_field() -> None:
    """A ref to a mutable field counts as «provided» at plan time —
    the planner trusts upstream output_model shapes. Execute-time
    validation re-checks after resolution (next test section)."""
    validate_action_args(
        UPDATE_SHOPPING_ITEM_SPEC,
        {
            "item_id": "${s1.items[0].item_id}",
            "title": "${s2.recipe.title}",
        },
    )  # no raise


def test_validate_action_args_accepts_literal_mutable() -> None:
    """All-literal call → full input_model validation runs (including
    @model_validator on UpdateShoppingItemInput)."""
    validate_action_args(
        UPDATE_SHOPPING_ITEM_SPEC,
        {"item_id": SH_VALID, "title": "новое название"},
    )  # no raise


def test_validate_action_args_rejects_extra_keys_for_literal_args() -> None:
    """All-literal path runs ``input_model.model_validate`` which
    rejects unknown keys (``extra='forbid'``)."""
    with pytest.raises(ValidationError):
        validate_action_args(
            UPDATE_SHOPPING_ITEM_SPEC,
            {
                "item_id": SH_VALID,
                "title": "x",
                "hallucinated_key": "value",
            },
        )


def test_validate_action_args_defers_extra_keys_with_refs() -> None:
    """When refs are present, ``input_model.model_validate`` is
    deferred to execute time — so plan-time validation doesn't
    surface extra-key errors. The required_any guard still fires
    if applicable. Phase B's executor catches the rest on
    ``validate_args_at_execute_time``.

    This is the deliberate plan-time/execute-time split — refs
    can't be type-validated without resolving them first."""
    validate_action_args(
        UPDATE_SHOPPING_ITEM_SPEC,
        {
            "item_id": "${s1.id}",
            "title": "${s2.title}",  # satisfies required_any
            "future_arg_for_phase_b": "${...}",  # passes plan-time
        },
    )  # no raise — Phase B will catch on execute


def test_validate_action_args_for_spec_without_required_any() -> None:
    """``add_shopping_items`` has no ``required_any_non_null_args`` —
    the no-op guard is a no-op. Literal args path runs full
    model_validate."""
    validate_action_args(
        ADD_SHOPPING_ITEMS_SPEC,
        {"items": [{"title": "молоко"}]},
    )  # no raise


def test_validate_action_args_full_literal_rejects_invalid() -> None:
    """Final defense: a fully-literal call with a bad value
    (malformed id) is rejected at plan time."""
    with pytest.raises(ValidationError):
        validate_action_args(
            UPDATE_SHOPPING_ITEM_SPEC,
            {"item_id": "sh_garbage", "title": "x"},
        )


# ---------------------------------------------------------------------------
# spec.validate_args_at_execute_time — execute-time boundary
# (Codex R5 MAJOR #3 — closes the deferred no-op for refs-resolved-to-None)
# ---------------------------------------------------------------------------


def test_execute_time_validation_rejects_resolved_null_no_mutable() -> None:
    """**The R5 MAJOR #3 contract**.

    Plan-time accepted ``{"item_id": "${s1.id}", "title": "${s2.t}"}``
    because both refs satisfied the required_any guard. At execute
    time, if ``${s2.t}`` resolved to ``None`` (legitimately — the
    upstream output schema allowed Optional title), the runtime
    would still no-op without complaint.

    ``validate_args_at_execute_time`` runs full
    ``input_model.model_validate(resolved_args)`` which fires the
    ``@model_validator`` no-op rejector. Test pins that contract."""
    resolved = {
        "item_id": SH_VALID,
        "title": None,           # ref resolved to null
        "quantity_text": None,   # ref resolved to null
        "category": None,        # ref resolved to null
    }
    with pytest.raises(ValidationError):
        UPDATE_SHOPPING_ITEM_SPEC.validate_args_at_execute_time(resolved)


def test_execute_time_validation_accepts_resolved_clear_intent() -> None:
    """If ``quantity_text`` ref resolves to empty string (clear
    intent), execute-time validation accepts — this is the runtime
    contract from housewife_shopping.py:401-402."""
    resolved = {
        "item_id": SH_VALID,
        "title": None,
        "quantity_text": "",
        "category": None,
    }
    instance = UPDATE_SHOPPING_ITEM_SPEC.validate_args_at_execute_time(resolved)
    assert instance.quantity_text == ""


def test_execute_time_validation_accepts_resolved_real_value() -> None:
    """Happy path: ref resolved to actual title → validation passes."""
    resolved = {
        "item_id": SH_VALID,
        "title": "Молоко обезжиренное",
    }
    instance = UPDATE_SHOPPING_ITEM_SPEC.validate_args_at_execute_time(resolved)
    assert instance.title == "Молоко обезжиренное"


def test_execute_time_validation_rejects_malformed_resolved_id() -> None:
    """Execute-time catches upstream tools that emit malformed IDs
    in their output (e.g. legacy ``sh_42`` from a not-yet-migrated
    parser path) — tight pattern rejects."""
    resolved = {"item_id": "sh_garbage", "title": "x"}
    with pytest.raises(ValidationError):
        UPDATE_SHOPPING_ITEM_SPEC.validate_args_at_execute_time(resolved)


# ---------------------------------------------------------------------------
# End-to-end contract: plan-time accept + execute-time reject for the
# refs-resolve-to-null scenario. Documents the two-phase split.
# ---------------------------------------------------------------------------


def test_two_phase_contract_for_refs_resolving_to_null() -> None:
    """**End-to-end contract documentation test.**

    Step 1 — plan time: refs satisfy required_any guard.
    Step 2 — execute time: refs resolve to null → input_model rejects.

    Both steps are needed: plan-time rejects the obvious no-op
    pattern (item_id-only, no mutable refs); execute-time rejects
    the subtler resolved-null case. Removing either step reopens
    the deferred no-op risk."""
    # STEP 1 — plan-time validation accepts refs:
    raw = {
        "item_id": "${s1.items[0].item_id}",
        "title": "${s2.recipe.title}",
    }
    validate_action_args(UPDATE_SHOPPING_ITEM_SPEC, raw)  # no raise

    # STEP 2 — at execute time, if the title ref resolved to None
    # (e.g. recipe.title was optional and absent), model_validator
    # fires and rejects:
    resolved_to_null = {
        "item_id": SH_VALID,  # item_id ref resolved fine
        "title": None,         # title ref resolved to null — no-op!
    }
    with pytest.raises(ValidationError):
        UPDATE_SHOPPING_ITEM_SPEC.validate_args_at_execute_time(
            resolved_to_null
        )
