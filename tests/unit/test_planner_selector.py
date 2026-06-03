"""Unit tests for PR-b — ``.only`` selector mechanism.

Covers:
1. ``selector_public_label`` — id-stripped, bounded labels for list items.
2. ``resolve_selector_refs`` — single-item resolves correctly; multi-item or
   empty raises ``SelectorAmbiguityError``; options_public contains no ids.
3. Executor integration — ``SelectorAmbiguityError`` routes to pre-invoke
   abort (arg_violation status, no tool invoked, no ledger row).
4. Validator rule 2h — ``.only`` on a ``list[T]`` field passes path
   validation; ``.only`` on a non-list field → Violation.
5. Validator rule 2f — ``.only`` inside a compose ``template_data`` ref
   → Violation.
6. Validator rule 2e — ``.only`` in action args without a producer
   ``status='empty'`` branch → Violation; with the branch → no Violation.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any, Literal, Union

import pytest
from pydantic import BaseModel, ConfigDict, Field

from sreda.runtime.planner.selector import (
    SelectorAmbiguityError,
    resolve_selector_refs,
    selector_public_label,
    unwrap_resolved,
)
from sreda.runtime.planner.interpolation import InvalidReferenceError
from sreda.runtime.planner.executor import execute_plan
from sreda.runtime.planner.plan_compiler import compile as compile_plan
from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.validator import validate_plan
from sreda.services.tool_schemas.base import ToolSpec


# ---------------------------------------------------------------------------
# Minimal helpers shared across test groups
# ---------------------------------------------------------------------------


def _reminder_item(reminder_id: str, raw_line: str) -> dict:
    return {"reminder_id": reminder_id, "raw_line": raw_line}


def _make_state(step_id: str, items: list[dict]) -> dict[str, Any]:
    """Fake state snapshot with a single producer step that has an items list."""
    return {step_id: {"status": "ok", "items": items}}


# ---------------------------------------------------------------------------
# 1. selector_public_label
# ---------------------------------------------------------------------------


class TestSelectorPublicLabel:
    def test_uses_raw_line_and_strips_rem_id(self) -> None:
        item = _reminder_item(
            "rem_aabbccdd11223344556677",
            "[rem_aabbccdd11223344556677] Купить молоко завтра в 09:00",
        )
        label = selector_public_label(item)
        assert label is not None
        # id must be stripped
        assert "rem_" not in label
        # human text must remain
        assert "Купить молоко" in label

    def test_strips_rec_id_from_raw_line(self) -> None:
        item = {"recipe_id": "rec_001122334455667788", "raw_line": "[rec_001122334455667788] Борщ (4 порции)"}
        label = selector_public_label(item)
        assert label is not None
        assert "rec_" not in label
        assert "Борщ" in label

    def test_falls_back_to_title_attr(self) -> None:
        class FakeItem:
            title = "Молоко завтра"

        label = selector_public_label(FakeItem())
        assert label == "Молоко завтра"

    def test_falls_back_to_name_attr(self) -> None:
        class FakeItem:
            name = "Борщ классический"

        label = selector_public_label(FakeItem())
        assert label == "Борщ классический"

    def test_returns_none_when_no_safe_label(self) -> None:
        # Only an id field — no raw_line, title or name
        label = selector_public_label({"reminder_id": "rem_aabbccdd11223344556677"})
        assert label is None

    def test_label_is_bounded(self) -> None:
        long_raw = "[rem_aabbccdd11223344556677] " + "X" * 200
        item = {"raw_line": long_raw}
        label = selector_public_label(item, max_len=60)
        assert label is not None
        assert len(label) <= 60

    def test_dict_title_key(self) -> None:
        item = {"title": "Позвонить врачу"}
        label = selector_public_label(item)
        assert label == "Позвонить врачу"

    def test_empty_raw_line_falls_through_to_title(self) -> None:
        item = {"raw_line": "", "title": "Резервный заголовок"}
        label = selector_public_label(item)
        assert label == "Резервный заголовок"


# ---------------------------------------------------------------------------
# 2. resolve_selector_refs
# ---------------------------------------------------------------------------


class TestResolveSelectorRefs:
    def test_single_item_resolves_field(self) -> None:
        state = _make_state(
            "s1",
            [_reminder_item("rem_aabbccdd11223344556677", "[rem_...] Купить молоко")],
        )
        args = {"reminder_id": "${s1.items.only.reminder_id}"}
        result = unwrap_resolved(resolve_selector_refs(args, state))
        assert result == {"reminder_id": "rem_aabbccdd11223344556677"}

    def test_two_items_raises_ambiguity(self) -> None:
        state = _make_state(
            "s1",
            [
                _reminder_item("rem_aabbccdd11223344556677", "[rem_aabb] Купить молоко"),
                _reminder_item("rem_bbccdd1122334455667788", "[rem_bbcc] Позвонить врачу"),
            ],
        )
        args = {"reminder_id": "${s1.items.only.reminder_id}"}
        with pytest.raises(SelectorAmbiguityError) as exc_info:
            resolve_selector_refs(args, state)
        err = exc_info.value
        assert err.count == 2
        assert err.selector == "only"
        assert "s1.items.only.reminder_id" in err.ref_path

    def test_two_items_options_public_has_no_ids(self) -> None:
        state = _make_state(
            "s1",
            [
                _reminder_item("rem_aabbccdd11223344556677", "[rem_aabb] Купить молоко"),
                _reminder_item("rem_bbccdd1122334455667788", "[rem_bbcc] Позвонить врачу"),
            ],
        )
        args = {"reminder_id": "${s1.items.only.reminder_id}"}
        with pytest.raises(SelectorAmbiguityError) as exc_info:
            resolve_selector_refs(args, state)
        options = exc_info.value.options_public
        # labels must not contain internal id patterns
        for label in options:
            assert not re.search(r"\brem_[0-9a-f]{16,}\b", label, re.IGNORECASE), (
                f"Public label leaks an id: {label!r}"
            )

    def test_zero_items_raises_ambiguity_count_zero(self) -> None:
        state = _make_state("s1", [])
        args = {"x": "${s1.items.only.reminder_id}"}
        with pytest.raises(SelectorAmbiguityError) as exc_info:
            resolve_selector_refs(args, state)
        assert exc_info.value.count == 0

    def test_plain_ref_unchanged(self) -> None:
        """A plain ref (no .only) passes through untouched."""
        state = {"s1": {"status": "ok", "count": 3}}
        args = {"n": "${s1.count}"}
        # Plain refs are left for resolve_refs; this pre-pass should not change them.
        result = resolve_selector_refs(args, state)
        assert result == {"n": "${s1.count}"}

    def test_only_on_non_list_raises_invalid_ref(self) -> None:
        state = {"s1": {"status": "ok", "count": 3}}
        args = {"x": "${s1.count.only.something}"}
        with pytest.raises(InvalidReferenceError):
            resolve_selector_refs(args, state)

    def test_only_without_trailing_field_returns_element(self) -> None:
        """${s1.items.only} with no trailing field → the element itself."""
        state = _make_state("s1", [{"reminder_id": "rem_abc123", "raw_line": "x"}])
        args = {"item": "${s1.items.only}"}
        result = unwrap_resolved(resolve_selector_refs(args, state))
        assert isinstance(result["item"], dict)
        assert result["item"]["reminder_id"] == "rem_abc123"

    def test_nested_dict_args_walked(self) -> None:
        state = _make_state("s1", [_reminder_item("rem_xxyyzz11223344556677", "x")])
        args = {"outer": {"inner": "${s1.items.only.reminder_id}"}}
        result = unwrap_resolved(resolve_selector_refs(args, state))
        assert result["outer"]["inner"] == "rem_xxyyzz11223344556677"

    def test_list_args_walked(self) -> None:
        state = _make_state("s1", [_reminder_item("rem_xxyyzz11223344556677", "x")])
        args = {"ids": ["${s1.items.only.reminder_id}"]}
        result = unwrap_resolved(resolve_selector_refs(args, state))
        assert result["ids"] == ["rem_xxyyzz11223344556677"]


# ---------------------------------------------------------------------------
# 3. Executor integration — SelectorAmbiguityError → pre-invoke abort
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Synthetic ToolSpec + Plan for executor integration tests
# ---------------------------------------------------------------------------
# These use minimal hand-rolled ToolSpecs with mocked process_output so
# we don't depend on the real parsers / raw output format.


class _ListOutput(BaseModel):
    """Minimal discriminated union for the synthetic list tool."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    items: list[dict]


class _ListEmptyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


_SyntheticListOutput = Annotated[
    Union[_ListOutput, _ListEmptyOutput],
    Field(discriminator="status"),
]


class _CancelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["cancelled"] = "cancelled"


class _CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminder_id: str


class _ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _make_synthetic_list_spec(name: str = "syn_list") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Список для unit-теста",
        family="reminders",
        effect="read",
        read_domains=["reminders"],
        write_domains=[],
        input_model=_ListInput,
        output_model=_SyntheticListOutput,
        timeout_seconds=5,
        side_effect_class="read_only",
    )


def _make_synthetic_cancel_spec(name: str = "syn_cancel") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Отмена для unit-теста",
        family="reminders",
        effect="write",
        read_domains=[],
        write_domains=["reminders"],
        input_model=_CancelInput,
        output_model=_SyntheticListOutput,  # reuse; executor only reads status
        timeout_seconds=5,
        side_effect_class="transactional_write",
    )


class _FakeTool:
    """Minimal async tool that returns a pre-baked parsed output dict via
    process_output bypass: ainvoke returns a sentinel string; the ToolSpec's
    process_output is monkey-patched to return the desired parsed model."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.call_count = 0
        self._raise: Exception | None = None

    async def ainvoke(self, args: dict) -> str:
        self.call_count += 1
        if self._raise is not None:
            raise self._raise
        return "__fake_raw__"


def _make_exec_plan_and_tools(
    *,
    items: list[dict],  # what the list tool "returns"
) -> tuple[Any, dict, dict]:
    """Build a 2-step plan (list → cancel via .only), compiled ExecutionPlan,
    tools dict, and registry dict.  process_output on the list ToolSpec is
    patched to return ``_ListOutput(items=items)`` directly.
    """
    list_spec = _make_synthetic_list_spec("syn_list")
    cancel_spec = _make_synthetic_cancel_spec("syn_cancel")

    # Patch process_output on list_spec to return our synthetic output.
    def _list_process_output(raw: str):
        if items:
            return _ListOutput(items=items)
        return _ListEmptyOutput()

    object.__setattr__(list_spec, "_process_output_override", _list_process_output)

    # Patch process_output on cancel_spec to return cancelled status.
    def _cancel_process_output(raw: str):
        return _CancelOutput()

    object.__setattr__(cancel_spec, "_process_output_override", _cancel_process_output)

    # ToolSpec.process_output calls executor_contract.dispatch_typed_output
    # We need to override it at the instance level.  ToolSpec is a pydantic
    # BaseModel with frozen=False, so we can patch via __dict__.
    list_spec_copy = list_spec.model_copy()
    cancel_spec_copy = cancel_spec.model_copy()

    # Bypass pydantic's extra='forbid' __setattr__ to patch process_output.
    # ToolSpec.process_output is a regular method on the class; setting it
    # as an instance attribute via object.__setattr__ makes Python look up
    # the instance dict first, shadowing the class method.
    object.__setattr__(list_spec_copy, "process_output",
                       lambda raw: _list_process_output(raw))
    object.__setattr__(cancel_spec_copy, "process_output",
                       lambda raw: _cancel_process_output(raw))

    # Build registry BEFORE constructing the plan (compile needs tool names)
    registry = {
        "syn_list": list_spec_copy,
        "syn_cancel": cancel_spec_copy,
    }

    payload: dict[str, Any] = {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "syn_list",
                "args": {},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": "s2"},
                    {"match": {"status": "empty"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "shopping_added_ok",
                                 "template_data": {"items": ["x"]}}},
                ],
                "intent_group": "g1",
                "depends_on": [],
            },
            "s2": {
                "tool": "syn_cancel",
                "args": {"reminder_id": "${s1.items.only.reminder_id}"},
                "expected_outcomes": [
                    {"match": {"status": "cancelled"}, "next": None},
                ],
                "intent_group": "g1",
                "depends_on": ["s1"],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        },
    }
    plan = Plan.model_validate(payload)
    ep = compile_plan(plan, registry)

    list_tool = _FakeTool("syn_list")
    cancel_tool = _FakeTool("syn_cancel")
    tools = {"syn_list": list_tool, "syn_cancel": cancel_tool}

    return ep, tools, registry


class TestExecutorSelectorAbort:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_single_item_resolves_and_invokes_consumer(self) -> None:
        """With exactly 1 item, .only resolves and s2 (cancel) is invoked."""
        items = [{"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"}]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        s1 = log.by_step_id().get("s1")
        s2 = log.by_step_id().get("s2")
        assert s1 is not None and s1.status == "ok"
        # s2 should have been invoked (not aborted by selector)
        assert s2 is not None
        assert s2.status != "arg_violation" or "selector_ambiguity" not in (s2.error_summary or "")
        # cancel tool must have been called
        assert tools["syn_cancel"].call_count == 1

    def test_two_items_aborts_before_consumer_invoked(self) -> None:
        """With 2 items, selector is ambiguous — s2 must NOT be invoked."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        s2 = log.by_step_id().get("s2")
        assert s2 is not None
        assert s2.status == "arg_violation"
        # PR-c: barrier sets "selector_barrier_ambiguity:" prefix; per-step fallback
        # sets "selector_ambiguity:". Accept both.
        err = s2.error_summary or ""
        assert "selector_ambiguity" in err or "selector_barrier_ambiguity" in err
        # cancel tool must NOT have been called
        assert tools["syn_cancel"].call_count == 0

    def test_two_items_no_side_effect(self) -> None:
        """When selector aborts, cancel (write) tool is never invoked — no side effect."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        # Verify cancel was not called (no write side effect)
        assert tools["syn_cancel"].call_count == 0, (
            "cancel tool must not be invoked when selector aborts"
        )
        # Outcome should reflect an abort
        assert log.outcome in ("aborted", "aborted_partial")


# ---------------------------------------------------------------------------
# 4. Validator rule 2h — .only on list[T] is valid; .only on non-list → Violation
# ---------------------------------------------------------------------------


class _ItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminder_id: str
    label: str


class _OkOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    items: list[_ItemModel]


class _EmptyOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


_TestListOutput = Annotated[
    Union[_OkOutputModel, _EmptyOutputModel],
    Field(discriminator="status"),
]


class _ConsumerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminder_id: str


def _make_test_list_spec(name: str = "test_list_tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Список тестовых напоминаний для unit-теста",
        family="reminders",
        effect="read",
        read_domains=["reminders"],
        write_domains=[],
        input_model=type("EmptyInput", (BaseModel,), {"model_config": ConfigDict(extra="forbid")}),
        output_model=_TestListOutput,
        timeout_seconds=5,
        side_effect_class="read_only",
    )


def _make_test_consumer_spec(name: str = "test_cancel_tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Отменить тестовый элемент для unit-теста",
        family="reminders",
        effect="write",
        read_domains=[],
        write_domains=["reminders"],
        input_model=_ConsumerInput,
        output_model=_TestListOutput,
        timeout_seconds=5,
        side_effect_class="transactional_write",
    )


def _make_plan_with_only_ref(
    *,
    producer_outcomes: list[dict] | None = None,
    ref: str = "${s1.items.only.reminder_id}",
    compose_ref: str | None = None,
) -> Plan:
    """Build a plan where s2 args contain a .only ref.

    ``producer_outcomes`` controls s1's expected_outcomes (for rule 2e tests).
    ``compose_ref`` if set is placed in root compose template_data (for rule 2f tests).
    """
    if producer_outcomes is None:
        producer_outcomes = [
            {"match": {"status": "ok"}, "next": "s2"},
            {"match": {"status": "empty"}, "next": None,
             "compose": {"kind": "template", "template_id": "shopping_added_ok",
                         "template_data": {"items": ["x"]}}},
        ]

    compose_data: dict[str, Any] = {"items": ["x"]}
    if compose_ref is not None:
        compose_data["reminder_id"] = compose_ref

    payload: dict[str, Any] = {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "test_list_tool",
                "args": {},
                "expected_outcomes": producer_outcomes,
                "intent_group": "g1",
                "depends_on": [],
            },
            "s2": {
                "tool": "test_cancel_tool",
                "args": {"reminder_id": ref},
                "expected_outcomes": [
                    {"match": {"status": "ok"}, "next": None},
                ],
                "intent_group": "g1",
                "depends_on": ["s1"],
            },
        },
        "compose": {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": compose_data,
        },
    }
    return Plan.model_validate(payload)


def _registry_for_validator_test() -> dict[str, ToolSpec]:
    return {
        "test_list_tool": _make_test_list_spec(),
        "test_cancel_tool": _make_test_consumer_spec(),
    }


class TestValidatorRule2h:
    """Rule 2h — .only on list[T] continues path with T; .only on non-list → Violation."""

    def test_valid_only_ref_no_violation(self) -> None:
        """${s1.items.only.reminder_id} — items is list[_ItemModel], .only valid."""
        plan = _make_plan_with_only_ref(ref="${s1.items.only.reminder_id}")
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        # Filter to path-related violations only (ignore other unrelated ones)
        path_violations = [
            v for v in violations
            if v.code in ("invalid_ref_syntax", "compose_ref_unknown_field",
                          "only_selector_non_list")
        ]
        assert path_violations == [], f"Unexpected path violations: {path_violations}"

    def test_only_in_compose_ref_on_non_list_field_is_violation(self) -> None:
        """Rule 2h: when .only appears in a compose ref on a non-list field,
        _annotation_path_status returns False → compose_ref_unknown_field Violation.

        Compose refs go through _maybe_check_field_path / _ref_field_exists_in_output
        which call _annotation_path_status — this is where 2h is exercised for compose.
        The root compose has ${s1.status.only.something}: status is a Literal[str],
        not a list, so .only returns False.
        """
        plan = _make_plan_with_only_ref(
            compose_ref="${s1.status.only.something}",
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        # With .only on a non-list field in compose, we expect at least one violation.
        # Rule 2f fires first (.only in compose → only_selector_in_compose), which
        # is the authoritative guard. Rule 2h (non-list check) provides defence-in-depth
        # for paths that somehow bypass 2f.
        assert len(violations) > 0, (
            "Expected at least one violation for .only on a non-list field in compose"
        )

    def test_only_on_list_field_in_compose_caught_by_2f_not_2h(self) -> None:
        """When .only is on a valid list[T] field in compose, rule 2f fires (not 2h).

        This confirms 2h specifically targets non-list (returns False),
        while valid list[T] fields are caught by the earlier 2f guard instead.
        """
        # ${s1.items.only.reminder_id} — items IS a list[T]; 2h would say True (valid),
        # but 2f still fires because .only in compose is forbidden regardless.
        plan = _make_plan_with_only_ref(
            compose_ref="${s1.items.only.reminder_id}",
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        # Rule 2f fires: only_selector_in_compose
        assert any(v.code == "only_selector_in_compose" for v in violations), (
            f"Expected 'only_selector_in_compose' Violation but got: "
            f"{[v.code for v in violations]}"
        )
        # Rule 2h should NOT fire (items IS a list — path is valid from 2h perspective)
        assert not any(v.code == "only_selector_non_list" for v in violations)


# ---------------------------------------------------------------------------
# 5. Validator rule 2f — .only inside compose template_data → Violation
# ---------------------------------------------------------------------------


class TestValidatorRule2f:
    def test_only_in_root_compose_is_violation(self) -> None:
        plan = _make_plan_with_only_ref(
            compose_ref="${s1.items.only.reminder_id}",
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        only_compose_violations = [
            v for v in violations if v.code == "only_selector_in_compose"
        ]
        assert len(only_compose_violations) >= 1, (
            f"Expected 'only_selector_in_compose' violation; got: {[v.code for v in violations]}"
        )

    def test_plain_ref_in_compose_no_violation(self) -> None:
        """A plain ref (no .only) in compose template_data must not trigger rule 2f."""
        plan = _make_plan_with_only_ref(
            # Plain ref — valid; no .only
            compose_ref=None,  # no extra compose ref
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        only_compose_violations = [
            v for v in violations if v.code == "only_selector_in_compose"
        ]
        assert only_compose_violations == []

    def test_only_in_branch_compose_is_violation(self) -> None:
        """A .only ref inside a branch compose (not root) also triggers rule 2f.

        The branch that has compose must NOT also have next (schema constraint).
        So we put the .only ref in the terminal empty-branch compose.
        """
        producer_outcomes = [
            {"match": {"status": "ok"}, "next": "s2"},
            # Terminal branch (no next) — compose with .only ref is forbidden
            {"match": {"status": "empty"}, "next": None,
             "compose": {
                 "kind": "template",
                 "template_id": "shopping_added_ok",
                 "template_data": {
                     "items": ["x"],
                     "bad_ref": "${s1.items.only.reminder_id}",
                 },
             }},
        ]
        plan = _make_plan_with_only_ref(producer_outcomes=producer_outcomes)
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        only_compose_violations = [
            v for v in violations if v.code == "only_selector_in_compose"
        ]
        assert len(only_compose_violations) >= 1


# ---------------------------------------------------------------------------
# 6. Validator rule 2e — producer must have status='empty' branch
# ---------------------------------------------------------------------------


class TestValidatorRule2e:
    def test_missing_empty_branch_is_violation(self) -> None:
        """Producer has only 'ok' branch, no 'empty' branch → Violation."""
        producer_outcomes_no_empty = [
            {"match": {"status": "ok"}, "next": "s2"},
        ]
        plan = _make_plan_with_only_ref(
            producer_outcomes=producer_outcomes_no_empty
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        rule2e_violations = [
            v for v in violations if v.code == "only_selector_missing_empty_branch"
        ]
        assert len(rule2e_violations) >= 1, (
            f"Expected 'only_selector_missing_empty_branch'; got: {[v.code for v in violations]}"
        )

    def test_with_empty_branch_no_violation(self) -> None:
        """Producer has both 'ok' and 'empty' branches → no rule 2e Violation."""
        producer_outcomes_with_empty = [
            {"match": {"status": "ok"}, "next": "s2"},
            {"match": {"status": "empty"}, "next": None,
             "compose": {"kind": "template", "template_id": "shopping_added_ok",
                         "template_data": {"items": ["x"]}}},
        ]
        plan = _make_plan_with_only_ref(
            producer_outcomes=producer_outcomes_with_empty
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        rule2e_violations = [
            v for v in violations if v.code == "only_selector_missing_empty_branch"
        ]
        assert rule2e_violations == [], (
            f"Unexpected rule 2e violations: {rule2e_violations}"
        )

    def test_no_only_ref_no_rule2e_violation(self) -> None:
        """If no .only ref is used, rule 2e never fires even without empty branch."""
        payload: dict[str, Any] = {
            "schema_version": 1,
            "turn_classification": {"is_new_turn": True, "reason": "test"},
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "test_list_tool",
                    "args": {},
                    "expected_outcomes": [
                        {"match": {"status": "ok"}, "next": None},
                        # intentionally no 'empty' branch
                    ],
                    "intent_group": "g1",
                    "depends_on": [],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "shopping_added_ok",
                "template_data": {"items": ["x"]},
            },
        }
        plan = Plan.model_validate(payload)
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        rule2e_violations = [
            v for v in violations if v.code == "only_selector_missing_empty_branch"
        ]
        assert rule2e_violations == []


# ---------------------------------------------------------------------------
# 7. FIX 1 — Double-resolution prevention via _ResolvedLiteral sentinel
# ---------------------------------------------------------------------------


class TestDoubleResolutionPrevention:
    """Selector-resolved values containing ${...} must NOT be re-resolved."""

    def test_resolved_string_with_dollar_brace_not_re_resolved(self) -> None:
        """A title that literally contains '${s1.status}' is returned verbatim."""
        from sreda.runtime.planner.selector import resolve_selector_refs, unwrap_resolved
        from sreda.runtime.planner.interpolation import resolve_refs

        # The resolved value contains a literal '${s1.status}' token.
        dangerous_title = "Reminder about ${s1.status} check"
        state: dict = {
            "s1": {
                "status": "ok",
                "items": [{"reminder_id": "rem_aabbccdd11223344556677", "title": dangerous_title}],
            }
        }
        args = {"title": "${s1.items.only.title}"}

        # Step 1: selector pre-pass
        selector_result = resolve_selector_refs(args, state)
        # Step 2: plain ref pass (must not re-resolve the literal ${...})
        after_plain = resolve_refs(selector_result, state)
        # Step 3: unwrap sentinels
        final = unwrap_resolved(after_plain)

        # The literal '${s1.status}' inside the title must survive verbatim.
        assert final["title"] == dangerous_title, (
            f"Double-resolution occurred: got {final['title']!r}, "
            f"expected {dangerous_title!r}"
        )

    def test_mixed_string_combined_pass(self) -> None:
        """Mixed string 'Элемент: ${s1.items.only.title}, count=${s1.count}'."""
        from sreda.runtime.planner.selector import resolve_selector_refs, unwrap_resolved
        from sreda.runtime.planner.interpolation import resolve_refs

        state: dict = {
            "s1": {
                "status": "ok",
                "items": [{"reminder_id": "rem_aabbccdd11223344556677", "title": "Купить молоко"}],
                "count": 1,
            }
        }
        # Mixed: selector ref + plain ref in the same string.
        args = {"msg": "Элемент: ${s1.items.only.title}, count=${s1.count}"}

        selector_result = resolve_selector_refs(args, state)
        after_plain = resolve_refs(selector_result, state)
        final = unwrap_resolved(after_plain)

        assert final["msg"] == "Элемент: Купить молоко, count=1", (
            f"Unexpected mixed resolution result: {final['msg']!r}"
        )


# ---------------------------------------------------------------------------
# 8. FIX 2 — Broadened id-prefix scrubber + title/name scrubbing
# ---------------------------------------------------------------------------


class TestSelectorPublicLabelFix2:
    """All id prefixes from common.py are stripped; title/name are also scrubbed."""

    @pytest.mark.parametrize("prefix,suffix", [
        ("task", "aabbccdd11223344556677"),
        ("checklist", "aabbccdd11223344556677"),
        ("clitem", "aabbccdd11223344556677"),
        ("menu", "aabbccdd11223344556677"),
        ("mpi", "aabbccdd11223344556677"),
        ("fm", "aabbccdd11223344556677"),
    ])
    def test_new_prefix_stripped_from_raw_line(self, prefix: str, suffix: str) -> None:
        item_id = f"{prefix}_{suffix}"
        item = {"raw_line": f"[{item_id}] Человекочитаемая строка"}
        label = selector_public_label(item)
        assert label is not None
        assert prefix + "_" not in label, (
            f"id prefix '{prefix}_' leaked into label: {label!r}"
        )
        assert "Человекочитаемая строка" in label

    def test_task_id_stripped_from_title(self) -> None:
        """title field containing a task_ id is scrubbed (FIX 2)."""
        item_id = "task_aabbccdd11223344556677"
        item = {"title": f"[{item_id}] Позвонить врачу"}
        label = selector_public_label(item)
        assert label is not None
        assert "task_" not in label, f"task_ id leaked into title label: {label!r}"
        assert "Позвонить врачу" in label

    def test_checklist_id_stripped_from_name(self) -> None:
        """name field containing a checklist_ id is scrubbed (FIX 2)."""
        item_id = "checklist_aabbccdd11223344556677"
        item = {"name": f"Список [{item_id}]"}
        label = selector_public_label(item)
        assert label is not None
        assert "checklist_" not in label, f"checklist_ id leaked into name label: {label!r}"

    def test_pure_display_title_unchanged(self) -> None:
        """A title without any id pattern is returned as-is (no corruption)."""
        item = {"title": "Купить молоко завтра"}
        label = selector_public_label(item)
        assert label == "Купить молоко завтра"


# ---------------------------------------------------------------------------
# 9. FIX 3 — Rule 2e requires TERMINAL empty branch (next is None)
# ---------------------------------------------------------------------------


class TestValidatorRule2eTerminalBranch:
    """Non-terminal empty branch (next != None) must NOT satisfy rule 2e."""

    def test_non_terminal_empty_branch_is_violation(self) -> None:
        """empty branch with next='s2' → not terminal → Violation."""
        producer_outcomes_nonterminal_empty = [
            {"match": {"status": "ok"}, "next": "s2"},
            # empty branch exists but is NOT terminal (next is not None)
            {"match": {"status": "empty"}, "next": "s2"},
        ]
        plan = _make_plan_with_only_ref(
            producer_outcomes=producer_outcomes_nonterminal_empty
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        rule2e_violations = [
            v for v in violations if v.code == "only_selector_missing_empty_branch"
        ]
        assert len(rule2e_violations) >= 1, (
            "Expected rule 2e violation for non-terminal empty branch; "
            f"got: {[v.code for v in violations]}"
        )

    def test_terminal_empty_branch_no_violation(self) -> None:
        """empty branch with next=None → terminal → no rule 2e violation."""
        producer_outcomes_terminal_empty = [
            {"match": {"status": "ok"}, "next": "s2"},
            {"match": {"status": "empty"}, "next": None,
             "compose": {"kind": "template", "template_id": "shopping_added_ok",
                         "template_data": {"items": ["x"]}}},
        ]
        plan = _make_plan_with_only_ref(
            producer_outcomes=producer_outcomes_terminal_empty
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        rule2e_violations = [
            v for v in violations if v.code == "only_selector_missing_empty_branch"
        ]
        assert rule2e_violations == [], (
            f"Unexpected rule 2e violations for terminal empty branch: {rule2e_violations}"
        )


# ---------------------------------------------------------------------------
# 10. FIX 4 — Rule 2h: .only on dict/tuple-typed field → static path invalid
# ---------------------------------------------------------------------------


class TestValidatorRule2hListOnly:
    """.only must only be valid on list[T] — not dict, set, tuple, frozenset."""

    def test_only_on_non_list_field_in_compose_is_violation(self) -> None:
        """.only on a non-list field in compose → at least one violation raised.

        Rule 2f fires for compose refs (.only in compose is always forbidden).
        This confirms .only on a non-list field is not silently allowed.
        """
        plan = _make_plan_with_only_ref(
            compose_ref="${s1.status.only.something}",
        )
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        assert len(violations) > 0, (
            "Expected violation for .only on non-list field in compose"
        )

    def test_only_on_list_field_in_action_args_is_valid(self) -> None:
        """.only on list[T] in action args → no 'only_selector_non_list' violation."""
        plan = _make_plan_with_only_ref(ref="${s1.items.only.reminder_id}")
        registry = _registry_for_validator_test()
        violations = validate_plan(plan, registry)
        h_violations = [v for v in violations if v.code == "only_selector_non_list"]
        assert h_violations == [], (
            f"Unexpected 'only_selector_non_list' violations: {h_violations}"
        )


# ---------------------------------------------------------------------------
# PR-c tests — 2c abort_kind carrier, 2g barrier, 2d routing, template, few-shot
# ---------------------------------------------------------------------------


class TestPRcAbortKindCarrier:
    """2c: ExecutionLog.abort_kind + abort_details_public/internal when .only ambiguous."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_abort_kind_set_when_ambiguous(self) -> None:
        """2+ items → ExecutionLog.abort_kind == 'selector_ambiguity'."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_kind == "selector_ambiguity"

    def test_abort_details_public_has_no_ids(self) -> None:
        """abort_details_public options must not contain internal rem_ ids."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_details_public is not None
        options = log.abort_details_public.get("options", [])
        count = log.abort_details_public.get("count")
        assert count == 2
        for label in options:
            assert "rem_" not in label, f"ID leaked into public label: {label!r}"

    def test_abort_details_internal_has_source_step_and_tool(self) -> None:
        """abort_details_internal contains source_step_id, target_tool, arg_path."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_details_internal is not None
        assert "source_step_id" in log.abort_details_internal
        assert "target_tool" in log.abort_details_internal
        assert "arg_path" in log.abort_details_internal

    def test_no_abort_kind_when_single_item(self) -> None:
        """Exactly 1 item → abort_kind is None (no ambiguity)."""
        items = [{"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"}]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_kind is None
        assert log.abort_details_public is None
        assert log.abort_details_internal is None

    def test_outcome_aborted_class_when_no_prior_write(self) -> None:
        """No prior writes → outcome in ('aborted', 'aborted_partial')."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        # s1 is read-only, so no write committed → outcome == "aborted" exactly
        # (Codex PR-c R1 MINOR: tighten from the loose aborted/aborted_partial).
        assert log.outcome == "aborted"

    def test_integration_ambiguous_only_renders_clarification(self) -> None:
        """End-to-end (Codex PR-c R1): execute a plan whose .only is ambiguous →
        ExecutionLog carries selector_ambiguity → clarification_compose_for_abort
        → compose() renders a clarification listing the public labels, NO ids."""
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        from sreda.services.composer import REGISTRY
        from sreda.services.composer.compose import compose

        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_kind == "selector_ambiguity"

        call = clarification_compose_for_abort(log)
        assert call is not None
        result = compose(call, log, registry=REGISTRY)
        text = result.text
        assert text and text.strip()
        # No raw ids leak into the user-facing clarification.
        assert "rem_" not in text
        # The sanitized public labels reach the user.
        assert "молоко" in text or "врачу" in text

    def test_selector_ambiguity_clarification_is_runtime_only(self) -> None:
        """selector_ambiguity_clarification is RUNTIME-only (Codex PR-c R1, medium):
        produced by clarification_compose_for_abort AFTER execution, NEVER emitted
        by the planner. So it must NOT be in CLARIFICATION_TEMPLATE_IDS (the planner
        clarification allowlist), but IS a real registered template so compose()
        can render it at runtime."""
        from sreda.runtime.planner.schemas import CLARIFICATION_TEMPLATE_IDS
        from sreda.services.composer import REGISTRY

        assert "selector_ambiguity_clarification" not in CLARIFICATION_TEMPLATE_IDS
        assert "selector_ambiguity_clarification" in REGISTRY.template_ids()

    def test_compose_for_execution_routes_abort_without_manual_swap(self) -> None:
        """PR-c R3 (Codex medium MAJOR): the single seam ``compose_for_execution``
        routes a selector-ambiguity abort to its clarification WITHOUT the caller
        doing the manual ``clarification_compose_for_abort`` swap. A consumer that
        passes only ``plan.compose`` (the naive/forgotten-swap path) still gets the
        clarification — never the original plan compose. No raw ids leak."""
        from sreda.runtime.planner.schemas import ComposerCall
        from sreda.runtime.planner.selector import compose_for_execution
        from sreda.services.composer import REGISTRY
        from sreda.services.composer.compose import compose

        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_kind == "selector_ambiguity"

        # The plan-level compose a consumer would "naively" pass straight to
        # compose(). The seam MUST ignore it for the abort.
        plan_call = ComposerCall.model_validate({
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        })
        result = compose_for_execution(plan_call, log, registry=REGISTRY)
        # What a consumer that FORGOT the swap would have rendered (the bug).
        naive = compose(plan_call, log, registry=REGISTRY)

        assert result.text and result.text.strip()
        # The seam diverges from the naive (wrong) path: clarification, not the
        # plan-level shopping compose.
        assert result.text != naive.text
        assert "rem_" not in result.text                       # no raw id leak
        assert "молоко" in result.text or "врачу" in result.text  # sanitized labels

    def test_compose_for_execution_passes_through_when_not_aborted(self) -> None:
        """PR-c R3: for a non-abort (completed) execution the seam is a faithful
        pass-through — identical to calling ``compose(plan.compose, log)`` directly,
        so it adds NO behaviour change on the normal path."""
        from sreda.runtime.planner.schemas import ComposerCall
        from sreda.runtime.planner.selector import compose_for_execution
        from sreda.services.composer import REGISTRY
        from sreda.services.composer.compose import compose

        # Single item → .only resolves → cancel runs → completed (no abort).
        items = [{"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"}]
        ep, tools, registry = _make_exec_plan_and_tools(items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_kind is None

        plan_call = ComposerCall.model_validate({
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        })
        via_seam = compose_for_execution(plan_call, log, registry=REGISTRY)
        direct = compose(plan_call, log, registry=REGISTRY)
        assert via_seam.text == direct.text
        assert via_seam.fallback_used == direct.fallback_used


class TestPRcAbortedPartial:
    """aborted_partial: prior write committed + .only ambiguity → outcome + routing."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_plan_with_write_then_only(
        self, *, list_items: list[dict],
    ):
        """Build a plan with two independent groups:
        - g_write: a write step that succeeds (committed side effect)
        - g_read: list (s_list) → .only consumer (s_act); ambiguous when >1 items

        The plan is compiled so g_write runs in layer 1, then s_list/s_act in later
        layers. When s_act's .only is ambiguous, aborted_partial fires because g_write
        already committed.
        """
        # Reuse synthetic specs from existing fixture
        list_spec = _make_synthetic_list_spec("wt_list")
        cancel_spec = _make_synthetic_cancel_spec("wt_cancel")
        write_spec = _make_synthetic_cancel_spec("wt_write")  # another write tool

        def _list_out(raw: str):
            if list_items:
                return _ListOutput(items=list_items)
            return _ListEmptyOutput()

        def _cancel_out(raw: str):
            return _CancelOutput()

        for spec, fn in [
            (list_spec, _list_out), (cancel_spec, _cancel_out), (write_spec, _cancel_out)
        ]:
            copy = spec.model_copy()
            object.__setattr__(copy, "process_output", lambda raw, _fn=fn: _fn(raw))
            if spec is list_spec:
                list_spec_final = copy
            elif spec is cancel_spec:
                cancel_spec_final = copy
            else:
                write_spec_final = copy

        registry = {
            "wt_list": list_spec_final,
            "wt_cancel": cancel_spec_final,
            "wt_write": write_spec_final,
        }

        payload: dict[str, Any] = {
            "schema_version": 1,
            "turn_classification": {"is_new_turn": True, "reason": "test"},
            "clarity": "clear",
            "actions": {
                # s1: independent write step (group gw) — committed before the .only step
                "s1": {
                    "tool": "wt_write",
                    "args": {"reminder_id": "rem_aabbccdd1122334455667788"},
                    "expected_outcomes": [
                        {"match": {"status": "cancelled"}, "next": None,
                         "compose": {"kind": "template",
                                     "template_id": "shopping_added_ok",
                                     "template_data": {"items": ["x"]}}},
                    ],
                    "intent_group": "gw",
                    "depends_on": [],
                },
                # s2: list step — depends on nothing (independent read)
                "s2": {
                    "tool": "wt_list",
                    "args": {},
                    "expected_outcomes": [
                        {"match": {"status": "ok"}, "next": "s3"},
                        {"match": {"status": "empty"}, "next": None,
                         "compose": {"kind": "template",
                                     "template_id": "reminders_list_empty",
                                     "template_data": {}}},
                    ],
                    "intent_group": "gr",
                    "depends_on": [],
                },
                # s3: .only consumer — depends on s2
                "s3": {
                    "tool": "wt_cancel",
                    "args": {"reminder_id": "${s2.items.only.reminder_id}"},
                    "expected_outcomes": [
                        {"match": {"status": "cancelled"}, "next": None,
                         "compose": {"kind": "template",
                                     "template_id": "shopping_added_ok",
                                     "template_data": {"items": ["x"]}}},
                    ],
                    "intent_group": "gr",
                    "depends_on": ["s2"],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "shopping_added_ok",
                "template_data": {"items": ["x"]},
            },
        }
        plan = Plan.model_validate(payload)
        ep = compile_plan(plan, registry)

        write_tool = _FakeTool("wt_write")
        list_tool = _FakeTool("wt_list")
        cancel_tool = _FakeTool("wt_cancel")
        tools = {
            "wt_write": write_tool,
            "wt_list": list_tool,
            "wt_cancel": cancel_tool,
        }
        return ep, tools, registry

    def test_aborted_partial_when_prior_write_committed(self) -> None:
        """Prior write committed + .only ambiguity → outcome == 'aborted_partial'."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = self._make_plan_with_write_then_only(list_items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.abort_kind == "selector_ambiguity"
        assert log.outcome == "aborted_partial"

    def test_clarification_compose_for_abort_partial_returns_selector_with_options(self) -> None:
        """aborted_partial → clarification_compose_for_abort returns the selector
        template with partial=True, the sanitized options AND a generic literal
        done_summary (Codex PR-c R4 high: the partial user must SEE the options)."""
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = self._make_plan_with_write_then_only(list_items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.outcome == "aborted_partial"
        call = clarification_compose_for_abort(log)
        assert call is not None
        assert call.template_id == "selector_ambiguity_clarification"
        assert call.template_data is not None
        td = call.template_data
        assert td["partial"] is True
        # The ambiguous options are carried through so the partial user sees them.
        assert td["options"] and isinstance(td["options"], list)
        # done_summary must be a GENERIC literal — no raw tool names (leak class).
        ds = td.get("done_summary")
        assert isinstance(ds, str) and ds.strip()

    def test_compose_for_execution_partial_renders_ack_and_options(self) -> None:
        """PR-c R4 (high) MAJOR fix, end-to-end through the seam: an
        aborted_partial selector ambiguity renders BOTH the acknowledgement AND
        the ambiguous option labels (so the partial user sees what to
        disambiguate), with no raw id leak."""
        from sreda.runtime.planner.schemas import ComposerCall
        from sreda.runtime.planner.selector import compose_for_execution
        from sreda.services.composer import REGISTRY

        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = self._make_plan_with_write_then_only(list_items=items)
        log = self._run(execute_plan(ep, tools, registry))
        assert log.outcome == "aborted_partial"

        plan_call = ComposerCall.model_validate({
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        })
        result = compose_for_execution(plan_call, log, registry=REGISTRY)
        text = result.text
        assert "Сделала:" in text                          # acknowledgement
        assert "молоко" in text or "врачу" in text          # ambiguous options shown
        assert "rem_" not in text                           # no raw id leak


class TestPRcBarrierSameBatch:
    """2g: barrier prevents same-batch siblings from running when one step is ambiguous."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_plan_two_parallel_consumers(self, *, list_items: list[dict]):
        """Build a plan where s_list feeds two consumers in the same batch (same layer,
        same intent_group so both are in one batch). Both use .only. When ambiguous,
        the barrier should fire before either consumer runs.

        Actually since the barrier iterates batch_filtered and halts on first ambiguous
        step, the OTHER step in the same batch should also be drained as skipped.
        """
        list_spec = _make_synthetic_list_spec("sb_list")
        cancel1_spec = _make_synthetic_cancel_spec("sb_cancel1")
        cancel2_spec = _make_synthetic_cancel_spec("sb_cancel2")

        def _list_out(raw: str):
            return _ListOutput(items=list_items) if list_items else _ListEmptyOutput()

        def _cancel_out(raw: str):
            return _CancelOutput()

        list_copy = list_spec.model_copy()
        object.__setattr__(list_copy, "process_output", lambda raw: _list_out(raw))
        c1_copy = cancel1_spec.model_copy()
        object.__setattr__(c1_copy, "process_output", lambda raw: _cancel_out(raw))
        c2_copy = cancel2_spec.model_copy()
        object.__setattr__(c2_copy, "process_output", lambda raw: _cancel_out(raw))

        registry = {"sb_list": list_copy, "sb_cancel1": c1_copy, "sb_cancel2": c2_copy}

        payload: dict[str, Any] = {
            "schema_version": 1,
            "turn_classification": {"is_new_turn": True, "reason": "test"},
            "clarity": "clear",
            "actions": {
                "s1": {
                    "tool": "sb_list",
                    "args": {},
                    "expected_outcomes": [
                        {"match": {"status": "ok"}, "next": "s2"},
                        {"match": {"status": "empty"}, "next": None,
                         "compose": {"kind": "template",
                                     "template_id": "reminders_list_empty",
                                     "template_data": {}}},
                    ],
                    "intent_group": "g1",
                    "depends_on": [],
                },
                "s2": {
                    "tool": "sb_cancel1",
                    "args": {"reminder_id": "${s1.items.only.reminder_id}"},
                    "expected_outcomes": [
                        {"match": {"status": "cancelled"}, "next": None},
                    ],
                    "intent_group": "g1",
                    "depends_on": ["s1"],
                },
                "s3": {
                    "tool": "sb_cancel2",
                    "args": {"reminder_id": "${s1.items.only.reminder_id}"},
                    "expected_outcomes": [
                        {"match": {"status": "cancelled"}, "next": None},
                    ],
                    "intent_group": "g1",
                    "depends_on": ["s1"],
                },
            },
            "compose": {
                "kind": "template",
                "template_id": "shopping_added_ok",
                "template_data": {"items": ["x"]},
            },
        }
        plan = Plan.model_validate(payload)
        ep = compile_plan(plan, registry)

        list_tool = _FakeTool("sb_list")
        cancel1_tool = _FakeTool("sb_cancel1")
        cancel2_tool = _FakeTool("sb_cancel2")
        tools = {
            "sb_list": list_tool, "sb_cancel1": cancel1_tool, "sb_cancel2": cancel2_tool,
        }
        return ep, tools, registry

    def test_same_batch_sibling_not_invoked(self) -> None:
        """When one step in a batch has ambiguous .only, the other same-batch step
        must also NOT be invoked (no side effect from siblings)."""
        items = [
            {"reminder_id": "rem_aabbccdd1122334455667788", "raw_line": "Купить молоко"},
            {"reminder_id": "rem_bbccdd112233445566778899", "raw_line": "Позвонить врачу"},
        ]
        ep, tools, registry = self._make_plan_two_parallel_consumers(list_items=items)
        log = self._run(execute_plan(ep, tools, registry))
        # Neither cancel tool should have been called
        assert tools["sb_cancel1"].call_count == 0, (
            "sb_cancel1 was invoked despite barrier — same-batch sibling must not run"
        )
        assert tools["sb_cancel2"].call_count == 0, (
            "sb_cancel2 was invoked despite barrier — same-batch sibling must not run"
        )
        assert log.abort_kind == "selector_ambiguity"


class TestPRcClarificationComposeForAbort:
    """2d: clarification_compose_for_abort maps abort_kind → ComposerCall or None."""

    def _make_aborted_log(
        self, *, outcome: str, abort_kind: str | None,
        options: list[str] | None = None, count: int = 0,
        ok_steps: list | None = None,
    ):
        """Build a minimal ExecutionLog for routing tests."""
        from sreda.runtime.planner.executor import ExecutionLog, StepResult
        steps = []
        if ok_steps:
            for step_id, tool in ok_steps:
                steps.append(StepResult(step_id=step_id, tool=tool, status="ok",
                                        parsed_output={"status": "ok"}))
        return ExecutionLog(
            steps=tuple(steps),
            outcome=outcome,  # type: ignore[arg-type]
            abort_kind=abort_kind,
            abort_details_public={"options": options or [], "count": count}
            if abort_kind == "selector_ambiguity" else None,
            abort_details_internal={"source_step_id": "s1", "target_tool": "t",
                                    "arg_path": "s1.items.only.id"}
            if abort_kind == "selector_ambiguity" else None,
        )

    def test_returns_none_when_no_abort_kind(self) -> None:
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        log = self._make_aborted_log(outcome="aborted", abort_kind=None)
        assert clarification_compose_for_abort(log) is None

    def test_returns_none_when_abort_kind_not_selector(self) -> None:
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        log = self._make_aborted_log(outcome="aborted", abort_kind="other_reason")
        assert clarification_compose_for_abort(log) is None

    def test_aborted_returns_selector_ambiguity_clarification(self) -> None:
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        log = self._make_aborted_log(
            outcome="aborted",
            abort_kind="selector_ambiguity",
            options=["Купить молоко", "Позвонить врачу"],
            count=2,
        )
        call = clarification_compose_for_abort(log)
        assert call is not None
        assert call.template_id == "selector_ambiguity_clarification"
        assert call.template_data is not None
        td = call.template_data
        assert td["count"] == 2
        assert td["partial"] is False
        assert "options" in td

    def test_aborted_partial_returns_selector_partial_true_with_done_summary(self) -> None:
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        log = self._make_aborted_log(
            outcome="aborted_partial",
            abort_kind="selector_ambiguity",
            options=["Купить молоко", "Позвонить врачу"],
            count=2,
            ok_steps=[("s_write", "wt_write")],
        )
        call = clarification_compose_for_abort(log)
        assert call is not None
        # PR-c R4 (high): partial routes to the selector template (which shows
        # options), with partial=True + a generic acknowledgement.
        assert call.template_id == "selector_ambiguity_clarification"
        td = call.template_data
        assert td["partial"] is True
        assert td["count"] == 2
        assert td["options"] == ["Купить молоко", "Позвонить врачу"]
        # done_summary is a GENERIC literal — must NOT leak the raw tool name.
        assert isinstance(td.get("done_summary"), str) and td["done_summary"].strip()
        assert "wt_write" not in td["done_summary"]

    def test_options_in_template_data(self) -> None:
        from sreda.runtime.planner.selector import clarification_compose_for_abort
        log = self._make_aborted_log(
            outcome="aborted",
            abort_kind="selector_ambiguity",
            options=["Опция А", "Опция Б"],
            count=2,
        )
        call = clarification_compose_for_abort(log)
        assert call is not None
        assert call.template_data["options"] == ["Опция А", "Опция Б"]


class TestPRcSelectorAmbiguityTemplate:
    """2d: selector_ambiguity_clarification template renders labels+count, no ids."""

    def _render(self, template_data: dict) -> str:
        from sreda.services.composer.registry import REGISTRY
        return REGISTRY.render("selector_ambiguity_clarification", template_data)

    def test_renders_option_labels(self) -> None:
        text = self._render({
            "options": ["Купить молоко", "Позвонить врачу"],
            "count": 2,
            "partial": False,
        })
        assert "Купить молоко" in text
        assert "Позвонить врачу" in text

    def test_renders_count_in_output(self) -> None:
        text = self._render({
            "options": ["Купить молоко", "Позвонить врачу"],
            "count": 2,
            "partial": False,
        })
        assert "2" in text

    def test_no_ids_in_rendered_output(self) -> None:
        # Template must render options as-is (id-stripping is caller's job),
        # but the template itself must not ADD ids. The key assertion: if a
        # caller passes clean options, no ids appear.
        clean_text = self._render({
            "options": ["Купить молоко", "Позвонить врачу"],
            "count": 2,
            "partial": False,
        })
        assert "rem_" not in clean_text

    def test_done_summary_not_rendered_without_partial_flag(self) -> None:
        # PR-c R4: done_summary renders ONLY when partial is truthy. Passing
        # done_summary WITHOUT partial=True must NOT render the acknowledgement —
        # guards the plain-aborted path against accidental ack leakage.
        text = self._render({
            "options": ["Купить молоко"],
            "count": 1,
            "done_summary": "добавила покупки",
        })
        assert "добавила покупки" not in text
        assert "Сделала:" not in text

    def test_partial_true_renders_ack_then_options(self) -> None:
        # PR-c R4 (high) MAJOR fix: aborted_partial → acknowledgement THEN the
        # option list, so the partial user still sees what to disambiguate. The
        # done_summary is a generic literal; no raw id leaks.
        text = self._render({
            "options": ["Купить молоко", "Позвонить врачу"],
            "count": 2,
            "partial": True,
            "done_summary": "часть действий уже выполнила",
        })
        assert "Сделала: часть действий уже выполнила" in text
        assert "Купить молоко" in text
        assert "Позвонить врачу" in text
        assert "2" in text
        assert "rem_" not in text

    def test_partial_false_no_done_summary_line(self) -> None:
        text = self._render({
            "options": ["Купить молоко"],
            "count": 1,
            "partial": False,
        })
        # "Сделала:" should not appear when partial=False
        assert "Сделала:" not in text

    def test_empty_options_renders_count_fallback(self) -> None:
        text = self._render({
            "options": [],
            "count": 3,
            "partial": False,
        })
        # When no option labels, template should fall back to count-only phrase
        assert "3" in text


class TestPRcFewShotExample:
    """The new .only few-shot example (example 11) passes validate_plan + model_validate."""

    def test_few_shot_only_example_model_validate(self) -> None:
        from sreda.runtime.planner.few_shot_examples import _ONLY_SELECTOR_EXAMPLE
        from sreda.runtime.planner.schemas import Plan
        # Must parse without error
        plan = Plan.model_validate(_ONLY_SELECTOR_EXAMPLE.plan)
        assert plan.clarity == "clear"
        # Must have both steps
        assert "s1" in plan.actions
        assert "s2" in plan.actions

    def test_few_shot_only_example_validate_plan(self) -> None:
        from sreda.runtime.planner.few_shot_examples import _ONLY_SELECTOR_EXAMPLE
        from sreda.runtime.planner.schemas import Plan
        from sreda.runtime.planner.validator import validate_plan
        from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
        registry = {s.name: s for s in MIGRATED_TOOL_SPECS}
        plan = Plan.model_validate(_ONLY_SELECTOR_EXAMPLE.plan)
        violations = validate_plan(plan, registry)
        # Only rule 2e/2f/2h violations would indicate a broken example.
        # Filter to selector-related violations — there must be none.
        sel_violations = [
            v for v in violations
            if "only" in v.code or "selector" in v.code
        ]
        assert sel_violations == [], (
            f"Few-shot .only example has selector violations: "
            f"{[(v.code, v.message) for v in sel_violations]}"
        )
