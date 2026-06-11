"""Tests for runtime/planner/plan_compiler.py — Sub-A12 Phase B.3.

Covers:
- can_run_parallel — read/read, read/write, write/write matrices
- _topological_layers — linear chain, parallel sources, cycle detection
- _partition_layer_by_parallelism — greedy assignment
- _infer_fail_modes — partial vs honest_partial per Group 2 rule
- compile — end-to-end on real specs from MIGRATED_TOOL_SPECS
"""

from __future__ import annotations

from typing import Any

import pytest

from sreda.runtime.planner.plan_compiler import (
    PlanCompileError,
    can_run_parallel,
    compile,
)
from sreda.runtime.planner.schemas import Plan
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}


# ---------------------------------------------------------------------------
# Plan-factory helpers (DRY)
# ---------------------------------------------------------------------------


def _plan(
    actions: dict[str, dict[str, Any]],
    *,
    clarity: str = "clear",
    clarity_reason: str | None = None,
    compose: dict[str, Any] | None = None,
) -> Plan:
    """Build a Plan with a small default compose. Tests pass in just the
    `actions` dict where each value is the raw Action JSON."""
    if compose is None:
        compose = {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["литерал"]},
        }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test plan"},
        "clarity": clarity,
        "actions": actions,
        "compose": compose,
    }
    if clarity_reason is not None:
        payload["clarity_reason"] = clarity_reason
    return Plan.model_validate(payload)


def _action(
    tool: str,
    *,
    args: dict | None = None,
    outcomes: list[dict] | None = None,
    intent_group: str = "default",
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Build an Action JSON with minimal valid fields."""
    if args is None:
        args = {"items": [{"title": "литерал"}]} if tool == "add_shopping_items" else {}
    if outcomes is None:
        outcomes = _default_outcomes_for(tool)
    return {
        "tool": tool,
        "args": args,
        "expected_outcomes": outcomes,
        "intent_group": intent_group,
        "depends_on": depends_on or [],
    }


def _default_outcomes_for(tool: str) -> list[dict]:
    """Minimal expected_outcomes: one terminal branch with compose."""
    spec = REGISTRY[tool]
    # Pick first non-error status as the "default success" — pulls from
    # the output_model union's first member's Literal.
    from typing import get_args
    union_t = get_args(spec.output_model)[0] if get_args(spec.output_model) else spec.output_model
    for member in get_args(union_t):
        sf = member.model_fields.get("status")
        if sf is None:
            continue
        for lit in get_args(sf.annotation):
            if lit != "error":
                return [{
                    "match": {"status": lit},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "shopping_added_ok",
                        "template_data": {"items": ["литерал"]},
                    },
                }]
    return []


# ---------------------------------------------------------------------------
# can_run_parallel matrix
# ---------------------------------------------------------------------------


def test_can_run_parallel_two_pure_reads_ok() -> None:
    a = REGISTRY["list_shopping"]
    b = REGISTRY["list_reminders"]
    assert can_run_parallel(a, b)


def test_can_run_parallel_read_write_disjoint_domains_ok() -> None:
    """list_reminders (read=[reminders]) vs add_shopping_items
    (write=[shopping]) — disjoint domains, safe."""
    read = REGISTRY["list_reminders"]
    write = REGISTRY["add_shopping_items"]
    assert can_run_parallel(read, write)


def test_can_run_parallel_read_write_overlapping_domains_blocked() -> None:
    """list_shopping (read=[shopping]) vs add_shopping_items
    (write=[shopping]) — overlap blocks parallel."""
    read = REGISTRY["list_shopping"]
    write = REGISTRY["add_shopping_items"]
    assert not can_run_parallel(read, write)
    # Symmetric
    assert not can_run_parallel(write, read)


def test_can_run_parallel_two_writes_overlapping_domains_blocked() -> None:
    """Two add_shopping_items in same plan — same write domain →
    not parallel-safe by default (parallel_safe=False)."""
    write = REGISTRY["add_shopping_items"]
    assert not can_run_parallel(write, write)


def test_can_run_parallel_two_writes_disjoint_still_requires_parallel_safe() -> None:
    """schedule_reminder (write=[reminders]) vs add_shopping_items
    (write=[shopping]) — disjoint domains BUT default parallel_safe=False
    on both → still blocked. Conservative default."""
    a = REGISTRY["schedule_reminder"]
    b = REGISTRY["add_shopping_items"]
    # Both default parallel_safe=False
    assert not can_run_parallel(a, b)


# ---------------------------------------------------------------------------
# Topological sort via compile()
# ---------------------------------------------------------------------------


def test_compile_empty_actions_returns_empty_execution_plan() -> None:
    """Clarification-mode plan (no actions) compiles to empty layers."""
    plan = _plan(
        {},
        clarity="needs_clarification",
        clarity_reason="не указано время",
        compose={
            "kind": "template",
            "template_id": "ask_when_to_remind",
            "template_data": {"what": "что-то"},
        },
    )
    ep = compile(plan, REGISTRY)
    assert ep.layers == ()
    assert ep.fail_modes == {}
    assert ep.all_step_ids() == ()


def test_compile_single_action_one_layer() -> None:
    """One step → one layer with that step."""
    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile(plan, REGISTRY)
    assert len(ep.layers) == 1
    assert ep.layers[0] == ("s1",)


def test_compile_independent_actions_same_layer_when_parallel_safe() -> None:
    """Two pure reads with no ref deps → same layer (parallel)."""
    plan = _plan({
        "s1": _action("list_shopping"),
        "s2": _action("list_reminders"),
    })
    ep = compile(plan, REGISTRY)
    assert len(ep.layers) == 1
    assert set(ep.layers[0]) == {"s1", "s2"}


def test_compile_chained_actions_separate_layers() -> None:
    """s2 references ${s1.added_count} → s1 before s2 in different layers."""
    plan = _plan({
        "s1": _action("add_shopping_items"),
        "s2": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_list_show",
                    "template_data": {
                        # ref forces dependency on s1
                        "items": "${s1.item_ids}",
                    },
                },
            }],
        ),
    })
    ep = compile(plan, REGISTRY)
    # s1 must precede s2 because of ref
    flat = ep.all_step_ids()
    assert flat.index("s1") < flat.index("s2")


def test_compile_depends_on_creates_dependency() -> None:
    """Explicit depends_on adds dep edge even without ref."""
    plan = _plan({
        "s1": _action("list_shopping"),
        "s2": _action("list_reminders", depends_on=["s1"]),
    })
    ep = compile(plan, REGISTRY)
    flat = ep.all_step_ids()
    assert flat.index("s1") < flat.index("s2")
    # In separate layers (explicit dep)
    assert len(ep.layers) == 2


def test_compile_partitions_same_layer_with_write_conflict() -> None:
    """Two writes both targeting `shopping` domain land in different
    sub-layers (sequential), not one parallel batch."""
    plan = _plan({
        "s1": _action("add_shopping_items",
                      args={"items": [{"title": "молоко"}]},
                      intent_group="g1"),
        "s2": _action("add_shopping_items",
                      args={"items": [{"title": "хлеб"}]},
                      intent_group="g2"),
    })
    ep = compile(plan, REGISTRY)
    # Two writes to `shopping` → not parallel-safe → sequential layers
    assert len(ep.layers) == 2


# ---------------------------------------------------------------------------
# fail_mode inference (Group 2)
# ---------------------------------------------------------------------------


def test_fail_mode_read_only_group_is_partial() -> None:
    """Group with only read actions → partial (no committed mutations
    to worry about)."""
    plan = _plan({
        "s1": _action("list_shopping"),
        "s2": _action("list_reminders"),
    })
    ep = compile(plan, REGISTRY)
    assert ep.fail_modes == {"default": "partial"}


def test_fail_mode_single_intent_group_is_honest_partial() -> None:
    """One intent_group with a write → honest_partial (no other-group
    pair to qualify for partial)."""
    plan = _plan({
        "s1": _action("add_shopping_items"),
    })
    ep = compile(plan, REGISTRY)
    assert ep.fail_modes["default"] == "honest_partial"


def test_fail_mode_multi_intent_disjoint_domains_is_partial() -> None:
    """Two intent_groups with disjoint write_domains → partial for both.

    g1=shopping (add_shopping_items) + g2=reminders (schedule_reminder).
    Different groups, disjoint domains → both qualify."""
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g1"),
        "s2": _action("schedule_reminder",
                      args={"title": "купить хлеб",
                            "trigger_iso": "2026-12-31T18:00:00+00:00"},
                      intent_group="g2"),
    })
    ep = compile(plan, REGISTRY)
    assert ep.fail_modes["g1"] == "partial"
    assert ep.fail_modes["g2"] == "partial"


def test_fail_mode_overlapping_writes_within_group_is_honest_partial() -> None:
    """Same intent_group, two writes to SAME domain → honest_partial.
    Even with multiple groups, the within-group overlap forces stop."""
    plan = _plan({
        "s1": _action("add_shopping_items",
                      args={"items": [{"title": "a"}]},
                      intent_group="g1"),
        "s2": _action("add_shopping_items",
                      args={"items": [{"title": "b"}]},
                      intent_group="g1"),
        "s3": _action("schedule_reminder",
                      args={"title": "x",
                            "trigger_iso": "2026-12-31T18:00:00+00:00"},
                      intent_group="g2"),
    })
    ep = compile(plan, REGISTRY)
    assert ep.fail_modes["g1"] == "honest_partial"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_compile_rejects_unknown_tool() -> None:
    """If validate_plan didn't catch it (or wasn't run), compile fails
    fast with PlanCompileError rather than crashing in partitioning."""
    # Schema doesn't validate tool against registry — so this is allowed
    # at Plan-level, must be caught at compile.
    plan = _plan({"s1": _action("list_shopping")})
    bad_registry = {}  # empty registry → all tools "unknown"
    with pytest.raises(PlanCompileError, match="unknown tool"):
        compile(plan, bad_registry)


# ---------------------------------------------------------------------------
# Utility methods on ExecutionPlan
# ---------------------------------------------------------------------------


def test_execution_plan_fail_mode_for_resolves_via_intent_group() -> None:
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g1"),
        "s2": _action("schedule_reminder",
                      args={"title": "x",
                            "trigger_iso": "2026-12-31T18:00:00+00:00"},
                      intent_group="g2"),
    })
    ep = compile(plan, REGISTRY)
    assert ep.fail_mode_for("s1") in ("partial", "honest_partial")
    assert ep.fail_mode_for("s1") == ep.fail_modes["g1"]


def test_execution_plan_all_step_ids_flattens_layers_in_order() -> None:
    plan = _plan({
        "s1": _action("add_shopping_items"),
        "s2": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_list_show",
                    "template_data": {"items": "${s1.item_ids}"},
                },
            }],
        ),
    })
    ep = compile(plan, REGISTRY)
    flat = ep.all_step_ids()
    assert set(flat) == {"s1", "s2"}
    assert flat.index("s1") < flat.index("s2")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_can_run_parallel_overlapping_writes_blocked_even_with_parallel_safe() -> None:
    """Codex B.3 R1 CRITICAL: overlapping write_domains are NEVER
    parallel-safe, regardless of parallel_safe flag. Use synthetic
    specs (real registry has parallel_safe=False everywhere)."""
    from pydantic import BaseModel
    from typing import Annotated, Literal, Union
    from pydantic import Field
    from sreda.services.tool_schemas.base import ToolSpec

    class _DummyInput(BaseModel):
        x: int = 0

    class _DummyOk(BaseModel):
        status: Literal["ok"] = "ok"

    class _DummyError(BaseModel):
        status: Literal["error"] = "error"
        error_code: str = ""
        message: str = ""

    DummyOutput = Annotated[
        Union[_DummyOk, _DummyError],
        Field(discriminator="status"),
    ]

    safe_write_a = ToolSpec(
        name="fake_write_a",
        description="synthetic test spec",
        family="shopping",
        effect="write",
        read_domains=[],
        write_domains=["shopping"],
        parallel_safe=True,  # opted-in
        input_model=_DummyInput,
        output_model=DummyOutput,
        timeout_seconds=5,
        side_effect_class="transactional_write",
    )
    safe_write_b = ToolSpec(
        name="fake_write_b",
        description="synthetic test spec",
        family="shopping",
        effect="write",
        read_domains=[],
        write_domains=["shopping"],  # SAME domain
        parallel_safe=True,  # opted-in
        input_model=_DummyInput,
        output_model=DummyOutput,
        timeout_seconds=5,
        side_effect_class="transactional_write",
    )
    # Both parallel_safe=True AND same write_domain → still blocked
    assert not can_run_parallel(safe_write_a, safe_write_b)


def test_compile_respects_next_control_flow_dependency() -> None:
    """Codex B.3 R1 MAJOR (HIGH): expected_outcomes[].next is a real
    edge — if s1.next=s2, then s2 cannot run until s1 has decided.

    Two pure reads with no ref/depends_on, just next→ should land in
    DIFFERENT layers."""
    plan = _plan({
        "s1": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": "s2",  # control flow points at s2
                "compose": None,
            }, {
                "match": {"status": "empty"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_list_empty",
                    "template_data": {},
                },
            }],
        ),
        "s2": _action("list_reminders"),
    })
    ep = compile(plan, REGISTRY)
    # Two pure reads: without next edge they'd be parallel (same layer).
    # With next edge: s2 depends on s1 → different layers.
    assert len(ep.layers) == 2, (
        f"expected 2 layers (s1 → s2 via next), got {ep.layers}"
    )
    assert ep.layers[0] == ("s1",)
    assert ep.layers[1] == ("s2",)


def test_branch_compose_ref_is_not_an_ordering_dependency() -> None:
    """#28 (supersedes the earlier B.3 R1 "compose ref = ordering dep"): a
    branch compose ref is NOT an execution-ordering dependency. s1's
    terminal branch compose references ${s2.items}, but the Phase D
    composer renders it AFTER the whole execution completes (the executor
    only records selected_compose, never renders inline) — so s2 need NOT
    run before s1. Two independent pure reads stay parallel (one layer);
    the compose ref imposes no s2-before-s1 ordering. Mirrors the executor's
    _build_data_dep_graph, which already excludes compose-template refs."""
    plan = _plan({
        "s1": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_list_show",
                    "template_data": {"items": "${s2.items}"},
                },
            }],
        ),
        "s2": _action("list_reminders"),
    })
    ep = compile(plan, REGISTRY)
    layer_of = {sid: i for i, layer in enumerate(ep.layers) for sid in layer}
    assert layer_of["s1"] == layer_of["s2"], (
        f"expected s1 and s2 in the same parallel layer — a compose ref is "
        f"not an ordering dep (#28); got layers={ep.layers}"
    )


def test_validate_plan_catches_cycle_via_next_edges() -> None:
    """Codex B.3 R2 HIGH MAJOR: cycle introduced through ``next`` edges
    must surface as a validator violation (cycle_detected), not as a
    later compiler error. Both validator and compiler now use
    ``build_full_dep_graph`` so they see the same edges.

    s1.next=s2 + s2.next=s1 → cycle → validate_plan reports it."""
    from sreda.runtime.planner.validator import validate_plan

    plan = _plan({
        "s1": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": "s2",  # → s2
                "compose": None,
            }, {
                "match": {"status": "empty"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_list_empty",
                    "template_data": {},
                },
            }],
        ),
        "s2": _action(
            "list_reminders",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": "s1",  # → s1 (cycle!)
                "compose": None,
            }, {
                "match": {"status": "empty"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "reminders_list_empty",
                    "template_data": {},
                },
            }],
        ),
    })
    violations = validate_plan(plan, registry=REGISTRY)
    codes = [v.code for v in violations]
    assert any("cycle" in c for c in codes), (
        f"validator must report cycle through next-edges; got codes: {codes}"
    )


def test_mutual_branch_compose_refs_are_not_a_cycle() -> None:
    """#28 (supersedes the earlier B.3 R3 "mutual compose refs = cycle"):
    mutual branch compose refs are NOT an execution cycle. s1's terminal
    compose refs ${s2.items} and s2's refs ${s1.items}; both are
    independent pure reads that both run to completion before the Phase D
    compose phase, so there is no execution-ordering cycle — compose refs
    are post-execution render reads, not ordering deps. The validator must
    NOT report a cycle (doing so would reject a valid plan)."""
    from sreda.runtime.planner.validator import validate_plan

    plan = _plan({
        # Перекрёстные ссылки (суть теста о циклах) с РОДНЫМИ парами
        # «источник → шаблон» (страж #130: s1-ветка показывает данные s2
        # шаблоном напоминаний — родным для list_reminders, и наоборот).
        "s1": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "reminders_list_show",
                    "template_data": {"items": "${s2.items}"},
                },
            }],
        ),
        "s2": _action(
            "list_reminders",
            args={},
            outcomes=[{
                "match": {"status": "ok"},
                "next": None,
                "compose": {
                    "kind": "template",
                    "template_id": "shopping_list_show",
                    "template_data": {"items": "${s1.items}"},
                },
            }],
        ),
    })
    violations = validate_plan(plan, registry=REGISTRY)
    # Fully valid plan (#28): no cycle AND no other violations — both field
    # refs (${s1.items}/${s2.items}) exist on the ok output variant.
    assert not violations, (
        f"mutual branch-compose refs must NOT be a cycle and the plan must be "
        f"fully valid (#28); got: {[v.code for v in violations]}"
    )


def test_branch_compose_ref_to_conditional_step_is_not_a_cycle() -> None:
    """#28 + Codex high R1 follow-up: a terminal branch's compose may
    reference a step reachable only via a DIFFERENT branch's ``next``. The
    validator accepts it (no static cycle — compose refs are not ordering
    deps). s1 branch A (status=ok) → next s2; s1 branch B (status=empty) is
    terminal and its compose refs ${s2.items}.

    NOTE (known gap, tracked as a follow-up — branch-reachability-aware
    compose validation): if branch B fires at runtime, s2 is never enabled,
    so ${s2.items} is unavailable at compose time. That is a COMPOSE-TIME
    availability concern (Phase D / Phase B.6 TODO), NOT a static execution
    cycle — the old compose-ordering edge only caught it accidentally as a
    bogus cycle (and did not make s2 run either). #28 keeps the validator
    honest: no false cycle here."""
    from sreda.runtime.planner.validator import validate_plan

    plan = _plan({
        "s1": _action(
            "list_shopping",
            args={},
            outcomes=[
                {"match": {"status": "ok"}, "next": "s2", "compose": None},
                {
                    "match": {"status": "empty"},
                    "next": None,
                    "compose": {
                        "kind": "template",
                        "template_id": "shopping_list_show",
                        "template_data": {"items": "${s2.items}"},
                    },
                },
            ],
        ),
        "s2": _action("list_reminders"),
    })
    violations = validate_plan(plan, registry=REGISTRY)
    assert not any("cycle" in v.code for v in violations), (
        f"compose ref to a conditional step must not be a static cycle (#28); "
        f"got: {[v.code for v in violations]}"
    )


def test_compile_layers_are_deterministic_per_input() -> None:
    """Same input plan → identical ExecutionPlan layers (sorted within
    each layer). Important for snapshot tests + #68 trace correlation."""
    plan = _plan({
        "s2": _action("list_reminders"),
        "s1": _action("list_shopping"),
    })
    ep1 = compile(plan, REGISTRY)
    ep2 = compile(plan, REGISTRY)
    assert ep1.layers == ep2.layers
    # Sorted within layer
    assert ep1.layers[0] == ("s1", "s2")
