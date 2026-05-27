"""Tool registry & output schemas for plan-execute architecture.

Sub-A1 (Epic #74): base ``ToolSpec`` + ``ToolOutput`` + contract violation
sentinel. Per-tool schemas (Sub-A4) and the wrapper that turns legacy
``str`` outputs into typed dicts arrive in later sub-issues.

Sub-A-77 item #1: family taxonomy + anti-pattern headers + registry
text renderer for the planner prompt's cached prefix.
"""

from sreda.services.tool_schemas.base import (
    ToolOutput,
    ToolOutputContractViolation,
    ToolSpec,
)
from sreda.services.tool_schemas.executor_contract import (
    PlannerGapError,
    dispatch_typed_output,
)
from sreda.services.tool_schemas.plan_validator import (
    is_ref,
    validate_action_args,
)
from sreda.services.tool_schemas.families import (
    FAMILIES,
    FAMILY_HEADERS,
    Family,
    FamilyHeader,
)
from sreda.services.tool_schemas.housewife import (
    PARSERS,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    InvalidRegistryError,
    RegistryQualityViolation,
    validate_tool_registry_quality,
)
from sreda.services.tool_schemas.registry_text import (
    render_family_header,
    render_registry_for_planner,
)

__all__ = [
    "FAMILIES",
    "FAMILY_HEADERS",
    "Family",
    "FamilyHeader",
    "InvalidRegistryError",
    "PARSERS",
    "PlannerGapError",
    "RegistryQualityViolation",
    "ToolOutput",
    "ToolOutputContractViolation",
    "ToolSpec",
    "dispatch_typed_output",
    "is_ref",
    "parse_tool_output",
    "render_family_header",
    "render_registry_for_planner",
    "validate_action_args",
    "validate_tool_registry_quality",
]
