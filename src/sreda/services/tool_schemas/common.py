"""Shared constrained pydantic aliases for ToolSpec input/output models.

Sub-A4 / Codex R1 MAJOR #2 + alternative #2 + Codex R2 MAJOR #1 / #2:
rather than have every ``specs_<family>.py`` repeat ``Field(min_length=1)``
(which accepts whitespace-only ``"   "`` strings and arbitrary IDs that
violate runtime contracts), centralise strict aliases here.

All aliases use pydantic v2 ``StringConstraints``:
- ``strip_whitespace=True`` — input normalised, no edge-whitespace
  surprises in tool args
- ``min_length`` reflects the runtime contract (NOT a guess — see
  service-layer code for the source of truth)
- ID patterns are exact matches against ``uuid4().hex[:24]`` shapes
  produced by ``housewife_*_service`` factories — typos surface at
  validation time, not at executor lookup
- String caps match runtime truncation points so the planner cannot
  send a value that runtime silently truncates (data loss) and
  cannot reject a value the runtime would accept (false-negative
  validation)

Sources of truth for the exact caps and shapes:
- ``services/housewife_shopping.py`` lines 96 (category[:64]),
  252-253 (title[:500], quantity_text[:64]), 291 (id=f"sh_{hex[:24]}").
- Other families pull caps from their own service modules — add
  per-family aliases below as those families migrate.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints


# ---------------------------------------------------------------------------
# Generic shared utilities — non-domain-specific
# ---------------------------------------------------------------------------


NonBlankStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
"""Stripped non-empty string. Rejects ``""`` and ``"   "``. Use for
free-form short labels where the runtime has no specific cap (rare in
the housewife domain — prefer the domain-specific aliases below)."""


ShortStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Stripped non-empty string capped at 200 chars. Generic medium-label
alias for future families whose service-layer cap is 200. Shopping
fields have their own aliases below (titles=500, qty/cat=64) — do NOT
use ``ShortStr`` for shopping inputs (mismatched caps were Codex R2
MAJOR #2)."""


# ---------------------------------------------------------------------------
# Shopping family — runtime caps per services/housewife_shopping.py
# Codex R2 MAJOR #2: ``ShortStr`` (200) silently truncates titles that
# runtime accepts up to 500, and over-permits qty/cat that runtime caps
# at 64. Split into three aliases matching exact runtime behaviour.
# ---------------------------------------------------------------------------


ShoppingTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Shopping item title. Runtime caps at 500 chars via
``title[:500]`` in ``housewife_shopping.py:252``. Must be non-blank
(empty title is silently no-op'd at line 396-399 — schema rejects to
keep the contract honest)."""


QuantityText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=64),
]
"""Shopping item quantity_text on the *update* path. Runtime caps at
64 chars and treats empty string as «clear» (``housewife_shopping.py:
401-402`` does ``q or None``). Codex R2 MAJOR #3: empty MUST be
accepted as a valid update intent («убери количество у молока»), so
this alias has ``min_length`` unset (defaults to 0)."""


AddQuantityText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
"""Shopping item quantity_text on the *add* path. Codex R3 MAJOR #1:
``ShoppingItemInput.quantity_text`` was typed as ``ShoppingTitle`` (500)
with a ``model_validator`` capping to 64 — but JSON schema still
advertised 500, and the planner's refs-present validation path skips
model_validators. Field-level type makes the contract visible in the
JSON schema (max_length=64) and enforced even when refs resolve at
execute time. On add there's nothing to clear, so non-blank required."""


CategoryName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
"""Shopping item category. Runtime caps at 64 chars via
``_normalize_category`` returning ``candidate[:64]``
(``housewife_shopping.py:96``). Non-blank — empty/blank category is
meaningless on both single-item update and bulk re-category paths."""


# ---------------------------------------------------------------------------
# ID shape constraints — Codex R2 MAJOR #1: tighten to match exact
# runtime generation. All four families use the same factory pattern
# ``f"<prefix>_{uuid4().hex[:24]}"`` (housewife_shopping.py:291,
# housewife_reminders.py:169, tasks.py:96/174, checklists.py:148/241).
# uuid4().hex produces lowercase [0-9a-f] only — pattern enforces that.
# ---------------------------------------------------------------------------


_HEX24 = r"[0-9a-f]{24}"
"""24 lowercase-hex chars — the suffix length used by every housewife
ID factory. Centralised so it cannot drift between aliases."""


ShoppingItemId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^sh_{_HEX24}$",
    ),
]
"""Shopping item id — ``sh_<24 hex chars>``. Pattern matches
``f"sh_{uuid4().hex[:24]}"`` (``housewife_shopping.py:291``) exactly.
Codex R2 MAJOR #1: the previous ``^sh_\\S+$`` accepted ``sh_1,sh_2``,
``sh_'foo'``, ``sh_<garbage>`` — planner typos slipped through to
executor lookup and surfaced as ``item_not_found`` instead of
validation failure."""


ReminderId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^rem_{_HEX24}$",
    ),
]
"""Reminder id — ``rem_<24 hex chars>`` (``housewife_reminders.py:169``)."""


TaskId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^task_{_HEX24}$",
    ),
]
"""Task id — ``task_<24 hex chars>`` (``tasks.py:96/174``)."""


ChecklistId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^checklist_{_HEX24}$",
    ),
]
"""Checklist id — ``checklist_<24 hex chars>`` (``checklists.py:148/241``)."""


# ---------------------------------------------------------------------------
# Reminders family — date/time + recurrence aliases.
# Codex Sub-A4 reminders R1 MAJOR #2 + #3: ``NonBlankStr`` (unbounded)
# accepted huge or malformed values before the runtime parser saw them;
# bounded ISO/RRULE aliases catch the obvious shape issues at planner
# validation time. UTC is a separate runtime concern (the runtime
# converts ``+03:00`` to UTC and treats naive as UTC — schema accepts
# both since rejecting offset-aware ISO would break planner usage).
# ---------------------------------------------------------------------------


TriggerIso = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,  # at least YYYY-MM-DD
        max_length=64,
        # Loose ISO-8601 shape — pydantic does the strict parsing.
        # Accepts: YYYY-MM-DDTHH:MM:SS, optional fractional seconds,
        # optional timezone (Z / +HH:MM / -HH:MM). Single-line only.
        pattern=(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?"
            r"(Z|[+-]\d{2}:?\d{2})?$"
        ),
    ),
]
"""ISO-8601 datetime string. Runtime parses via
``datetime.fromisoformat`` and normalizes to UTC
(``housewife_chat_tools.py:325-349`` for schedule_reminder, :498-508
for update_reminder). The shape regex catches obvious typos before
the runtime emits ``cannot_parse_trigger_iso``. Capped at 64 chars —
the longest legitimate ISO with microseconds + offset is ~32 chars,
double for breathing room."""


RecurrenceRule = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=4,  # ``FREQ=`` minimum
        max_length=512,
        # Single-line (no \n/\r); must start with FREQ= (RFC-5545 baseline).
        pattern=r"^FREQ=[^\r\n]+$",
    ),
]
"""RFC-5545 RRULE string. Runtime hands this to ``dateutil.rrule``
which is the format gatekeeper. The pattern enforces single-line +
``FREQ=`` prefix so we catch trivial mistakes (multiline strings,
empty fragments) at planner time. Capped at 512 — RRULEs longer
than that are almost certainly malformed."""


__all__ = [
    "AddQuantityText",
    "CategoryName",
    "ChecklistId",
    "NonBlankStr",
    "QuantityText",
    "RecurrenceRule",
    "ReminderId",
    "ShoppingItemId",
    "ShoppingTitle",
    "ShortStr",
    "TaskId",
    "TriggerIso",
]
