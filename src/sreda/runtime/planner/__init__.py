"""Plan-execute planner runtime.

Sub-A1 (Epic #74): pydantic schemas + variable interpolation.
Sub-A-77 item #4: argument validator (defense-in-depth between planner
output and executor invocation).

The full planner LLM client, executor, and composer arrive in later
sub-issues.
"""

from sreda.runtime.planner.interpolation import (
    InvalidReferenceError,
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
    validate_action_args,
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
    "resolve_refs",
    "validate_action_args",
    "validate_plan_args",
    "validate_plan_or_raise",
]
