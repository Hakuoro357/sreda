"""Tests for ``runtime/planner/validator.py`` — Sub-A-77 item #4.

What this guards:

1. ``validate_action_args`` runs the tool's ``input_model`` against
   only the concrete (non-ref) keys in ``action.args``.
2. Variable refs (``${node.field}``) are skipped — they resolve at
   executor time after step results land in state.
3. «Missing required field» is suppressed when the slot is filled by
   a ref (planner DID provide the value, just deferred).
4. Wrong-type concrete values surface as errors.
5. ``extra='forbid'`` on input_model surfaces unexpected keys.
6. Nested refs inside dict/list values are detected — not just
   top-level string values.
7. Plan-level validator aggregates errors per-step with step id
   prefix; unknown tool names surface as plan-level errors.
8. Empty actions dict (clarity='needs_clarification' shape) yields no
   errors trivially.
"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)
from sreda.runtime.planner.validator import (
    InvalidPlanError,
    _contains_ref,
    validate_action_args,
    validate_plan_args,
    validate_plan_or_raise,
)
from sreda.services.tool_schemas.base import ToolOutput, ToolSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _ShoppingInput(BaseModel):
    """Mimics ``add_shopping_items`` minimal input contract.

    ``items`` required, ``category`` optional. ``extra='forbid'`` so
    the validator can catch extra keys.
    """

    model_config = ConfigDict(extra="forbid")
    items: list[str]
    category: str | None = None


class _ReminderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    trigger_iso: str


class _OkOutput(ToolOutput):
    status: Literal["ok"] = "ok"


def _spec(name: str, input_model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name=name,
        description=f"Test spec for {name}",
        family="shopping",
        effect="write",
        read_domains=[],
        write_domains=["shopping"],
        input_model=input_model,
        output_model=_OkOutput,
    )


def _action(tool: str, args: dict) -> Action:
    return Action(
        tool=tool,
        args=args,
        expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
    )


def _plan(actions: dict[str, Action]) -> Plan:
    """Build a Plan with sane defaults for tests.

    The Plan schema rejects empty ``actions`` when ``clarity='clear'``
    (the default), so for empty-actions test cases we explicitly set
    ``clarity='needs_clarification'`` with a reason — that's the real
    shape a planner emits for «нужно уточнение» turns and is exactly
    what the validator should accept without errors.
    """
    if not actions:
        return Plan(
            turn_classification=TurnClassification(
                is_new_turn=True, reason="test"
            ),
            clarity="needs_clarification",
            clarity_reason="ничего не делаем — нужно уточнение",
            actions=actions,
            compose=ComposerCall(
                kind="template",
                template_id="ask_user_for_clarification",
                template_data={"clarity_reason": "test"},
            ),
        )
    return Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="test"
        ),
        actions=actions,
        compose=ComposerCall(kind="template", template_id="add_items_success"),
    )


# ---------------------------------------------------------------------------
# _contains_ref helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("plain", False),
    ("${s1.x}", True),
    ("prefix-${s1.x}-suffix", True),
    ("$not-a-ref", False),
    (42, False),
    (None, False),
    ([], False),
    ([1, 2, 3], False),
    (["a", "b"], False),
    (["a", "${s1.x}"], True),                    # ref nested in list
    ({"k": "v"}, False),
    ({"k": "${s1.x}"}, True),                    # ref nested in dict
    ({"k": [{"inner": "${s1.y}"}]}, True),       # deeply nested
    ((1, 2), False),
    ((1, "${s1.x}"), True),                      # ref in tuple
])
def test_contains_ref(value: object, expected: bool) -> None:
    assert _contains_ref(value) == expected


# ---------------------------------------------------------------------------
# validate_action_args — happy path + refs skipping
# ---------------------------------------------------------------------------


def test_valid_action_no_refs_passes() -> None:
    action = _action(
        tool="add_shopping_items",
        args={"items": ["молоко", "хлеб"], "category": "молочка"},
    )
    spec = _spec("add_shopping_items", _ShoppingInput)
    assert validate_action_args(action, spec) == []


def test_valid_action_only_optional_omitted_passes() -> None:
    action = _action(
        tool="add_shopping_items", args={"items": ["молоко"]}
    )
    spec = _spec("add_shopping_items", _ShoppingInput)
    assert validate_action_args(action, spec) == []


def test_missing_required_field_is_reported() -> None:
    # Forgot required 'items'.
    action = _action(tool="add_shopping_items", args={"category": "x"})
    spec = _spec("add_shopping_items", _ShoppingInput)
    errors = validate_action_args(action, spec)
    assert len(errors) == 1
    assert "items" in errors[0].lower()


def test_wrong_type_concrete_value_is_reported() -> None:
    # items should be list[str], passing a single str.
    action = _action(
        tool="add_shopping_items", args={"items": "молоко"}
    )
    spec = _spec("add_shopping_items", _ShoppingInput)
    errors = validate_action_args(action, spec)
    assert len(errors) == 1
    assert "items" in errors[0].lower()


def test_extra_field_with_forbid_extra_is_reported() -> None:
    action = _action(
        tool="add_shopping_items",
        args={"items": ["x"], "unknown_field": "value"},
    )
    spec = _spec("add_shopping_items", _ShoppingInput)
    errors = validate_action_args(action, spec)
    assert any("unknown_field" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_action_args — variable refs are skipped
# ---------------------------------------------------------------------------


def test_pure_ref_value_skips_required_check() -> None:
    # 'items' is filled by a ref — pydantic would say «missing» since
    # we strip it from concrete args, but the validator suppresses
    # that specific error because the field is ref-filled.
    action = _action(
        tool="add_shopping_items",
        args={"items": "${s1.items}"},
    )
    spec = _spec("add_shopping_items", _ShoppingInput)
    assert validate_action_args(action, spec) == []


def test_ref_nested_in_list_skips_required_check() -> None:
    action = _action(
        tool="add_shopping_items",
        args={"items": ["concrete", "${s1.item}"]},
    )
    spec = _spec("add_shopping_items", _ShoppingInput)
    # The whole 'items' key has a ref inside → it gets skipped entirely.
    # Behavior is intentional: partial-ref containers also defer.
    assert validate_action_args(action, spec) == []


def test_concrete_alongside_ref_validates_concrete_only() -> None:
    # 'title' concrete (correct type), 'trigger_iso' is a ref.
    action = _action(
        tool="schedule_reminder",
        args={"title": "купить хлеб", "trigger_iso": "${s1.time}"},
    )
    spec = _spec("schedule_reminder", _ReminderInput)
    assert validate_action_args(action, spec) == []


def test_concrete_wrong_type_alongside_ref_still_errors() -> None:
    # 'title' wrong type (int instead of str), 'trigger_iso' is a ref.
    action = _action(
        tool="schedule_reminder",
        args={"title": 42, "trigger_iso": "${s1.time}"},
    )
    spec = _spec("schedule_reminder", _ReminderInput)
    errors = validate_action_args(action, spec)
    assert len(errors) == 1
    assert "title" in errors[0].lower()


def test_missing_truly_missing_field_still_reports_when_others_have_refs() -> None:
    # 'trigger_iso' is a ref, 'title' is genuinely absent.
    action = _action(
        tool="schedule_reminder",
        args={"trigger_iso": "${s1.time}"},
    )
    spec = _spec("schedule_reminder", _ReminderInput)
    errors = validate_action_args(action, spec)
    assert len(errors) == 1
    assert "title" in errors[0].lower()


# ---------------------------------------------------------------------------
# validate_plan_args — aggregation + unknown tool + empty actions
# ---------------------------------------------------------------------------


def test_plan_with_no_actions_returns_no_errors() -> None:
    plan = _plan(actions={})
    assert validate_plan_args(plan, registry={}) == []


def test_plan_unknown_tool_is_reported() -> None:
    plan = _plan(actions={
        "s1": _action(tool="hallucinated_tool", args={}),
    })
    errors = validate_plan_args(plan, registry={})
    assert len(errors) == 1
    assert "s1" in errors[0]
    assert "hallucinated_tool" in errors[0]
    assert "unknown tool" in errors[0].lower()


def test_plan_valid_actions_return_no_errors() -> None:
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
    }
    plan = _plan(actions={
        "s1": _action(tool="add_shopping_items", args={"items": ["x"]}),
    })
    assert validate_plan_args(plan, registry=registry) == []


def test_plan_aggregates_errors_across_steps_with_step_id_prefix() -> None:
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
    }
    plan = _plan(actions={
        "s1": _action(tool="add_shopping_items", args={}),                 # missing items
        "s2": _action(tool="schedule_reminder", args={"title": "x"}),       # missing trigger
    })
    errors = validate_plan_args(plan, registry=registry)
    assert len(errors) == 2
    # Both errors prefixed with their step id, so feedback can be
    # routed accurately.
    assert any(e.startswith("s1:") for e in errors)
    assert any(e.startswith("s2:") for e in errors)


def test_plan_error_messages_contain_field_path() -> None:
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
    }
    plan = _plan(actions={
        "s1": _action(tool="add_shopping_items", args={"items": "wrong-type"}),
    })
    errors = validate_plan_args(plan, registry=registry)
    assert len(errors) == 1
    assert "items" in errors[0]


# ---------------------------------------------------------------------------
# validate_plan_or_raise — exception-style flow
# ---------------------------------------------------------------------------


def test_validate_plan_or_raise_returns_silently_on_valid_plan() -> None:
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
    }
    plan = _plan(actions={
        "s1": _action(tool="add_shopping_items", args={"items": ["x"]}),
    })
    validate_plan_or_raise(plan, registry=registry)  # no exception


def test_validate_plan_or_raise_raises_with_aggregated_errors() -> None:
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
    }
    plan = _plan(actions={
        "s1": _action(tool="add_shopping_items", args={}),
    })
    with pytest.raises(InvalidPlanError) as exc_info:
        validate_plan_or_raise(plan, registry=registry)
    assert len(exc_info.value.errors) == 1
    assert "items" in str(exc_info.value)


def test_invalid_plan_error_message_includes_all_violations() -> None:
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
    }
    plan = _plan(actions={
        "s1": _action(tool="add_shopping_items", args={"items": 42}),
        "s2": _action(tool="schedule_reminder", args={}),
    })
    with pytest.raises(InvalidPlanError) as exc_info:
        validate_plan_or_raise(plan, registry=registry)
    assert len(exc_info.value.errors) >= 2  # at least one per step


# ---------------------------------------------------------------------------
# Edge cases: dict-valued args, ref deep inside dict
# ---------------------------------------------------------------------------


class _NestedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, str]


def test_dict_valued_arg_with_ref_inside_is_skipped() -> None:
    action = _action(
        tool="nested_tool",
        args={"metadata": {"key": "${s1.value}"}},
    )
    spec = _spec("nested_tool", _NestedInput)
    # metadata contains ref → entire key skipped.
    assert validate_action_args(action, spec) == []


def test_dict_valued_arg_all_concrete_validates() -> None:
    action = _action(
        tool="nested_tool",
        args={"metadata": {"key": "val", "other": "ok"}},
    )
    spec = _spec("nested_tool", _NestedInput)
    assert validate_action_args(action, spec) == []


def test_dict_valued_arg_concrete_wrong_type_errors() -> None:
    # metadata values should be str, passing int.
    action = _action(
        tool="nested_tool",
        args={"metadata": {"key": 42}},
    )
    spec = _spec("nested_tool", _NestedInput)
    errors = validate_action_args(action, spec)
    assert len(errors) == 1
    assert "metadata" in errors[0].lower()
