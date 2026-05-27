"""Pydantic schemas for plan-execute planner output (Sub-A1, Epic #74).

The planner LLM returns a JSON document conforming to ``Plan``. The
validator (Group 2 — Validator-driven parallelism) later derives the
execution graph (topological layers, fail_modes, scheduling) from the
declared actions; the planner itself does not need to spell out
parallelism or join nodes.

Strict pydantic v2 mode (``extra='forbid'``) is part of the contract —
silently accepting unknown fields would let planner drift go undetected
until it produced wrong behaviour at runtime.

Plan-level validation responsibilities live in ``Plan._validate_graph``:

- ``depends_on`` references must point at existing action ids
- ``depends_on`` must not be self-referential
- ``depends_on`` must form a DAG (no cycles)
- ``expected_outcomes[].next`` must point at existing action ids

Two operating modes for a Plan (vex-assistant#77 item #2 — clarity):

  ``clarity='clear'`` (default) — normal execution plan. ``actions``
    MUST be non-empty; the validator + executor run the DAG.
  ``clarity='needs_clarification'`` — planner caught ambiguity in the
    user request and wants to ask back. ``clarity_reason`` MUST be
    populated. ``actions`` MAY be empty (typical case — pure ask)
    or partially filled (mixed: do what's safe, ask about the rest).
    Executor renders ``compose`` directly without dispatching tools
    when ``actions`` is empty.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_STRICT = ConfigDict(extra="forbid", strict=False)
"""Project-wide pydantic config for planner schemas.

``extra='forbid'`` is the load-bearing invariant — see module docstring.
``strict=False`` keeps coercion of ints from JSON numbers etc. (pydantic's
default), only field-level type checks remain strict.
"""


class TurnClassification(BaseModel):
    """Whether the current user message starts a new conversation turn.

    Returned by the planner alongside the plan itself. ``is_new_turn``
    drives the Stage-2 transition in ``conversation_turns`` (open new or
    keep current). ``reason`` is logged for debugging and surfaces in
    ``planner_executions.turn_classification_reason``.
    """

    model_config = _STRICT

    is_new_turn: bool
    reason: str = Field(min_length=1, max_length=500)


class ComposerCall(BaseModel):
    """How to compose the user-facing reply.

    ``kind='template'`` renders a Jinja2 template from the composer
    registry (Group 6.5 — single source of truth). ``kind='llm'`` invokes
    a small LLM with no tools, using ``llm_prompt_key`` to pick the prompt
    template (used for narrative-heavy replies — recipes, multi-action
    summaries).
    """

    model_config = _STRICT

    kind: Literal["template", "llm"]
    template_id: str | None = None
    template_data: dict[str, Any] = Field(default_factory=dict)
    llm_prompt_key: str | None = None

    @model_validator(mode="after")
    def _validate_kind_consistency(self) -> ComposerCall:
        if self.kind == "template" and not self.template_id:
            raise ValueError(
                "ComposerCall(kind='template') requires non-empty template_id"
            )
        if self.kind == "llm" and not self.llm_prompt_key:
            raise ValueError(
                "ComposerCall(kind='llm') requires non-empty llm_prompt_key"
            )
        return self


class OutcomeBranch(BaseModel):
    """One conditional outcome for a tool call.

    ``match`` is an exact-equality dict against the tool's output fields
    (typically ``{"status": "added"}``). When the executor finds a branch
    matching the actual result, it either:

    - jumps to ``next`` action (continues execution), OR
    - runs ``compose`` and ends the branch (terminal), OR
    - falls through to the plan-level ``Plan.compose`` if both are None.

    Having ``next`` AND ``compose`` set is ambiguous and rejected.
    """

    model_config = _STRICT

    match: dict[str, Any]
    next: str | None = None
    compose: ComposerCall | None = None

    @model_validator(mode="after")
    def _validate_terminus(self) -> OutcomeBranch:
        if self.next is not None and self.compose is not None:
            raise ValueError(
                "OutcomeBranch cannot have both `next` and `compose` — pick "
                "one: continue execution (next) OR end with compose."
            )
        return self


class Action(BaseModel):
    """One step of a plan — a tool invocation with branches.

    ``intent_group`` (default ``"default"``) groups actions that should
    share fail-mode semantics. ``depends_on`` declares explicit UX
    ordering (the validator infers data dependencies from
    ``${node.field}`` references inside ``args`` separately).
    """

    model_config = _STRICT

    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    expected_outcomes: list[OutcomeBranch] = Field(min_length=1)
    intent_group: str = Field(default="default", min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """Top-level plan returned by the planner LLM.

    Final, validator-driven format (Group 2): just a dict of actions plus
    a final composer call. The validator infers topological layers, joins,
    and fail_modes from action metadata; the planner does not declare
    parallelism explicitly.

    ``clarity`` (vex-assistant#77 item #2) lets the planner proactively
    signal that it caught ambiguity in the user request and wants to ask
    back instead of guessing. ``clarity='needs_clarification'`` requires
    ``clarity_reason`` to be populated and allows ``actions`` to be empty
    (the typical "pure ask" case). See module docstring for the full
    operating-mode contract.
    """

    model_config = _STRICT

    schema_version: int = 1
    turn_classification: TurnClassification
    clarity: Literal["clear", "needs_clarification"] = "clear"
    clarity_reason: str | None = Field(default=None, max_length=500)
    # NOT ``min_length=1`` — clarity='needs_clarification' allows empty
    # actions (pure ask path). The clear-vs-needs_clarification consistency
    # check below enforces that ``clarity='clear'`` still requires at least
    # one action.
    actions: dict[str, Action] = Field(default_factory=dict)
    compose: ComposerCall

    @model_validator(mode="after")
    def _validate_clarity(self) -> Plan:
        """Enforce the clarity operating-mode contract:

        - ``clarity='clear'`` (default normal-plan mode) requires
          non-empty ``actions``. Empty actions with clear=clarity would
          mean "I have nothing to do AND I'm not asking" — a no-op
          plan that produces no user-visible work.
        - ``clarity='needs_clarification'`` requires a non-empty
          ``clarity_reason`` so the composer can surface WHY we're
          asking (template-rendered, no LLM call).
        """
        if self.clarity == "clear" and len(self.actions) == 0:
            raise ValueError(
                "Plan(clarity='clear') requires at least one action. "
                "If the planner has nothing to do, set "
                "clarity='needs_clarification' with a clarity_reason "
                "explaining what to ask the user."
            )
        if self.clarity == "needs_clarification":
            if self.clarity_reason is None or not self.clarity_reason.strip():
                raise ValueError(
                    "Plan(clarity='needs_clarification') requires a "
                    "non-empty clarity_reason explaining what's "
                    "ambiguous about the user request."
                )
        return self

    @model_validator(mode="after")
    def _validate_graph(self) -> Plan:
        action_ids = set(self.actions.keys())

        for action_id, action in self.actions.items():
            # depends_on integrity
            seen_deps: set[str] = set()
            for dep in action.depends_on:
                if dep == action_id:
                    raise ValueError(
                        f"Action '{action_id}' depends_on includes itself "
                        f"— remove the self-reference."
                    )
                if dep not in action_ids:
                    raise ValueError(
                        f"Action '{action_id}' depends_on references "
                        f"unknown action '{dep}'. Known actions: "
                        f"{sorted(action_ids)}"
                    )
                # No-silent-acceptance principle: duplicate entries are
                # never useful and break Kahn's in-degree math. Code-review
                # 2026-05-25 MAJOR #2.
                if dep in seen_deps:
                    raise ValueError(
                        f"Action '{action_id}' has duplicate entry in "
                        f"depends_on: '{dep}' (each dep must appear at most once)."
                    )
                seen_deps.add(dep)
            # branch.next integrity
            for branch in action.expected_outcomes:
                if branch.next is None:
                    continue
                if branch.next == action_id:
                    # Code-review 2026-05-25 MINOR #5 — branch jumping to
                    # its own action would deadloop in the executor.
                    raise ValueError(
                        f"Action '{action_id}' has an expected_outcomes "
                        f"branch with next='{branch.next}' pointing to "
                        f"itself — this would create an infinite loop."
                    )
                if branch.next not in action_ids:
                    raise ValueError(
                        f"Action '{action_id}' has expected_outcomes branch "
                        f"with next='{branch.next}' which is not a known "
                        f"action. Known actions: {sorted(action_ids)}"
                    )

        # Cycle detection over depends_on edges (a depends_on=[b] ⇒ b→a)
        cycle_nodes = _detect_cycle_in_depends_on(self.actions)
        if cycle_nodes:
            raise ValueError(
                f"depends_on graph has a cycle involving: "
                f"{sorted(cycle_nodes)}. Plans must be DAGs."
            )

        return self


def _detect_cycle_in_depends_on(actions: dict[str, Action]) -> set[str]:
    """Return the set of node ids involved in a depends_on cycle (or empty).

    Uses Kahn's algorithm — if topological sort leaves any node with
    non-zero in-degree, those nodes form (or feed into) a cycle.
    """
    # in_degree[node] = how many other actions this node depends on
    in_degree: dict[str, int] = {
        aid: sum(1 for d in a.depends_on if d in actions)
        for aid, a in actions.items()
    }
    # Reverse adjacency: dep -> list of nodes that depend on it
    dependents: dict[str, list[str]] = {aid: [] for aid in actions}
    for aid, action in actions.items():
        for dep in action.depends_on:
            if dep in dependents:
                dependents[dep].append(aid)

    # Start with nodes that have no dependencies (in_degree 0).
    # ``deque`` avoids the O(n) ``list.pop(0)`` per BFS step — irrelevant
    # for realistic <50-action plans but a free correctness win and
    # standard practice (code-review 2026-05-25 MINOR #4).
    queue: deque[str] = deque(
        aid for aid, deg in in_degree.items() if deg == 0
    )
    visited: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for dependent in dependents[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    return set(actions.keys()) - visited


__all__ = [
    "Action",
    "ComposerCall",
    "OutcomeBranch",
    "Plan",
    "TurnClassification",
]
