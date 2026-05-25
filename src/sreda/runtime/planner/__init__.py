"""Plan-execute planner runtime.

Sub-A1 (Epic #74): pydantic schemas + variable interpolation. The full
planner LLM client, validator, executor, and composer arrive in later
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

__all__ = [
    "Action",
    "ComposerCall",
    "InvalidReferenceError",
    "OutcomeBranch",
    "Plan",
    "TurnClassification",
    "resolve_refs",
]
