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

from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, StringConstraints


def _validate_iso_date_string(value: str) -> str:
    """Codex Sub-A4 menu R1 MAJOR #6 — verify the regex-shape calendar
    date is also a semantically valid date via ``date.fromisoformat``.

    Catches ``2026-02-31`` / ``2026-13-01`` etc. that pass the
    ``YYYY-MM-DD`` shape regex but would crash at runtime
    ``_coerce_monday`` parsing.

    Re-raises ``ValueError`` on failure; pydantic catches and emits
    a ``ValidationError``.
    """
    date.fromisoformat(value)  # raises ValueError on impossible dates
    return value


IsoDateStr = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    AfterValidator(_validate_iso_date_string),
]
"""Shared ISO calendar date alias (``YYYY-MM-DD``). Use for any
date-only field across families. Codex Sub-A4 menu R1 MAJOR #6 + MINOR #1
fix — two-layer validation: regex shape + ``date.fromisoformat``
semantic check catches impossible dates like ``2026-02-31``."""


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


def validate_rrule_static(rrule_str: str) -> None:
    """Codex Sub-A4 reminders R5 MAJOR #1 — dtstart-independent RRULE
    checks that run on ANY non-ref recurrence_rule, including partial
    updates that don't carry trigger_iso.

    Checks:
    1. Numeric ranges (INTERVAL≥1, COUNT≥1, BYHOUR/BYMINUTE/BYSECOND
       bounds, BYMONTH/BYMONTHDAY bounds) — dateutil parses these
       without bounds-checking so we enforce explicitly.
    2. Cross-param feasibility for BYMONTH + BYMONTHDAY (Codex R5
       MAJOR #3) — ``FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30`` would never
       fire (Feb has 28-29 days); accept-at-schema, never-schedule
       is a production bug for reminders.

    Skipped for ``${...}`` refs (planner validator defers refs-
    present paths to execute time).
    """
    if _is_ref(rrule_str):
        return
    params = _parse_rrule_int_params(rrule_str)
    _check_rrule_numeric_ranges(params)
    _check_month_day_feasibility(params)


def validate_rrule_with_trigger(rrule_str: str, trigger_iso: str) -> None:
    """Codex Sub-A4 reminders R3 MINOR #2 + R4 MAJOR #1 + R5 MAJOR #1
    — full RRULE validation including dateutil parse with the ACTUAL
    ``trigger_iso`` as dtstart.

    Runs in this order:
    1. ``validate_rrule_static(rrule_str)`` — numeric ranges +
       cross-param feasibility. Pure RRULE checks that don't need
       dtstart. (Also reachable directly from partial-update paths.)
    2. ``dateutil.rrulestr(rrule_str, dtstart=trigger_iso)`` — full
       RFC-5545 grammar with the planner-supplied dtstart. Catches
       rules whose validity depends on the dtstart's hour/minute
       components (e.g. ``FREQ=HOURLY;BYHOUR=13`` must align with
       dtstart's hour to fire at all).

    Called from the ``@model_validator`` on ``ScheduleReminderInput``
    and ``UpdateReminderInput`` where both ``recurrence_rule`` and
    ``trigger_iso`` are available together. Re-raises ``ValueError``
    on failure; pydantic catches and emits ``ValidationError``.

    Skips entirely if either value is a ``${...}`` ref — the planner
    validator defers refs-present paths to execute time.
    """
    if _is_ref(rrule_str) or _is_ref(trigger_iso):
        return  # deferred — executor validates after refs resolve

    # STEP 1 — dtstart-independent checks (numeric ranges + feasibility).
    validate_rrule_static(rrule_str)

    # STEP 2 — full dateutil parse with the actual dtstart.
    from dateutil.rrule import rrulestr  # lazy import — heavy module
    candidate = (
        trigger_iso.replace("Z", "+00:00")
        if trigger_iso.endswith("Z") else trigger_iso
    )
    try:
        dtstart = datetime.fromisoformat(candidate)
    except ValueError:
        # If trigger_iso doesn't parse, the dedicated TriggerIso
        # validator already raised. Skip here to avoid double-report.
        return  # pragma: no cover
    try:
        rrulestr(rrule_str, dtstart=dtstart)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid RRULE: {exc}") from exc


def _is_ref(value: str) -> bool:
    """Loose check for plan-time ``${...}`` reference placeholders."""
    return value.startswith("${") and value.endswith("}")


def _parse_rrule_int_params(rrule_str: str) -> dict[str, list[int]]:
    """Extract integer params from an RRULE string. Returns
    ``{KEY: [int, ...]}`` for the params we range-check.

    Tolerates non-integer values (e.g. ``BYDAY=MO,TU``) by skipping
    them — only collects keys whose values parse as int(s).
    """
    out: dict[str, list[int]] = {}
    # Drop the FREQ= prefix; iterate the rest.
    parts = rrule_str.split(";")
    for part in parts[1:]:  # skip FREQ=
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        ints: list[int] = []
        for token in value.split(","):
            token = token.strip()
            try:
                ints.append(int(token))
            except ValueError:
                # Token like "MO" or "1MO" — not a pure int; skip.
                # BYDAY may have "1MO" / "-1FR" forms — those go
                # through dateutil's grammar and bypass range check.
                continue
        if ints:
            out[key] = ints
    return out


_RRULE_RANGE_CHECKS = {
    "INTERVAL": (1, None, "INTERVAL must be ≥ 1 (zero/negative produces non-progressing recurrence)"),
    "COUNT":    (1, None, "COUNT must be ≥ 1 (zero produces empty recurrence)"),
    "BYHOUR":   (0, 23,   "BYHOUR must be in [0, 23]"),
    "BYMINUTE": (0, 59,   "BYMINUTE must be in [0, 59]"),
    "BYSECOND": (0, 59,   "BYSECOND must be in [0, 59]"),
    "BYMONTH":  (1, 12,   "BYMONTH must be in [1, 12]"),
}


def _check_rrule_numeric_ranges(params: dict[str, list[int]]) -> None:
    """Codex Sub-A4 reminders R4 MAJOR #2 — RFC-5545 numeric range
    enforcement. dateutil parses int values without bounds-checking;
    values like ``INTERVAL=0`` would hang at iteration rather than
    fail at construction. Raise ValueError eagerly here.

    BYMONTHDAY allows negative values for «N days from end of month»
    (RFC-5545); range is ``[-31, -1] ∪ [1, 31]``. Handled separately.
    """
    for key, (low, high, message) in _RRULE_RANGE_CHECKS.items():
        if key not in params:
            continue
        for v in params[key]:
            if v < low or (high is not None and v > high):
                raise ValueError(f"{message} (got {key}={v})")
    if "BYMONTHDAY" in params:
        for v in params["BYMONTHDAY"]:
            if v == 0 or v < -31 or v > 31:
                raise ValueError(
                    f"BYMONTHDAY must be in [-31, -1] ∪ [1, 31] "
                    f"(got BYMONTHDAY={v}; zero invalid per RFC-5545)"
                )


# Maximum days for each month (leap-friendly: Feb caps at 29 because
# recurring rules span multiple years and we want at least ONE year
# where the combo fires).
_MONTH_MAX_DAY = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def _check_month_day_feasibility(params: dict[str, list[int]]) -> None:
    """Codex Sub-A4 reminders R5 MAJOR #3 — cross-param impossibility
    check for ``BYMONTH`` + ``BYMONTHDAY`` combinations.

    Example impossible combos:
      - ``FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30`` — Feb has 28-29 days
      - ``FREQ=YEARLY;BYMONTH=4,6;BYMONTHDAY=31`` — Apr/Jun have 30

    «Accepted but never schedulable» is a production bug for
    reminders — the proactive worker would scan forever and never
    fire. Catch at planner-input time.

    Rule: at least ONE (BYMONTH, BYMONTHDAY) pair must be feasible,
    i.e. there exists a month in BYMONTH whose max-day is ≥ the
    absolute BYMONTHDAY value. Feb caps at 29 (leap-tolerant — we
    only need ONE year where the recurrence can fire).

    Negative BYMONTHDAY (e.g. -1 = last day) is always feasible because
    every month has at least 28 days; skip the check.

    Only fires when BOTH BYMONTH and BYMONTHDAY are present.
    """
    if "BYMONTH" not in params or "BYMONTHDAY" not in params:
        return
    months = params["BYMONTH"]
    days = params["BYMONTHDAY"]
    # Codex Sub-A4 reminders R6 MAJOR #2: treat positive and negative
    # BYMONTHDAY uniformly via abs(). R5's fix skipped all negative
    # days, accidentally accepting ``BYMONTH=2;BYMONTHDAY=-31`` (which
    # would never fire — Feb has 28-29 days, so «31 days from end of
    # Feb» is impossible). Per RFC-5545 semantics: ``-N`` means «N
    # days from end of month», requires month with ≥N days.
    #
    # ``-1..-28`` are universal (every month has ≥28 days), so they
    # always fire. ``-29..-31`` need progressively longer months.
    # Same as positive: the abs value is the «N days required».
    max_day_available = max(_MONTH_MAX_DAY.get(m, 31) for m in months)
    min_required = min(abs(d) for d in days)
    if max_day_available < min_required:
        raise ValueError(
            f"BYMONTH={months} + BYMONTHDAY={days} is infeasible: "
            f"the largest selected month has {max_day_available} days, "
            f"smallest required (by abs value) is {min_required}. "
            f"Recurrence would never fire. Re-check months/days."
        )


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


TitleMatch = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
"""Fuzzy title substring for the #122 ``title_match`` filter fields
(list_shopping / list_reminders / list_tasks). Audit 2026-07-18 MINOR:
those fields were bare ``str | None`` — the only unbounded match-inputs
(siblings: ``ItemTitleMatch`` ≤200 in specs_checklists.py,
``SearchQuery`` ≤200 in specs_recipes.py, ``RecallQuery`` ≤500 in
specs_memory.py). Cap 200 mirrors the checklist sibling; the runtime
match is a substring ILIKE with no smaller limit to honor."""


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


RecipeId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^rec_{_HEX24}$",
    ),
]
"""Recipe id — ``rec_<24 hex chars>`` (``housewife_recipes.py:217/353``)."""


MenuPlanId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^menu_{_HEX24}$",
    ),
]
"""Weekly menu plan id — ``menu_<24 hex chars>``
(``housewife_menu.py:138``)."""


MenuItemId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^mpi_{_HEX24}$",
    ),
]
"""Menu plan item id (one cell = day×meal) — ``mpi_<24 hex chars>``
(``housewife_menu.py:164,182,243``)."""


FamilyMemberId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^fm_{_HEX24}$",
    ),
]
"""Family member id — ``fm_<24 hex chars>`` (``housewife_family.py:96``).
Matches the ``uuid4().hex[:24]`` runtime token. Tight regex shape
catches malformed planner refs at validation time."""


ChecklistItemId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=rf"^clitem_{_HEX24}$",
    ),
]
"""Checklist item id — ``clitem_<24 hex chars>`` (``checklists.py:301,471``).
Items inside a checklist (separate from `ChecklistId` for the list
itself). Tight regex catches malformed planner refs."""


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


def _alias_static_rrule_check(value: str) -> str:
    """AfterValidator wrapper around ``validate_rrule_static`` for the
    ``RecurrenceRule`` alias. Codex Sub-A4 reminders R6 MAJOR #1: the
    planner validator's refs-present Phase 2 uses per-field
    ``TypeAdapter(annotation_with_constraints)`` which DOES run
    ``AfterValidator`` on each concrete field — but skips
    ``@model_validator`` on the parent model. Putting the static
    checks on the alias makes them fire on the common
    ``update_reminder(reminder_id=ref, recurrence_rule=literal)`` shape
    where the model-level validator wouldn't run.

    Returns the original value (per AfterValidator convention).
    """
    validate_rrule_static(value)
    return value


RecurrenceRule = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,  # ``FREQ=DAILY`` minimum
        max_length=255,  # A/B-study HIGH catch: matches DB columns
        # — db/models/housewife.py:62 + db/models/tasks.py:90 both
        # use String(255). Pre-A/B alias cap was 512 → silent DB
        # truncation on long RRULEs.
        # Single-line (no \n/\r); FREQ= must be one of the 7 RFC-5545
        # frequency values; followed by any RFC-5545 parameter chain
        # (BYHOUR, BYDAY, COUNT, INTERVAL, etc.).
        pattern=(
            rf"^FREQ=({_RFC5545_FREQ_PATTERN})"
            r"(;[A-Z]+=[A-Za-z0-9,+\-]+)*$"
        ),
    ),
    # Codex Sub-A4 reminders R3 MINOR #2 + R4 MAJOR #1 + R6 MAJOR #1:
    # static (dtstart-INDEPENDENT) checks run here so the planner's
    # refs-present per-field validation path catches INTERVAL=0 /
    # BYHOUR=99 / BYMONTH=2;BYMONTHDAY=30 etc. on the common
    # ``update_reminder(reminder_id=ref, recurrence_rule=literal)``
    # shape where ``@model_validator`` is skipped.
    #
    # Full dtstart-aware dateutil validation stays in the
    # ``@model_validator`` on ScheduleReminderInput /
    # UpdateReminderInput where both fields are available together
    # — alias-level can't do that without knowing the trigger_iso.
    AfterValidator(_alias_static_rrule_check),
]
"""RFC-5545 RRULE string. Three-layer validation:

1. ``StringConstraints.pattern`` requires FREQ= one of the 7
   canonical frequencies (SECONDLY/MINUTELY/HOURLY/DAILY/WEEKLY/
   MONTHLY/YEARLY), followed by zero or more ``;KEY=VALUE``
   parameter pairs. Catches typos (``FREQ=WEKLY``) and multiline
   strings at planner time.
2. Runtime hands the validated string to ``dateutil.rrule`` which
   is the parameter-grammar gatekeeper.
3. Capped at 255 chars — matches the DB column ``String(255)``;
   longer RRULEs would be silently truncated on persist.

Codex Sub-A4 reminders R2 MINOR #1: previously the pattern was the
permissive ``^FREQ=[^\\r\\n]+$`` which accepted any payload. The
tightened whitelist catches obvious typos at planner-input time."""


__all__ = [
    "AddQuantityText",
    "CategoryName",
    "ChecklistId",
    "ChecklistItemId",
    "FamilyMemberId",
    "IsoDateStr",
    "MenuItemId",
    "MenuPlanId",
    "NonBlankStr",
    "QuantityText",
    "RecipeId",
    "RecurrenceRule",
    "ReminderId",
    "ShoppingItemId",
    "ShoppingTitle",
    "ShortStr",
    "TaskId",
    "TitleMatch",
    "TriggerIso",
    "validate_rrule_static",
    "validate_rrule_with_trigger",
]
