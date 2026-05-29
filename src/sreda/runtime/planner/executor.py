"""Plan executor — Sub-A12 Phase C.

Pure async library function that drives an ``ExecutionPlan`` (compiled
by Phase B.3) through the actual tool registry. Returns a structured
``ExecutionLog`` for the orchestrator/composer to consume.

Library, not a LangGraph node — Phase E wraps this in a graph node when
end-to-end integration time comes. Keeping the lifecycle here pure-async
lets the snapshot/eval/unit tests cover the executor without touching
LangGraph.

What this owns
==============

1. **Branch-driven execution** (Codex Phase C R1 CRITICAL #1) — the
   compiler topo-sorts every reachable step into layers, but at runtime
   only the steps reachable via *matched* ``branch.next`` edges should
   actually fire. Steps that exist solely as fallback targets of
   un-matched branches get ``status='skipped'`` (reason
   ``branch_not_selected``). Roots (steps not referenced by any
   ``branch.next``) are enabled implicitly.

2. **Per-step contract checks** (Codex Phase C R1 CRITICAL #2/#3):
   - ``ToolSpec.validate_args_at_execute_time(resolved_args)`` runs
     after ref resolution; ``ValidationError`` aborts the plan with
     status ``arg_violation``.
   - ``ToolSpec.process_output(raw)`` runs instead of bare
     ``parse_tool_output``; ``PlannerGapError`` aborts the plan with
     status ``plan_gap``.

3. **Layer batching** — within one compiled layer, steps are split into
   sub-batches so that same-intent_group steps under ``honest_partial``
   run **sequentially** (Codex Phase C R1 MAJOR #5). Independent steps
   still parallel via ``asyncio.gather``.

4. **Tool invocation with timeout** — ``tool.ainvoke(args)`` (preferred)
   or ``asyncio.to_thread(tool.invoke, args)`` (sync fallback) wrapped
   in ``asyncio.wait_for`` using ``ToolSpec.timeout_seconds``.
   Sync tools cannot be cancelled (``to_thread`` ignores cancellation);
   ``status='timeout'`` is recorded but the underlying thread may
   continue and eventually commit. See Codex Phase C R1 MAJOR #7.

5. **fail_mode per intent_group** (Group 2 finalised):
   - ``partial``: on step failure, keep going through remaining steps
     (including other layers).
   - ``honest_partial``: on step failure of intent_group X, skip
     remaining X-steps; other intent_groups continue. Already-committed
     steps are NOT rolled back (per-step transactions; no long
     transaction wraps an intent_group).

6. **Execution log** — appends one ``StepResult`` per attempted step
   (success or failure). Final ``ExecutionLog.outcome`` summarises:
   ``completed`` / ``partial_failure`` / ``failed`` / ``aborted``.

What this does NOT own (deferred)
=================================

* Persistence integration with ``planner_executions.execution_log_json``
  — Phase E wires the orchestrator/executor handoff.
* LangGraph subgraph construction — Phase E.
* Recovery from crash mid-execution (Group 3.4 Option A+) — needs the
  durable checkpoint pattern from Sub-A6 PostgresSaver, addressed when
  the executor lands in a graph node.
* ``planner_gaps`` write on ``unknown_outcome`` / ``plan_gap`` —
  Phase E persistence wiring + Phase F GEPA training data path.
* Compose rendering — Phase D. The executor returns matched
  ``branch.compose`` overrides via ``StepResult.selected_compose`` for
  the composer to honour, but does not render them itself.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ValidationError

from sreda.runtime.planner.interpolation import (
    extract_step_id,
    iter_refs,
    resolve_refs,
)
from sreda.runtime.planner.plan_compiler import ExecutionPlan
from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
)
from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.executor_contract import PlannerGapError


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


StepStatus = Literal[
    "ok",
    "error",
    "timeout",
    "unknown_outcome",
    "plan_gap",          # contract violation from process_output()
    "arg_violation",     # execute-time arg validation failed
    "skipped",
]


@dataclass(frozen=True)
class StepResult:
    """One step's outcome — appended to execution log per attempt."""

    step_id: str
    tool: str
    status: StepStatus
    parsed_output: dict | None = None
    raw_output: str | None = None
    latency_ms: int = 0
    error_summary: str | None = None
    matched_branch_index: int | None = None
    matched_status: str | None = None
    # Codex Phase C R1 CRITICAL #1 — when a branch matches and the
    # branch has its OWN compose override (terminal branch), the
    # composer (Phase D) must pick that over the plan-level compose.
    # Executor surfaces it here without itself rendering anything.
    selected_compose: ComposerCall | None = None


@dataclass(frozen=True)
class ExecutionLog:
    """Aggregate of step results + final outcome.

    Outcome semantics:
    - ``completed`` — all attempted steps succeeded.
    - ``partial_failure`` — some success, some error/timeout (no abort).
    - ``failed`` — all attempted steps failed (no success, no abort).
    - ``aborted`` — fail-closed halt (unknown_outcome / plan_gap /
      arg_violation) BEFORE any successful step committed.
    - ``aborted_partial`` — same halt conditions as ``aborted`` BUT at
      least one step already succeeded (committed side effects). Phase D
      composer must reflect what was done; Phase E persistence stores
      this distinct from plain ``aborted`` (matches
      ``planner_executions.execution_status`` enum — see Group 6.3
      ``aborted_partial`` state). Codex Phase C R3-high MAJOR.
    """

    steps: tuple[StepResult, ...]
    outcome: Literal[
        "completed",
        "partial_failure",
        "failed",
        "aborted",
        "aborted_partial",
    ]

    def by_step_id(self) -> dict[str, StepResult]:
        return {s.step_id: s for s in self.steps}

    def successful_step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps if s.status == "ok")

    def terminal_compose(self) -> ComposerCall | None:
        """The first matched branch.compose override (in execution order),
        or None if no step terminated through a branch with its own
        compose. Phase D composer falls back to plan.compose when None.

        Note (Codex Phase C R1-high MAJOR #4): for multi-action plans
        where several steps match branches with their own compose, the
        "first" policy may pick a narrow template and omit other
        successful work. Phase D should use ``terminal_composes()``
        (plural) to apply its own resolution policy (e.g. deepest
        reachable terminal, or error/clarification priority)."""
        for step in self.steps:
            if step.status == "ok" and step.selected_compose is not None:
                return step.selected_compose
        return None

    def terminal_composes(self) -> tuple[tuple[str, ComposerCall], ...]:
        """All matched-branch compose overrides, in **compiled log
        order** (NOT real-time completion order).

        Returns a tuple of ``(step_id, ComposerCall)``. Phase D
        composer decides which to honour (or how to combine) — the
        executor does not pick.

        Codex Phase C R1-high MAJOR #4 + R2-high MINOR #3: ordering
        is the executor's append order, which inside a parallel batch
        is `asyncio.gather` input order (== compiled batch order),
        NOT real wall-clock completion order. Phase D MUST apply
        explicit policy (deepest terminal, error/clarification
        priority, intent-group salience) and not infer priority from
        tuple position."""
        return tuple(
            (step.step_id, step.selected_compose)
            for step in self.steps
            if step.status == "ok" and step.selected_compose is not None
        )


class ExecutorError(RuntimeError):
    """Raised on unrecoverable executor bug (tool name in plan not in
    tools_by_name, missing registry entry). Validator should have caught
    these — if we get here, the caller didn't validate the plan first.
    Phase E node will catch this and persist a diagnostic failed
    execution record."""


# ---------------------------------------------------------------------------
# Tool invocation (Codex Phase C R1 MINOR #10 — robust awaitable detection)
# ---------------------------------------------------------------------------


async def _ainvoke_tool(
    tool: Any, args: dict, *, timeout_seconds: float,
) -> str:
    """Call ``tool.ainvoke(args)`` (preferred) or sync ``invoke`` wrapped
    in ``asyncio.to_thread``, with a wall-clock timeout. Returns raw
    string output.

    Awaitable detection: call ``ainvoke`` and check if the *result* is
    awaitable, rather than relying on ``iscoroutinefunction`` of the
    bound method (which can misclassify decorated/wrapped LangChain
    methods).

    Sync timeout caveat (Codex Phase C R1 MAJOR #7): ``asyncio.to_thread``
    wraps a sync call in a thread; ``asyncio.wait_for`` cancels the
    awaitable but the underlying OS thread keeps running. A write tool
    that eventually commits after we record ``status='timeout'`` is the
    documented sync-timeout limitation. Phase E executor-in-node should
    log this with elevated severity so we can spot the case in audit.
    """
    if hasattr(tool, "ainvoke") and callable(tool.ainvoke):
        # Codex Phase C R1-high MINOR #5 — no try/except around the
        # call: a TypeError from a real ainvoke is a tool-side
        # contract bug, not a "method has wrong shape" signal, and
        # silently falling back to sync invoke could double-call the
        # tool after partial side effects. Let it propagate as
        # status='error'.
        call_result = tool.ainvoke(args)
        if inspect.isawaitable(call_result):
            return await asyncio.wait_for(call_result, timeout=timeout_seconds)
        # Some wrappers expose ``ainvoke`` but return the raw value
        # synchronously. Honour that — no thread needed.
        return call_result
    # Sync invoke — wrap in to_thread so the event loop survives long
    # tool calls. Note: cannot truly cancel a running sync call.
    coro = asyncio.to_thread(tool.invoke, args)
    return await asyncio.wait_for(coro, timeout=timeout_seconds)


# ---------------------------------------------------------------------------
# Branch matching (Codex Phase C R1 MAJOR #8 — catch-all enforced last)
# ---------------------------------------------------------------------------


def _output_as_dict(parsed: BaseModel | dict | None) -> dict:
    """Normalise parsed tool output to a plain dict for branch matching.

    Pydantic outputs come from ``ToolSpec.process_output``; a defensive
    fallback handles dict (tests) and None (skipped)."""
    if parsed is None:
        return {}
    if isinstance(parsed, BaseModel):
        return parsed.model_dump(mode="python")
    if isinstance(parsed, dict):
        return parsed
    return {}


def _match_branch(
    branches: list[OutcomeBranch], parsed_output: dict,
) -> tuple[int, OutcomeBranch] | None:
    """Find first branch whose ``match`` is satisfied by ``parsed_output``.

    ``match`` is a dict of ``{field: expected_value}`` — exact-match
    only (per Group 2 finalised simple format). Multiple keys = logical
    AND.

    Catch-all branches (empty ``match``) are only allowed as the LAST
    branch (Codex Phase C R1 MAJOR #8 defensive check). If a catch-all
    sits at a non-final index, this function ignores its catch-all
    behaviour and treats it as a strict (no-field) match against the
    output — i.e. it only fires when the output has zero fields, which
    is almost never the case. The validator should reject such plans
    up front; this is belt-and-braces.
    """
    last_idx = len(branches) - 1
    for idx, branch in enumerate(branches):
        if not branch.match:
            if idx == last_idx:
                return idx, branch
            # Misplaced catch-all — refuse to consume earlier branches'
            # outputs. Log for diagnostics; fall through.
            logger.warning(
                "executor: misplaced catch-all branch at index %d "
                "(must be last); ignoring catch-all semantics",
                idx,
            )
            continue
        if all(
            parsed_output.get(field_) == expected
            for field_, expected in branch.match.items()
        ):
            return idx, branch
    return None


# ---------------------------------------------------------------------------
# Branch-enable analysis (Codex Phase C R1 CRITICAL #1)
# ---------------------------------------------------------------------------


def _compute_enabled_initial(plan: Plan) -> set[str]:
    """Initial enabled set = roots of the ``branch.next`` DAG.

    A step is a *root* iff no other step's ``expected_outcomes[].next``
    points to it. Such steps execute unconditionally (subject to
    ref-deps + parallelism order from compiler). Non-root steps stay
    disabled until some completed predecessor's MATCHED branch enables
    them via ``branch.next``.
    """
    referenced_by_next: set[str] = set()
    for action in plan.actions.values():
        for branch in action.expected_outcomes:
            if branch.next is not None:
                referenced_by_next.add(branch.next)
    return set(plan.actions.keys()) - referenced_by_next


def _build_data_dep_graph(plan: Plan) -> dict[str, set[str]]:
    """Codex Phase C R1-high CRITICAL + R2-medium MAJOR #2 — execution
    deps only (NOT compose-template deps).

    Returns ``consumer → {producer_ids}`` covering edges that imply
    "consumer cannot RUN if producer didn't produce output":

    - ``${producer.field}`` refs inside ``action.args``
    - explicit ``action.depends_on`` entries

    **Excluded** (Codex R2-medium MAJOR #2):
    - ``expected_outcomes[].compose.template_data`` refs — these are
      PER-BRANCH and only matter if THAT branch ultimately matches.
      Pre-cascading on them would skip the host step before it could
      discover that a different branch (with no missing-ref compose)
      was the right one. Phase D resolves compose refs against the
      MATCHED branch's data at render time.
    - ``expected_outcomes[].next`` edges — CONTROL edges, already
      handled by the enable-set.

    Self-refs and edges to missing steps are dropped (validator should
    have caught those before us).
    """
    graph: dict[str, set[str]] = {sid: set() for sid in plan.actions}
    for sid, action in plan.actions.items():
        # args refs
        for ref_path in iter_refs(action.args):
            producer = extract_step_id(ref_path)
            if producer != sid and producer in plan.actions:
                graph[sid].add(producer)
        # depends_on
        for dep in action.depends_on:
            if dep != sid and dep in plan.actions:
                graph[sid].add(dep)
    return graph


# ---------------------------------------------------------------------------
# Execution state
# ---------------------------------------------------------------------------


@dataclass
class _MutableState:
    """Accumulates step outputs for ref resolution + tracks failures.

    Lives only inside ``execute_plan`` — never returned to caller."""

    step_outputs: dict[str, dict] = field(default_factory=dict)
    """Maps step_id → parsed_output_dict. ``${s1.field}`` refs read here."""

    failed_intent_groups: set[str] = field(default_factory=set)
    """Intent groups that hit a failure in honest_partial mode.
    Subsequent steps belonging to these groups are skipped."""

    enabled: set[str] = field(default_factory=set)
    """Steps enabled to execute (roots + branch.next targets of matched
    branches). Populated incrementally as the plan progresses."""

    skipped_step_ids: set[str] = field(default_factory=set)
    """Codex Phase C R1-high CRITICAL — steps that didn't run (branch
    not selected / upstream skipped / halted-after-abort). Used by the
    cascade-skip rule: a step whose data-dep predecessors are skipped
    cannot resolve refs, so it MUST also skip rather than fail with
    a misleading ref_resolve_error."""

    failed_step_ids: dict[str, str] = field(default_factory=dict)
    """Codex Phase C R2 CRITICAL — step_id → status ('error' or
    'timeout') for steps that ran but produced no output. Used together
    with ``skipped_step_ids`` to cascade-skip downstream consumers that
    would otherwise either hit ``arg_violation`` (refs) or run on a
    broken precondition (``depends_on``)."""

    results: list[StepResult] = field(default_factory=list)
    """Accumulated StepResults — what we'll wrap into ExecutionLog."""


# ---------------------------------------------------------------------------
# Per-step execution
# ---------------------------------------------------------------------------


async def _execute_one_step(
    *,
    step_id: str,
    action: Action,
    tool_spec: ToolSpec,
    tool: Any,
    step_outputs_snapshot: Mapping[str, dict],
    timeout_seconds: float,
) -> StepResult:
    """Run one Action: resolve refs → validate args → ainvoke → process_output
    → match branch.

    Pure: doesn't mutate any shared state. Caller decides whether to
    register the output in ``state.step_outputs`` (only on success).
    The ``step_outputs_snapshot`` is intentionally a Mapping (read-only
    view) — Codex Phase C R1 MINOR #9 wants per-layer immutability for
    refs so concurrent same-layer steps see a deterministic view."""
    # 1. Resolve refs in args
    try:
        resolved_args = resolve_refs(action.args, dict(step_outputs_snapshot))
    except Exception as exc:  # noqa: BLE001
        # Codex Phase C R1-high MAJOR #3: unresolved refs at execute
        # time are a planner/runtime contract failure, not a user-level
        # tool error. Reclassify as arg_violation so the plan aborts
        # instead of continuing with corrupted assumptions under
        # ``partial`` fail_mode.
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="arg_violation",
            error_summary=f"ref_resolve_error: {exc}",
        )

    # 2. Execute-time arg validation (Codex Phase C R1 CRITICAL #2)
    try:
        validated_args = tool_spec.validate_args_at_execute_time(resolved_args)
    except ValidationError as exc:
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="arg_violation",
            error_summary=f"execute_time_arg_violation: {exc.errors()}",
        )
    except Exception as exc:  # noqa: BLE001
        # Defensive — non-ValidationError surfacing from the validator
        # path is an internal bug, still must halt the plan.
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="arg_violation",
            error_summary=f"arg_validation_internal: {type(exc).__name__}: {exc}",
        )

    started = time.monotonic()

    # 3. Invoke tool with timeout
    try:
        raw_output = await _ainvoke_tool(
            tool, validated_args, timeout_seconds=timeout_seconds,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Codex Phase C R1 MAJOR #7 — for sync tools wrapped via
        # to_thread, the OS thread keeps running silently. Emit a
        # warning so audit can spot lingering writes. We classify
        # heuristically by whether the bound ``ainvoke`` is a coroutine
        # function; some wrappers will be mis-classified but the
        # warning is best-effort, not load-bearing.
        looks_sync = not (
            hasattr(tool, "ainvoke")
            and inspect.iscoroutinefunction(tool.ainvoke)
        )
        if looks_sync:
            logger.warning(
                "executor: SYNC tool %s timed out at step %s after %.1fs "
                "— underlying thread may still complete and commit",
                action.tool, step_id, timeout_seconds,
            )
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="timeout",
            latency_ms=elapsed_ms,
            error_summary=f"timeout after {timeout_seconds}s",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.exception(
            "executor: tool %s raised at step %s", action.tool, step_id,
        )
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="error",
            latency_ms=elapsed_ms,
            error_summary=f"tool_exception:{type(exc).__name__}: {exc}",
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # 4. Process output via ToolSpec contract (Codex Phase C R1 CRITICAL #3)
    try:
        parsed = tool_spec.process_output(raw_output)
    except PlannerGapError as exc:
        # Sentinel contract violation — planner_gaps candidate. Halt.
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="plan_gap",
            raw_output=raw_output,
            latency_ms=elapsed_ms,
            error_summary=f"contract_violation: {exc!s}",
        )
    except Exception as exc:  # noqa: BLE001
        # Parse/validation crashed unexpectedly → treat as plan_gap too;
        # we don't trust downstream branch matching against broken output.
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="plan_gap",
            raw_output=raw_output,
            latency_ms=elapsed_ms,
            error_summary=f"process_output_failure: {type(exc).__name__}: {exc}",
        )
    parsed_dict = _output_as_dict(parsed)

    # 5. Match to expected_outcomes branch
    match = _match_branch(action.expected_outcomes, parsed_dict)
    if match is None:
        return StepResult(
            step_id=step_id,
            tool=action.tool,
            status="unknown_outcome",
            parsed_output=parsed_dict,
            raw_output=raw_output,
            latency_ms=elapsed_ms,
            error_summary=(
                f"no expected_outcomes branch matched output "
                f"with status={parsed_dict.get('status')!r}"
            ),
        )

    branch_idx, matched_branch = match
    matched_status = matched_branch.match.get("status") if matched_branch.match else None

    return StepResult(
        step_id=step_id,
        tool=action.tool,
        status="ok",
        parsed_output=parsed_dict,
        raw_output=raw_output,
        latency_ms=elapsed_ms,
        matched_branch_index=branch_idx,
        matched_status=matched_status if isinstance(matched_status, str) else None,
        selected_compose=matched_branch.compose,
    )


# ---------------------------------------------------------------------------
# Layer batching (Codex Phase C R1 MAJOR #5)
# ---------------------------------------------------------------------------


def _build_layer_batches(
    layer_step_ids: list[str],
    plan: Plan,
    execution_plan: ExecutionPlan,
) -> list[list[str]]:
    """Split a compiled layer into sequential batches so that multiple
    steps from the same ``honest_partial`` intent_group never run
    concurrently.

    Rationale (Codex Phase C R1 MAJOR #5): under ``honest_partial``,
    failure of one step must skip the rest of that intent_group. If two
    same-group steps launch in parallel via gather and one fails, the
    other's commit has already happened — violating "stop the group".

    Algorithm: walk steps in compiled order. Track ``seen_groups``
    per-batch. If the current step's intent_group is honest_partial AND
    already represented in the current batch, start a new batch.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    seen_honest_groups: set[str] = set()

    for sid in layer_step_ids:
        action = plan.actions[sid]
        group = action.intent_group
        fail_mode = execution_plan.fail_modes.get(group, "honest_partial")

        if fail_mode == "honest_partial" and group in seen_honest_groups:
            # Flush current batch, start fresh for this step.
            batches.append(current)
            current = [sid]
            seen_honest_groups = {group}
            continue

        current.append(sid)
        if fail_mode == "honest_partial":
            seen_honest_groups.add(group)

    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


async def execute_plan(
    execution_plan: ExecutionPlan,
    tools_by_name: Mapping[str, Any],
    registry: Mapping[str, ToolSpec],
    *,
    timeout_seconds_default: float = 15.0,
) -> ExecutionLog:
    """Execute a compiled plan layer by layer (branch-driven).

    Parameters
    ----------
    execution_plan :
        Output of ``plan_compiler.compile``. ``layers`` is already
        topologically sorted + parallel-safe-partitioned (refs +
        depends_on + next-edges all considered in topo).
    tools_by_name :
        Maps ``tool.name`` → LangChain callable. Phase E passes the
        production registry; tests use mocks.
    registry :
        Maps ``ToolSpec.name`` → ToolSpec. Source of
        ``timeout_seconds``, ``validate_args_at_execute_time``,
        ``process_output``, etc.
    timeout_seconds_default :
        Fallback step timeout when ``ToolSpec.timeout_seconds`` is
        unset (shouldn't normally happen).

    Returns
    -------
    ExecutionLog :
        Per-step results + overall outcome
        (``completed`` / ``partial_failure`` / ``failed`` / ``aborted``).
    """
    plan = execution_plan.plan
    state = _MutableState(enabled=_compute_enabled_initial(plan))
    data_deps = _build_data_dep_graph(plan)

    if not execution_plan.layers:
        # Clarification-mode plan (empty actions) — nothing to execute.
        return ExecutionLog(steps=(), outcome="completed")

    aborted = False
    abort_reason: str | None = None

    def _record_skip(sid: str, reason: str) -> None:
        """Append a skip result AND track in skipped_step_ids so cascade
        can find it. Single helper so we never forget the set update."""
        state.results.append(_skip_result(sid, plan, reason))
        state.skipped_step_ids.add(sid)

    for layer in execution_plan.layers:
        if aborted:
            # Drain remaining layers as skipped.
            for sid in layer:
                _record_skip(sid, f"halted_{abort_reason}")
            continue

        # Partition: enabled-runnable vs not-yet-enabled (branch not
        # selected) vs skipped-because-group-failed vs upstream-skipped.
        runnable_ids: list[str] = []
        for sid in layer:
            if sid not in state.enabled:
                _record_skip(sid, "branch_not_selected")
                continue
            # Codex Phase C R1-high CRITICAL + R2 CRITICAL — cascade
            # skip if any data dep was skipped OR failed (error/timeout
            # produced no output). Skipped deps: planner contract gap
            # (branch_not_selected/halted). Failed deps under partial
            # fail_mode: tool ran but produced nothing; refs would
            # ref_resolve_error, depends_on contract is broken.
            unavailable_skipped = data_deps[sid] & state.skipped_step_ids
            unavailable_failed = data_deps[sid] & state.failed_step_ids.keys()
            if unavailable_skipped:
                _record_skip(
                    sid,
                    f"upstream_skipped:{','.join(sorted(unavailable_skipped))}",
                )
                continue
            if unavailable_failed:
                # Use the producer's status in the reason for diagnostics
                # (e.g. 'upstream_timeout:s1' vs 'upstream_failed:s1').
                reasons = sorted(
                    f"{state.failed_step_ids[d]}:{d}" for d in unavailable_failed
                )
                _record_skip(sid, f"upstream_unavailable:{','.join(reasons)}")
                continue
            action = plan.actions[sid]
            fail_mode = execution_plan.fail_modes.get(
                action.intent_group, "honest_partial",
            )
            if (
                fail_mode == "honest_partial"
                and action.intent_group in state.failed_intent_groups
            ):
                _record_skip(sid, "honest_partial_group_failed")
                continue
            runnable_ids.append(sid)

        if not runnable_ids:
            continue

        # Snapshot step_outputs for the layer (Codex Phase C R1 MINOR #9):
        # all concurrent steps in this layer resolve refs against an
        # immutable view; successful outputs merge AFTER the layer.
        snapshot = dict(state.step_outputs)
        per_layer_new_outputs: dict[str, dict] = {}

        # Build batches so same-group honest_partial steps run sequentially.
        batches = _build_layer_batches(runnable_ids, plan, execution_plan)

        for batch_idx, batch in enumerate(batches):
            # Re-filter batch in case a prior batch failed a honest_partial
            # group whose later steps appear in this batch.
            batch_filtered: list[str] = []
            for sid in batch:
                action = plan.actions[sid]
                fail_mode = execution_plan.fail_modes.get(
                    action.intent_group, "honest_partial",
                )
                if (
                    fail_mode == "honest_partial"
                    and action.intent_group in state.failed_intent_groups
                ):
                    _record_skip(sid, "honest_partial_group_failed")
                    continue
                batch_filtered.append(sid)
            if not batch_filtered:
                continue

            coros = []
            for sid in batch_filtered:
                action = plan.actions[sid]
                tool_spec = registry.get(action.tool)
                if tool_spec is None:
                    raise ExecutorError(
                        f"step {sid!r} references tool {action.tool!r} "
                        f"missing from registry — caller must validate_plan first"
                    )
                tool = tools_by_name.get(action.tool)
                if tool is None:
                    raise ExecutorError(
                        f"step {sid!r} tool {action.tool!r} not in tools_by_name"
                    )
                timeout = (
                    float(tool_spec.timeout_seconds)
                    if tool_spec.timeout_seconds
                    else timeout_seconds_default
                )
                coros.append(_execute_one_step(
                    step_id=sid,
                    action=action,
                    tool_spec=tool_spec,
                    tool=tool,
                    step_outputs_snapshot=snapshot,
                    timeout_seconds=timeout,
                ))

            batch_results = await asyncio.gather(*coros, return_exceptions=False)

            # Codex Phase C R2 MAJOR fix — process ALL gather'd results
            # before reacting to abort conditions. Skipping siblings here
            # would drop their already-committed side effects from the
            # execution log (audit/composer/persistence would see an
            # incomplete record).
            for sid, result in zip(batch_filtered, batch_results):
                state.results.append(result)
                action = plan.actions[sid]

                if result.status == "ok":
                    # Commit output for downstream layers (NOT visible
                    # to concurrent siblings in this layer's snapshot).
                    per_layer_new_outputs[sid] = result.parsed_output or {}
                    # Branch-driven: enable matched branch.next target.
                    if result.matched_branch_index is not None:
                        branch = action.expected_outcomes[result.matched_branch_index]
                        if branch.next is not None:
                            state.enabled.add(branch.next)
                    continue

                if result.status in ("unknown_outcome", "plan_gap", "arg_violation"):
                    # Record abort condition (first one wins) but keep
                    # appending the rest of the gather'd results so the
                    # audit log is complete. Outer loops use ``aborted``
                    # to skip subsequent batches/layers.
                    if not aborted:
                        aborted = True
                        abort_reason = result.status
                    continue

                # status in {error, timeout} — Codex Phase C R2 CRITICAL:
                # track for cascade so downstream consumers with refs
                # or depends_on cleanly skip instead of arg_violation.
                state.failed_step_ids[sid] = result.status

                # Codex Phase C R7-high CRITICAL — write-tool timeout
                # must escalate to abort. ``asyncio.to_thread`` cannot
                # cancel a running sync tool's thread; a write may
                # commit AFTER we record ``status='timeout'``. If we
                # leave aborted=False, outcome becomes 'failed' →
                # Phase E recovery treats as "nothing committed, safe
                # to retry" → potential double-write. Fail-closed:
                # escalate to abort so outcome becomes 'aborted_partial'
                # (write timeout is in POST_INVOKE_COMMITTED_STATUSES),
                # signalling recovery to verify state first. Read-tool
                # timeouts stay ordinary failures (no commit risk).
                if result.status == "timeout":
                    tool_spec = registry.get(action.tool)
                    is_write = tool_spec is None or tool_spec.effect == "write"
                    if is_write and not aborted:
                        aborted = True
                        abort_reason = "write_timeout"
                        continue  # skip the fail_mode bookkeeping; abort overrides

                fail_mode = execution_plan.fail_modes.get(
                    action.intent_group, "honest_partial",
                )
                if fail_mode == "honest_partial":
                    state.failed_intent_groups.add(action.intent_group)
                # partial → keep going (other groups continue; downstream
                # consumers cascade-skip via failed_step_ids set).

            if aborted:
                # Codex Phase C R1-high MAJOR #2: drain remaining
                # batches in this layer as halted skips so the audit
                # log shows them (R2 fix only drained later LAYERS).
                for later_batch in batches[batch_idx + 1:]:
                    for sid in later_batch:
                        _record_skip(sid, f"halted_{abort_reason}")
                break  # don't run more batches in this layer

        # Merge layer-new outputs into state for next layer.
        state.step_outputs.update(per_layer_new_outputs)

    outcome = _summarise_outcome(state.results, aborted, registry)
    return ExecutionLog(steps=tuple(state.results), outcome=outcome)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip_result(step_id: str, plan: Plan, reason: str) -> StepResult:
    """Build a 'skipped' StepResult for the execution log."""
    action = plan.actions.get(step_id)
    return StepResult(
        step_id=step_id,
        tool=action.tool if action else "<unknown>",
        status="skipped",
        error_summary=reason,
    )


def _summarise_outcome(
    results: list[StepResult],
    aborted: bool,
    registry: Mapping[str, ToolSpec],
) -> Literal[
    "completed", "partial_failure", "failed", "aborted", "aborted_partial",
]:
    """Roll up per-step results into a single outcome string.

    ``aborted`` overrides everything else (unknown_outcome / plan_gap /
    arg_violation in any step).

    Codex Phase C R3-high MAJOR: when the plan aborts AFTER at least
    one step succeeded, distinguish from plain ``aborted`` so Phase D
    composer + Phase E persistence reflect the committed side effects.

    Codex Phase C R4-high MAJOR refinement: ``aborted_partial`` must
    be **side-effect-aware**. A successful read-only step before abort
    didn't commit anything — should stay plain ``aborted``. Only an
    ``ok`` step whose ToolSpec.effect == "write" indicates a (potential)
    committed side effect; this is what Group 6.3 spec calls "abort
    after side effects". Tools missing from the registry (defensive)
    are treated as writes — fail-closed semantic.

    Codex Phase C R5-high MAJOR (KNOWN LIMITATION, deferred to
    Task #29): some write tools have idempotent no-op success paths
    (``link_task_to_checklist → already_linked``,
    ``add_shopping_items → empty`` when count=0). These return
    ``status='ok'`` but did not actually mutate DB; current heuristic
    over-approximates them as committed writes, surfacing
    ``aborted_partial`` when ``aborted`` would be more precise. Proper
    fix requires per-status mutation metadata on ToolSpec (e.g.
    ``committed_statuses: frozenset[str]``). Over-approximation is the
    safer failure direction (Phase E persistence + composer treat
    ``aborted_partial`` as "may have side effects" — they will read
    actual state before recovery, so a false positive only adds extra
    verification, not data corruption).

    Codex Phase C R6 A/B convergent MAJOR fix — closed false-negative
    where write tool committed (parsed status='added' with row ids)
    but planner only listed mismatched branch → status=unknown_outcome
    → previously classified as plain ``aborted``, losing the committed
    fact. Now POST_INVOKE_COMMITTED_STATUSES = {ok, unknown_outcome,
    plan_gap, timeout} all count as "may have committed" for write
    tools.

    Otherwise (non-aborted) classify by mix of success and failure
    across non-skipped steps.
    """
    # Codex Phase C R6 A/B convergent MAJOR fix — write tools can
    # commit DB mutations and STILL be classified as abort status.
    # Example: ``add_shopping_items`` returns parsed ``status='added'``
    # with new row ids; planner only expected ``'empty'`` → step status
    # becomes ``unknown_outcome``. Tool committed; we MUST surface
    # ``aborted_partial`` so recovery + persistence treat it as "may
    # have side effects".
    #
    # Set chosen to match both R6 A/B reviewers (medium + high agreed):
    # - ``ok``: matched a branch, output parsed cleanly — committed.
    # - ``unknown_outcome``: tool ran + output parsed cleanly, but no
    #   branch matched → committed (parsed status indicates write
    #   happened, planner missed the branch).
    # - ``plan_gap``: tool ran, output parser raised ``PlannerGapError``
    #   (contract violation) → may have committed before parse failed.
    # - ``timeout``: tool was cancelled mid-flight, may have committed
    #   before timeout fired.
    #
    # EXCLUDED:
    # - ``arg_violation``: pre-invoke (refs/args failed validation).
    # - ``skipped``: never ran.
    # - ``error``: tool raised exception. In Sreda's tool surface most
    #   write tools wrap mutations in SQLAlchemy transactions that roll
    #   back on exception → typically no commit. Including ``error``
    #   would over-classify common transactional failures.
    _POST_INVOKE_COMMITTED_STATUSES = frozenset({
        "ok", "unknown_outcome", "plan_gap", "timeout",
    })

    def _may_have_committed_write(r: StepResult) -> bool:
        if r.status not in _POST_INVOKE_COMMITTED_STATUSES:
            return False
        spec = registry.get(r.tool)
        if spec is None:
            # Unknown tool — fail-closed, assume it could have written.
            return True
        if spec.effect != "write":
            return False
        # Sub-A12 #29: for a cleanly-matched 'ok' step, consult the tool's
        # committed_statuses (when declared) to exclude idempotent no-op
        # success paths (add_shopping_items→empty, link_task_to_checklist→
        # already_linked, unlink_task→not_linked). Non-'ok' post-invoke
        # statuses (unknown_outcome/plan_gap/timeout) keep fail-closed
        # semantics — matched_status is unreliable there, and under-reporting
        # a real commit is the dangerous direction (would skip recovery
        # verification; cf. the R6 false-negative fix above).
        if r.status == "ok" and spec.committed_statuses is not None:
            # Use the tool's ACTUAL parsed output status — NOT matched_status.
            # matched_status comes from the matched branch's match["status"]
            # and is None when the step matched via a catch-all ({}) or a
            # non-status branch; keying on it would UNDER-report a real
            # mutation (Codex #29 R1 high MAJOR — the dangerous direction).
            # Fail closed (may have committed) if the parsed status is not a
            # readable string.
            parsed = r.parsed_output if isinstance(r.parsed_output, dict) else {}
            parsed_status = parsed.get("status")
            if not isinstance(parsed_status, str):
                return True
            return parsed_status in spec.committed_statuses
        return True

    has_committed_write = any(_may_have_committed_write(r) for r in results)
    if aborted:
        return "aborted_partial" if has_committed_write else "aborted"
    relevant = [r for r in results if r.status != "skipped"]
    has_failure = any(r.status in ("error", "timeout") for r in relevant)
    has_success = any(r.status == "ok" for r in relevant)
    if has_failure and has_success:
        return "partial_failure"
    if has_failure and not has_success:
        return "failed"
    return "completed"


__all__ = [
    "ExecutionLog",
    "ExecutorError",
    "StepResult",
    "StepStatus",
    "execute_plan",
]
