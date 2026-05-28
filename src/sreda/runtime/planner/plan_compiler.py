"""Plan → ExecutionPlan compiler — Sub-A12 Phase B.3.

Validated `Plan` (Sub-A1) → `ExecutionPlan` ready for the executor (Phase C).

What this module owns
=====================

The planner LLM emits a flat `Plan` with an `actions` dict and a `compose`
block. The executor needs more structure:

* **Topological layers** — which steps can run in parallel.
* **fail_mode per intent_group** — what executor does on first failure
  inside an intent_group: keep going (`partial`) or stop and report
  honestly (`honest_partial`, per Group 2 of the architecture plan).

`compile(plan, registry)` does both, returning a frozen `ExecutionPlan`.
Cycle detection / unknown-tool checks live in `validator.py` —
`compile()` assumes those passed (raises if dependency graph is
nontrivially malformed; caller should run `validate_plan()` first).

Why not in validator
====================

`validator.py` answers "is this plan structurally sound?" — yes/no.
`plan_compiler.py` answers "what's the execution shape?" — structured
output. Keeping these separate so the validator stays a pure check
function and the compiler stays a pure transformation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from sreda.runtime.planner.schemas import Action, Plan
from sreda.runtime.planner.validator import build_full_dep_graph
from sreda.services.tool_schemas.base import ToolSpec


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


FailMode = Literal["partial", "honest_partial"]


@dataclass(frozen=True)
class ExecutionPlan:
    """Compiled plan — executor (Phase C) consumes this.

    Attributes
    ----------
    layers :
        Topologically ordered. Each layer is a tuple of step_ids
        that may execute concurrently (the executor still owns scheduling;
        layers are the parallel-safe partitioning hint).
        Note: a single layer may contain steps with conflicting writes
        only if the compiler couldn't partition them further safely;
        callers should treat layers as "can run in parallel iff
        ``can_run_parallel(...)`` returns True for every pair", which
        is enforced at construction time.
    fail_modes :
        Maps each intent_group label to its fail_mode. ``"partial"`` =
        on first failure inside group, executor keeps running other
        steps of the group. ``"honest_partial"`` = stop the group on
        first failure but DO NOT roll back already-committed steps
        (per Group 2 spec — `honest_partial` not `strict`).
    plan :
        Reference to the source plan (saves callers re-passing it).
    """

    layers: tuple[tuple[str, ...], ...]
    fail_modes: Mapping[str, FailMode]
    plan: Plan

    def all_step_ids(self) -> tuple[str, ...]:
        """Convenience: flatten all step_ids in execution order."""
        return tuple(step for layer in self.layers for step in layer)

    def fail_mode_for(self, step_id: str) -> FailMode:
        """Resolve a single step's fail_mode via its action's intent_group."""
        action = self.plan.actions[step_id]
        return self.fail_modes[action.intent_group]


class PlanCompileError(ValueError):
    """Raised when the plan cannot be compiled — typically a cycle or
    unknown step/tool that slipped past `validate_plan()`."""


# ---------------------------------------------------------------------------
# Parallel-safety predicate (Group 2)
# ---------------------------------------------------------------------------


def can_run_parallel(spec_a: ToolSpec, spec_b: ToolSpec) -> bool:
    """Per Group 2 — domain-based parallel safety predicate.

    Codex Sub-A12 B.3 R1 CRITICAL fix: overlapping write_domains are
    NEVER parallel-safe, regardless of ``parallel_safe`` flag. The flag
    is opt-in for ambient-state safety on DISJOINT domains only — it
    can't conjure away a write/write race on the same domain.

    * Pure read/read → always safe.
    * Read/write → blocks if writer's write_domains overlaps reader's
      read_domains.
    * Write/write:
        - overlapping domains → ALWAYS blocked
        - disjoint domains → safe iff BOTH parallel_safe (defensive
          default for ambient-state coupling not visible to compiler)
    """
    if spec_a.effect == "read" and spec_b.effect == "read":
        return True

    if spec_a.effect != spec_b.effect:
        writer, reader = (
            (spec_a, spec_b) if spec_a.effect == "write" else (spec_b, spec_a)
        )
        return not (set(writer.write_domains) & set(reader.read_domains))

    # Both writes
    if set(spec_a.write_domains) & set(spec_b.write_domains):
        # Same domain — NEVER parallel-safe, even with parallel_safe=True.
        return False
    # Disjoint write domains — opt-in via parallel_safe on BOTH specs.
    return spec_a.parallel_safe and spec_b.parallel_safe


# ---------------------------------------------------------------------------
# Topological sort + parallel partitioning
# ---------------------------------------------------------------------------


def _topological_layers(
    dep_graph: dict[str, set[str]],
) -> list[list[str]]:
    """Kahn-style: each layer = steps with no remaining unmet deps.

    Returns layers in execution order. Empty input → empty output.
    Raises PlanCompileError if a cycle is detected (deps remain when
    no nodes are ready).
    """
    if not dep_graph:
        return []

    remaining_deps: dict[str, set[str]] = {
        node: set(deps) for node, deps in dep_graph.items()
    }
    layers: list[list[str]] = []
    placed: set[str] = set()

    while len(placed) < len(remaining_deps):
        # Sort step_ids in each layer for deterministic output —
        # tests and prompt-cache predictability.
        ready = sorted(
            node
            for node, deps in remaining_deps.items()
            if node not in placed and not (deps - placed)
        )
        if not ready:
            unplaced = sorted(set(remaining_deps) - placed)
            raise PlanCompileError(
                f"cycle or unsatisfiable dep in plan; unplaced steps: {unplaced}"
            )
        layers.append(ready)
        placed.update(ready)
    return layers


def _partition_layer_by_parallelism(
    layer: list[str],
    plan: Plan,
    registry: Mapping[str, ToolSpec],
) -> list[list[str]]:
    """Split one topological layer into sub-layers where every pair
    inside a sub-layer is parallel-safe.

    Greedy assignment — for each step, find the first existing sub-layer
    where it's compatible with all members; otherwise open a new one.
    """
    sub_layers: list[list[str]] = []
    for step_id in layer:
        action_spec = registry[plan.actions[step_id].tool]
        placed = False
        for sub in sub_layers:
            if all(
                can_run_parallel(action_spec, registry[plan.actions[s].tool])
                for s in sub
            ):
                sub.append(step_id)
                placed = True
                break
        if not placed:
            sub_layers.append([step_id])
    return sub_layers


# ---------------------------------------------------------------------------
# fail_mode inference (Group 2 final rule)
# ---------------------------------------------------------------------------


def _infer_fail_modes(
    plan: Plan, registry: Mapping[str, ToolSpec],
) -> dict[str, FailMode]:
    """For each intent_group, decide fail_mode.

    Per Group 2 finalised rule (architecture plan, after Codex hollistic
    CRITICAL #2 fix):
    - `partial` only when BOTH conditions hold across the group's write
      actions: no pairwise write_domain overlap AND there's at least one
      pair with DIFFERENT intent_groups outside the group (mixed-intent
      cases).
    - Otherwise `honest_partial` — stop the group on first failure but
      do NOT roll back already-committed steps (per-step commits are
      atomic; long transactions are out of scope).

    Read-only intent_groups → `partial` (no committed mutations to worry
    about).
    """
    actions_by_group: dict[str, list[Action]] = {}
    for action in plan.actions.values():
        actions_by_group.setdefault(action.intent_group, []).append(action)

    fail_modes: dict[str, FailMode] = {}
    other_intent_groups = set(actions_by_group.keys())
    for group, actions in actions_by_group.items():
        write_actions = [a for a in actions if registry[a.tool].effect == "write"]
        if not write_actions:
            fail_modes[group] = "partial"
            continue

        # Mixed-intent check — other intent groups present in plan?
        # (Single-group plan never qualifies for `partial`.)
        has_other_groups = (other_intent_groups - {group}) != set()
        no_domain_overlap = _all_pairs_disjoint_write_domains(
            write_actions, registry,
        )
        if no_domain_overlap and has_other_groups:
            fail_modes[group] = "partial"
        else:
            fail_modes[group] = "honest_partial"
    return fail_modes


def _all_pairs_disjoint_write_domains(
    write_actions: list[Action], registry: Mapping[str, ToolSpec],
) -> bool:
    """True iff no two write actions share any write_domain."""
    seen_domains: set[str] = set()
    for action in write_actions:
        spec = registry[action.tool]
        for domain in spec.write_domains:
            if domain in seen_domains:
                return False
            seen_domains.add(domain)
    return True


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def compile(
    plan: Plan,
    registry: Mapping[str, ToolSpec],
) -> ExecutionPlan:
    """Build ExecutionPlan from validated Plan.

    PRECONDITION: caller has run `validate_plan(plan, registry)` and got
    no violations. If unknown tools or cycles slip through, this raises
    PlanCompileError — but for hygiene the orchestrator (Phase B.4)
    should always validate first.

    Clarification-mode plans (clarity='needs_clarification' with empty
    actions) are returned with empty layers + empty fail_modes — the
    executor short-circuits to the compose step without any tool runs.
    """
    # Verify every tool is in the registry — fail-fast over later
    # KeyError surface during partitioning.
    for step_id, action in plan.actions.items():
        if action.tool not in registry:
            raise PlanCompileError(
                f"step {step_id!r} references unknown tool {action.tool!r} — "
                f"caller forgot to run validate_plan() first"
            )

    if not plan.actions:
        # Pure clarification — no execution layer needed.
        return ExecutionPlan(layers=(), fail_modes={}, plan=plan)

    dep_graph = build_full_dep_graph(plan)
    topo = _topological_layers(dep_graph)

    layers: list[tuple[str, ...]] = []
    for layer in topo:
        for sub_layer in _partition_layer_by_parallelism(layer, plan, registry):
            layers.append(tuple(sub_layer))

    fail_modes = _infer_fail_modes(plan, registry)
    return ExecutionPlan(
        layers=tuple(layers),
        fail_modes=fail_modes,
        plan=plan,
    )


__all__ = [
    "ExecutionPlan",
    "FailMode",
    "PlanCompileError",
    "can_run_parallel",
    "compile",
]
