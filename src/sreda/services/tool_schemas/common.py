"""Shared constrained pydantic aliases for ToolSpec input models.

Sub-A4 / Codex R1 MAJOR #2 + alternative #2: rather than have every
``specs_<family>.py`` repeat ``Field(min_length=1)`` (which accepts
whitespace-only ``"   "`` strings and arbitrary IDs that violate
runtime contracts), centralise the strict aliases here.

All aliases use pydantic v2 ``StringConstraints``:
- ``strip_whitespace=True`` — input normalised, no edge-whitespace
  surprises in tool args
- ``min_length=1`` after strip — true non-blank
- pattern-based aliases enforce ID shapes (``sh_…``, ``rem_…``, etc.)
  so planner-emitted typos surface at validation time, not at
  executor lookup
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints


# ---------------------------------------------------------------------------
# Generic non-blank string. Use anywhere a tool input field accepts a
# free-form short label (titles, categories, free-form notes).
# ---------------------------------------------------------------------------


NonBlankStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
"""Stripped non-empty string. Rejects ``""`` and ``"   "``."""


ShortStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Stripped non-empty string capped at 200 chars — matches service-layer
title/category caps in the housewife domain."""


# ---------------------------------------------------------------------------
# ID shape constraints — prevent planner from emitting typo'd ids.
# Patterns match the prefixes ``housewife_*_service`` produces:
# ``sh_*`` (shopping items), ``rem_*`` (reminders), ``task_*`` (tasks),
# ``checklist_*`` (checklists), etc.
# ---------------------------------------------------------------------------


ShoppingItemId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=4,  # 'sh_' + at least 1 char
        max_length=64,
        pattern=r"^sh_\S+$",
    ),
]
"""Shopping item id — ``sh_<suffix>``. Suffix uses ``\\S+`` (non-blank
chars) — exact alphabet TBD by the service; this catches the obvious
mistakes (typos, empty id, whitespace)."""


ReminderId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=5,
        max_length=64,
        pattern=r"^rem_\S+$",
    ),
]
"""Reminder id — ``rem_<suffix>``."""


TaskId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=6,
        max_length=64,
        pattern=r"^task_\S+$",
    ),
]
"""Task id — ``task_<suffix>``."""


ChecklistId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=11,
        max_length=64,
        pattern=r"^checklist_\S+$",
    ),
]
"""Checklist id — ``checklist_<suffix>``."""


__all__ = [
    "ChecklistId",
    "NonBlankStr",
    "ReminderId",
    "ShoppingItemId",
    "ShortStr",
    "TaskId",
]
