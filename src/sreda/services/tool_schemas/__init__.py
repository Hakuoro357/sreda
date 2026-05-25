"""Tool registry & output schemas for plan-execute architecture.

Sub-A1 (Epic #74): base ``ToolSpec`` + ``ToolOutput`` + contract violation
sentinel. Per-tool schemas (Sub-A4) and the wrapper that turns legacy
``str`` outputs into typed dicts arrive in later sub-issues.
"""

from sreda.services.tool_schemas.base import (
    ToolOutput,
    ToolOutputContractViolation,
    ToolSpec,
)

__all__ = [
    "ToolOutput",
    "ToolOutputContractViolation",
    "ToolSpec",
]
