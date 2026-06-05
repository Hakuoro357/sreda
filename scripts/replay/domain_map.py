"""Frozen tool-to-domain-action adapter map for the offline replay harness (#87).

Invariant #4 (imprecise following) grades BOTH old (archived tool_calls) and
new (planned actions) at DOMAIN/OUTCOME level — NOT at planner tool-name level
(plan §4.4 / §8 R3 circularity fix).  This module is the single source of
truth for that projection.

Design decisions (flagged for orchestrator review):
---------------------------------------------------
1.  Domain-action vocabulary is a SHORT closed set: one value per tool.
    Format: ``"<family>:<verb>"`` using the Family taxonomy from
    ``services/tool_schemas/families.py``.  This avoids inventing new
    vocabulary; the family is already the planner-side grouping, and the
    verb captures the mutation direction.

2.  Read tools map to ``"<family>:read"`` (no side-effect claim; used only
    to verify that a "read" expected_outcome was satisfied).

3.  Tools with no domain-action equivalent (e.g. ``log_unsupported_request``)
    map to ``"utility:log"``.

4.  ``reply_with_buttons`` maps to ``"ui:present"`` — request-local write,
    no DB side effect.

JUDGMENT CALLS (review requested):
    - The exact verb for each family is fixed here as the minimal set
      needed for invariant #4.  A richer taxonomy (e.g. distinguishing
      ``memory:save_fact`` vs ``memory:save_episode``) is possible but not
      required by the plan — kept simple to avoid over-engineering.
    - Tools that do BOTH read and write (e.g. ``generate_shopping_from_menu``)
      are mapped to their PRIMARY WRITE action (``shopping:write``) since
      invariant #4 grades whether the user's expected write outcome was achieved.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping


# ---------------------------------------------------------------------------
# Domain-action vocabulary
# ---------------------------------------------------------------------------

# Format: "<family>:<verb>"
# Verbs: read | write | log | present | complete | cancel | delete | clear
# These match the outcome level the gold labels use.

TOOL_TO_DOMAIN_ACTION: Final[Mapping[str, str]] = MappingProxyType({
    # ---- shopping (7) ---------------------------------------------------
    "add_shopping_items":             "shopping:write",
    "mark_shopping_bought":           "shopping:write",
    "remove_shopping_items":          "shopping:write",
    "update_shopping_item":           "shopping:write",
    "update_shopping_items_category": "shopping:write",
    "list_shopping":                  "shopping:read",
    "clear_bought_shopping":          "shopping:clear",

    # ---- reminders (4) --------------------------------------------------
    "schedule_reminder":              "reminders:write",
    "list_reminders":                 "reminders:read",
    "update_reminder":                "reminders:write",
    "cancel_reminder":                "reminders:cancel",

    # ---- recipes (5) ----------------------------------------------------
    "save_recipe":                    "recipes:write",
    "save_recipes_batch":             "recipes:write",
    "search_recipes":                 "recipes:read",
    "get_recipe":                     "recipes:read",
    "delete_recipe":                  "recipes:delete",

    # ---- menu (5) -------------------------------------------------------
    "plan_week_menu":                 "menu:write",
    "update_menu_item":               "menu:write",
    "list_menu":                      "menu:read",
    "generate_shopping_from_menu":    "shopping:write",   # cross-family: primary write is shopping
    "clear_menu":                     "menu:clear",

    # ---- household (4) --------------------------------------------------
    "add_family_members":             "household:write",
    "list_family_members":            "household:read",
    "update_family_member":           "household:write",
    "remove_family_member":           "household:write",

    # ---- tasks (11) -----------------------------------------------------
    "add_task":                       "tasks:write",
    "list_tasks":                     "tasks:read",
    "update_task":                    "tasks:write",
    "complete_task":                  "tasks:complete",
    "uncomplete_task":                "tasks:write",
    "cancel_task":                    "tasks:cancel",
    "delete_task":                    "tasks:delete",
    "attach_reminder":                "tasks:write",
    "detach_reminder":                "tasks:write",
    "link_task_to_checklist":         "tasks:write",
    "unlink_task":                    "tasks:write",

    # ---- checklists (8) -------------------------------------------------
    "create_checklist":               "checklists:write",
    "add_checklist_items":            "checklists:write",
    "list_checklists":                "checklists:read",
    "show_checklist":                 "checklists:read",
    "move_task_to_checklist":         "checklists:write",
    "mark_checklist_item_done":       "checklists:complete",
    "delete_checklist_item":          "checklists:delete",
    "archive_checklist":              "checklists:write",

    # ---- onboarding (3) -------------------------------------------------
    "onboarding_answered":            "onboarding:write",
    "onboarding_deferred":            "onboarding:write",
    "onboarding_complete":            "onboarding:write",

    # ---- ui (1) ---------------------------------------------------------
    "reply_with_buttons":             "ui:present",

    # ---- memory (3) -----------------------------------------------------
    "save_core_fact":                 "memory:write",
    "save_episode":                   "memory:write",
    "recall_memory":                  "memory:read",

    # ---- utility (1) ----------------------------------------------------
    "log_unsupported_request":        "utility:log",

    # ---- web (3) --------------------------------------------------------
    "get_weather":                    "web:read",
    "web_search":                     "web:read",
    "fetch_url":                      "web:read",
})
"""Frozen map from every MVP tool name to its domain-action code.

Used by invariant #4 to project both old (archived) and new (planned)
tool calls into the same domain taxonomy before grading whether the
``required_outcomes`` / ``acceptable_action_sets`` were achieved.

A different-but-correct action path (e.g. old uses ``save_recipe`` +
``add_shopping_items``, new uses ``save_recipes_batch`` +
``generate_shopping_from_menu``) maps to the same domain-actions and is
NOT falsely marked wrong.
"""


def tool_to_domain_action(tool_name: str) -> str | None:
    """Return the domain-action code for ``tool_name``, or ``None`` if unknown.

    Callers should treat ``None`` as «unmapped tool — treat as unknown
    domain-action» (i.e. cannot grade invariant #4 for this step).
    """
    return TOOL_TO_DOMAIN_ACTION.get(tool_name)


def tools_to_domain_actions(tool_names: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Project a list of tool names to the set of domain-action codes.

    Unknown tool names are silently dropped (caller decides how to handle
    the missing coverage).
    """
    result: set[str] = set()
    for name in tool_names:
        da = TOOL_TO_DOMAIN_ACTION.get(name)
        if da is not None:
            result.add(da)
    return frozenset(result)


__all__ = [
    "TOOL_TO_DOMAIN_ACTION",
    "tool_to_domain_action",
    "tools_to_domain_actions",
]
