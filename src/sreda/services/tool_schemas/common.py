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

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, StringConstraints


def _validate_iso_datetime_string(value: str) -> str:
    """Codex Sub-A4 reminders R2 MAJOR #2 — verify the regex-shape
    string is also a semantically valid datetime via
    ``datetime.fromisoformat``. Catches impossible timestamps like
    ``2026-99-99T99:99Z`` that pass the shape regex but would crash
    runtime parsing.

    Normalizes ``Z`` suffix to ``+00:00`` for Python <3.11
    compatibility (3.11+ handles ``Z`` natively, but we support both).
    Re-raises ``ValueError`` on failure; pydantic catches and emits a
    ``ValidationError``.
    """
    candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    datetime.fromisoformat(candidate)  # raises ValueError on bad shape
    return value  # return original string; runtime owns UTC normalization


def _validate_rrule_string(value: str) -> str:
    """Codex Sub-A4 reminders R3 MINOR #2 — verify the RRULE string
    parses successfully via ``dateutil.rrulestr``. Catches malformed
    parameter values like ``FREQ=DAILY;COUNT=abc`` or
    ``FREQ=WEEKLY;BYDAY=XX`` that pass the coarse ``;KEY=VALUE``
    pattern but fail downstream at runtime.

    Needs a dummy ``dtstart`` because ``rrulestr`` requires one to
    construct the rule, but the value isn't used for validation
    semantics — just for parse success.

    Re-raises ``ValueError`` on failure; pydantic catches and emits
    a ``ValidationError``.
    """
    from dateutil.rrule import rrulestr  # lazy import — heavy module
    try:
        rrulestr(value, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))
    except (ValueError, KeyError, TypeError) as exc:
        # rrulestr can raise multiple types depending on the failure;
        # normalize to ValueError so pydantic surfaces a clean error.
        raise ValueError(f"invalid RRULE: {exc}") from exc
    return value


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
#
# Codex Sub-A4 reminders R1 MAJOR #2 + #3: ``NonBlankStr`` (unbounded)
# accepted huge or malformed values before the runtime parser saw them;
# bounded ISO/RRULE aliases catch shape issues at planner validation
# time.
#
# Codex Sub-A4 reminders R2 MAJOR #1 + R3 MINOR #4: the planner contract
# is now STRICTER than the runtime — ``TriggerIso`` REQUIRES ``Z`` or
# explicit offset (``+HH:MM`` / ``+HHMM`` / ``-HH:MM``). Runtime
# (``housewife_chat_tools.py:346-349``) still has a legacy fallback
# that treats naive datetimes as UTC for non-planner callers, but the
# planner-facing alias rejects naive to eliminate the timezone-drift
# class entirely. Seconds are required (``:SS``) to keep the planner
# format predictable.
# ---------------------------------------------------------------------------


TriggerIso = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=20,  # ``YYYY-MM-DDTHH:MM:SSZ`` = 20 chars
        max_length=64,
        # Codex Sub-A4 reminders R2 MAJOR #1 + R3 MINOR #3: require
        # explicit Z or offset AND seconds. Previously seconds were
        # optional (``:\d{2})?``) which mismatched ``min_length=20``
        # — ``2026-05-27T18:00Z`` matched the regex but failed length.
        # Tightening seconds to required keeps regex and min_length
        # aligned and matches the runtime emit format (which always
        # includes seconds via ``datetime.isoformat()``).
        #
        # Accepted shapes:
        #   2026-05-27T18:00:00Z              (UTC, no fractional)
        #   2026-05-27T18:00:00.123Z          (with microseconds)
        #   2026-05-27T18:00:00+03:00         (colon offset)
        #   2026-05-27T18:00:00+0300          (no-colon RFC-3339)
        #   2026-05-27T18:00:00.123+03:00     (fractional + offset)
        pattern=(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?"
            r"(Z|[+-]\d{2}:?\d{2})$"
        ),
    ),
    AfterValidator(_validate_iso_datetime_string),
]
"""Offset-aware ISO-8601 datetime string. Codex Sub-A4 reminders R2
MAJOR #1 + #2: two-layer validation:

1. ``StringConstraints.pattern`` enforces shape AND requires explicit
   timezone (``Z`` / ``+HH:MM`` / ``-HH:MM`` / ``+HHMM``). Naive
   timestamps are rejected at planner-input level — the runtime's
   «treat as UTC» fallback exists for legacy callers but the planner
   contract is stricter, eliminating the timezone-drift class.
2. ``AfterValidator`` runs ``datetime.fromisoformat`` on the
   regex-passing string to catch impossible values like
   ``2026-99-99T99:99Z`` that match the shape but aren't real dates.

Runtime (``housewife_chat_tools.py:325-349`` for schedule_reminder,
:498-508 for update_reminder) parses + normalizes to UTC. Cap at 64
chars — the longest legitimate ISO with microseconds + offset is
~32 chars, double for breathing room."""


# RFC-5545 RRULE FREQ values. Codex Sub-A4 reminders R2 MINOR #1:
# previously the pattern accepted any FREQ=... value, including typos
# like FREQ=WEKLY. Whitelist catches those at planner-input level.
_RFC5545_FREQ_VALUES = (
    "SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY",
)
_RFC5545_FREQ_PATTERN = "|".join(_RFC5545_FREQ_VALUES)


RecurrenceRule = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,  # ``FREQ=DAILY`` minimum
        max_length=512,
        # Single-line (no \n/\r); FREQ= must be one of the 7 RFC-5545
        # frequency values; followed by any RFC-5545 parameter chain
        # (BYHOUR, BYDAY, COUNT, INTERVAL, etc.).
        pattern=(
            rf"^FREQ=({_RFC5545_FREQ_PATTERN})"
            r"(;[A-Z]+=[A-Za-z0-9,+\-]+)*$"
        ),
    ),
    # Codex Sub-A4 reminders R3 MINOR #2: parameter-chain regex
    # alone admits typos like ``FREQ=DAILY;COUNT=abc`` (non-numeric
    # COUNT) or ``FREQ=WEEKLY;BYDAY=XX`` (invalid weekday). Run
    # ``dateutil.rrulestr`` for literal RRULEs to catch the grammar
    # at planner-input time. Refs (``${...}``) bypass — pydantic's
    # planner-side validator handles refs separately.
    AfterValidator(_validate_rrule_string),
]
"""RFC-5545 RRULE string. Three-layer validation:

1. ``StringConstraints.pattern`` requires FREQ= one of the 7
   canonical frequencies (SECONDLY/MINUTELY/HOURLY/DAILY/WEEKLY/
   MONTHLY/YEARLY), followed by zero or more ``;KEY=VALUE``
   parameter pairs. Catches typos (``FREQ=WEKLY``) and multiline
   strings at planner time.
2. Runtime hands the validated string to ``dateutil.rrule`` which
   is the parameter-grammar gatekeeper.
3. Capped at 512 chars — RRULEs longer than that are almost
   certainly malformed.

Codex Sub-A4 reminders R2 MINOR #1: previously the pattern was the
permissive ``^FREQ=[^\\r\\n]+$`` which accepted any payload. The
tightened whitelist catches obvious typos at planner-input time."""


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
