"""Tests for ``runtime/planner/validator.py`` — Sub-A-77 item #4.

Coverage layers:

**Layer 1 — Phase 1 structural checks**:
- Unknown arg key (concrete) → ``unknown_arg``.
- Unknown arg key (ref-valued) → ``unknown_arg`` (Codex R1 MAJOR #1).
- Alias-named arg key → accepted as canonical field (Codex R1 MINOR #11).
- Ref to unknown step → ``unknown_ref_target`` (Codex R1 MAJOR #5).
- Self-reference → ``self_ref`` (Codex R1 MAJOR #5).
- Forward-reference without depends_on → ``forward_ref`` (Codex R1 MAJOR #5).
- Ref with explicit ``depends_on`` declaration → no forward_ref violation.

**Layer 2 — Phase 2 schema-aware partial**:
- No-refs plan: full ``model_validate`` runs (including cross-field
  validators).
- Pure full-ref string in required slot → deferred, no violation.
- Mixed interpolated string ``"prefix ${s1.x}"`` in str field → OK.
- Mixed interpolated string in non-str field → ``invalid_arg_type``
  (Codex R1 MAJOR #2).
- Container with mixed refs → deferred (MVP limitation per
  validator.py docstring).
- Container with all-concrete values → element-level type check.
- Cross-field model validator (``@model_validator``) — fires on
  no-refs path, NOT on refs-present path (Codex R1 MAJOR #4).

**Layer 3 — public API**:
- ``Violation`` structured fields populated.
- ``render_violations`` formats step/tool/field/message.
- ``validate_plan_args`` (legacy strings) still works.
- ``validate_plan_or_raise`` raises with structured ``.violations``.
"""

from __future__ import annotations

import collections.abc
import typing
from typing import Annotated, Literal, NewType

import pytest
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field as PydField,
    model_validator,
)

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)
from sreda.runtime.planner.validator import (
    InvalidPlanError,
    Violation,
    render_violations,
    validate_plan,
    validate_plan_args,
    validate_plan_or_raise,
)
from sreda.services.tool_schemas.base import ToolOutput, ToolSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _ShoppingInput(BaseModel):
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


def _action(
    tool: str,
    args: dict,
    *,
    depends_on: list[str] | None = None,
) -> Action:
    return Action(
        tool=tool,
        args=args,
        expected_outcomes=[OutcomeBranch(match={"status": "ok"})],
        depends_on=depends_on or [],
    )


def _plan_with_actions(actions: dict[str, Action]) -> Plan:
    return Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="test"
        ),
        actions=actions,
        compose=ComposerCall(kind="template", template_id="add_items_success"),
    )


def _empty_plan() -> Plan:
    """Plan with no actions — uses clarity='needs_clarification' shape."""
    return Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="test"
        ),
        clarity="needs_clarification",
        clarity_reason="empty for tests",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"clarity_reason": "test"},
        ),
    )


# ---------------------------------------------------------------------------
# Phase 1 — unknown args (Codex R1 MAJOR #1)
# ---------------------------------------------------------------------------


def test_unknown_concrete_arg_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {
            "items": ["x"],
            "hallucinated_key": "value",
        }),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    codes = {v.code for v in violations}
    assert "unknown_arg" in codes
    assert any(v.field_path == "hallucinated_key" for v in violations)


def test_unknown_ref_valued_arg_is_reported() -> None:
    """Codex R1 MAJOR #1: ``hallucinated: ${s1.y}`` previously bypassed
    extra='forbid' because the ref-valued key was stripped before
    pydantic saw it. Now the key check happens FIRST."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "add_shopping_items",
            {"items": ["y"], "hallucinated": "${s1.items}"},
            depends_on=["s1"],
        ),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    s2_violations = [v for v in violations if v.step_id == "s2"]
    assert any(
        v.code == "unknown_arg" and v.field_path == "hallucinated"
        for v in s2_violations
    )


# ---------------------------------------------------------------------------
# Phase 1 — alias handling (Codex R1 MINOR #11)
# ---------------------------------------------------------------------------


class _AliasedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items_internal: list[str] = PydField(alias="items")


def test_arg_under_alias_is_accepted() -> None:
    plan = _plan_with_actions({
        "s1": _action("aliased_tool", {"items": ["x"]}),
    })
    registry = {"aliased_tool": _spec("aliased_tool", _AliasedInput)}
    violations = validate_plan(plan, registry)
    assert violations == []


# ---------------------------------------------------------------------------
# Phase 1 — ref integrity (Codex R1 MAJOR #5)
# ---------------------------------------------------------------------------


def test_unknown_ref_target_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": "${s99.items}"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert any(v.code == "unknown_ref_target" for v in violations)


def test_self_reference_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": "${s1.items}"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert any(v.code == "self_ref" for v in violations)


def test_reference_to_later_step_is_fine_via_inferred_dep() -> None:
    """A ref FROM s1 TO s2 inherently makes s2 a predecessor of s1.
    The topology-aware scheduler (Group 2) reorders by data flow, not
    by dict iteration order — so dict-order doesn't matter."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": "${s2.items}"}),
        "s2": _action("add_shopping_items", {"items": ["x"]}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    # No ref-integrity violations — target exists and isn't self.
    assert not any(
        v.code in {"unknown_ref_target", "self_ref", "forward_ref"}
        for v in violations
    )


# NOTE: depends_on integrity (self-reference, unknown target, cycles)
# is enforced by ``Plan`` schema at construction time — see
# ``schemas.py:_validate_actions`` and ``test_planner_schemas.py``.
# This validator does not re-check those invariants.


def test_ref_with_explicit_depends_on_passes_integrity_check() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "add_shopping_items",
            {"items": "${s1.items}"},
            depends_on=["s1"],
        ),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    # No ref-integrity violations.
    assert not any(
        v.code in {"unknown_ref_target", "self_ref", "forward_ref"}
        for v in violations
    )


def test_ref_to_implicit_predecessor_passes_integrity_check() -> None:
    """If s2 refs s1 directly, s1 is automatically s2's predecessor —
    no explicit depends_on required."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action("add_shopping_items", {"items": "${s1.items}"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert not any(
        v.code in {"unknown_ref_target", "self_ref", "forward_ref"}
        for v in violations
    )


# ---------------------------------------------------------------------------
# Phase 2 — schema validation, no refs (full model_validate)
# ---------------------------------------------------------------------------


def test_valid_plan_no_refs_passes() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["молоко"], "category": "молочка"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    assert validate_plan(plan, registry) == []


def test_missing_required_no_refs_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"category": "x"}),  # items missing
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert any(
        v.code == "missing_arg" and v.field_path == "items"
        for v in violations
    )


def test_wrong_type_no_refs_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": "single-string"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert any(v.field_path == "items" for v in violations)


# ---------------------------------------------------------------------------
# Phase 2 — refs present, pure full-ref defers
# ---------------------------------------------------------------------------


def test_pure_ref_in_required_field_is_deferred() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "add_shopping_items",
            {"items": "${s1.items}"},
            depends_on=["s1"],
        ),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    # No missing_arg for s2.items even though it's "missing" concretely.
    violations = validate_plan(plan, registry)
    assert not any(
        v.step_id == "s2" and v.code == "missing_arg" for v in violations
    )


def test_truly_missing_field_with_other_ref_filled_still_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": ["x"]}),  # trigger_iso missing
        "s2": _action(
            "schedule_reminder",
            {"trigger_iso": "${s1.iso}"},  # title genuinely absent
            depends_on=["s1"],
        ),
    })
    registry = {"schedule_reminder": _spec("schedule_reminder", _ReminderInput)}
    violations = validate_plan(plan, registry)
    s2_missing = [
        v for v in violations
        if v.step_id == "s2" and v.code == "missing_arg" and v.field_path == "title"
    ]
    assert len(s2_missing) == 1


# ---------------------------------------------------------------------------
# Phase 2 — mixed interpolated strings (Codex R1 MAJOR #2)
# ---------------------------------------------------------------------------


def test_mixed_string_in_str_field_passes() -> None:
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "schedule_reminder",
            {"title": "напомни про ${s1.title}", "trigger_iso": "iso"},
            depends_on=["s1"],
        ),
    })
    registry = {"schedule_reminder": _spec("schedule_reminder", _ReminderInput)}
    violations = validate_plan(plan, registry)
    # Mixed string into title (str field) is fine.
    s2_violations = [v for v in violations if v.step_id == "s2"]
    assert s2_violations == []


class _IntFieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


def test_mixed_string_in_int_field_is_reported() -> None:
    """Codex R1 MAJOR #2: ``"prefix ${s1.count}"`` resolves to str at
    executor time; if the consumer field is ``int``, that's a type
    mismatch the validator should catch statically."""
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "int_tool",
            {"count": "prefix ${s1.title}"},  # str → int field
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "int_tool": _spec("int_tool", _IntFieldInput),
    }
    violations = validate_plan(plan, registry)
    assert any(
        v.step_id == "s2"
        and v.code == "invalid_arg_type"
        and v.field_path == "count"
        for v in violations
    )


# ---------------------------------------------------------------------------
# Phase 2 — model-level validators (Codex R1 MAJOR #4)
# ---------------------------------------------------------------------------


class _CrossFieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int
    end: int

    @model_validator(mode="after")
    def _check_order(self) -> "_CrossFieldInput":
        if self.end < self.start:
            raise ValueError("end must be >= start")
        return self


def test_model_validator_fires_on_no_refs_path() -> None:
    plan = _plan_with_actions({
        "s1": _action("cross_field", {"start": 5, "end": 1}),
    })
    registry = {"cross_field": _spec("cross_field", _CrossFieldInput)}
    violations = validate_plan(plan, registry)
    assert any("start" in (v.message or "") or "end" in (v.message or "")
               for v in violations)


def test_model_validator_does_not_fire_when_refs_present() -> None:
    """Codex R1 MAJOR #4: cross-field validator must NOT fire on
    stripped data — it would produce false positives. With a ref
    present, we validate per-field via TypeAdapter only."""
    plan = _plan_with_actions({
        "s1": _action("cross_field", {"start": 1, "end": 5}),
        "s2": _action(
            "cross_field",
            {"start": 5, "end": "${s1.end}"},  # would fail model_validator
                                               # if stripped data ran it
            depends_on=["s1"],
        ),
    })
    registry = {"cross_field": _spec("cross_field", _CrossFieldInput)}
    violations = validate_plan(plan, registry)
    s2_violations = [v for v in violations if v.step_id == "s2"]
    # No spurious cross-field error from stripped data.
    assert all(
        "end must be >= start" not in (v.message or "")
        for v in s2_violations
    )


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_validate_plan_returns_structured_violations() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"category": "x"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert all(isinstance(v, Violation) for v in violations)
    # Fields populated:
    v = violations[0]
    assert v.step_id == "s1"
    assert v.tool == "add_shopping_items"
    assert v.code in {"missing_arg", "missing"}


def test_render_violations_includes_step_tool_field_message() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"category": "x"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    rendered = validate_plan_args(plan, registry)
    assert len(rendered) >= 1
    assert "s1" in rendered[0]
    assert "add_shopping_items" in rendered[0]
    assert "items" in rendered[0]


def test_unknown_tool_surfaces_as_plan_level_violation() -> None:
    plan = _plan_with_actions({
        "s1": _action("hallucinated", {}),
    })
    violations = validate_plan(plan, registry={})
    assert len(violations) == 1
    assert violations[0].code == "unknown_tool"
    assert violations[0].tool == "hallucinated"
    assert violations[0].step_id == "s1"


def test_empty_plan_returns_no_violations() -> None:
    assert validate_plan(_empty_plan(), registry={}) == []


def test_validate_plan_or_raise_silent_on_valid() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    validate_plan_or_raise(plan, registry)


def test_validate_plan_or_raise_raises_with_violations() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"category": "x"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    with pytest.raises(InvalidPlanError) as exc_info:
        validate_plan_or_raise(plan, registry)
    assert len(exc_info.value.violations) >= 1
    assert all(isinstance(v, Violation) for v in exc_info.value.violations)


def test_invalid_plan_error_message_lists_all_violations() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": 42}),
        "s2": _action("schedule_reminder", {}),
    })
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
    }
    with pytest.raises(InvalidPlanError) as exc_info:
        validate_plan_or_raise(plan, registry)
    msg = str(exc_info.value)
    assert "s1" in msg
    assert "s2" in msg


def test_render_violations_handles_plan_level_violation() -> None:
    """Unknown tool has no field_path; render must not crash."""
    v = Violation(
        step_id="s1",
        tool="hallucinated",
        code="unknown_tool",
        message="unknown tool 'hallucinated'",
    )
    [rendered] = render_violations([v])
    assert "s1" in rendered
    assert "hallucinated" in rendered


# ---------------------------------------------------------------------------
# Phase 2 — container handling: all-concrete validates
# ---------------------------------------------------------------------------


def test_concrete_list_with_wrong_inner_type_no_refs_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["ok", 42]}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert any(v.step_id == "s1" for v in violations)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_violations_aggregated_across_steps() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {}),
        "s2": _action("schedule_reminder", {"title": "x"}),  # trigger_iso missing
    })
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
    }
    violations = validate_plan(plan, registry)
    step_ids = {v.step_id for v in violations}
    assert step_ids == {"s1", "s2"}


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #1 — container element walking (was deferred in R1)
# ---------------------------------------------------------------------------


def test_list_with_mixed_concrete_and_ref_validates_concrete_leaves() -> None:
    """Mixed list ``["ok", 123, "${s1.x}"]`` against ``list[str]``:
    the concrete int leaf gets caught even though one element is a
    deferred ref."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "add_shopping_items",
            {"items": ["ok", 123, "${s1.x}"]},
            depends_on=["s1"],
        ),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    s2_violations = [v for v in violations if v.step_id == "s2"]
    # The int leaf at index 1 must be flagged.
    assert any(
        "items[1]" in (v.field_path or "") for v in s2_violations
    ), f"expected items[1] violation; got: {s2_violations}"


def test_list_with_only_concrete_validates_normally() -> None:
    """Same as above but without refs — no-refs path's full
    ``model_validate`` already covers this. Sanity check that
    element-walk doesn't break that path."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["ok", 123]}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert any(v.step_id == "s1" for v in violations)


class _DictFieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, str]


def test_dict_with_mixed_ref_and_concrete_validates_concrete_values() -> None:
    plan = _plan_with_actions({
        "s1": _action("dict_tool", {"metadata": {"a": "1"}}),
        "s2": _action(
            "dict_tool",
            {"metadata": {"a": "1", "b": 42, "c": "${s1.x}"}},
            depends_on=["s1"],
        ),
    })
    registry = {"dict_tool": _spec("dict_tool", _DictFieldInput)}
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    # The int value for key 'b' must be flagged.
    assert any("metadata['b']" in (v.field_path or "") for v in s2)


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #2 — Field constraints preserved on refs-present path
# ---------------------------------------------------------------------------


class _ConstrainedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = PydField(ge=1)
    other: str


def test_field_constraint_caught_on_refs_present_path() -> None:
    """``count: int = Field(ge=1)``. With another field as a ref, the
    refs-present per-field path would previously drop the ``ge=1``
    constraint. Now annotation is wrapped ``Annotated[int, Ge(1)]``."""
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "constrained_tool",
            {"count": 0, "other": "${s1.title}"},
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "constrained_tool": _spec("constrained_tool", _ConstrainedInput),
    }
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    # count=0 violates ge=1 — must be reported.
    assert any(v.field_path == "count" for v in s2), f"got: {s2}"


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #3 — alias-only when populate_by_name=False
# ---------------------------------------------------------------------------


class _AliasOnlyInput(BaseModel):
    # populate_by_name not set → defaults to False; only alias accepted.
    model_config = ConfigDict(extra="forbid")
    items_internal: list[str] = PydField(alias="items")


def test_field_name_rejected_when_populate_by_name_false() -> None:
    """If model uses alias and ``populate_by_name=False`` (default),
    field name should NOT be accepted — pydantic itself would reject
    it. Our validator must mirror that semantics (Codex R2 MAJOR #3)."""
    plan = _plan_with_actions({
        "s1": _action("alias_only_tool", {"items_internal": ["x"]}),
    })
    registry = {"alias_only_tool": _spec("alias_only_tool", _AliasOnlyInput)}
    violations = validate_plan(plan, registry)
    # Field name `items_internal` should surface as unknown_arg.
    assert any(
        v.code == "unknown_arg" and v.field_path == "items_internal"
        for v in violations
    )


class _PopulateByNameInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    items_internal: list[str] = PydField(alias="items")


def test_field_name_accepted_when_populate_by_name_true() -> None:
    """With ``populate_by_name=True``, BOTH the alias and the field
    name are valid input keys — validator must accept both."""
    plan = _plan_with_actions({
        "s1": _action("populate_tool", {"items_internal": ["x"]}),
        "s2": _action("populate_tool", {"items": ["y"]}),
    })
    registry = {"populate_tool": _spec("populate_tool", _PopulateByNameInput)}
    violations = validate_plan(plan, registry)
    assert violations == []


class _ValidationAliasInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = PydField(validation_alias=AliasChoices("v", "value_input"))


def test_alias_choices_keys_accepted() -> None:
    """``AliasChoices('v', 'value_input')`` should accept both 'v' and
    'value_input' but NOT 'value' (field name) since pop_by_name=False."""
    registry = {"choice_tool": _spec("choice_tool", _ValidationAliasInput)}

    # 'v' accepted
    plan_v = _plan_with_actions({"s1": _action("choice_tool", {"v": "x"})})
    assert validate_plan(plan_v, registry) == []

    # 'value_input' accepted
    plan_vi = _plan_with_actions({"s1": _action("choice_tool", {"value_input": "x"})})
    assert validate_plan(plan_vi, registry) == []

    # 'value' (field name) rejected
    plan_field = _plan_with_actions({"s1": _action("choice_tool", {"value": "x"})})
    violations = validate_plan(plan_field, registry)
    assert any(v.code == "unknown_arg" for v in violations)


# ---------------------------------------------------------------------------
# Codex R2 MAJOR #4 — ref-cycle detection
# ---------------------------------------------------------------------------


def test_ref_cycle_two_steps_is_detected() -> None:
    plan = _plan_with_actions({
        # s1 refs s2, s2 refs s1 — closed loop via args.
        "s1": _action("add_shopping_items", {"items": "${s2.items}"}),
        "s2": _action("add_shopping_items", {"items": "${s1.items}"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    cycle_violations = [v for v in violations if v.code == "cycle"]
    assert len(cycle_violations) >= 2
    step_ids = {v.step_id for v in cycle_violations}
    assert step_ids == {"s1", "s2"}


def test_ref_cycle_three_steps_is_detected() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": "${s3.items}"}),
        "s2": _action("add_shopping_items", {"items": "${s1.items}"}),
        "s3": _action("add_shopping_items", {"items": "${s2.items}"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    cycle_violations = [v for v in violations if v.code == "cycle"]
    step_ids = {v.step_id for v in cycle_violations}
    assert step_ids == {"s1", "s2", "s3"}


def test_no_cycle_in_linear_chain() -> None:
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["a"]}),
        "s2": _action("add_shopping_items", {"items": "${s1.items}"}),
        "s3": _action("add_shopping_items", {"items": "${s2.items}"}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    assert not any(v.code == "cycle" for v in violations)


# ---------------------------------------------------------------------------
# Codex R2 MINOR #5 — annotation introspection for mixed-string refs
# ---------------------------------------------------------------------------


class _OptionalStrInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    trigger_iso: str


def test_mixed_string_into_optional_str_field_passes() -> None:
    """``str | None`` accepts strings — mixed-string ref must not
    trigger ``invalid_arg_type``."""
    plan = _plan_with_actions({
        "s1": _action("optional_tool", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "optional_tool",
            {"title": "prefix ${s1.title}", "trigger_iso": "iso"},
            depends_on=["s1"],
        ),
    })
    registry = {"optional_tool": _spec("optional_tool", _OptionalStrInput)}
    violations = validate_plan(plan, registry)
    s2 = [
        v for v in violations
        if v.step_id == "s2" and v.code == "invalid_arg_type"
    ]
    assert s2 == []


# ---------------------------------------------------------------------------
# Codex R2 MINOR #6 — ref regex rejects malformed dotted paths
# ---------------------------------------------------------------------------


def test_malformed_ref_trailing_dot_not_matched() -> None:
    """``${s1.}`` is not a valid ref → not picked up by iter_refs →
    not validated as a ref. (Treated as a plain string with weird
    text — pydantic str validator will accept it.)"""
    from sreda.runtime.planner.interpolation import iter_refs
    assert list(iter_refs("${s1.}")) == []


def test_malformed_ref_double_dot_not_matched() -> None:
    from sreda.runtime.planner.interpolation import iter_refs
    assert list(iter_refs("${s1..x}")) == []


# ---------------------------------------------------------------------------
# Codex R3 MAJOR #1 — nested container element walking
# ---------------------------------------------------------------------------


class _NestedListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, str]]


def test_nested_list_of_dict_walks_concrete_inner_leaves() -> None:
    """``list[dict[str, str]]`` — when outer list has refs, recurse
    into each dict and check inner concrete values."""
    plan = _plan_with_actions({
        "s1": _action("nested_tool", {"rows": [{"a": "1"}]}),
        "s2": _action(
            "nested_tool",
            {"rows": [{"a": "1", "b": 42}, "${s1.rows}"]},
            depends_on=["s1"],
        ),
    })
    registry = {"nested_tool": _spec("nested_tool", _NestedListInput)}
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    # Inner dict value 'b'=42 (int) should be flagged — that path is
    # rows[0]['b'] (or similar).
    assert any("rows[0]" in (v.field_path or "") for v in s2), f"got: {s2}"


class _OptionalListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str] | None = None
    trigger: str


def test_optional_list_peels_correctly() -> None:
    """``Optional[list[str]]`` — peel must strip the Optional wrapper."""
    plan = _plan_with_actions({
        "s1": _action("opt_tool", {"items": ["a"], "trigger": "x"}),
        "s2": _action(
            "opt_tool",
            {"items": ["ok", 42, "${s1.trigger}"], "trigger": "y"},
            depends_on=["s1"],
        ),
    })
    registry = {"opt_tool": _spec("opt_tool", _OptionalListInput)}
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    # Int 42 at index 1 must be flagged.
    assert any("items[1]" in (v.field_path or "") for v in s2)


# ---------------------------------------------------------------------------
# Codex R3 MAJOR #3 — duplicate canonical field (alias + name both)
# ---------------------------------------------------------------------------


def test_alias_and_field_name_both_supplied_under_populate_by_name() -> None:
    """``populate_by_name=True`` accepts both — but emitting both is
    a planner bug. Surface as duplicate_arg."""
    plan = _plan_with_actions({
        "s1": _action(
            "populate_tool",
            {"items": ["a"], "items_internal": ["b"]},  # both, conflicting
        ),
    })
    registry = {"populate_tool": _spec("populate_tool", _PopulateByNameInput)}
    violations = validate_plan(plan, registry)
    assert any(v.code == "duplicate_arg" for v in violations)


# ---------------------------------------------------------------------------
# Codex R3 MINOR #5 — invalid_ref_syntax violations
# ---------------------------------------------------------------------------


def test_malformed_ref_in_string_field_reports_invalid_ref_syntax() -> None:
    plan = _plan_with_actions({
        "s1": _action(
            "schedule_reminder",
            {"title": "напоминание ${s1.}", "trigger_iso": "iso"},
        ),
    })
    registry = {"schedule_reminder": _spec("schedule_reminder", _ReminderInput)}
    violations = validate_plan(plan, registry)
    assert any(v.code == "invalid_ref_syntax" for v in violations)


def test_double_dot_ref_reports_invalid_ref_syntax() -> None:
    plan = _plan_with_actions({
        "s1": _action(
            "schedule_reminder",
            {"title": "${s1..x}", "trigger_iso": "iso"},
        ),
    })
    registry = {"schedule_reminder": _spec("schedule_reminder", _ReminderInput)}
    violations = validate_plan(plan, registry)
    assert any(v.code == "invalid_ref_syntax" for v in violations)


def test_private_segment_ref_reports_invalid_ref_syntax() -> None:
    """``${s1._private}`` would crash executor's resolve_refs;
    validator must catch."""
    plan = _plan_with_actions({
        "s1": _action(
            "schedule_reminder",
            {"title": "x", "trigger_iso": "iso"},
        ),
        "s2": _action(
            "schedule_reminder",
            {"title": "${s1._private}", "trigger_iso": "iso"},
            depends_on=["s1"],
        ),
    })
    registry = {"schedule_reminder": _spec("schedule_reminder", _ReminderInput)}
    violations = validate_plan(plan, registry)
    assert any(
        v.code == "invalid_ref_syntax" and v.step_id == "s2"
        for v in violations
    )


# ---------------------------------------------------------------------------
# Codex R3 MINOR #6 — _annotation_accepts_string is conservative on unknowns
# ---------------------------------------------------------------------------


def test_annotation_accepts_string_concrete_int_returns_false() -> None:
    from sreda.runtime.planner.validator import _annotation_accepts_string
    assert _annotation_accepts_string(int) is False
    assert _annotation_accepts_string(float) is False
    assert _annotation_accepts_string(bool) is False
    assert _annotation_accepts_string(bytes) is False
    assert _annotation_accepts_string(list[str]) is False
    assert _annotation_accepts_string(dict[str, int]) is False


def test_annotation_accepts_string_concrete_str_returns_true() -> None:
    from sreda.runtime.planner.validator import _annotation_accepts_string
    assert _annotation_accepts_string(str) is True
    assert _annotation_accepts_string(str | None) is True
    assert _annotation_accepts_string(typing.Optional[str]) is True  # type: ignore[arg-type]


def test_annotation_accepts_string_unknown_custom_type_returns_true() -> None:
    """Custom validator classes (NewType, pydantic types, etc.) — defer
    to executor, do not over-reject."""
    from sreda.runtime.planner.validator import _annotation_accepts_string

    UserId = NewType("UserId", str)
    assert _annotation_accepts_string(UserId) is True


# ---------------------------------------------------------------------------
# Codex R3 MINOR #7 — dict key annotation walking
# ---------------------------------------------------------------------------


class _ConstrainedKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counts: dict[int, str]  # keys are int — string key is wrong type


def test_dict_concrete_int_key_violation_no_refs() -> None:
    plan = _plan_with_actions({
        "s1": _action("ck_tool", {"counts": {"not-int": "x"}}),
    })
    registry = {"ck_tool": _spec("ck_tool", _ConstrainedKeyInput)}
    violations = validate_plan(plan, registry)
    assert any(v.step_id == "s1" for v in violations)


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #1 — nested BaseModel walking
# ---------------------------------------------------------------------------


class _Author(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    age: int


class _PostInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    author: _Author


def test_nested_basemodel_with_ref_walks_concrete_fields() -> None:
    """``author: _Author`` field receives dict with refs — sub-model's
    own contract enforced: bad concrete fields surface even when
    siblings are deferred refs."""
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "post_tool",
            {
                "title": "x",
                "author": {"name": 42, "age": "${s1.title}"},  # name is int (wrong)
            },
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "post_tool": _spec("post_tool", _PostInput),
    }
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    assert any("author.name" in (v.field_path or "") for v in s2)


def test_nested_basemodel_unknown_key_is_reported() -> None:
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "post_tool",
            {
                "title": "x",
                "author": {"name": "Alice", "age": 30, "bogus": "${s1.title}"},
            },
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "post_tool": _spec("post_tool", _PostInput),
    }
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    assert any(
        v.code == "unknown_arg" and "author.bogus" in (v.field_path or "")
        for v in s2
    )


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #2 — container shell constraints under refs
# ---------------------------------------------------------------------------


class _LenConstrainedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str] = PydField(min_length=2)
    trigger: str


def test_min_length_caught_on_literal_container_with_refs() -> None:
    """``items: list[str] = Field(min_length=2)`` with ``["${s1.x}"]`` —
    length is statically 1, refs don't splice. Validator must catch."""
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "len_tool",
            {"items": ["${s1.title}"], "trigger": "ok"},
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "len_tool": _spec("len_tool", _LenConstrainedInput),
    }
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    assert any(v.field_path == "items" for v in s2), f"got: {s2}"


# ---------------------------------------------------------------------------
# Codex R4 MAJOR #3 — alias vs validation_alias precedence
# ---------------------------------------------------------------------------


class _ValidationAliasOnlyInput(BaseModel):
    """``validation_alias`` set without ``alias`` — pydantic accepts
    ONLY the validation_alias as input key."""
    model_config = ConfigDict(extra="forbid")
    value: str = PydField(alias="serialized_v", validation_alias="v_in")


def test_validation_alias_wins_over_alias_for_input() -> None:
    """When ``validation_alias`` is set, input must use it — plain
    ``alias`` is for serialization, not validation input. Validator
    must mirror pydantic semantics exactly."""
    registry = {"va_tool": _spec("va_tool", _ValidationAliasOnlyInput)}

    # validation_alias accepted:
    plan_v_in = _plan_with_actions({"s1": _action("va_tool", {"v_in": "x"})})
    assert validate_plan(plan_v_in, registry) == []

    # plain alias rejected (it's only for serialization):
    plan_alias = _plan_with_actions({
        "s1": _action("va_tool", {"serialized_v": "x"}),
    })
    violations = validate_plan(plan_alias, registry)
    assert any(v.code == "unknown_arg" for v in violations)


# ---------------------------------------------------------------------------
# Codex R4 MINOR #4 — ref-like dict keys
# ---------------------------------------------------------------------------


class _DictWithRefKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, str]
    trigger: str


def test_ref_in_dict_key_is_unsupported_ref_location() -> None:
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "key_tool",
            {
                "metadata": {"${s1.title}": "value"},  # ref in KEY
                "trigger": "${s1.title}",  # other ref to make it refs-present
            },
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "key_tool": _spec("key_tool", _DictWithRefKeyInput),
    }
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    assert any(v.code == "unsupported_ref_location" for v in s2)


# ---------------------------------------------------------------------------
# Codex R5 MAJOR #1 — duplicate_arg detection inside nested BaseModel
# ---------------------------------------------------------------------------


class _AuthorPopulate(BaseModel):
    """Nested model with populate_by_name=True so alias AND field name
    both accepted — emitting both should trigger duplicate_arg."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    full_name: str = PydField(alias="name")
    age: int


class _PostWithPopulate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    author: _AuthorPopulate


def test_nested_basemodel_duplicate_arg_field_path_is_clean() -> None:
    """Codex R6 MINOR #2: nested duplicate_arg field_path must be a
    clean JSON-path (``author.full_name``) without tool-name prefix —
    the renderer prints ``tool`` separately."""
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "post_pop_tool",
            {
                "title": "x",
                "author": {
                    "name": "Alice",
                    "full_name": "Alicia",
                    "age": "${s1.title}",
                },
            },
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "post_pop_tool": _spec("post_pop_tool", _PostWithPopulate),
    }
    violations = validate_plan(plan, registry)
    dups = [v for v in violations if v.code == "duplicate_arg"]
    assert len(dups) == 1
    # Field path is clean: "author.full_name" — no tool name prefix.
    assert dups[0].field_path == "author.full_name"
    assert dups[0].tool == "post_pop_tool"


def test_nested_basemodel_duplicate_arg_detected_under_refs() -> None:
    """When nested BaseModel uses populate_by_name=True and the planner
    emits both alias and field-name for the same nested field, surface
    as duplicate_arg with the nested field_path."""
    plan = _plan_with_actions({
        "s1": _action("schedule_reminder", {"title": "x", "trigger_iso": "iso"}),
        "s2": _action(
            "post_pop_tool",
            {
                "title": "x",
                "author": {
                    "name": "Alice",        # alias
                    "full_name": "Alicia",  # field name — duplicate
                    "age": "${s1.title}",   # ref makes it refs-present
                },
            },
            depends_on=["s1"],
        ),
    })
    registry = {
        "schedule_reminder": _spec("schedule_reminder", _ReminderInput),
        "post_pop_tool": _spec("post_pop_tool", _PostWithPopulate),
    }
    violations = validate_plan(plan, registry)
    s2 = [v for v in violations if v.step_id == "s2"]
    assert any(
        v.code == "duplicate_arg" and "author.full_name" in (v.field_path or "")
        for v in s2
    ), f"got: {s2}"


# ---------------------------------------------------------------------------
# Phase 1.d — required_any_non_null_args (Codex Sub-A4 R5/R6 MAJOR #1)
# ---------------------------------------------------------------------------


class _UpdateLikeInput(BaseModel):
    """Mimics ``UpdateShoppingItemInput`` shape: required id + optional
    mutable fields (title/qty/category). Used in this test file to keep
    the validator tests independent of the shopping spec."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    title: str | None = None
    quantity_text: str | None = None
    category: str | None = None

    @model_validator(mode="after")
    def _at_least_one_mutable(self) -> "_UpdateLikeInput":
        provided = (
            ("title" in self.model_fields_set and self.title is not None)
            or ("quantity_text" in self.model_fields_set)
            or ("category" in self.model_fields_set and self.category is not None)
        )
        if not provided:
            raise ValueError("no mutable field provided")
        return self


def _update_spec_with_required_any(name: str = "update_like") -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name=name,
        description=f"Test spec for {name}",
        family="shopping",
        effect="write",
        read_domains=[],
        write_domains=["shopping"],
        input_model=_UpdateLikeInput,
        output_model=_OkOutput,
        required_any_non_null_args=["title", "quantity_text", "category"],
    )


def test_required_any_rejects_refs_only_no_mutable() -> None:
    """**Codex R5/R6 MAJOR #1 closure test**.

    Plan with ``update_like(item_id="${s1.items[0].item_id}")`` — only
    ``item_id`` provided, no mutable fields. Phase 2 skips model_validator
    on the refs-present path; the new Phase 1.d
    ``_phase1_check_required_any_non_null`` MUST fire and emit
    ``silent_noop_call`` so the planner retries with a real update."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "update_like",
            {"item_id": "${s1.items[0]}"},
            depends_on=["s1"],
        ),
    })
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "update_like": _update_spec_with_required_any(),
    }
    violations = validate_plan(plan, registry)
    silent_noop = [
        v for v in violations
        if v.code == "silent_noop_call" and v.step_id == "s2"
    ]
    assert silent_noop, (
        f"Expected silent_noop_call on s2 but got: {violations}"
    )


def test_required_any_accepts_ref_on_mutable_field() -> None:
    """Ref to a mutable field satisfies the «non-null-by-shape» rule
    at plan time. Execute-time validation re-checks resolution via
    ``spec.validate_args_at_execute_time``."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "update_like",
            {"item_id": "${s1.items[0]}", "title": "${s1.category}"},
            depends_on=["s1"],
        ),
    })
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "update_like": _update_spec_with_required_any(),
    }
    violations = validate_plan(plan, registry)
    silent_noop = [v for v in violations if v.code == "silent_noop_call"]
    assert not silent_noop, f"unexpected silent_noop_call: {silent_noop}"


def test_required_any_accepts_literal_mutable() -> None:
    """All-literal call with at least one mutable field — no
    silent_noop violation. The input_model's @model_validator runs
    on the no-refs path; this test exercises the «happy literal» case."""
    plan = _plan_with_actions({
        "s1": _action(
            "update_like",
            {"item_id": "sh_abc", "title": "новое название"},
        ),
    })
    registry = {"update_like": _update_spec_with_required_any()}
    violations = validate_plan(plan, registry)
    silent_noop = [v for v in violations if v.code == "silent_noop_call"]
    assert not silent_noop


def test_required_any_rejects_all_explicit_nulls() -> None:
    """All mutable fields explicit-null with refs to suppress
    model_validator — Phase 1.d still rejects because explicit null
    is not «provided» under the rule."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "update_like",
            {
                "item_id": "${s1.items[0]}",
                "title": None,
                "quantity_text": None,
                "category": None,
            },
            depends_on=["s1"],
        ),
    })
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "update_like": _update_spec_with_required_any(),
    }
    violations = validate_plan(plan, registry)
    s2_silent_noop = [
        v for v in violations
        if v.code == "silent_noop_call" and v.step_id == "s2"
    ]
    assert s2_silent_noop


def test_required_any_accepts_empty_string_for_clear_intent() -> None:
    """Empty string is non-null — shopping ``quantity_text=""`` is the
    clear-intent. Phase 1.d must accept it as «provided»."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
        "s2": _action(
            "update_like",
            {"item_id": "${s1.items[0]}", "quantity_text": ""},
            depends_on=["s1"],
        ),
    })
    registry = {
        "add_shopping_items": _spec("add_shopping_items", _ShoppingInput),
        "update_like": _update_spec_with_required_any(),
    }
    violations = validate_plan(plan, registry)
    silent_noop = [v for v in violations if v.code == "silent_noop_call"]
    assert not silent_noop


def test_required_any_does_not_false_positive_for_specs_without_setting() -> None:
    """Specs without ``required_any_non_null_args`` set must NOT
    surface ``silent_noop_call``. The check is opt-in per spec."""
    plan = _plan_with_actions({
        "s1": _action("add_shopping_items", {"items": ["x"]}),
    })
    registry = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}
    violations = validate_plan(plan, registry)
    silent_noop = [v for v in violations if v.code == "silent_noop_call"]
    assert not silent_noop


def test_required_any_real_update_shopping_item_spec_integration() -> None:
    """End-to-end with the REAL ``UPDATE_SHOPPING_ITEM_SPEC`` from
    ``specs_shopping.py``. Verifies the production spec's declaration
    of ``required_any_non_null_args`` flows through the production
    validator (Codex R6 explicit ask)."""
    from sreda.services.tool_schemas.specs_shopping import (
        ADD_SHOPPING_ITEMS_SPEC,
        LIST_SHOPPING_SPEC,
        UPDATE_SHOPPING_ITEM_SPEC,
    )
    plan = _plan_with_actions({
        "s1": _action("list_shopping", {}),
        "s2": _action(
            "update_shopping_item",
            {"item_id": "${s1.items[0].item_id}"},
            depends_on=["s1"],
        ),
    })
    registry = {
        "list_shopping": LIST_SHOPPING_SPEC,
        "update_shopping_item": UPDATE_SHOPPING_ITEM_SPEC,
        "add_shopping_items": ADD_SHOPPING_ITEMS_SPEC,
    }
    violations = validate_plan(plan, registry)
    silent_noop = [
        v for v in violations
        if v.code == "silent_noop_call" and v.step_id == "s2"
    ]
    assert silent_noop, (
        f"Expected silent_noop_call on s2 with real UPDATE_SHOPPING_ITEM_SPEC "
        f"but got: {[(v.code, v.message[:80]) for v in violations]}"
    )




# ---------------------------------------------------------------------------
# Sub-A12 Phase D.2-enable — composer allowlist membership
# ---------------------------------------------------------------------------


def _plan_with_compose(compose: ComposerCall) -> Plan:
    """A clear single-action plan whose ROOT compose we control, for
    isolating the composer-allowlist check."""
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={"s1": _action("add_shopping_items", {"items": ["x"]})},
        compose=compose,
    )


_ALLOWLIST_REGISTRY = {"add_shopping_items": _spec("add_shopping_items", _ShoppingInput)}


def test_allowlist_skipped_when_none() -> None:
    """Back-compat: no allowlists passed → no membership check (existing
    callers/tests keep working)."""
    plan = _plan_with_compose(
        ComposerCall(kind="template", template_id="anything_goes")
    )
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    assert not [v for v in violations if v.code == "unknown_template_id"]


def test_unknown_template_id_flagged_when_allowlist_given() -> None:
    plan = _plan_with_compose(
        ComposerCall(kind="template", template_id="not_in_registry")
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset(),
    )
    bad = [v for v in violations if v.code == "unknown_template_id"]
    assert bad and "not_in_registry" in bad[0].message


def test_known_template_id_passes() -> None:
    plan = _plan_with_compose(
        ComposerCall(kind="template", template_id="shopping_added_ok")
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset(),
    )
    assert not [v for v in violations if v.code == "unknown_template_id"]


def test_unknown_llm_prompt_key_flagged() -> None:
    plan = _plan_with_compose(
        ComposerCall(kind="llm", llm_prompt_key="not_a_real_key")
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset({"recipe_narrative"}),
    )
    bad = [v for v in violations if v.code == "unknown_llm_prompt_key"]
    assert bad and "not_a_real_key" in bad[0].message


def test_known_llm_prompt_key_passes() -> None:
    plan = _plan_with_compose(
        ComposerCall(kind="llm", llm_prompt_key="recipe_narrative")
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset({"recipe_narrative"}),
    )
    assert not [v for v in violations if v.code == "unknown_llm_prompt_key"]


def test_branch_compose_llm_key_also_checked() -> None:
    """The allowlist check covers branch composes, not just root."""
    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={
            "s1": Action(
                tool="add_shopping_items",
                args={"items": ["x"]},
                expected_outcomes=[
                    OutcomeBranch(
                        match={"status": "ok"},
                        compose=ComposerCall(
                            kind="llm", llm_prompt_key="bad_branch_key"
                        ),
                    ),
                ],
            ),
        },
        compose=ComposerCall(kind="template", template_id="shopping_added_ok"),
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset({"recipe_narrative"}),
    )
    bad = [v for v in violations if v.code == "unknown_llm_prompt_key"]
    assert bad and "bad_branch_key" in bad[0].message
    assert bad[0].step_id == "s1"


def test_llm_compose_missing_required_data_flagged() -> None:
    """Sub-A12 D.2-enable — a registered llm_prompt_key whose required
    template_data is missing is caught at PLAN time (before execution),
    not as a late compose-time failure."""
    plan = _plan_with_compose(
        ComposerCall(kind="llm", llm_prompt_key="recipe_narrative",
                     template_data={})  # missing required keys
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset({"recipe_narrative"}),
        llm_prompt_required_keys={"recipe_narrative": frozenset({"recipe_title", "ingredients"})},
    )
    bad = [v for v in violations if v.code == "llm_compose_missing_data"]
    assert bad and "recipe_title" in bad[0].message


def test_llm_compose_refs_count_as_present() -> None:
    """A ${...} ref satisfies a required key at plan time (executor
    resolves it later) — no missing-data violation."""
    plan = _plan_with_compose(
        ComposerCall(kind="llm", llm_prompt_key="recipe_narrative",
                     template_data={"recipe_title": "${s1.title}",
                                    "ingredients": "${s1.items}"})
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset({"recipe_narrative"}),
        llm_prompt_required_keys={"recipe_narrative": frozenset({"recipe_title", "ingredients"})},
    )
    assert not [v for v in violations if v.code == "llm_compose_missing_data"]


def test_llm_required_keys_skipped_when_map_none() -> None:
    """Back-compat: no required-keys map → no data check."""
    plan = _plan_with_compose(
        ComposerCall(kind="llm", llm_prompt_key="recipe_narrative",
                     template_data={})
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_template_ids=frozenset({"shopping_added_ok"}),
        composer_llm_prompt_keys=frozenset({"recipe_narrative"}),
        llm_prompt_required_keys=None,
    )
    assert not [v for v in violations if v.code == "llm_compose_missing_data"]


# ---------------------------------------------------------------------------
# #36 — nested-path ref validation in compose template_data.
# `_ref_field_exists_in_output` previously checked ONLY the first ref
# segment; `${s1.recipe.bogus}` (recipe = nested BaseModel) passed plan
# validation and blew up at runtime resolve_refs. Now nested segments are
# walked when the field is an introspectable nested BaseModel (peeling
# Optional/Annotated/list); opaque shapes (dict/Any) stay deferred.
# ---------------------------------------------------------------------------


class _RecipeIngredient(BaseModel):
    name: str
    qty: str


class _RecipeData(BaseModel):
    title: str
    ingredients: list[_RecipeIngredient]


class _RecipeFoundOutput(ToolOutput):
    status: Literal["found"] = "found"
    recipe: _RecipeData
    extras: dict = {}  # opaque deeper shape — must stay deferred


def _recipe_spec() -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name="get_recipe_any_source",
        description="test recipe spec",
        family="recipes",
        effect="read",
        read_domains=["recipes"],
        write_domains=[],
        input_model=_ShoppingInput,
        output_model=_RecipeFoundOutput,
    )


def _recipe_plan(ref: str) -> Plan:
    """Single read step whose ROOT compose holds the ref under test."""
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={
            "s1": Action(
                tool="get_recipe_any_source",
                args={"items": ["x"]},
                expected_outcomes=[OutcomeBranch(match={"status": "found"})],
            )
        },
        compose=ComposerCall(
            kind="llm",
            llm_prompt_key="recipe_narrative",
            template_data={"text": ref},
        ),
    )


def test_nested_compose_ref_valid_subfield_passes() -> None:
    plan = _recipe_plan("${s1.recipe.title}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_nested_compose_ref_invalid_subfield_flagged() -> None:
    plan = _recipe_plan("${s1.recipe.bogus}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    bad = [v for v in violations if v.code == "compose_ref_unknown_field"]
    assert bad and "bogus" in bad[0].message


def test_compose_ref_terminal_list_passes() -> None:
    """Referencing the whole list (no further segment) is fine — the
    executor resolves `${s1.recipe.ingredients}` to the list value."""
    plan = _recipe_plan("${s1.recipe.ingredients}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_compose_ref_through_list_flagged() -> None:
    """Codex #36 MAJOR #1: the runtime resolver (interpolation._resolve_path)
    walks dict-keys/attrs only — it NEVER projects through a sequence. So
    `${s1.recipe.ingredients.name}` is a guaranteed runtime failure and must
    be flagged at plan time, NOT blessed by peeling the list element."""
    plan = _recipe_plan("${s1.recipe.ingredients.name}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    assert [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_compose_ref_through_list_invalid_inner_also_flagged() -> None:
    plan = _recipe_plan("${s1.recipe.ingredients.bogus}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    assert [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_nested_compose_ref_opaque_field_deferred() -> None:
    """`extras` is a plain dict — deeper path not introspectable, must
    NOT be flagged (defer to runtime resolve_refs)."""
    plan = _recipe_plan("${s1.extras.whatever.deep}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_first_level_compose_ref_unknown_field_still_flagged() -> None:
    """Regression: existing first-segment check keeps working."""
    plan = _recipe_plan("${s1.nonexistent}")
    violations = validate_plan(plan, {"get_recipe_any_source": _recipe_spec()})
    bad = [v for v in violations if v.code == "compose_ref_unknown_field"]
    assert bad and "nonexistent" in bad[0].message


# ---------------------------------------------------------------------------
# Latent-trap guard: a nested field typed `Union[ModelA, ModelB]` (two
# distinct BaseModels) must NOT be walked against a single arm.
# `_strip_optional_and_annotated` collapses such a union to its FIRST arm,
# so a ref to a field that only exists on the SECOND arm would have been
# spuriously rejected with compose_ref_unknown_field. The fix defers such
# ambiguous unions to runtime resolve_refs. No production output model has
# this shape yet (all nested unions are `Model | None` or `list[Model]`),
# but the guard prevents a future model from tripping the validator.
# ---------------------------------------------------------------------------


class _PayloadVariantA(BaseModel):
    kind_a_field: str


class _PayloadVariantB(BaseModel):
    kind_b_field: str


class _UnionPayloadOutput(ToolOutput):
    status: Literal["found"] = "found"
    payload: _PayloadVariantA | _PayloadVariantB


def _union_payload_spec() -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name="get_union_payload",
        description="test union payload spec",
        family="recipes",
        effect="read",
        read_domains=["recipes"],
        write_domains=[],
        input_model=_ShoppingInput,
        output_model=_UnionPayloadOutput,
    )


def _union_payload_plan(ref: str) -> Plan:
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={
            "s1": Action(
                tool="get_union_payload",
                args={"items": ["x"]},
                expected_outcomes=[OutcomeBranch(match={"status": "found"})],
            )
        },
        compose=ComposerCall(
            kind="llm",
            llm_prompt_key="recipe_narrative",
            template_data={"text": ref},
        ),
    )


def test_nested_union_of_distinct_models_subfield_deferred() -> None:
    """Ref to a field that exists ONLY on the second union arm must be
    deferred (no compose_ref_unknown_field), not walked against the first
    arm. Previously `payload` collapsed to `_PayloadVariantA`, so the
    `_PayloadVariantB`-only `kind_b_field` was spuriously flagged."""
    plan = _union_payload_plan("${s1.payload.kind_b_field}")
    violations = validate_plan(plan, {"get_union_payload": _union_payload_spec()})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_nested_union_of_distinct_models_first_arm_subfield_deferred() -> None:
    """Symmetry: a field present on the FIRST arm also resolves (valid via
    that arm), so no compose_ref_unknown_field."""
    plan = _union_payload_plan("${s1.payload.kind_a_field}")
    violations = validate_plan(plan, {"get_union_payload": _union_payload_spec()})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_nested_union_subfield_on_neither_arm_flagged() -> None:
    """Precision (tri-state walker): a field that exists on NEITHER union
    arm is a real bug — every arm rejects it → flagged."""
    plan = _union_payload_plan("${s1.payload.on_no_arm}")
    violations = validate_plan(plan, {"get_union_payload": _union_payload_spec()})
    assert [v for v in violations if v.code == "compose_ref_unknown_field"]


# ---------------------------------------------------------------------------
# #36 R2 — union/sequence shapes a flat distinct-model count missed
# (Codex R2 MAJOR/MEDIUM): Optional[A|B], Annotated-per-arm unions, and
# abstract Sequence[Model]. Now handled by the tri-state walker.
# ---------------------------------------------------------------------------

class _OptUnionOutput(ToolOutput):
    status: Literal["found"] = "found"
    payload: _PayloadVariantA | _PayloadVariantB | None = None


class _AnnotatedArmsOutput(ToolOutput):
    status: Literal["found"] = "found"
    payload: (
        Annotated[_PayloadVariantA, "a"]
        | Annotated[_PayloadVariantB, "b"]
    )


class _AbstractSeqOutput(ToolOutput):
    status: Literal["found"] = "found"
    rows: collections.abc.Sequence[_PayloadVariantA]


def _spec_with_output(name: str, output_model: type) -> ToolSpec:
    return ToolSpec(  # type: ignore[arg-type]
        name=name,
        description=f"test spec {name}",
        family="recipes",
        effect="read",
        read_domains=["recipes"],
        write_domains=[],
        input_model=_ShoppingInput,
        output_model=output_model,
    )


def _plan_for(tool: str, ref: str) -> Plan:
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={
            "s1": Action(
                tool=tool, args={"items": ["x"]},
                expected_outcomes=[OutcomeBranch(match={"status": "found"})],
            )
        },
        compose=ComposerCall(
            kind="llm", llm_prompt_key="recipe_narrative",
            template_data={"text": ref},
        ),
    )


def test_optional_union_subfield_resolves_via_arm() -> None:
    """Optional[A|B] is a nested union arg the flat count missed; the
    walker still resolves a B-only field through arm B (no violation)."""
    spec = _spec_with_output("get_opt_union", _OptUnionOutput)
    plan = _plan_for("get_opt_union", "${s1.payload.kind_b_field}")
    violations = validate_plan(plan, {"get_opt_union": spec})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_optional_union_subfield_on_neither_arm_flagged() -> None:
    spec = _spec_with_output("get_opt_union", _OptUnionOutput)
    plan = _plan_for("get_opt_union", "${s1.payload.on_no_arm}")
    violations = validate_plan(plan, {"get_opt_union": spec})
    assert [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_annotated_per_arm_union_resolves_via_arm() -> None:
    """`Annotated[A,...] | Annotated[B,...]` — per-arm Annotated the flat
    count missed; walker strips Annotated per arm and resolves via B."""
    spec = _spec_with_output("get_annot_union", _AnnotatedArmsOutput)
    plan = _plan_for("get_annot_union", "${s1.payload.kind_b_field}")
    violations = validate_plan(plan, {"get_annot_union": spec})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_abstract_sequence_through_flagged() -> None:
    """Abstract `collections.abc.Sequence[Model]` — runtime still can't
    project it, so a segment after it is flagged (Codex R2 MEDIUM)."""
    spec = _spec_with_output("get_abstract_seq", _AbstractSeqOutput)
    plan = _plan_for("get_abstract_seq", "${s1.rows.kind_a_field}")
    violations = validate_plan(plan, {"get_abstract_seq": spec})
    assert [v for v in violations if v.code == "compose_ref_unknown_field"]


def test_abstract_sequence_terminal_passes() -> None:
    spec = _spec_with_output("get_abstract_seq", _AbstractSeqOutput)
    plan = _plan_for("get_abstract_seq", "${s1.rows}")
    violations = validate_plan(plan, {"get_abstract_seq": spec})
    assert not [v for v in violations if v.code == "compose_ref_unknown_field"]


# ---------------------------------------------------------------------------
# #26 — misplaced catch-all branch (empty match at non-final index). The
# executor's first-match scan never reaches branches after a catch-all, so
# they are unreachable; the validator must reject it at plan time instead of
# letting the executor silently ignore it at runtime.
# ---------------------------------------------------------------------------


def _plan_one_action_outcomes(outcomes: list[OutcomeBranch]) -> Plan:
    return _plan_with_actions({
        "s1": Action(
            tool="add_shopping_items",
            args={"items": ["x"]},
            expected_outcomes=outcomes,
        )
    })


def test_catch_all_as_last_branch_ok() -> None:
    plan = _plan_one_action_outcomes([
        OutcomeBranch(match={"status": "ok"}),
        OutcomeBranch(match={}),  # catch-all LAST — valid
    ])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    assert not [v for v in violations if v.code == "misplaced_catch_all_branch"]


def test_catch_all_sole_branch_ok() -> None:
    plan = _plan_one_action_outcomes([OutcomeBranch(match={})])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    assert not [v for v in violations if v.code == "misplaced_catch_all_branch"]


def test_catch_all_at_non_final_index_flagged() -> None:
    plan = _plan_one_action_outcomes([
        OutcomeBranch(match={}),  # catch-all FIRST — later branch dead
        OutcomeBranch(match={"status": "ok"}),
    ])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    bad = [v for v in violations if v.code == "misplaced_catch_all_branch"]
    assert bad and bad[0].step_id == "s1"
    assert "[0]" in bad[0].message


def test_catch_all_in_middle_flagged() -> None:
    plan = _plan_one_action_outcomes([
        OutcomeBranch(match={"status": "ok"}),
        OutcomeBranch(match={}),  # middle catch-all
        OutcomeBranch(match={"status": "ok"}),
    ])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    bad = [v for v in violations if v.code == "misplaced_catch_all_branch"]
    assert bad and "[1]" in bad[0].message


def test_no_catch_all_branches_ok() -> None:
    plan = _plan_one_action_outcomes([
        OutcomeBranch(match={"status": "ok"}),
    ])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    assert not [v for v in violations if v.code == "misplaced_catch_all_branch"]


def test_multiple_catch_alls_only_nonfinal_flagged() -> None:
    """Two catch-alls: the non-final one is flagged, the final one is not."""
    plan = _plan_one_action_outcomes([
        OutcomeBranch(match={}),  # idx 0 — flagged
        OutcomeBranch(match={}),  # idx 1 (last) — valid
    ])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    bad = [v for v in violations if v.code == "misplaced_catch_all_branch"]
    assert len(bad) == 1 and "[0]" in bad[0].message


def test_catch_all_last_in_three_branches_ok() -> None:
    plan = _plan_one_action_outcomes([
        OutcomeBranch(match={"status": "ok"}),
        OutcomeBranch(match={"status": "ok"}),
        OutcomeBranch(match={}),  # catch-all last of 3 — valid
    ])
    violations = validate_plan(plan, _ALLOWLIST_REGISTRY)
    assert not [v for v in violations if v.code == "misplaced_catch_all_branch"]


# ---------------------------------------------------------------------------
# Phase-B humanize_result key-allowlist (rot-enablement Phase 1 Codex R2
# MAJOR) — validate_plan MUST reject a clear plan whose
# compose.llm_prompt_key='humanize_result' carries extra/internal keys
# BEFORE execution (side effects are committed on execute, compose fails
# late — the window this check closes).
# ---------------------------------------------------------------------------

_HR_ALLOWLIST_KEYS = frozenset({"humanize_result"})


def _plan_hr_compose(template_data: dict) -> Plan:
    """Clear single-action plan with a root humanize_result LLM compose."""
    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={"s1": _action("add_shopping_items", {"items": ["x"]})},
        compose=ComposerCall(
            kind="llm",
            llm_prompt_key="humanize_result",
            template_data=template_data,
        ),
    )


def test_humanize_result_valid_static_actions_passes_phase_b() -> None:
    """Well-formed {intent, actions:[{user_visible_summary, status}]} passes."""
    plan = _plan_hr_compose({
        "intent": "додати покупки",
        "actions": [
            {"user_visible_summary": "Додано 3 товари", "status": "ok"},
        ],
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    bad = [
        v for v in violations
        if v.code in ("humanize_result_extra_top_keys", "humanize_result_extra_action_keys")
    ]
    assert not bad, f"Unexpected violations: {bad}"


def test_humanize_result_extra_top_key_rejected_phase_b() -> None:
    """Extra top-level key (execution_id) is rejected at Phase B."""
    plan = _plan_hr_compose({
        "intent": "додати покупки",
        "actions": [{"user_visible_summary": "Додано", "status": "ok"}],
        "execution_id": "abc-123",  # internal field — must be rejected
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    bad = [v for v in violations if v.code == "humanize_result_extra_top_keys"]
    assert bad, "Expected humanize_result_extra_top_keys violation"
    assert "execution_id" in bad[0].message


def test_humanize_result_extra_action_key_rejected_phase_b() -> None:
    """Action item with extra key (tool) is rejected at Phase B."""
    plan = _plan_hr_compose({
        "intent": "оновити список",
        "actions": [
            {
                "user_visible_summary": "Оновлено",
                "status": "ok",
                "tool": "add_shopping_items",  # raw internal field — must be rejected
            }
        ],
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    bad = [v for v in violations if v.code == "humanize_result_extra_action_keys"]
    assert bad, "Expected humanize_result_extra_action_keys violation"
    assert "tool" in bad[0].message
    assert "[0]" in bad[0].message


def test_humanize_result_action_error_key_rejected_phase_b() -> None:
    """Action item with 'error' key is rejected at Phase B."""
    plan = _plan_hr_compose({
        "intent": "запит",
        "actions": [
            {
                "user_visible_summary": "Помилка",
                "status": "error",
                "error": "timeout",  # must not reach LLM
            }
        ],
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    bad = [v for v in violations if v.code == "humanize_result_extra_action_keys"]
    assert bad, "Expected humanize_result_extra_action_keys violation"
    assert "error" in bad[0].message


def test_humanize_result_ref_string_actions_skips_item_check() -> None:
    """When actions is an unresolved ref string, per-item check is skipped
    (value resolves post-execution), but top-level allowlist is still enforced."""
    plan = _plan_hr_compose({
        "intent": "виконати",
        "actions": "${s1.result_items}",  # unresolved ref — not a list yet
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    # No per-item violation expected (actions is a ref, not an inspectable list)
    bad = [v for v in violations if v.code == "humanize_result_extra_action_keys"]
    assert not bad, f"Unexpected item violations for ref-string actions: {bad}"
    # Top-level check still runs: no extra top-level keys → no top-level violation
    top_bad = [v for v in violations if v.code == "humanize_result_extra_top_keys"]
    assert not top_bad


def test_humanize_result_extra_top_key_still_caught_with_ref_actions() -> None:
    """Extra top-level key is rejected even when actions is a ref string."""
    plan = _plan_hr_compose({
        "intent": "виконати",
        "actions": "${s1.result_items}",
        "execution_id": "xyz",  # disallowed regardless of actions being a ref
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    bad = [v for v in violations if v.code == "humanize_result_extra_top_keys"]
    assert bad, "Expected humanize_result_extra_top_keys even with ref-string actions"
    assert "execution_id" in bad[0].message


def test_humanize_result_field_level_refs_deferred_under_allow_refs() -> None:
    """A full ${...} ref in a humanize_result action FIELD (user_visible_summary
    / status) or in `intent` is DEFERRED at the static stage (allow_refs=True):
    it resolves post-execution, so it must NOT trip the per-item non-empty
    string check. Distinct from test_humanize_result_ref_string_actions_skips_
    item_check, which defers the whole `actions` value — here `actions` is an
    inspectable list and the refs live INSIDE its items.

    Contrast assertion: a LITERAL blank in the same field position still trips
    the check — proving the pass above is genuine ref deferral, not the check
    silently accepting everything. Violations are filtered to the
    humanize_result_* family (a ref to a non-existent output field may also
    raise compose_ref_* codes, which are orthogonal to contract deferral)."""
    # All per-item fields + intent are full refs → every value deferred.
    plan = _plan_hr_compose({
        "intent": "${s1.intent}",
        "actions": [
            {"user_visible_summary": "${s1.summary}", "status": "${s1.status}"},
        ],
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    deferred = [v for v in violations if v.code.startswith("humanize_result")]
    assert not deferred, (
        f"full ${{...}} refs in item fields must defer at the static stage; "
        f"got {[(v.code, v.message) for v in deferred]}"
    )

    # Contrast: a literal blank status (NOT a ref) is still rejected — proves
    # the per-item value check is live, so the deferral above is real.
    plan_blank = _plan_hr_compose({
        "intent": "выполнить",
        "actions": [
            {"user_visible_summary": "готово", "status": "   "},  # blank, not a ref
        ],
    })
    blank_violations = validate_plan(
        plan_blank, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    blank_bad = [v for v in blank_violations if v.code.startswith("humanize_result")]
    assert blank_bad, (
        "a literal blank status must still be rejected — the per-item check is "
        "live, confirming the ref case above is genuine deferral"
    )


def test_humanize_result_check_runs_without_llm_prompt_required_keys() -> None:
    """The key-allowlist check is NOT gated on llm_prompt_required_keys being
    provided — it runs whenever llm_prompt_key == 'humanize_result'."""
    plan = _plan_hr_compose({
        "intent": "тест",
        "actions": [{"user_visible_summary": "x", "status": "ok", "tool": "boom"}],
    })
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
        llm_prompt_required_keys=None,  # explicitly omitted
    )
    bad = [v for v in violations if v.code == "humanize_result_extra_action_keys"]
    assert bad, "Key-allowlist check must run even when llm_prompt_required_keys=None"


# ---------------------------------------------------------------------------
# Phase-B humanize_result FULL static contract (rot-enablement Phase 1 Codex
# R3 MAJOR) — single source of truth, all structural rules enforced at plan
# time, not just extra-key check.
# ---------------------------------------------------------------------------


def _validate_hr(template_data: dict) -> list:  # type: ignore[type-arg]
    """Run validate_plan for a root humanize_result compose and return
    all humanize_result-prefixed violations."""
    plan = _plan_hr_compose(template_data)
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    return [v for v in violations if v.code.startswith("humanize_result")]


def test_hr_static_missing_status_rejected_phase_b() -> None:
    """actions=[{user_visible_summary only}] — missing status is rejected."""
    violations = _validate_hr({
        "intent": "тест",
        "actions": [{"user_visible_summary": "Зроблено"}],
    })
    assert violations, "Expected violation for missing status"
    codes = [v.code for v in violations]
    assert any("missing" in c or "action" in c for c in codes), codes


def test_hr_static_extra_key_on_item_rejected_phase_b() -> None:
    """actions=[{user_visible_summary, status, tool}] — extra item key rejected."""
    violations = _validate_hr({
        "intent": "тест",
        "actions": [{"user_visible_summary": "x", "status": "ok", "tool": "boom"}],
    })
    bad = [v for v in violations if v.code == "humanize_result_extra_action_keys"]
    assert bad, "Expected humanize_result_extra_action_keys"
    assert "tool" in bad[0].message


def test_hr_static_non_dict_item_rejected_phase_b() -> None:
    """actions=[42] — non-dict item is rejected."""
    violations = _validate_hr({
        "intent": "тест",
        "actions": [42],
    })
    assert violations, "Expected violation for non-dict action item"
    codes = [v.code for v in violations]
    assert any("not_dict" in c or "action" in c for c in codes), codes


def test_hr_static_non_list_non_ref_actions_rejected_phase_b() -> None:
    """actions='not-a-ref' — non-list, non-ref string is rejected."""
    violations = _validate_hr({
        "intent": "тест",
        "actions": "not-a-ref",
    })
    assert violations, "Expected violation for non-list non-ref actions"
    codes = [v.code for v in violations]
    assert any("list" in c or "action" in c for c in codes), codes


def test_hr_static_empty_list_actions_rejected_phase_b() -> None:
    """actions=[] — empty list is rejected."""
    violations = _validate_hr({
        "intent": "тест",
        "actions": [],
    })
    assert violations, "Expected violation for empty actions list"
    codes = [v.code for v in violations]
    assert any("list" in c or "action" in c for c in codes), codes


def test_hr_static_blank_status_rejected_phase_b() -> None:
    """actions=[{user_visible_summary, status=''}] — blank status rejected."""
    violations = _validate_hr({
        "intent": "тест",
        "actions": [{"user_visible_summary": "ok text", "status": ""}],
    })
    assert violations, "Expected violation for blank status"
    codes = [v.code for v in violations]
    assert any("action" in c or "invalid" in c for c in codes), codes


def test_hr_branch_compose_malformed_extra_key_rejected_phase_b() -> None:
    """expected_outcomes[].compose with humanize_result + extra item key is
    rejected at Phase B (branch compose, not root compose)."""
    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="t"),
        actions={"s1": Action(
            tool="add_shopping_items",
            args={"items": ["x"]},
            expected_outcomes=[
                OutcomeBranch(
                    match={"status": "ok"},
                    compose=ComposerCall(
                        kind="llm",
                        llm_prompt_key="humanize_result",
                        template_data={
                            "intent": "додати",
                            "actions": [
                                {
                                    "user_visible_summary": "Зроблено",
                                    "status": "ok",
                                    "execution_id": "123",  # disallowed
                                }
                            ],
                        },
                    ),
                ),
            ],
        )},
        compose=ComposerCall(
            kind="llm",
            llm_prompt_key="humanize_result",
            template_data={"intent": "root", "actions": [{"user_visible_summary": "x", "status": "ok"}]},
        ),
    )
    violations = validate_plan(
        plan, _ALLOWLIST_REGISTRY,
        composer_llm_prompt_keys=_HR_ALLOWLIST_KEYS,
    )
    bad = [v for v in violations if v.code == "humanize_result_extra_action_keys"]
    assert bad, "Branch compose must also enforce per-item key allowlist"
    assert "execution_id" in bad[0].message


def test_hr_ref_string_actions_passes_phase_b() -> None:
    """actions='${s1.x}' (full ref) with allow_refs=True → accepted at Phase B."""
    violations = _validate_hr({
        "intent": "виконати",
        "actions": "${s1.result_items}",
    })
    assert not violations, f"Full-ref actions must pass Phase B: {violations}"


def test_hr_well_formed_static_passes_phase_b() -> None:
    """Well-formed static payload passes all Phase-B checks."""
    violations = _validate_hr({
        "intent": "замовити продукти",
        "actions": [
            {"user_visible_summary": "Додано молоко", "status": "ok"},
            {"user_visible_summary": "Хліб вже є", "status": "duplicate"},
        ],
    })
    assert not violations, f"Well-formed payload must pass: {violations}"


# ===========================================================================
# PR-d (Piece 3) — malformed-ref over compose template_data,
# per-template contract dispatch, invalid-case guards
# ===========================================================================

from sreda.services.composer.registry import REGISTRY as _COMPOSER_REGISTRY  # noqa: E402
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS  # noqa: E402

_REAL_REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}
_REAL_TEMPLATE_IDS = frozenset(_COMPOSER_REGISTRY.template_ids())


def _real_plan(plan_dict: dict) -> Plan:
    return Plan.model_validate(plan_dict)


# ---------------------------------------------------------------------------
# malformed-ref over compose template_data (root + branch)
# ---------------------------------------------------------------------------


def test_malformed_ref_in_root_compose_template_data_flagged() -> None:
    """A ${..bad..} token in ROOT compose template_data is caught with the
    same invalid_ref_syntax code as malformed action-arg refs."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "added"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "shopping_added_ok",
                                 "template_data": {"items": ["молоко"]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "recipe_show",
            # double-dot malformed ref
            "template_data": {"recipe_text": "${s1..raw_text}"},
        },
    })
    violations = validate_plan(plan, _REAL_REGISTRY)
    bad = [v for v in violations if v.code == "invalid_ref_syntax"]
    assert bad, f"expected invalid_ref_syntax; got {[(v.code, v.message) for v in violations]}"
    assert "${s1..raw_text}" in bad[0].message
    assert "root compose" in bad[0].message


def test_malformed_ref_in_branch_compose_template_data_flagged() -> None:
    """A ${..bad..} token in a BRANCH compose template_data is caught."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "error"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "generic_tool_error",
                                 # trailing-dot malformed ref
                                 "template_data": {"error_code": "${s1.}"}}},
                    {"match": {"status": "added"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "shopping_added_ok",
                                 "template_data": {"items": ["молоко"]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["молоко"]},
        },
    })
    violations = validate_plan(plan, _REAL_REGISTRY)
    bad = [v for v in violations if v.code == "invalid_ref_syntax"]
    assert bad, f"expected invalid_ref_syntax; got {[(v.code, v.message) for v in violations]}"
    assert any(v.step_id == "s1" for v in bad)


def test_well_formed_ref_in_compose_template_data_not_flagged() -> None:
    """A well-formed compose ref must NOT trip invalid_ref_syntax — and, being
    a real output field of the matched variant, must not trip the semantic
    compose-ref check either. Exercises the POSITIVE path of
    _phase1_check_compose_ref_syntax (medium R2 MINOR: the prior version had no
    actual ${...} ref, so a regression that wrongly rejected valid compose refs
    would have gone uncaught)."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "schedule_reminder",
                "args": {"title": "x", "trigger_iso": "2026-06-05T10:00:00+03:00"},
                "expected_outcomes": [
                    # ${s1.trigger_at_iso} is a real field of the 'scheduled'
                    # output variant (ScheduleReminderScheduled) — a well-formed,
                    # semantically valid compose ref.
                    {"match": {"status": "scheduled"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "reminder_set_ok",
                                 "template_data": {"what": "x",
                                                   "when_phrase": "${s1.trigger_at_iso}"}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "reminder_set_ok",
            "template_data": {"what": "x", "when_phrase": "в 10:00"},
        },
    })
    violations = validate_plan(plan, _REAL_REGISTRY)
    # Positive path: a well-formed, real-field ref produces NO ref violation of
    # any kind (neither syntax nor semantic).
    ref_bad = [
        v for v in violations
        if v.code in ("invalid_ref_syntax", "compose_ref_unknown_field",
                      "compose_ref_unknown_target")
    ]
    assert not ref_bad, f"valid compose ref must not be flagged; got {ref_bad}"


# ---------------------------------------------------------------------------
# per-template contract dispatch via the registry
# ---------------------------------------------------------------------------


def test_contracted_template_bad_payload_rejected_via_registry() -> None:
    """A bad payload for a CONTRACTED template (clarification family: unknown
    missing_fields code) is rejected — dispatched through the per-template
    contract registry."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "needs_clarification",
        "clarity_reason": "не указано время",
        "actions": {},
        "compose": {
            "kind": "template",
            "template_id": "ask_user_for_clarification",
            # 'totally_made_up' is NOT in CLARIFICATION_FIELDS
            "template_data": {"missing_fields": ["totally_made_up"],
                              "clarity_reason": "не указано время"},
        },
    })
    violations = validate_plan(
        plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
    )
    bad = [v for v in violations if v.code == "clarification_payload_invalid"]
    assert bad, (
        f"contracted template with bad payload must be rejected; got "
        f"{[(v.code, v.message) for v in violations]}"
    )
    assert "totally_made_up" in bad[0].message


def test_valid_clarification_payload_passes_via_registry() -> None:
    """A well-formed clarification payload (known missing_fields codes +
    literal done_summary) passes the per-template registry dispatch at the
    static stage. NOTE: the clarification contract has no ref-deferring field
    (done_summary must be literal; missing_fields are closed-enum codes), so
    allow_refs deferral is proved separately against humanize_result — see
    ``test_humanize_result_field_level_refs_deferred_under_allow_refs``."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "needs_clarification",
        "clarity_reason": "уточнение",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "added"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "partial_with_clarification",
                                 "template_data": {
                                     "done_summary": "добавила молоко",
                                     "missing_fields": ["time"]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "partial_with_clarification",
            "template_data": {"done_summary": "добавила молоко",
                              "missing_fields": ["time"]},
        },
    })
    violations = validate_plan(
        plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
    )
    assert not [v for v in violations if v.code == "clarification_payload_invalid"], (
        f"valid clarification payload must pass; got "
        f"{[(v.code, v.message) for v in violations]}"
    )


def test_no_contract_template_payload_not_dispatched() -> None:
    """A NO_CONTRACT template (shopping_added_ok) with an arbitrary extra key
    must NOT be rejected by the contract dispatch (there is no contract)."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "added"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "shopping_added_ok",
                                 "template_data": {"items": ["молоко"]}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["молоко"], "extra_key": "whatever"},
        },
    })
    violations = validate_plan(
        plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
    )
    assert not [
        v for v in violations
        if v.code in ("clarification_payload_invalid", "composer_contract_invalid")
    ], f"NO_CONTRACT template must not be contract-dispatched; got {violations}"


# ---------------------------------------------------------------------------
# Invalid-case GUARD tests — each of the 4 invalid few-shot cases, when fed
# as a plan, is rejected by the validator with the expected violation.
# ---------------------------------------------------------------------------


def test_invalid_case_index_ref_bracket_rejected() -> None:
    """Invalid few-shot #2: `[0]` bracket index ref in args → invalid_ref_syntax.
    (Bracket indexing is not in the ref grammar; .only is the sanctioned way.)"""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_reminders",
                "args": {},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": "s2"},
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "reminders_list_empty",
                                 "template_data": {}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
            "s2": {
                "tool": "update_reminder",
                "args": {
                    # bracket index — malformed ref
                    "reminder_id": "${s1.items[0].reminder_id}",
                    "trigger_iso": "2026-06-05T10:00:00+03:00",
                },
                "expected_outcomes": [
                    {"match": {"status": "updated"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "reminder_set_ok",
                                 "template_data": {"what": "x",
                                                   "when_phrase": "в 10:00"}}},
                ],
                "intent_group": "default",
                "depends_on": ["s1"],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "reminder_set_ok",
            "template_data": {"what": "x", "when_phrase": "в 10:00"},
        },
    })
    violations = validate_plan(
        plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
    )
    assert any(v.code == "invalid_ref_syntax" for v in violations), (
        f"bracket index ref must be rejected as invalid_ref_syntax; got "
        f"{[(v.code, v.message) for v in violations]}"
    )


def test_invalid_case_guessed_output_field_rejected() -> None:
    """Invalid few-shot #3: guessed/undeclared output field
    (add_shopping_items has no `items`) → compose_ref_unknown_field."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]},
                "expected_outcomes": [
                    {"match": {"status": "added"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "shopping_added_ok",
                                 # add_shopping_items has no `items` output field
                                 "template_data": {"items": "${s1.items}"}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["литерал"]},
        },
    })
    violations = validate_plan(
        plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
    )
    assert any(v.code == "compose_ref_unknown_field" for v in violations), (
        f"guessed output field must be rejected as compose_ref_unknown_field; "
        f"got {[(v.code, v.message) for v in violations]}"
    )


def test_invalid_case_only_in_compose_rejected() -> None:
    """Invalid few-shot #4: `.only` inside a compose ref → only_selector_in_compose."""
    plan = _real_plan({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "t"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "list_reminders",
                "args": {},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "reminder_set_ok",
                                 # .only forbidden in compose
                                 "template_data": {
                                     "what": "${s1.items.only.title}",
                                     "when_phrase": "в 10:00"}}},
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "reminders_list_empty",
                                 "template_data": {}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "reminder_set_ok",
            "template_data": {"what": "x", "when_phrase": "в 10:00"},
        },
    })
    violations = validate_plan(
        plan, _REAL_REGISTRY, composer_template_ids=_REAL_TEMPLATE_IDS,
    )
    assert any(v.code == "only_selector_in_compose" for v in violations), (
        f".only in compose must be rejected; got "
        f"{[(v.code, v.message) for v in violations]}"
    )


def test_invalid_case_smalltalk_in_clear_rejected() -> None:
    """Invalid few-shot #1: a conversational template (smalltalk_fallback) in a
    'clear' plan is rejected at the SCHEMA layer (Plan.model_validate raises)
    — conversational targets are reply_only-only, and clear needs ≥1 action."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Plan.model_validate({
            "schema_version": 1,
            "turn_classification": {"is_new_turn": True, "reason": "t"},
            "clarity": "clear",
            "actions": {},
            "compose": {
                "kind": "template",
                "template_id": "smalltalk_fallback",
                "template_data": {},
            },
        })


def test_all_invalid_few_shot_cases_have_expected_violation_markers() -> None:
    """Cross-check: the documented violation markers on the invalid few-shot
    anti-patterns are exactly the four PR-d boundaries, so the prompt and the
    guard tests above stay in lock-step."""
    from sreda.runtime.planner.few_shot_examples import all_invalid_examples

    violations = {e.violation for e in all_invalid_examples()}
    assert "invalid_ref_syntax" in violations
    assert "compose_ref_unknown_field" in violations
    assert "only_selector_in_compose" in violations
    # smalltalk-in-clear is a schema-level rejection (prefixed "schema:").
    assert any(v.startswith("schema:") for v in violations)
