"""Central ToolSpec aggregator for the housewife planner registry
(Sub-A4 — Plan-Execute Epic vex-assistant#74).

This module imports every per-family ``specs_<family>.py`` and exposes:

- ``MIGRATED_TOOL_SPECS`` — list of all ToolSpec instances migrated so
  far. Grows as Sub-A4 lands one family per PR. Used by the CI
  acceptance gate, by the planner system prompt builder (Sub-B1),
  and by anyone who needs «every typed tool we know about right now».

- ``ALL_TOOL_SPECS`` — read-only view (tuple) of every migrated
  ToolSpec. Codex Sub-A4 reminders R1 MINOR #10: previously a
  mutable list alias which let callers accidentally mutate either
  name. Tuple makes the «forward compatibility» contract
  enforceable. Final count (post-migration) is **55 tools** (47
  housewife + 4 memory + 3 web + 1 utility). The future
  ``get_recipe_any_source`` (architecture-map TODO-2) will bump
  this to 56 when the runtime function ships.

**ToolOutputContractViolation contract** (Codex Sub-A4 R1 MAJOR #6):

Parsers in ``housewife.py`` can return ``ToolOutputContractViolation``
when a tool's raw ``str`` output matches none of the registered
patterns for that tool. This sentinel is **intentionally not** in any
``ToolSpec.output_model`` discriminator union. Reason:

- ``output_model`` represents «expected outcomes the planner may
  branch on» — the universe of statuses the LLM was trained against.
- ``ToolOutputContractViolation`` is the «we don't know how to
  interpret this raw output» escape hatch. The executor catches it
  BEFORE running ``output_model`` validation, halts the plan, writes
  a ``planner_gaps`` record with ``gap_type='contract_violation'``,
  and alerts the admin.
- If a violation ever needs to round-trip through the discriminated
  union (e.g. for GEPA training examples), wrap it in an outer envelope
  rather than polluting every tool's union with the sentinel.
"""

from __future__ import annotations

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.specs_household import HOUSEHOLD_SPECS
from sreda.services.tool_schemas.specs_menu import MENU_SPECS
from sreda.services.tool_schemas.specs_recipes import RECIPES_SPECS
from sreda.services.tool_schemas.specs_reminders import REMINDERS_SPECS
from sreda.services.tool_schemas.specs_shopping import SHOPPING_SPECS


MIGRATED_TOOL_SPECS: list[ToolSpec] = [
    *SHOPPING_SPECS,
    *REMINDERS_SPECS,
    *RECIPES_SPECS,
    *MENU_SPECS,
    *HOUSEHOLD_SPECS,
    # Sub-A4 phases 6-7 will add:
    # *TASKS_SPECS,
    # *CHECKLISTS_SPECS,
    # *ONBOARDING_SPECS,
    # *UI_SPECS,
    # *MEMORY_SPECS,
    # *UTILITY_SPECS,
    # *WEB_SPECS,
]
"""Every ToolSpec migrated to the typed registry so far. Initial set
is the 7-tool shopping family; expand as each subsequent family ships."""


ALL_TOOL_SPECS: tuple[ToolSpec, ...] = tuple(MIGRATED_TOOL_SPECS)
"""Read-only view of every migrated ToolSpec. Currently equals
``MIGRATED_TOOL_SPECS`` (as a tuple). Will diverge once incremental
Sub-A4 work creates a distinction between «known to the planner»
and «known to the migration plan». Final count post-migration is
**55 tools** (47 housewife + 4 memory + 3 web + 1 utility), with the
future ``get_recipe_any_source`` (architecture-map TODO-2) bumping
this to 56 when the runtime function ships.

Codex Sub-A4 reminders R1 MINOR #10: was ``list[ToolSpec]`` aliasing
``MIGRATED_TOOL_SPECS`` directly — any mutation on either name would
silently mutate both. ``tuple`` makes the read-only intent
enforceable at the type level."""


__all__ = [
    "ALL_TOOL_SPECS",
    "MIGRATED_TOOL_SPECS",
]
