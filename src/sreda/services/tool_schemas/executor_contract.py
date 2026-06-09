"""Executor boundary contract for typed tool outputs.

This module pins the ordering the production executor (Phase B) MUST
follow when processing a raw tool ``str`` output through the typed
registry. Codex Sub-A4 R2/R3 MAJOR #5 — the prior boundary check only
proved «sentinel is not in any output_model union»; it did NOT prove
that the executor catches the sentinel BEFORE running output_model
validation. A future wrapper could silently swap the order and
``ToolOutputContractViolation`` would surface as a generic Pydantic
``ValidationError`` instead of the intended fail-closed
``planner_gaps(gap_type='contract_violation')`` path.

This module provides:

- ``PlannerGapError`` — raised when a tool emits unparseable raw
  output (sentinel detected). The Phase B executor catches this and
  records a ``planner_gaps`` row; the plan halts and the user gets a
  «my logic broke, try again» fallback.

- ``dispatch_typed_output(tool_name, raw, output_model)`` — the
  reference implementation of the boundary contract. Phase B's
  wrapper must mirror this ordering. Tests against THIS function
  (``tests/unit/test_executor_contract.py``) lock the order so future
  refactors don't accidentally swap it.

Boundary contract (in order, do not reorder):

  1. Parse the raw ``str`` via ``PARSERS[tool_name]``. Unknown raw →
     parser returns ``ToolOutputContractViolation``.
  2. If parser returned ``ToolOutputContractViolation`` →
     ``raise PlannerGapError(sentinel)``. The executor catches this,
     writes ``planner_gaps``, alerts the admin, and halts the plan.
     Do NOT attempt output_model validation on the sentinel — by
     design it does not satisfy any tool's output_model union.
  3. Validate the parser-typed output against the spec's
     ``output_model`` via ``TypeAdapter`` — defensive layer in case
     the parser drifts from the spec (e.g. new variant added to the
     parser but not yet wired into the union).
  4. Return the validated instance for the planner to branch on.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import parse_tool_output


class PlannerGapError(Exception):
    """Raised when a tool's raw output is unparseable (sentinel path).

    The Phase B executor catches this exception specifically and:
    1. Writes ``planner_gaps(gap_type='contract_violation')`` with
       ``self.sentinel.raw_output`` + ``self.sentinel.tool_name``.
    2. Alerts the admin (P1) so GEPA can be retrained.
    3. Halts the plan; composer renders the «unknown_outcome» template.

    The exception carries the original sentinel so the executor can
    serialise the raw output for ``planner_gaps`` without re-running
    the parser.
    """

    def __init__(self, sentinel: ToolOutputContractViolation) -> None:
        self.sentinel = sentinel
        super().__init__(
            f"contract_violation: tool={sentinel.tool_name} "
            f"raw_output={sentinel.raw_output!r}"
        )


def dispatch_typed_output(
    tool_name: str,
    raw: str,
    output_model: Any,
) -> Any:
    """Reference executor-boundary handling for a typed tool output.

    Implements the four-step contract documented at the module top.
    Phase B's wrapper MUST mirror this ordering — tests in
    ``test_executor_contract.py`` exercise both the sentinel and
    happy paths against this function so the contract is locked.

    Parameters
    ----------
    tool_name:
        Name registered in ``PARSERS`` (e.g. ``"update_shopping_item"``).
    raw:
        The raw ``str`` output produced by the legacy tool function.
    output_model:
        The ``ToolSpec.output_model`` (discriminator union) for the
        named tool. Caller passes it explicitly so this module stays
        decoupled from the ``ToolSpec`` registry.

    Raises
    ------
    PlannerGapError:
        Raw output couldn't be parsed (sentinel). Executor catches
        and records as a planner gap.
    ValidationError:
        Parser produced a typed result that doesn't satisfy the
        spec's ``output_model`` (parser-vs-spec drift). Defensive
        check — should never fire in production but catches regressions
        in test.

    Returns
    -------
    The parser-typed output, after defensive output_model
    revalidation. Same shape the planner will branch on.
    """
    parsed = parse_tool_output(tool_name, raw)
    # STEP 2 — sentinel check MUST come BEFORE output_model validation.
    # If we let the sentinel reach TypeAdapter, it raises a generic
    # ValidationError and the executor records the wrong gap_type.
    if isinstance(parsed, ToolOutputContractViolation):
        raise PlannerGapError(parsed)
    # STEP 3 — defensive output_model revalidation.
    # #115: exclude @computed_field values (e.g. ``display_summary``) from the
    # round-trip dump. Computed fields are output-only — feeding them back as
    # INPUT to a variant with ``extra='forbid'`` raises ``extra_forbidden``.
    # The re-validated model recomputes them, so the returned instance (and the
    # executor's ``model_dump`` for the presenter) still carries them.
    computed = set(getattr(type(parsed), "model_computed_fields", {}))
    dumped = parsed.model_dump(exclude=computed) if computed else parsed.model_dump()
    return TypeAdapter(output_model).validate_python(dumped)


__all__ = [
    "PlannerGapError",
    "dispatch_typed_output",
]
