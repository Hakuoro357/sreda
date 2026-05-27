"""Plan-time argument validation against ToolSpec contracts.

This module is the planner-side counterpart to ``executor_contract.py``:
it owns the **static validation** of a tool call's args BEFORE refs
are resolved, so the planner can reject malformed plans without ever
running them.

Codex Sub-A4 R5 MAJOR #1: the R4 helpers (``ToolSpec.required_any_non_null_args``
+ ``ToolSpec.validate_args_satisfy_required_any``) exist on the spec
but had no production caller. Phase B planner code MUST go through
``validate_action_args(spec, raw_args)`` instead of reimplementing
the rules — that way the boundary tests pin runtime behavior, not
just unit-tested helper semantics.

Two-phase split:

- **Plan time** (this module): refs are still ``${...}`` strings.
  We can only check structural rules: presence of required keys,
  the «at least one mutable arg» no-op guard, extra-args rejection
  (via ``model_validate`` on the literal-only subset).
- **Execute time** (``ToolSpec.validate_args_at_execute_time``):
  after refs resolve, we run full ``input_model.model_validate`` to
  catch the «ref-resolved-to-None» deferred no-op (Codex R5 MAJOR #3).

Phase B's planner validator imports ``validate_action_args``. Phase
B's executor calls ``spec.validate_args_at_execute_time(resolved)``
right before invoking the legacy tool. Both code paths share the
same ToolSpec contracts.
"""

from __future__ import annotations

import re
from typing import Any

from sreda.services.tool_schemas.base import ToolSpec


_REF_PATTERN = re.compile(r"^\$\{[^}]+\}$")
"""A plan ref of the form ``${node_id.path.to.field}``. The planner
emits these as opaque placeholder strings; the executor resolves
them via the variable interpolator before invoking the tool."""


def is_ref(value: Any) -> bool:
    """Return True if ``value`` is a plan-time reference placeholder
    (``${...}``). Used by ``validate_action_args`` to decide whether
    to defer per-field constraint checking to execute time."""
    return isinstance(value, str) and bool(_REF_PATTERN.match(value))


def validate_action_args(spec: ToolSpec, raw_args: dict[str, Any]) -> None:
    """Plan-time validation of a single tool call's args.

    Codex Sub-A4 R5 MAJOR #1: this is the canonical entry point the
    planner uses to validate ``action.args`` against the spec. The
    function is intentionally small — its job is to **call the rules
    defined on ToolSpec** so contract changes land in one place.

    Steps:

    1. ``spec.validate_args_satisfy_required_any(raw_args)`` —
       rejects the silent no-op pattern even when refs are present
       (Codex R4 MAJOR #1). For tools without
       ``required_any_non_null_args`` set, this is a no-op.
    2. If any value in ``raw_args`` is a ref placeholder
       (``${...}``), DEFER full input_model validation to execute
       time. Otherwise run ``input_model.model_validate(raw_args)``
       immediately for the «all literals» fast path.

    Raises ``ValueError`` (from ToolSpec helpers) or
    ``pydantic.ValidationError`` (from input_model) — caller maps
    both into the planner's ``invalid_plan`` retry path (Group 4.4).
    """
    # STEP 1 — refs-aware no-op guard (works on the raw args even
    # before refs resolve).
    spec.validate_args_satisfy_required_any(raw_args)

    # STEP 2 — if any value is a ref placeholder, full input_model
    # validation can't run (model_validator on a string "${...}"
    # would reject the wrong shape). Defer to execute time.
    if any(is_ref(v) for v in raw_args.values()):
        return

    # All literals — run full plan-time validation. This catches
    # extra keys, wrong types, field validators, model validator
    # (e.g. the «at least one mutable field» rule on
    # UpdateShoppingItemInput).
    spec.input_model.model_validate(raw_args)


__all__ = [
    "is_ref",
    "validate_action_args",
]
