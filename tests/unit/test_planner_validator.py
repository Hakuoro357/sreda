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

import typing
from typing import Literal, NewType

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
