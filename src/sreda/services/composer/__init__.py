"""Composer registry — single source of truth for planner output templates.

Sub-A5 of Plan-Execute Epic (#74). The planner emits ``compose.template_id``
in its plan; the executor / validator look the id up here and render
with Jinja2. Per Group 6.5 of the architecture plan, this registry is
the ONLY allowed list of compose-side shapes:

- planner system-prompt enum is generated from ``REGISTRY.template_ids()``
- validator rejects plans whose ``template_id`` is not registered
- snapshot hash is recorded in ``planner_executions`` so a registry
  change between plan and compose is detected (Phase B race guard)

Adding a new template requires a registry entry — no silent fallback,
no inline strings, no "if id == 'x'" routing anywhere else in the
codebase.
"""

from sreda.services.composer.compose import (
    ComposeResult,
    ComposerContext,
    FallbackUsed,
    LLMComposer,
    compose,
)
from sreda.services.composer.registry import (
    REGISTRY,
    ComposerRegistry,
    UnknownTemplateError,
    render,
)

__all__ = [
    "ComposeResult",
    "ComposerContext",
    "ComposerRegistry",
    "FallbackUsed",
    "LLMComposer",
    "REGISTRY",
    "UnknownTemplateError",
    "compose",
    "render",
]
