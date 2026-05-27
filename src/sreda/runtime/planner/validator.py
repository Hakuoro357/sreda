"""Plan-level argument validator (Sub-A-77 item #4).

Defense in depth for the planner LLM: after a plan parses against the
``Plan`` schema, this module walks every ``Action`` and checks that
``action.args`` would actually validate against the tool's
``ToolSpec.input_model``. If the planner forgot a required arg or
emitted a wrong-type value, we catch it BEFORE the executor tries to
invoke the tool — and the retry feedback to the planner can be
specific («schedule_reminder requires 'trigger_iso'; if you don't
know it from user, set clarity='needs_clarification'»).

Variable references (``${node.field.subfield}``) resolve at executor
time — they're skipped during validation. Mixed args (some concrete,
some refs) get partially validated: only the concrete keys are
checked, and «missing» errors on ref-filled fields are suppressed.

Design notes:

- The registry is **injected** rather than imported, so tests can supply
  ad-hoc ``ToolSpec`` fixtures without touching the global registry.
  Production code will pass a registry assembled from real ``ToolSpec``
  instances (Sub-A4 follow-up).

- Unknown tool names surface as plan-level errors so the planner can't
  invoke a fabricated tool — but the actual list of «known tools»
  lives in the registry, not here.

- Validation errors are returned as **list of strings** rather than
  raised, so the caller can decide whether to retry the planner,
  fallback to user, or aggregate errors across multiple Actions in
  one feedback round. ``InvalidPlanError`` is provided for callers
  that want exception-style flow.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ValidationError

from sreda.runtime.planner.interpolation import _REF_PATTERN
from sreda.runtime.planner.schemas import Action, Plan
from sreda.services.tool_schemas.base import ToolSpec


class InvalidPlanError(ValueError):
    """Raised when a plan fails argument validation.

    Carries the list of errors so callers can surface them all to the
    planner retry prompt at once — not just the first one. Use
    ``str(exc)`` for a human-readable summary, ``exc.errors`` for the
    structured list.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(
            f"Plan invalid: {len(errors)} arg violation(s):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


def _contains_ref(value: Any) -> bool:
    """Return True if ``value`` (or any nested str inside it) contains
    a ``${...}`` reference. Recursively walks dicts / lists / tuples.

    Treats anything that isn't a container as a leaf — only strings can
    embed refs. The interpolation engine in ``interpolation.py`` uses
    the same pattern.
    """

    if isinstance(value, str):
        return _REF_PATTERN.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_ref(item) for item in value)
    return False


def validate_action_args(action: Action, tool_spec: ToolSpec) -> list[str]:
    """Validate one Action's args against its tool's ``input_model``.

    Returns a list of human-readable error strings. Empty list means
    the args are valid (given the present concrete values; ref-filled
    values get checked at executor time after resolution).

    Strategy:
    1. Identify which keys hold variable refs. These keys are EXCLUDED
       from validation because their value isn't known yet — it
       resolves from ``state[step_id].field`` at executor time.
    2. Pass only concrete (non-ref) keys to ``input_model.model_validate``.
    3. If pydantic complains about a missing required field, suppress
       the error iff the original ``action.args`` had that field with
       a ref value — that's the «filled at runtime» case, not a real
       missing arg.
    4. Other validation errors (wrong type, value out of range, extra
       field if ``extra='forbid'``) propagate.

    Error format: ``<field_path>: <pydantic message>`` (one entry per
    pydantic error). Caller prepends the step id for the plan-level
    message.
    """

    input_model = tool_spec.input_model
    if input_model is None:  # pragma: no cover — ToolSpec field is required
        return []

    keys_with_refs = {k for k, v in action.args.items() if _contains_ref(v)}
    concrete = {k: v for k, v in action.args.items() if k not in keys_with_refs}

    try:
        input_model.model_validate(concrete)
        return []
    except ValidationError as exc:
        errors: list[str] = []
        for err in exc.errors():
            loc = err.get("loc") or ()
            err_type = err.get("type", "unknown")
            # Suppress «missing» errors when the slot is filled by a ref
            # (the planner did provide the field, just deferred).
            if err_type == "missing" and loc and loc[0] in keys_with_refs:
                continue
            path = ".".join(str(p) for p in loc) if loc else "<root>"
            msg = err.get("msg", err_type)
            errors.append(f"{path}: {msg}")
        return errors


def validate_plan_args(
    plan: Plan,
    registry: Mapping[str, ToolSpec],
) -> list[str]:
    """Walk every Action in ``plan.actions`` and aggregate validation
    errors against the corresponding ``ToolSpec`` in ``registry``.

    Empty list = plan ready to execute. Non-empty = planner retry with
    feedback string built from the errors.

    Plans with ``clarity='needs_clarification'`` and empty actions
    (the standard «ask user» shape) trivially return empty — there's
    nothing to validate.

    Unknown tool names (``action.tool`` not in registry) are reported
    as plan-level errors. The caller decides what to do — usually it
    means the planner emitted a hallucinated tool name and the retry
    feedback should remind it of the real registry.
    """

    errors: list[str] = []
    for step_id, action in plan.actions.items():
        spec = registry.get(action.tool)
        if spec is None:
            errors.append(
                f"{step_id}: unknown tool {action.tool!r} (not in registry)"
            )
            continue
        for err in validate_action_args(action, spec):
            errors.append(f"{step_id}: {err}")
    return errors


def validate_plan_or_raise(
    plan: Plan,
    registry: Mapping[str, ToolSpec],
) -> None:
    """Convenience wrapper that raises ``InvalidPlanError`` on the
    first error set. Use this in code paths that prefer
    exception-style flow over list-checking. For retry-feedback
    builders, prefer ``validate_plan_args`` so all errors surface in
    one round.
    """

    errors = validate_plan_args(plan, registry)
    if errors:
        raise InvalidPlanError(errors)


__all__ = [
    "InvalidPlanError",
    "validate_action_args",
    "validate_plan_args",
    "validate_plan_or_raise",
]
