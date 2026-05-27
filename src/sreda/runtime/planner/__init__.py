"""Plan-execute planner runtime.

Sub-A1 (Epic #74): pydantic schemas + variable interpolation.
Sub-A-77 item #4: argument validator (defense-in-depth between planner
output and executor invocation).

The full planner LLM client, executor, and composer arrive in later
sub-issues.
"""

from sreda.runtime.planner.interpolation import (
    InvalidReferenceError,
    contains_ref,
    extract_step_id,
    is_full_ref_string,
    iter_refs,
    resolve_refs,
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
    validate_action_args,
    validate_plan,
    validate_plan_args,
    validate_plan_or_raise,
)

__all__ = [
    "Action",
    "ComposerCall",
    "InvalidPlanError",
    "InvalidReferenceError",
    "OutcomeBranch",
    "Plan",
    "TurnClassification",
    "Violation",
    "contains_ref",
    "extract_step_id",
    "is_full_ref_string",
    "iter_refs",
    "render_violations",
    "resolve_refs",
    "validate_action_args",
    "validate_plan",
    "validate_plan_args",
    "validate_plan_or_raise",
]
