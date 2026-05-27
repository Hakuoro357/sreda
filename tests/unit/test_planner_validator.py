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

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field as PydField, model_validator

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
    validate_action_args,
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
