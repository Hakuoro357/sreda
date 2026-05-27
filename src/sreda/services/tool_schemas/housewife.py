"""Tool output schemas + parsers for top-5 housewife tools (Sub-A4, Epic #74).

Wraps legacy ``str`` outputs from ``services/housewife_chat_tools.py`` into
typed pydantic discriminated unions the planner / validator / executor
can match against. The legacy tools themselves stay unchanged — the
wrapper at ``services/tool_schemas/wrapper.py`` does the parsing at
executor visit time so the planner sees structured ``ToolOutput`` dicts
instead of patterns like ``ok:added:3:ids=[shop_1,shop_2,shop_3]``.

Unknown patterns return ``ToolOutputContractViolation`` (the executor
halts the plan and writes to ``planner_gaps(gap_type='contract_violation')``
per Group 6.5 — fail-closed, no silent acceptance).

Top-5 chosen per Sub-A4 issue and frequency in production traces:

  add_shopping_items   most-used write — driver of the recipe pipeline
  schedule_reminder    most-used reminder write
  list_shopping        most-used read
  list_reminders       second most-used read
  get_recipe           recipe-read driver for cooking flow

Other 50 tools migrate in subsequent commits — wrapper falls through
to ``raw_text`` pass-through until each has a parser registered.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.common import (
    ChecklistId,
    ChecklistItemId,
    FamilyMemberId,
    MenuItemId,
    MenuPlanId,
    RecipeId,
    ReminderId,
    ShoppingItemId,
    TaskId,
    TriggerIso,
)


# ---------------------------------------------------------------------------
# Shared error variant used by every tool that emits ``error: ...`` patterns
# ---------------------------------------------------------------------------


class HousewifeToolError(BaseModel):
    """Common error shape — every tool that maps to ``error: ...`` lands here.

    ``error_code`` is the machine-readable identifier the planner matches
    against (e.g. ``{"status": "error", "error_code": "validation_failed"}``).
    ``message`` is the original error string for surfacing in admin
    review / GEPA training data.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["error"] = "error"
    error_code: str = Field(min_length=1)
    message: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Stable error-code patterns — mapped BEFORE the generic dynamic-code
# fallback in ``_parse_error``. Without this layer, messages that embed
# a dynamic value (item id, reminder id, etc.) produce a different
# ``error_code`` for every call (e.g. ``item_'sh_42'_not_found``,
# ``item_'sh_7'_not_found``), making planner branching non-deterministic.
# Codex Sub-A4 R1 CRITICAL.
#
# Add patterns here as new dynamic-message errors are discovered in tool
# implementations. Order matters — first match wins.
# ---------------------------------------------------------------------------

_STABLE_ERROR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # update_shopping_item: ``error: item 'sh_42' not found``
    (re.compile(r"^item\s+.+\s+not\s+found$", re.IGNORECASE), "item_not_found"),
    # task_chat_tools: ``error: task 'task_42' not found``
    (re.compile(r"^task\s+.+\s+not\s+found$", re.IGNORECASE), "task_not_found"),
    # reminders: ``error: reminder 'rem_42' not found``
    (re.compile(r"^reminder\s+.+\s+not\s+found$", re.IGNORECASE), "reminder_not_found"),
    # checklists: ``error: checklist 'cl_42' not found``
    (
        re.compile(r"^checklist\s+.+\s+not\s+found$", re.IGNORECASE),
        "checklist_not_found",
    ),
    # schedule_reminder / update_reminder: ``error: cannot parse trigger_iso='завтра'``
    # Codex R2 MAJOR #6: without this, ``_parse_error`` produces a
    # value-dependent code like ``cannot_parse_trigger_iso='завтра'``
    # (different per input), making planner branching nondeterministic.
    # Source: ``housewife_chat_tools.py:339, :504``.
    (
        re.compile(r"^cannot\s+parse\s+trigger_iso=.+$", re.IGNORECASE),
        "cannot_parse_trigger_iso",
    ),
    # save_recipe / get_recipe / delete_recipe: ``error: recipe 'rec_X' not found``
    # Sub-A4 recipes phase: same dynamic-id-in-message class as item/
    # task/reminder not_found. Source: ``housewife_chat_tools.py:1179, :1214``.
    (
        re.compile(r"^recipe\s+.+\s+not\s+found$", re.IGNORECASE),
        "recipe_not_found",
    ),
    # update_family_member / remove_family_member: ``error: member 'fm_X' not found``
    # Sub-A4 household phase: same dynamic-id-in-message class.
    # Source: ``housewife_chat_tools.py:1682, :1697``.
    (
        re.compile(r"^member\s+.+\s+not\s+found$", re.IGNORECASE),
        "member_not_found",
    ),
    # checklist family — list_not_found / item_not_found with embedded
    # repr'd needle: ``error: list_not_found: 'дача'`` /
    # ``error: item_not_found: 'молоко'`` / ``error: not_found: 'X'``.
    # Source: ``housewife_chat_tools.py:2590,2623,2628,2662,2668,2692``.
    (
        re.compile(r"^list_not_found:.+", re.IGNORECASE),
        "checklist_list_not_found",
    ),
    (
        re.compile(r"^item_not_found:.+", re.IGNORECASE),
        "checklist_item_not_found",
    ),
    # onboarding family — Codex R5 MAJOR (HIGH catch): runtime rejects
    # non-active topics via `error: topic_not_in_active_flow 'X'`.
    # Source: ``housewife_chat_tools.py`` onboarding_answered/deferred.
    (
        re.compile(r"^topic_not_in_active_flow .+$", re.IGNORECASE),
        "topic_not_in_active_flow",
    ),
    # link_task_to_checklist conflict shapes (Codex Sub-A4 tasks R1 MAJOR #5).
    # Source: ``housewife_chat_tools.py:2239-2247``.
    #   "error: task_already_linked:task_X:checklist_Y. Unlink сначала..."
    #   "error: checklist_already_linked_to_task_X. Сначала unlink..."
    # The dynamic ids (task_X / checklist_Y) embedded in the message
    # would produce per-id error codes via the fallback path; stable
    # patterns make planner branching deterministic.
    (
        re.compile(r"^task_already_linked:.+", re.IGNORECASE),
        "task_already_linked",
    ),
    (
        re.compile(r"^checklist_already_linked_to_task_.+", re.IGNORECASE),
        "checklist_already_linked",
    ),
]
# Codex Sub-A4 tasks R2 MAJOR (new) — link_task_to_checklist
# «not_found:*» / «archived:*» catch-all patterns are intentionally
# NOT in the global _STABLE_ERROR_PATTERNS table because they'd
# match unrelated tools' messages. Instead, `parse_link_task_to_checklist`
# inspects the parsed `info` payload and remaps to
# link-task-specific stable codes
# (link_task_not_found / link_checklist_not_found / checklist_archived).


def _parse_error(raw: str) -> HousewifeToolError | None:
    """Normalize ``error: <code>[: <detail>]`` into HousewifeToolError.

    Returns ``None`` if ``raw`` isn't an error line so callers can
    distinguish "not an error" from "unparseable error".

    Behaviour layers (Codex Sub-A4 R1 CRITICAL fix — stable codes):
    1. Match against ``_STABLE_ERROR_PATTERNS`` first — produces a
       fixed ``error_code`` ignoring dynamic id/value content. Lets
       the planner branch deterministically on
       ``error_code=='item_not_found'`` regardless of which specific
       id triggered.
    2. Fallback to dynamic code derivation (lowercase, spaces→
       underscores). Handles ``error:internal`` (no space) and
       ``error: : detail`` (collapses to ``unknown``).
    """
    if not raw.startswith("error:"):
        return None
    rest = raw[len("error:"):].strip()
    if not rest:
        # Bare ``error:`` with no payload — treat as unknown so the
        # planner branch can still match deterministically.
        return HousewifeToolError(error_code="unknown", message="unknown")
    # Stable-code lookup — first match wins.
    for pattern, stable_code in _STABLE_ERROR_PATTERNS:
        if pattern.match(rest):
            return HousewifeToolError(error_code=stable_code, message=rest)
    # Fallback to dynamic code derivation.
    if ":" in rest:
        code_part, _detail = rest.split(":", 1)
        code = code_part.strip().lower().replace(" ", "_") or "unknown"
        message = rest.strip() or "unknown"
    else:
        code = rest.strip().lower().replace(" ", "_") or "unknown"
        message = rest.strip() or "unknown"
    return HousewifeToolError(error_code=code, message=message)


# ---------------------------------------------------------------------------
# 1. add_shopping_items
#    ok:added:0                       → AddShoppingItemsEmptyOutput
#    ok:added:N:ids=[sh_1,sh_2,...]   → AddShoppingItemsAddedOutput
#    error: ...                       → HousewifeToolError
# ---------------------------------------------------------------------------


class AddShoppingItemsAdded(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["added"] = "added"
    added_count: int = Field(ge=1)
    item_ids: list[ShoppingItemId]
    """Codex R2 MAJOR #4: was ``list[str]`` — accepted malformed legacy
    output and let bad ids become planner-visible refs. Now uses the
    tight ``sh_<24 hex>`` pattern; parsers wrap construction in
    ``ValidationError`` → ``ToolOutputContractViolation`` so the
    executor fail-closes on bad raw output."""

    @model_validator(mode="after")
    def _validate_count_matches_ids(self) -> AddShoppingItemsAdded:
        # Code-reviewer + Codex 2026-05-26: production today always
        # emits matching counts, but defensive validation catches
        # regressions before they reach the planner (which uses
        # added_count for branch decisions and item_ids for refs).
        if len(self.item_ids) != self.added_count:
            raise ValueError(
                f"added_count={self.added_count} but item_ids has "
                f"{len(self.item_ids)} entries — mismatch."
            )
        return self


class AddShoppingItemsEmpty(BaseModel):
    """``ok:added:0`` — every requested item was a duplicate of existing row."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"
    added_count: Literal[0] = 0


AddShoppingItemsOutput = Annotated[
    Union[AddShoppingItemsAdded, AddShoppingItemsEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


_ADD_SHOPPING_RE = re.compile(
    r"^ok:added:(?P<count>\d+)(?::ids=\[(?P<ids>[^\]]*)\])?$"
)


def parse_add_shopping_items(
    raw: str,
) -> AddShoppingItemsAdded | AddShoppingItemsEmpty | HousewifeToolError | ToolOutputContractViolation:
    """Parse a raw add_shopping_items output line."""
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ADD_SHOPPING_RE.match(raw.strip())
    if m is not None:
        count = int(m.group("count"))
        ids_csv = m.group("ids") or ""
        ids = [x.strip() for x in ids_csv.split(",") if x.strip()]
        if count == 0:
            # Codex R3 MINOR: ``ok:added:0:ids=[sh_x]`` is internally
            # inconsistent — count says «nothing added» but ids
            # claims one row was created. The ``count > 0`` path has
            # a strict id/count match guard; symmetry demands the
            # zero path also fail-close on non-empty ids.
            if ids:
                return ToolOutputContractViolation(
                    raw_output=raw,
                    tool_name="add_shopping_items",
                    timestamp=datetime.now(timezone.utc),
                )
            return AddShoppingItemsEmpty()
        # Codex R2 MAJOR #4: tight ShoppingItemId pattern rejects
        # malformed legacy output (e.g. ``ids=[sh_1,sh_2]`` vs the
        # ``sh_<24 hex>`` runtime contract). On ValidationError fall
        # through to ToolOutputContractViolation so the executor
        # halts the plan instead of letting bad ids reach the planner.
        try:
            return AddShoppingItemsAdded(added_count=count, item_ids=ids)
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="add_shopping_items",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 2. schedule_reminder
#    ok:scheduled:rem_<id>:<iso>      → ScheduleReminderScheduled
#    error: cannot parse trigger_iso=…→ HousewifeToolError(error_code='cannot_parse_trigger_iso')
#    error: ...                       → HousewifeToolError
# ---------------------------------------------------------------------------


class ScheduleReminderScheduled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["scheduled"] = "scheduled"
    reminder_id: ReminderId
    """Codex R2 MAJOR #4: was ``str(min_length=1)`` — accepted any
    non-empty token. Now uses the tight ``rem_<24 hex>`` pattern."""
    trigger_at_iso: str = Field(min_length=1)


class ScheduleReminderSkippedPast(BaseModel):
    """``skipped:past:<iso>:late_by_<n>min`` — trigger time was already
    in the past at schedule call.

    Real production path from housewife_chat_tools.py:373. Code-reviewer
    2026-05-26 CRITICAL #2 — was silently treated as ContractViolation
    by my v1 parser, halting the plan on a legitimate skipped-reminder
    outcome.
    """

    model_config = ConfigDict(extra="forbid")
    status: Literal["skipped_past"] = "skipped_past"
    trigger_at_iso: str = Field(min_length=1)
    late_by_minutes: int = Field(ge=0)


ScheduleReminderOutput = Annotated[
    Union[
        ScheduleReminderScheduled,
        ScheduleReminderSkippedPast,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]


_SCHEDULE_REMINDER_RE = re.compile(
    r"^ok:scheduled:(?P<id>[^:]+):(?P<iso>.+)$"
)
_SCHEDULE_SKIPPED_PAST_RE = re.compile(
    r"^skipped:past:(?P<iso>.+):late_by_(?P<min>\d+)min$"
)


def parse_schedule_reminder(
    raw: str,
) -> (
    ScheduleReminderScheduled
    | ScheduleReminderSkippedPast
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    m = _SCHEDULE_REMINDER_RE.match(stripped)
    if m is not None:
        try:
            return ScheduleReminderScheduled(
                reminder_id=m.group("id"),
                trigger_at_iso=m.group("iso"),
            )
        except ValidationError:
            # Codex R2 MAJOR #4: malformed reminder_id (not matching
            # rem_<24 hex>) — fail-closed via sentinel.
            return ToolOutputContractViolation(
                raw_output=raw,
                tool_name="schedule_reminder",
                timestamp=datetime.now(timezone.utc),
            )
    m = _SCHEDULE_SKIPPED_PAST_RE.match(stripped)
    if m is not None:
        return ScheduleReminderSkippedPast(
            trigger_at_iso=m.group("iso"),
            late_by_minutes=int(m.group("min")),
        )
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="schedule_reminder",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 3. list_shopping
#    ``no shopping items``            → ListShoppingEmpty
#    multi-line ``[sh_id] title qty`` → ListShoppingItems
#    error: ...                       → HousewifeToolError
# ---------------------------------------------------------------------------


class ListShoppingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: ShoppingItemId
    """Codex R2 MAJOR #4: was ``str`` — accepted any token after the
    ``[`` bracket. Now uses tight ``sh_<24 hex>`` pattern."""
    category: str
    """Category bucket the legacy tool grouped this item under (e.g.
    ``молочные`` / ``бакалея``). Comes from the surrounding ``[category]``
    line that immediately precedes the ``  [sh_id] ...`` block."""
    raw_line: str
    """Original line — title / qty parsing varies tool-version-by-version,
    so the planner gets the raw text and the structured id for refs."""


class ListShoppingEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


class ListShoppingItems(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    items: list[ListShoppingItem] = Field(min_length=1)
    """Non-empty by contract — empty list is the ``no shopping items``
    variant which routes to ``ListShoppingEmpty`` instead. Codex review
    Medium #1 — empty here would fail-open."""
    raw_text: str


ListShoppingOutput = Annotated[
    Union[ListShoppingItems, ListShoppingEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


_LIST_SHOPPING_HEADER = "pending shopping items:"
_LIST_SHOPPING_CATEGORY_RE = re.compile(r"^\[(?P<cat>[^\]]+)\]$")
"""Bare ``[category_name]`` line (no surrounding text)."""

_LIST_SHOPPING_ITEM_RE = re.compile(r"^\[(?P<id>sh_[^\]]+)\]\s+(?P<rest>.+)$")
"""Indented ``[sh_<id>] title (qty)`` line — id must start with ``sh_``
so we never mistake a category line for an item."""


def parse_list_shopping(
    raw: str,
) -> ListShoppingItems | ListShoppingEmpty | HousewifeToolError | ToolOutputContractViolation:
    """Parse list_shopping output (code-reviewer 2026-05-26 CRITICAL #1 fix).

    Real production format from ``housewife_chat_tools.py:880-889``:

        pending shopping items:
        [молочные]
          [sh_abc] молоко (1 л)
          [sh_def] хлеб
        [бакалея]
          [sh_xyz] сахар (1 кг)
    """
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if not stripped:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_shopping",
            timestamp=datetime.now(timezone.utc),
        )
    if stripped == "no shopping items":
        return ListShoppingEmpty()

    lines = [ln.rstrip() for ln in stripped.splitlines() if ln.strip()]
    if not lines or lines[0].strip() != _LIST_SHOPPING_HEADER:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_shopping",
            timestamp=datetime.now(timezone.utc),
        )

    items: list[ListShoppingItem] = []
    current_category: str | None = None
    for line in lines[1:]:
        # Category lines are bare ``[name]`` at column 0; item lines are
        # indented ``  [sh_id] title (qty)``. We .strip() the indent
        # uniformly and let the regexes distinguish by ``sh_`` prefix.
        clean = line.strip()
        cat_match = _LIST_SHOPPING_CATEGORY_RE.match(clean)
        if cat_match is not None and not clean.startswith("[sh_"):
            current_category = cat_match.group("cat")
            continue
        item_match = _LIST_SHOPPING_ITEM_RE.match(clean)
        if item_match is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_shopping",
                timestamp=datetime.now(timezone.utc),
            )
        if current_category is None:
            # Item before any category — shouldn't happen in production
            # but surface as violation rather than silently default.
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_shopping",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            items.append(
                ListShoppingItem(
                    item_id=item_match.group("id"),
                    category=current_category,
                    raw_line=clean,
                )
            )
        except ValidationError:
            # Codex R2 MAJOR #4: regex captured a bracketed token that
            # starts with ``sh_`` but isn't a valid 24-hex id — fail
            # the whole list output via sentinel rather than emit a
            # partial-but-malformed list to the planner.
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_shopping",
                timestamp=datetime.now(timezone.utc),
            )
    if not items:
        # Header + categories but no items — malformed for "ok" variant.
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_shopping",
            timestamp=datetime.now(timezone.utc),
        )
    return ListShoppingItems(items=items, raw_text=stripped)


# ---------------------------------------------------------------------------
# 4. list_reminders
#    ``no active reminders``                  → ListRemindersEmpty
#    ``active reminders:\n[rem_id] title → t``→ ListRemindersList
#    error: ...                               → HousewifeToolError
# ---------------------------------------------------------------------------


class ListRemindersItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminder_id: ReminderId
    """Codex R2 MAJOR #4: was ``str`` — now uses the tight
    ``rem_<24 hex>`` pattern."""
    raw_line: str


class ListRemindersEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


class ListRemindersList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    items: list[ListRemindersItem] = Field(min_length=1)
    """Non-empty by contract — empty list is the ``no active reminders``
    variant (ListRemindersEmpty). Codex review Medium #2."""
    raw_text: str


ListRemindersOutput = Annotated[
    Union[ListRemindersList, ListRemindersEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


_LIST_REMINDER_LINE_RE = re.compile(r"^\[(?P<id>[^\]]+)\]\s+(?P<rest>.+)$")
_REMINDERS_HEADER = "active reminders:"


def parse_list_reminders(
    raw: str,
) -> ListRemindersList | ListRemindersEmpty | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "no active reminders":
        return ListRemindersEmpty()
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines or lines[0] != _REMINDERS_HEADER:
        return ToolOutputContractViolation(
            raw_output=raw,
            tool_name="list_reminders",
            timestamp=datetime.now(timezone.utc),
        )
    items: list[ListRemindersItem] = []
    for line in lines[1:]:
        m = _LIST_REMINDER_LINE_RE.match(line)
        if m is None:
            return ToolOutputContractViolation(
                raw_output=raw,
                tool_name="list_reminders",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            items.append(ListRemindersItem(reminder_id=m.group("id"), raw_line=line))
        except ValidationError:
            # Codex R2 MAJOR #4: bracketed token isn't a valid
            # ``rem_<24 hex>`` — fail-closed via sentinel.
            return ToolOutputContractViolation(
                raw_output=raw,
                tool_name="list_reminders",
                timestamp=datetime.now(timezone.utc),
            )
    if not items:
        # Header but no rows — production never emits this (empty state
        # is the ``no active reminders`` short-circuit). Codex Medium #2.
        return ToolOutputContractViolation(
            raw_output=raw,
            tool_name="list_reminders",
            timestamp=datetime.now(timezone.utc),
        )
    return ListRemindersList(items=items, raw_text=stripped)


# ---------------------------------------------------------------------------
# 5. get_recipe
#    Multi-line block (title + servings + ingredients + steps)
#    ``error: recipe {id!r} not found``       → HousewifeToolError(error_code='not_found')
#    ``error: ...``                            → HousewifeToolError
# ---------------------------------------------------------------------------


class GetRecipeFound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["found"] = "found"
    raw_text: str
    """Recipe body is free-form multi-line — the planner gets it verbatim
    for composer's narrative templates. Structured ingredient extraction
    (Sub-A5) is a separate parser."""


GetRecipeOutput = Annotated[
    Union[GetRecipeFound, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_get_recipe(
    raw: str,
) -> GetRecipeFound | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        # Codex Sub-A4 recipes phase: the ``recipe X not found`` stable
        # pattern in ``_STABLE_ERROR_PATTERNS`` already produces
        # ``error_code='recipe_not_found'``. The old special-case here
        # was overriding it to ``not_found`` (legacy code-reviewer
        # 2026-05-26 convention before the stable patterns existed) —
        # that broke planner branching consistency across families
        # (item_not_found, task_not_found, reminder_not_found,
        # checklist_not_found, recipe_not_found all share the same
        # ``<entity>_not_found`` shape now).
        return err
    stripped = raw.strip()
    if not stripped:
        return ToolOutputContractViolation(
            raw_output=raw,
            tool_name="get_recipe",
            timestamp=datetime.now(timezone.utc),
        )
    return GetRecipeFound(raw_text=stripped)


# ---------------------------------------------------------------------------
# 6. mark_shopping_bought       `ok:bought:N`
# ---------------------------------------------------------------------------


class MarkShoppingBoughtOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["bought"] = "bought"
    bought_count: int = Field(ge=0)


MarkShoppingBoughtOutput = Annotated[
    Union[MarkShoppingBoughtOk, HousewifeToolError],
    Field(discriminator="status"),
]

_MARK_BOUGHT_RE = re.compile(r"^ok:bought:(?P<n>\d+)$")


def parse_mark_shopping_bought(
    raw: str,
) -> MarkShoppingBoughtOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _MARK_BOUGHT_RE.match(raw.strip())
    if m is not None:
        return MarkShoppingBoughtOk(bought_count=int(m.group("n")))
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="mark_shopping_bought",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 7. remove_shopping_items       `ok:removed:N`
# ---------------------------------------------------------------------------


class RemoveShoppingItemsOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["removed"] = "removed"
    removed_count: int = Field(ge=0)


RemoveShoppingItemsOutput = Annotated[
    Union[RemoveShoppingItemsOk, HousewifeToolError],
    Field(discriminator="status"),
]

_REMOVE_RE = re.compile(r"^ok:removed:(?P<n>\d+)$")


def parse_remove_shopping_items(
    raw: str,
) -> RemoveShoppingItemsOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _REMOVE_RE.match(raw.strip())
    if m is not None:
        return RemoveShoppingItemsOk(removed_count=int(m.group("n")))
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="remove_shopping_items",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 8. update_shopping_item        `ok:updated:<id>` | not-found
# ---------------------------------------------------------------------------


class UpdateShoppingItemOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["updated"] = "updated"
    item_id: ShoppingItemId
    """Codex R2 MAJOR #4: was ``str(min_length=1)`` — accepted any
    non-blank token. Now uses the tight ``sh_<24 hex>`` pattern."""


UpdateShoppingItemOutput = Annotated[
    Union[UpdateShoppingItemOk, HousewifeToolError],
    Field(discriminator="status"),
]

_UPDATE_ITEM_RE = re.compile(r"^ok:updated:(?P<id>[^\s]+)$")


def parse_update_shopping_item(
    raw: str,
) -> UpdateShoppingItemOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _UPDATE_ITEM_RE.match(raw.strip())
    if m is not None:
        try:
            return UpdateShoppingItemOk(item_id=m.group("id"))
        except ValidationError:
            # Codex R2 MAJOR #4: id doesn't match sh_<24 hex>.
            pass
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="update_shopping_item",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 9. update_shopping_items_category   `ok:updated:N` (count, not id)
# ---------------------------------------------------------------------------


class UpdateShoppingItemsCategoryOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["updated_category"] = "updated_category"
    updated_count: int = Field(ge=0)


UpdateShoppingItemsCategoryOutput = Annotated[
    Union[UpdateShoppingItemsCategoryOk, HousewifeToolError],
    Field(discriminator="status"),
]

_UPDATE_CATEGORY_RE = re.compile(r"^ok:updated:(?P<n>\d+)$")


def parse_update_shopping_items_category(
    raw: str,
) -> (
    UpdateShoppingItemsCategoryOk
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _UPDATE_CATEGORY_RE.match(raw.strip())
    if m is not None:
        return UpdateShoppingItemsCategoryOk(
            updated_count=int(m.group("n"))
        )
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="update_shopping_items_category",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 10. clear_bought_shopping     `ok:cleared:N`
# ---------------------------------------------------------------------------


class ClearBoughtShoppingOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["cleared"] = "cleared"
    cleared_count: int = Field(ge=0)


ClearBoughtShoppingOutput = Annotated[
    Union[ClearBoughtShoppingOk, HousewifeToolError],
    Field(discriminator="status"),
]

_CLEAR_BOUGHT_RE = re.compile(r"^ok:cleared:(?P<n>\d+)$")


def parse_clear_bought_shopping(
    raw: str,
) -> ClearBoughtShoppingOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _CLEAR_BOUGHT_RE.match(raw.strip())
    if m is not None:
        return ClearBoughtShoppingOk(cleared_count=int(m.group("n")))
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="clear_bought_shopping",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 11. update_reminder           `ok:updated:rem_<id>:<iso>` | `:none`
#                               | error: reminder 'X' not found
#                               | error: cannot parse trigger_iso=...
# ---------------------------------------------------------------------------


class UpdateReminderOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["updated"] = "updated"
    reminder_id: ReminderId
    next_trigger_at_iso: TriggerIso | None = None
    """ISO-8601 of the next scheduled fire after the update. ``None``
    when the recurrence was cleared and no future trigger remains
    (legacy emits the literal ``"none"`` which the parser maps here).

    Codex Sub-A4 reminders R1 MAJOR #4: previously typed ``str | None``
    which accepted any non-``none`` payload — a malformed legacy
    output like ``ok:updated:rem_xxx:tomorrow`` would have surfaced
    as typed success. Now uses ``TriggerIso`` (ISO-shape regex +
    ≤64 chars); the parser catches the ValidationError and routes
    to ``ToolOutputContractViolation`` (fail-closed)."""


UpdateReminderOutput = Annotated[
    Union[UpdateReminderOk, HousewifeToolError],
    Field(discriminator="status"),
]

_UPDATE_REMINDER_RE = re.compile(
    r"^ok:updated:(?P<id>rem_[^:\s]+):(?P<next>.+)$"
)


def parse_update_reminder(
    raw: str,
) -> UpdateReminderOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _UPDATE_REMINDER_RE.match(raw.strip())
    if m is not None:
        next_at_raw = m.group("next").strip()
        next_at = None if next_at_raw == "none" else next_at_raw
        try:
            return UpdateReminderOk(
                reminder_id=m.group("id"),
                next_trigger_at_iso=next_at,
            )
        except ValidationError:
            # Tight rem_<24 hex> alias rejected — fail-closed.
            pass
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="update_reminder",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 12. cancel_reminder           `ok:cancelled` | error: reminder 'X' not found
# ---------------------------------------------------------------------------


class CancelReminderOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["cancelled"] = "cancelled"


CancelReminderOutput = Annotated[
    Union[CancelReminderOk, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_cancel_reminder(
    raw: str,
) -> CancelReminderOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    if raw.strip() == "ok:cancelled":
        return CancelReminderOk()
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="cancel_reminder",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 13. save_recipe              `ok:saved:rec_<id>` | `ok:duplicate:rec_<id>`
# ---------------------------------------------------------------------------


class SaveRecipeOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["saved", "duplicate"]
    recipe_id: RecipeId
    """``saved`` for new rows; ``duplicate`` when title-dedup short-
    circuited (housewife_chat_tools.py:1022-1027). Both branches are
    «happy outcomes» — planner branches on status to differentiate
    «added to book» vs «already in book»."""


SaveRecipeOutput = Annotated[
    Union[SaveRecipeOk, HousewifeToolError],
    Field(discriminator="status"),
]

_SAVE_RECIPE_RE = re.compile(r"^ok:(?P<status>saved|duplicate):(?P<id>rec_[^\s]+)$")


def parse_save_recipe(
    raw: str,
) -> SaveRecipeOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _SAVE_RECIPE_RE.match(raw.strip())
    if m is not None:
        try:
            return SaveRecipeOk(
                status=m.group("status"),
                recipe_id=m.group("id"),
            )
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="save_recipe",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 14. save_recipes_batch
#   `ok:batch_saved:N:skipped_as_duplicate:M:ids=[rec_a,rec_b]`
#   `ok:batch_saved:0:skipped_as_duplicate:M`  (no ids when 0 created)
# ---------------------------------------------------------------------------


class SaveRecipesBatchOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["batch_saved"] = "batch_saved"
    created_count: int = Field(ge=0)
    skipped_as_duplicate: int = Field(ge=0)
    recipe_ids: list[RecipeId]

    @model_validator(mode="after")
    def _validate_count_matches_ids(self) -> SaveRecipesBatchOk:
        if len(self.recipe_ids) != self.created_count:
            raise ValueError(
                f"created_count={self.created_count} but recipe_ids has "
                f"{len(self.recipe_ids)} entries — mismatch."
            )
        return self


SaveRecipesBatchOutput = Annotated[
    Union[SaveRecipesBatchOk, HousewifeToolError],
    Field(discriminator="status"),
]

# Two forms:
#   ok:batch_saved:0:skipped_as_duplicate:M
#   ok:batch_saved:N:skipped_as_duplicate:M:ids=[rec_a,rec_b,...]
_SAVE_BATCH_RE = re.compile(
    r"^ok:batch_saved:(?P<n>\d+):skipped_as_duplicate:(?P<m>\d+)"
    r"(?::ids=\[(?P<ids>[^\]]*)\])?$"
)


def parse_save_recipes_batch(
    raw: str,
) -> SaveRecipesBatchOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _SAVE_BATCH_RE.match(raw.strip())
    if m is not None:
        n = int(m.group("n"))
        skipped = int(m.group("m"))
        ids_csv = m.group("ids") or ""
        ids = [x.strip() for x in ids_csv.split(",") if x.strip()]
        # Codex Sub-A4 shopping R3 MINOR analogue: zero-count + ids
        # mismatch fails closed (legacy emits no ids group when N=0,
        # so ids should be empty here too).
        if n == 0 and ids:
            return ToolOutputContractViolation(
                raw_output=raw,
                tool_name="save_recipes_batch",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            return SaveRecipesBatchOk(
                created_count=n,
                skipped_as_duplicate=skipped,
                recipe_ids=ids,
            )
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="save_recipes_batch",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 15. search_recipes      `no recipes found` | `N recipe(s):\n  [rec_id] ...`
# ---------------------------------------------------------------------------


class SearchRecipesItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: RecipeId
    raw_line: str
    """Raw line text — title + badge + tags blob. Renderer-friendly,
    parsed forward into ``planner`` references for compose templates."""


class SearchRecipesEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


class SearchRecipesList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    items: list[SearchRecipesItem] = Field(min_length=1)
    raw_text: str


SearchRecipesOutput = Annotated[
    Union[SearchRecipesList, SearchRecipesEmpty, HousewifeToolError],
    Field(discriminator="status"),
]

_SEARCH_HEADER_RE = re.compile(r"^(?P<n>\d+)\s+recipe\(s\):$")
_SEARCH_ITEM_RE = re.compile(r"^\[(?P<id>rec_[^\]]+)\]\s+.+$")


def parse_search_recipes(
    raw: str,
) -> SearchRecipesList | SearchRecipesEmpty | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "no recipes found":
        return SearchRecipesEmpty()
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="search_recipes",
            timestamp=datetime.now(timezone.utc),
        )
    header = lines[0]
    if not _SEARCH_HEADER_RE.match(header):
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="search_recipes",
            timestamp=datetime.now(timezone.utc),
        )
    items: list[SearchRecipesItem] = []
    for line in lines[1:]:
        m = _SEARCH_ITEM_RE.match(line)
        if m is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="search_recipes",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            items.append(SearchRecipesItem(
                recipe_id=m.group("id"),
                raw_line=line,
            ))
        except ValidationError:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="search_recipes",
                timestamp=datetime.now(timezone.utc),
            )
    if not items:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="search_recipes",
            timestamp=datetime.now(timezone.utc),
        )
    return SearchRecipesList(items=items, raw_text=stripped)


# ---------------------------------------------------------------------------
# 16. delete_recipe         `ok:deleted` | `error: recipe 'X' not found`
# ---------------------------------------------------------------------------


class DeleteRecipeOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["deleted"] = "deleted"


DeleteRecipeOutput = Annotated[
    Union[DeleteRecipeOk, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_delete_recipe(
    raw: str,
) -> DeleteRecipeOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    if raw.strip() == "ok:deleted":
        return DeleteRecipeOk()
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="delete_recipe",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 17. plan_week_menu       `ok:plan_created:menu_<id>:<week_iso>`
# ---------------------------------------------------------------------------


class PlanWeekMenuOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["plan_created"] = "plan_created"
    menu_id: MenuPlanId
    week_start_iso: str = Field(min_length=10)
    """Monday-normalised week start date (YYYY-MM-DD). Service writes
    via ``plan.week_start_date.isoformat()``."""


PlanWeekMenuOutput = Annotated[
    Union[PlanWeekMenuOk, HousewifeToolError],
    Field(discriminator="status"),
]

_PLAN_WEEK_MENU_RE = re.compile(
    r"^ok:plan_created:(?P<id>menu_[^:]+):(?P<week>.+)$"
)


def parse_plan_week_menu(
    raw: str,
) -> PlanWeekMenuOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _PLAN_WEEK_MENU_RE.match(raw.strip())
    if m is not None:
        try:
            return PlanWeekMenuOk(
                menu_id=m.group("id"),
                week_start_iso=m.group("week"),
            )
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="plan_week_menu",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 18. update_menu_item     `ok:updated:mpi_<id>` | `ok:cleared_or_not_found`
# ---------------------------------------------------------------------------


class UpdateMenuItemUpdated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["updated"] = "updated"
    item_id: MenuItemId


class UpdateMenuItemClearedOrNotFound(BaseModel):
    """Runtime emits this when the cell was cleared (both recipe_id
    AND free_text None) OR when the plan_id doesn't exist. Two
    semantics collapsed into one output — planner has to branch on
    context to disambiguate. Documented for the future split if
    needed."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["cleared_or_not_found"] = "cleared_or_not_found"


UpdateMenuItemOutput = Annotated[
    Union[UpdateMenuItemUpdated, UpdateMenuItemClearedOrNotFound, HousewifeToolError],
    Field(discriminator="status"),
]

_UPDATE_MENU_ITEM_RE = re.compile(r"^ok:updated:(?P<id>mpi_[^\s]+)$")


def parse_update_menu_item(
    raw: str,
) -> (
    UpdateMenuItemUpdated
    | UpdateMenuItemClearedOrNotFound
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "ok:cleared_or_not_found":
        return UpdateMenuItemClearedOrNotFound()
    m = _UPDATE_MENU_ITEM_RE.match(stripped)
    if m is not None:
        try:
            return UpdateMenuItemUpdated(item_id=m.group("id"))
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="update_menu_item",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 19. list_menu      `no menu plan for that week` | multiline grid
# ---------------------------------------------------------------------------


class ListMenuEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


class ListMenuOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    menu_id: MenuPlanId
    week_start_iso: str = Field(min_length=10)
    raw_text: str
    """Full multi-line grid as emitted by ``_iter_menu_plan_day_lines``.
    Composer templates can pull structured data later; for MVP the raw
    text is passed through verbatim."""


ListMenuOutput = Annotated[
    Union[ListMenuOk, ListMenuEmpty, HousewifeToolError],
    Field(discriminator="status"),
]

_LIST_MENU_ID_RE = re.compile(r"^menu_id:\s+(?P<id>menu_[^\s]+)$")
_LIST_MENU_WEEK_RE = re.compile(r"^week_start:\s+(?P<week>.+)$")


def parse_list_menu(
    raw: str,
) -> ListMenuOk | ListMenuEmpty | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "no menu plan for that week":
        return ListMenuEmpty()
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 2:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_menu",
            timestamp=datetime.now(timezone.utc),
        )
    id_match = _LIST_MENU_ID_RE.match(lines[0].strip())
    week_match = _LIST_MENU_WEEK_RE.match(lines[1].strip())
    if id_match is None or week_match is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_menu",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return ListMenuOk(
            menu_id=id_match.group("id"),
            week_start_iso=week_match.group("week"),
            raw_text=stripped,
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_menu",
            timestamp=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# 20. generate_shopping_from_menu  `ok:generated:N:eaters=E`
# ---------------------------------------------------------------------------


class GenerateShoppingFromMenuOk(BaseModel):
    """Happy-path shape: shopping items extracted from a menu plan.

    Codex Sub-A4 menu R3 MAJOR (gen_shopping split): the «empty» and
    «scaled-but-zero» cases were previously conflated. Now distinct:
    - ``status="generated"`` with ``generated_count>=0`` AND
      ``eaters>=1`` is the recipe-cells-existed-and-aggregated path.
      ``generated_count=0 + eaters>=1`` represents «recipes exist but
      conversion dropped everything (e.g. all «по вкусу»)» — the
      runtime emits ``ok:generated:0:eaters=E`` for this.
    - Free-text-only plan → ``GenerateShoppingFromMenuPlanNoRecipes``
      (separate variant; see below).
    - Unknown plan_id → ``HousewifeToolError`` with
      ``error_code='plan_not_found'``.

    Eaters invariant (two one-way implications, NOT a biconditional):
    - ``generated_count > 0`` ⟹ ``eaters >= 1`` (runtime always
      carries eaters when it did any work).
    - ``generated_count == 0 && eaters is None`` is currently allowed
      ONLY for legacy ``ok:generated:0`` outputs — the new runtime
      after R3 always emits ``:eaters=E`` for this shape. Once R3
      runtime is fully deployed, this combination becomes unreachable
      and the model could tighten further; for now the schema accepts
      both (None and >=1) when count==0 to keep legacy compatibility.

    The forbidden shape is ``generated_count > 0 && eaters is None``
    — that's malformed runtime output and raises at schema time.
    """

    model_config = ConfigDict(extra="forbid")
    status: Literal["generated"] = "generated"
    generated_count: int = Field(ge=0)
    eaters: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_eaters_invariant(self) -> "GenerateShoppingFromMenuOk":
        if self.generated_count > 0 and self.eaters is None:
            raise ValueError(
                f"generated_count={self.generated_count} but eaters is None "
                f"— non-zero counts MUST carry an eaters value. Only "
                f"generated_count==0 is allowed to have eaters=None "
                f"(legacy 'ok:generated:0' shape before R3 runtime split)."
            )
        return self


class GenerateShoppingFromMenuPlanNoRecipes(BaseModel):
    """Codex Sub-A4 menu R3 MAJOR (gen_shopping split): the plan
    exists but every cell is free_text — there are no recipe_id'd
    items to extract ingredients from. Runtime emits this distinct
    from ``ok:generated:0:eaters=E`` so composer can say «у этого
    меню нет сохранённых рецептов» rather than «покупок нет»."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["plan_no_recipes"] = "plan_no_recipes"


GenerateShoppingFromMenuOutput = Annotated[
    Union[
        GenerateShoppingFromMenuOk,
        GenerateShoppingFromMenuPlanNoRecipes,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]

_GEN_SHOPPING_RE = re.compile(
    r"^ok:generated:(?P<n>\d+)(?::eaters=(?P<e>\d+))?$"
)
"""Runtime shapes (R3 split — housewife_chat_tools.py:1474-1538):
- ``ok:generated:N:eaters=E`` — happy path with explicit eaters
  scaling (N may be 0 for empty-conversion).
- ``ok:generated:0`` — legacy shape; pre-R3 runtime emitted this
  for free_text-only / unknown-plan / empty cases. New R3 runtime
  no longer emits this (those cases go to ``ok:plan_no_recipes``
  or ``error:plan_not_found``), but parser keeps backward
  compatibility so in-flight outputs from old runtimes parse cleanly
  during deploy."""


def parse_generate_shopping_from_menu(
    raw: str,
) -> (
    GenerateShoppingFromMenuOk
    | GenerateShoppingFromMenuPlanNoRecipes
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "ok:plan_no_recipes":
        return GenerateShoppingFromMenuPlanNoRecipes()
    m = _GEN_SHOPPING_RE.match(stripped)
    if m is not None:
        eaters_raw = m.group("e")
        try:
            return GenerateShoppingFromMenuOk(
                generated_count=int(m.group("n")),
                eaters=int(eaters_raw) if eaters_raw is not None else None,
            )
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="generate_shopping_from_menu",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 21. clear_menu     `ok:cleared:N`
# ---------------------------------------------------------------------------


class ClearMenuOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["cleared"] = "cleared"
    cleared_count: int = Field(ge=0)


ClearMenuOutput = Annotated[
    Union[ClearMenuOk, HousewifeToolError],
    Field(discriminator="status"),
]

_CLEAR_MENU_RE = re.compile(r"^ok:cleared:(?P<n>\d+)$")


def parse_clear_menu(
    raw: str,
) -> ClearMenuOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _CLEAR_MENU_RE.match(raw.strip())
    if m is not None:
        return ClearMenuOk(cleared_count=int(m.group("n")))
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="clear_menu",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 22-25. Household family (Sub-A4 phase 5):
#         add_family_members / list_family_members /
#         update_family_member / remove_family_member
#
# Runtime shapes:
#   add:    "ok:added:0:skipped_as_duplicate:M"
#           "ok:added:N:skipped_as_duplicate:M:ids=[fm_...,...]"
#   list:   "no family members recorded"
#           "N member(s):\n  [fm_...] name (role, age) — notes\n..."
#   update: "ok:updated" / error
#   remove: "ok:removed" / error
# ---------------------------------------------------------------------------


class AddFamilyMembersOk(BaseModel):
    """Codex Sub-A4 household R1 CRITICAL #1 + Acceptable Alternative:
    collapsed shape (was AddFamilyMembersAdded + AddFamilyMembersAllDuplicate
    — both with status='added' which broke the Pydantic v2 discriminator
    union requirement). One model now covers both runtime shapes:

    - ``ok:added:N:skipped_as_duplicate:M:ids=[fm_,...]`` (N >= 1)
      → ``added_count=N, member_ids=[N items]``
    - ``ok:added:0:skipped_as_duplicate:M`` (no ids segment, M >= 1)
      → ``added_count=0, member_ids=[]``

    Planner branches on ``added_count == 0`` to say «эти уже есть»
    vs «добавила N (M уже было)». Cross-field invariant:
    ``len(member_ids) == added_count``."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["added"] = "added"
    added_count: int = Field(ge=0)
    skipped_as_duplicate: int = Field(ge=0)
    member_ids: list[FamilyMemberId]

    @model_validator(mode="after")
    def _validate_count_matches_ids(self) -> "AddFamilyMembersOk":
        if len(self.member_ids) != self.added_count:
            raise ValueError(
                f"added_count={self.added_count} but member_ids has "
                f"{len(self.member_ids)} entries — mismatch."
            )
        if self.added_count == 0 and self.skipped_as_duplicate == 0:
            # Runtime never emits both zeros (empty batch is rejected
            # upstream with error: empty batch). Reject the malformed
            # shape so the executor fail-closes.
            raise ValueError(
                "added_count=0 AND skipped_as_duplicate=0 — runtime "
                "never emits this shape (empty batch returns "
                "'error: empty batch' instead)."
            )
        return self


AddFamilyMembersOutput = Annotated[
    Union[
        AddFamilyMembersOk,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]

# Two runtime shapes:
#   ok:added:0:skipped_as_duplicate:M             — all-duplicate batch
#   ok:added:N:skipped_as_duplicate:M:ids=[fm_,...] — happy path
_ADD_FAMILY_RE = re.compile(
    r"^ok:added:(?P<n>\d+):skipped_as_duplicate:(?P<m>\d+)"
    r"(?::ids=\[(?P<ids>[^\]]*)\])?$"
)


def parse_add_family_members(
    raw: str,
) -> AddFamilyMembersOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ADD_FAMILY_RE.match(raw.strip())
    if m is not None:
        n = int(m.group("n"))
        skipped = int(m.group("m"))
        ids_csv = m.group("ids") or ""
        ids = [x.strip() for x in ids_csv.split(",") if x.strip()]
        # Codex Sub-A4 household R1 CRITICAL #1: pre-check the
        # «zero count + ids» drift before construction. The model
        # validator catches it via count/ids mismatch, but emitting
        # ContractViolation directly is faster and gives a clearer
        # tool_name in the gap log.
        if n == 0 and ids:
            return ToolOutputContractViolation(
                raw_output=raw,
                tool_name="add_family_members",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            return AddFamilyMembersOk(
                added_count=n,
                skipped_as_duplicate=skipped,
                member_ids=ids,
            )
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="add_family_members",
        timestamp=datetime.now(timezone.utc),
    )


class ListFamilyMembersRow(BaseModel):
    """One row in the structured list_family_members output.

    Codex Sub-A4 household R1 MAJOR #7: household is ID-driven —
    update_family_member / remove_family_member both need an
    ``fm_<24 hex>`` id. Pre-R2 the list returned only ``raw_text``
    forcing the planner to parse «[fm_X] name (role, age) — notes»
    from prose. Now the parser deconstructs the dump into typed
    rows so the planner refers to ``${list_step.members[i].member_id}``
    directly.

    ``age_text`` is the free-form age blob from the dump («8 лет»
    or «школьник») — keeps the originally-recorded distinction
    between birth_year-derived ages and age_hint without forcing
    the planner to disambiguate at this layer."""

    model_config = ConfigDict(extra="forbid")
    member_id: FamilyMemberId
    name: str = Field(min_length=1, max_length=200)
    role: Literal["self", "spouse", "child", "parent", "other"]
    age_text: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=500)
    """Codex Sub-A4 household R2 MAJOR (new): caps widened to match
    runtime truncation (housewife_family.py:99,102,103). Pre-R2
    schema rejected legacy/Mini-App rows longer than 80/40/300 —
    that would block list_family_members for affected tenants and
    deny update/remove since the planner has no other source of
    member_id."""


class ListFamilyMembersOk(BaseModel):
    """Structured list of household members (R1 MAJOR #7 promotion
    from raw_text). ``members`` always non-empty here — the
    «no family members recorded» path goes to
    ``ListFamilyMembersEmpty``."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    members: list[ListFamilyMembersRow] = Field(min_length=1)


class ListFamilyMembersEmpty(BaseModel):
    """Distinct variant for the «no members recorded» path so the
    planner branches cleanly («у тебя пока нет записанной семьи —
    хочешь добавить?» vs «вот члены семьи»)."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


ListFamilyMembersOutput = Annotated[
    Union[ListFamilyMembersOk, ListFamilyMembersEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


# Runtime list rendering at housewife_chat_tools.py:1626-1640. Per-row format:
#   "  [fm_<24hex>] <name> (<role>, <age_text>) — <notes>"
#   age_text and "— notes" are optional segments.
_LIST_FAMILY_HEADER_RE = re.compile(r"^(?P<n>[1-9]\d*)\s+member\(s\):$")
_LIST_FAMILY_ROW_RE = re.compile(
    r"^\s+\[(?P<id>fm_[0-9a-f]{24})\]\s+(?P<name>[^()\n]+?)\s+\("
    r"(?P<role>self|spouse|child|parent|other)"
    r"(?:,\s+(?P<age>[^)]+))?"
    r"\)"
    r"(?:\s+—\s+(?P<notes>.+))?$"
)


def parse_list_family_members(
    raw: str,
) -> (
    ListFamilyMembersOk
    | ListFamilyMembersEmpty
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "no family members recorded":
        return ListFamilyMembersEmpty()
    # Codex Sub-A4 household R1 MINOR #1: tighten header regex to
    # reject ``0 member(s):`` — runtime emits the empty-string shape
    # «no family members recorded» for zero count, so a numeric 0
    # header is contract drift.
    lines = stripped.splitlines()
    if not lines:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_family_members",
            timestamp=datetime.now(timezone.utc),
        )
    header_match = _LIST_FAMILY_HEADER_RE.match(lines[0].strip())
    if header_match is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_family_members",
            timestamp=datetime.now(timezone.utc),
        )
    expected_count = int(header_match.group("n"))
    rows: list[ListFamilyMembersRow] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        rmatch = _LIST_FAMILY_ROW_RE.match(line)
        if rmatch is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_family_members",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            rows.append(
                ListFamilyMembersRow(
                    member_id=rmatch.group("id"),
                    name=rmatch.group("name").strip(),
                    role=rmatch.group("role"),
                    age_text=(
                        rmatch.group("age").strip()
                        if rmatch.group("age") else None
                    ),
                    notes=(
                        rmatch.group("notes").strip()
                        if rmatch.group("notes") else None
                    ),
                )
            )
        except ValidationError:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_family_members",
                timestamp=datetime.now(timezone.utc),
            )
    if len(rows) != expected_count:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_family_members",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return ListFamilyMembersOk(members=rows)
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_family_members",
            timestamp=datetime.now(timezone.utc),
        )


class UpdateFamilyMemberOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["updated"] = "updated"


UpdateFamilyMemberOutput = Annotated[
    Union[UpdateFamilyMemberOk, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_update_family_member(
    raw: str,
) -> UpdateFamilyMemberOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    if raw.strip() == "ok:updated":
        return UpdateFamilyMemberOk()
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="update_family_member",
        timestamp=datetime.now(timezone.utc),
    )


class RemoveFamilyMemberOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["removed"] = "removed"


RemoveFamilyMemberOutput = Annotated[
    Union[RemoveFamilyMemberOk, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_remove_family_member(
    raw: str,
) -> RemoveFamilyMemberOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    if raw.strip() == "ok:removed":
        return RemoveFamilyMemberOk()
    return ToolOutputContractViolation(
        raw_output=raw,
        tool_name="remove_family_member",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 26-36. Tasks family (Sub-A4 phase 6) — 11 tools:
#         add_task / list_tasks / update_task /
#         complete_task / uncomplete_task / cancel_task / delete_task /
#         attach_reminder / detach_reminder /
#         link_task_to_checklist / unlink_task
#
# Runtime shapes (see housewife_chat_tools.py:1789-2287):
#
#   add_task:
#     "ok:created:{task_id}"
#     "ok:created:{task_id}:reminder=за Nмин"
#     "ok:created:{task_id}:checklist={checklist_id}"
#     "error: reminder requires scheduled_date + time_start; ..."
#     "error: reminder_offset_minutes не поддерживается вместе с details_items. ..."
#     "error:<ValueError>"
#     "error: internal"
#
#   list_tasks: "no tasks" OR multi-line "_fmt_task_for_llm" dump
#
#   update_task: "ok:updated:{task_id}" OR "error: task '...' not found"
#
#   complete/uncomplete/cancel: "ok:{status}:{task_id}" OR not_found
#   delete_task: "ok:deleted" OR not_found
#
#   attach_reminder:
#     "ok:reminder_attached:{reminder_id}:за Nмин"
#     "error: offset_minutes must be a positive integer"
#     "error:<ValueError>"  (no schedule, etc.)
#     "error: task '...' not found"
#
#   detach_reminder: "ok:reminder_detached" OR not_found
#
#   link_task_to_checklist:
#     "ok:linked:{task_id}:{checklist_id}"
#     "ok:already_linked:{task_id}:{checklist_id}"
#     "error: task_already_linked:{task_id}:{other_checklist_id}. ..."
#     "error: checklist_already_linked_to_task_{other_task_id}. ..."
#     "error: not found" / "error: archived" / "error: internal"
#
#   unlink_task:
#     "ok:unlinked:{task_id}:{checklist_id}"
#     "ok:not_linked:{task_id}"
#     "error: task '...' not found"
# ---------------------------------------------------------------------------


# 26. add_task
class AddTaskCreated(BaseModel):
    """Plain happy path — no reminder, no checklist (legacy code path)."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["created"] = "created"
    task_id: TaskId


class AddTaskCreatedWithReminder(BaseModel):
    """Happy path with auto-attached reminder. ``reminder_offset_minutes``
    is the integer the planner supplied — runtime echoes it back in the
    «за Nмин» segment which the parser extracts."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["created_with_reminder"] = "created_with_reminder"
    task_id: TaskId
    reminder_offset_minutes: int = Field(ge=1)


class AddTaskCreatedWithChecklist(BaseModel):
    """R-33 details_items composite path — fresh Checklist created in
    same transaction. ``checklist_id`` is the new checklist's id so
    the planner can reference it in subsequent calls."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["created_with_checklist"] = "created_with_checklist"
    task_id: TaskId
    checklist_id: ChecklistId


AddTaskOutput = Annotated[
    Union[
        AddTaskCreated,
        AddTaskCreatedWithReminder,
        AddTaskCreatedWithChecklist,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]


_ADD_TASK_RE = re.compile(
    r"^ok:created:(?P<task_id>task_[0-9a-f]{24})"
    r"(?::reminder=за\s+(?P<rmins>\d+)мин"
    r"|:checklist=(?P<chk>checklist_[0-9a-f]{24}))?$"
)


def parse_add_task(
    raw: str,
) -> (
    AddTaskCreated
    | AddTaskCreatedWithReminder
    | AddTaskCreatedWithChecklist
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ADD_TASK_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="add_task",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        task_id = m.group("task_id")
        rmins = m.group("rmins")
        chk = m.group("chk")
        if rmins is not None:
            return AddTaskCreatedWithReminder(
                task_id=task_id, reminder_offset_minutes=int(rmins),
            )
        if chk is not None:
            return AddTaskCreatedWithChecklist(task_id=task_id, checklist_id=chk)
        return AddTaskCreated(task_id=task_id)
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="add_task",
            timestamp=datetime.now(timezone.utc),
        )


# 27. list_tasks


class ListTasksRow(BaseModel):
    """Codex Sub-A4 tasks R1 MAJOR #3: structured row replacing
    raw_text. Tasks are ID-driven (update_task / complete_task /
    cancel_task / delete_task / attach_reminder / detach_reminder
    / link_task_to_checklist / unlink_task — 8 of 11 tools take
    task_id). Pre-R2 raw_text forced the planner to parse the
    «[task_id] title · on YYYY-MM-DD HH:MM ...» prose to extract
    task_id. Now parser decomposes ``_fmt_task_for_llm`` output
    (housewife_chat_tools.py:1758-1787) into typed fields so the
    planner refers to ``${list_tasks.tasks[i].task_id}`` directly.

    Optional fields mirror runtime emissions:
    - ``scheduled_date_iso`` — ISO date string when task has a date
    - ``time_start`` / ``time_end`` — HH:MM strings when present
    - ``recurrence_rule`` — RFC 5545 RRULE string when task recurs
    - ``reminder_offset_minutes`` — int when task has linked reminder
    - ``runtime_status`` — «completed»/«cancelled» (non-«pending»);
      «pending» is implicit when omitted
    - ``notes`` — free-form prose
    """

    model_config = ConfigDict(extra="forbid")
    task_id: TaskId
    title: str = Field(min_length=1, max_length=500)
    scheduled_date_iso: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    time_start: str | None = Field(
        default=None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    time_end: str | None = Field(
        default=None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    recurrence_rule: str | None = Field(default=None, max_length=255)
    reminder_offset_minutes: int | None = Field(default=None, ge=0)
    runtime_status: Literal["completed", "cancelled"] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ListTasksEmpty(BaseModel):
    """Empty path: «no tasks» — planner branches to «нет задач на этот
    день» / «inbox пустой»."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


class ListTasksOk(BaseModel):
    """Structured list of tasks (R1 MAJOR #3 promotion).

    Codex Sub-A4 tasks R2 MAJOR (new): no ``min_length=1`` on
    ``tasks``. Runtime currently routes empty rows to the «no tasks»
    string (parsed as ``ListTasksEmpty``), but defensive contract
    allows an empty list here too — future runtime drift that emits
    «N member(s):» with N=0 (analog of household R1 MINOR #1) would
    still construct cleanly. The parser's job is to disambiguate
    `ListTasksEmpty` vs `ListTasksOk` via the «no tasks» literal."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    tasks: list[ListTasksRow] = Field(default_factory=list)


ListTasksOutput = Annotated[
    Union[ListTasksOk, ListTasksEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


# _fmt_task_for_llm format (housewife_chat_tools.py:1758-1787):
#   [task_id] title [· on YYYY-MM-DD [HH:MM[–HH:MM]]] [· recurring=RRULE]
#   [· reminder=за Nмин] [· status=X] [· notes=Y]
# Segments separated by ` · `. First two are positional (id-bracket
# + title). Rest are prefixed key=value or `on ...`.

_LIST_TASKS_ID_RE = re.compile(r"^\[(?P<task_id>task_[0-9a-f]{24})\]$")
_LIST_TASKS_NOTES_SEP = " · notes="
_LIST_TASKS_SUFFIX_PREFIXES = ("on ", "recurring=", "reminder=за ", "status=")


def _parse_list_tasks_row(line: str) -> ListTasksRow | None:
    """Decompose one ``_fmt_task_for_llm`` line into a typed row.
    Returns ``None`` on malformed input so caller fail-closes via
    ContractViolation.

    Codex Sub-A4 tasks R2 MAJOR (new): naive ``line.split(' · ')``
    breaks when title or notes contain ` · ` (Russian middot is a
    legitimate user character). Robust two-phase approach:

    1. Extract trailing notes by splitting on the FIRST occurrence
       of ` · notes=` — runtime always emits notes last and
       everything after `notes=` is free-form (could itself contain
       ` · `).
    2. Split the remaining prefix on ` · ` to extract id (position 0)
       + title and known suffix segments. Walk left-to-right
       consuming known prefixes; everything between id and the
       first known prefix = title (joined back with ` · ` to
       preserve user input that contained the separator).
    """
    # Phase 1: split off notes from the right.
    notes: str | None = None
    notes_split_idx = line.find(_LIST_TASKS_NOTES_SEP)
    if notes_split_idx >= 0:
        prefix_part = line[:notes_split_idx]
        notes = line[notes_split_idx + len(_LIST_TASKS_NOTES_SEP):].strip()
        if not notes:
            return None
    else:
        prefix_part = line

    parts = [p.strip() for p in prefix_part.split(" · ")]
    if len(parts) < 2:
        return None
    id_match = _LIST_TASKS_ID_RE.match(parts[0])
    if id_match is None:
        return None
    task_id = id_match.group("task_id")

    # Phase 2: walk from position 1 onward; accumulate title until
    # we hit the first known suffix prefix, then parse suffixes.
    scheduled_date_iso: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    recurrence_rule: str | None = None
    reminder_offset_minutes: int | None = None
    runtime_status: str | None = None

    title_segments: list[str] = []
    suffix_start: int | None = None
    for idx, segment in enumerate(parts[1:], start=1):
        if any(segment.startswith(pfx) for pfx in _LIST_TASKS_SUFFIX_PREFIXES):
            suffix_start = idx
            break
        title_segments.append(segment)

    if not title_segments:
        return None
    title = " · ".join(title_segments)
    if len(title) > 500:
        return None

    if suffix_start is not None:
        for segment in parts[suffix_start:]:
            if segment.startswith("on "):
                when = segment[len("on "):].strip()
                when_parts = when.split(" ")
                if not when_parts:
                    return None
                head = when_parts[0]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", head):
                    scheduled_date_iso = head
                    time_token = when_parts[1] if len(when_parts) > 1 else None
                elif re.match(r"^([01]\d|2[0-3]):[0-5]\d", head):
                    time_token = head
                else:
                    return None
                if time_token is not None:
                    if "–" in time_token:
                        t_parts = time_token.split("–")
                        if len(t_parts) != 2:
                            return None
                        time_start, time_end = t_parts[0], t_parts[1]
                    else:
                        time_start = time_token
            elif segment.startswith("recurring="):
                recurrence_rule = segment[len("recurring="):].strip()
            elif segment.startswith("reminder=за "):
                rest = segment[len("reminder=за "):].strip()
                if not rest.endswith("мин"):
                    return None
                try:
                    reminder_offset_minutes = int(rest[:-len("мин")].strip())
                except ValueError:
                    return None
            elif segment.startswith("status="):
                value = segment[len("status="):].strip()
                if value not in {"completed", "cancelled"}:
                    return None
                runtime_status = value
            else:
                # Already filtered by prefix-match above; any
                # post-suffix segment that doesn't match a known
                # prefix means runtime drift — fail closed.
                return None

    try:
        return ListTasksRow(
            task_id=task_id,
            title=title,
            scheduled_date_iso=scheduled_date_iso,
            time_start=time_start,
            time_end=time_end,
            recurrence_rule=recurrence_rule,
            reminder_offset_minutes=reminder_offset_minutes,
            runtime_status=runtime_status,
            notes=notes,
        )
    except ValidationError:
        return None


def parse_list_tasks(
    raw: str,
) -> ListTasksOk | ListTasksEmpty | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "no tasks":
        return ListTasksEmpty()
    if not stripped:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_tasks",
            timestamp=datetime.now(timezone.utc),
        )
    # Defensive: ok:/error: prefix means runtime is emitting a status
    # token where a task dump is expected — drift.
    if stripped.startswith("ok:") or stripped.startswith("error:"):
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_tasks",
            timestamp=datetime.now(timezone.utc),
        )
    rows: list[ListTasksRow] = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        row = _parse_list_tasks_row(line)
        if row is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_tasks",
                timestamp=datetime.now(timezone.utc),
            )
        rows.append(row)
    if not rows:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_tasks",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return ListTasksOk(tasks=rows)
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_tasks",
            timestamp=datetime.now(timezone.utc),
        )


# 28. update_task
class UpdateTaskOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["updated"] = "updated"
    task_id: TaskId


UpdateTaskOutput = Annotated[
    Union[UpdateTaskOk, HousewifeToolError],
    Field(discriminator="status"),
]


_UPDATE_TASK_RE = re.compile(r"^ok:updated:(?P<task_id>task_[0-9a-f]{24})$")


def parse_update_task(
    raw: str,
) -> UpdateTaskOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _UPDATE_TASK_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="update_task",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return UpdateTaskOk(task_id=m.group("task_id"))
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="update_task",
            timestamp=datetime.now(timezone.utc),
        )


# 29-32. complete_task / uncomplete_task / cancel_task / delete_task
#       All share ``ok:<verb>:{task_id}`` shape EXCEPT delete which
#       emits just ``ok:deleted`` without an id (delete returns bool).


class CompleteTaskOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed"] = "completed"
    task_id: TaskId


CompleteTaskOutput = Annotated[
    Union[CompleteTaskOk, HousewifeToolError],
    Field(discriminator="status"),
]


_COMPLETE_TASK_RE = re.compile(r"^ok:completed:(?P<task_id>task_[0-9a-f]{24})$")


def parse_complete_task(
    raw: str,
) -> CompleteTaskOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _COMPLETE_TASK_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="complete_task",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return CompleteTaskOk(task_id=m.group("task_id"))
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="complete_task",
            timestamp=datetime.now(timezone.utc),
        )


class UncompleteTaskOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["uncompleted"] = "uncompleted"
    task_id: TaskId


UncompleteTaskOutput = Annotated[
    Union[UncompleteTaskOk, HousewifeToolError],
    Field(discriminator="status"),
]


_UNCOMPLETE_TASK_RE = re.compile(r"^ok:uncompleted:(?P<task_id>task_[0-9a-f]{24})$")


def parse_uncomplete_task(
    raw: str,
) -> UncompleteTaskOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _UNCOMPLETE_TASK_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="uncomplete_task",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return UncompleteTaskOk(task_id=m.group("task_id"))
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="uncomplete_task",
            timestamp=datetime.now(timezone.utc),
        )


class CancelTaskOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["cancelled"] = "cancelled"
    task_id: TaskId


CancelTaskOutput = Annotated[
    Union[CancelTaskOk, HousewifeToolError],
    Field(discriminator="status"),
]


_CANCEL_TASK_RE = re.compile(r"^ok:cancelled:(?P<task_id>task_[0-9a-f]{24})$")


def parse_cancel_task(
    raw: str,
) -> CancelTaskOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _CANCEL_TASK_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="cancel_task",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return CancelTaskOk(task_id=m.group("task_id"))
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="cancel_task",
            timestamp=datetime.now(timezone.utc),
        )


class DeleteTaskOk(BaseModel):
    """Delete differs from complete/cancel/uncomplete — runtime emits
    just ``ok:deleted`` without a task_id (the row is gone, the id no
    longer references anything addressable). Planner branches off
    ``status == 'deleted'`` and doesn't need the id back."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["deleted"] = "deleted"


DeleteTaskOutput = Annotated[
    Union[DeleteTaskOk, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_delete_task(
    raw: str,
) -> DeleteTaskOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    if raw.strip() == "ok:deleted":
        return DeleteTaskOk()
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="delete_task",
        timestamp=datetime.now(timezone.utc),
    )


# 33. attach_reminder
class AttachReminderOk(BaseModel):
    """Reminder created and linked to task. Runtime echoes the
    reminder_id and the offset back so the planner can refer to
    either later."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["reminder_attached"] = "reminder_attached"
    reminder_id: ReminderId
    offset_minutes: int = Field(ge=1)


AttachReminderOutput = Annotated[
    Union[AttachReminderOk, HousewifeToolError],
    Field(discriminator="status"),
]


_ATTACH_REMINDER_RE = re.compile(
    r"^ok:reminder_attached:(?P<rem_id>rem_[0-9a-f]{24}):за\s+(?P<mins>\d+)мин$"
)


def parse_attach_reminder(
    raw: str,
) -> AttachReminderOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ATTACH_REMINDER_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="attach_reminder",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return AttachReminderOk(
            reminder_id=m.group("rem_id"),
            offset_minutes=int(m.group("mins")),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="attach_reminder",
            timestamp=datetime.now(timezone.utc),
        )


# 34. detach_reminder
class DetachReminderOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["reminder_detached"] = "reminder_detached"


DetachReminderOutput = Annotated[
    Union[DetachReminderOk, HousewifeToolError],
    Field(discriminator="status"),
]


def parse_detach_reminder(
    raw: str,
) -> DetachReminderOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    if raw.strip() == "ok:reminder_detached":
        return DetachReminderOk()
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="detach_reminder",
        timestamp=datetime.now(timezone.utc),
    )


# 35. link_task_to_checklist
class LinkTaskToChecklistLinked(BaseModel):
    """Newly linked task↔checklist pair."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["linked"] = "linked"
    task_id: TaskId
    checklist_id: ChecklistId


class LinkTaskToChecklistAlreadyLinked(BaseModel):
    """Idempotent re-link — same pair was already linked."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["already_linked"] = "already_linked"
    task_id: TaskId
    checklist_id: ChecklistId


LinkTaskToChecklistOutput = Annotated[
    Union[
        LinkTaskToChecklistLinked,
        LinkTaskToChecklistAlreadyLinked,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]


_LINK_TASK_RE = re.compile(
    r"^ok:(?P<status>linked|already_linked):"
    r"(?P<task_id>task_[0-9a-f]{24}):"
    r"(?P<checklist_id>checklist_[0-9a-f]{24})$"
)


def parse_link_task_to_checklist(
    raw: str,
) -> (
    LinkTaskToChecklistLinked
    | LinkTaskToChecklistAlreadyLinked
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        # Codex Sub-A4 tasks R2 MAJOR (prior-not-closed +
        # new-introduced): tool-scoped remap of generic «not_found:*»
        # / «archived:*» catch-alls from line 2248 runtime
        # (`return f"{status}: {info or ''}"`). Split task vs
        # checklist distinction so planner can branch to relist
        # the right collection. NOT in global _STABLE_ERROR_PATTERNS
        # because other tools' messages must not be remapped.
        if err.error_code == "unknown" or err.error_code.startswith("not_found"):
            msg_lower = err.message.lower()
            if "task" in msg_lower and "checklist" not in msg_lower:
                return HousewifeToolError(
                    error_code="link_task_not_found", message=err.message,
                )
            if "checklist" in msg_lower and "task" not in msg_lower:
                return HousewifeToolError(
                    error_code="link_checklist_not_found", message=err.message,
                )
            if err.error_code.startswith("not_found") or (
                "task" in msg_lower and "checklist" in msg_lower
            ):
                # «not_found:» without disambiguation, or both names
                # present — keep ambiguous but stable.
                return HousewifeToolError(
                    error_code="link_target_not_found", message=err.message,
                )
        if err.error_code == "archived" or (
            err.error_code == "unknown"
            and err.message.lower().startswith("archived")
        ):
            return HousewifeToolError(
                error_code="checklist_archived", message=err.message,
            )
        return err
    m = _LINK_TASK_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="link_task_to_checklist",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        if m.group("status") == "linked":
            return LinkTaskToChecklistLinked(
                task_id=m.group("task_id"),
                checklist_id=m.group("checklist_id"),
            )
        return LinkTaskToChecklistAlreadyLinked(
            task_id=m.group("task_id"),
            checklist_id=m.group("checklist_id"),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="link_task_to_checklist",
            timestamp=datetime.now(timezone.utc),
        )


# 36. unlink_task
class UnlinkTaskUnlinked(BaseModel):
    """Was linked, now unlinked."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["unlinked"] = "unlinked"
    task_id: TaskId
    checklist_id: ChecklistId


class UnlinkTaskNotLinked(BaseModel):
    """Idempotent: task wasn't linked to any checklist. Planner
    treats both states as success."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["not_linked"] = "not_linked"
    task_id: TaskId


UnlinkTaskOutput = Annotated[
    Union[
        UnlinkTaskUnlinked,
        UnlinkTaskNotLinked,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]


_UNLINK_TASK_UNLINKED_RE = re.compile(
    r"^ok:unlinked:(?P<task_id>task_[0-9a-f]{24}):"
    r"(?P<checklist_id>checklist_[0-9a-f]{24})$"
)
_UNLINK_TASK_NOT_LINKED_RE = re.compile(
    r"^ok:not_linked:(?P<task_id>task_[0-9a-f]{24})$"
)


def parse_unlink_task(
    raw: str,
) -> (
    UnlinkTaskUnlinked
    | UnlinkTaskNotLinked
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    m_unlinked = _UNLINK_TASK_UNLINKED_RE.match(stripped)
    if m_unlinked is not None:
        try:
            return UnlinkTaskUnlinked(
                task_id=m_unlinked.group("task_id"),
                checklist_id=m_unlinked.group("checklist_id"),
            )
        except ValidationError:
            pass
    m_not_linked = _UNLINK_TASK_NOT_LINKED_RE.match(stripped)
    if m_not_linked is not None:
        try:
            return UnlinkTaskNotLinked(task_id=m_not_linked.group("task_id"))
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="unlink_task",
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 37-44. Checklists family (Sub-A4 phase 7) — 8 tools:
#         create_checklist / add_checklist_items / move_task_to_checklist /
#         list_checklists / show_checklist /
#         mark_checklist_item_done / delete_checklist_item / archive_checklist
#
# Runtime shapes (housewife_chat_tools.py:2368-2698):
#
#   create_checklist:
#     "ok:created:{checklist_id}:{title}"
#     error:<msg>
#
#   add_checklist_items:
#     "ok:added:N:list={checklist_id}"
#     "ok:added:N:dups:M:list={checklist_id}"
#     "error: empty items"
#     error:<msg>
#
#   move_task_to_checklist:
#     "ok:moved:item_id={clitem_id}:list={checklist_id}"
#     "ok:moved:item_id=existing:list={checklist_id}:dup"  (idempotent)
#     "error: task_not_found"
#     "error: task_has_empty_title"
#     "error: list_resolve_failed"
#     "error: nothing_added"
#     "error: internal" / "error: internal_cancel" / "error: internal_add"
#
#   list_checklists:
#     "no checklists"  (empty)
#     multi-line "[{id}] · {title} · {p} pending, {d} done, {t} total"
#
#   show_checklist:
#     "error: not_found: '<needle>'"  (list not found)
#     "empty: list={id} title='<title>'"  (list exists but no items)
#     "# {title} ({id})\n[{item_id}] ☐ {title}\n..."  (populated)
#
#   mark_checklist_item_done:
#     "ok:done:{clitem_id}:{title}"
#     "error: list_not_found: '<needle>'"
#     "error: item_not_found: '<needle>'"
#
#   delete_checklist_item:
#     "ok:deleted:{clitem_id}:{title}"
#     "error: list_not_found: '<needle>'"
#     "error: item_not_found: '<needle>'"
#
#   archive_checklist:
#     "ok:archived:{checklist_id}"
#     "error: not_found: '<needle>'"
# ---------------------------------------------------------------------------


# 37. create_checklist
class CreateChecklistOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["created"] = "created"
    checklist_id: ChecklistId
    title: str = Field(min_length=1, max_length=500)


CreateChecklistOutput = Annotated[
    Union[CreateChecklistOk, HousewifeToolError],
    Field(discriminator="status"),
]


_CREATE_CHECKLIST_RE = re.compile(
    r"^ok:created:(?P<cid>checklist_[0-9a-f]{24}):(?P<title>.+)$"
)


def parse_create_checklist(
    raw: str,
) -> CreateChecklistOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _CREATE_CHECKLIST_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="create_checklist",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return CreateChecklistOk(
            checklist_id=m.group("cid"),
            title=m.group("title").strip(),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="create_checklist",
            timestamp=datetime.now(timezone.utc),
        )


# 38. add_checklist_items
class AddChecklistItemsOk(BaseModel):
    """Variant 1: ``ok:added:N:list=<id>`` (no dups segment).
    All items new, no duplicates skipped."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["added"] = "added"
    added_count: int = Field(ge=0)
    duplicate_count: int = 0
    checklist_id: ChecklistId


class AddChecklistItemsWithDups(BaseModel):
    """Variant 2: ``ok:added:N:dups:M:list=<id>``. Some items
    matched existing pending items in the list — runtime
    dedup-skipped them."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["added_with_dups"] = "added_with_dups"
    added_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=1)
    checklist_id: ChecklistId


AddChecklistItemsOutput = Annotated[
    Union[
        AddChecklistItemsOk,
        AddChecklistItemsWithDups,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]


_ADD_CHECKLIST_ITEMS_NODUPS_RE = re.compile(
    r"^ok:added:(?P<n>\d+):list=(?P<cid>checklist_[0-9a-f]{24})$"
)
_ADD_CHECKLIST_ITEMS_WITHDUPS_RE = re.compile(
    r"^ok:added:(?P<n>\d+):dups:(?P<m>\d+):"
    r"list=(?P<cid>checklist_[0-9a-f]{24})$"
)


def parse_add_checklist_items(
    raw: str,
) -> (
    AddChecklistItemsOk
    | AddChecklistItemsWithDups
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    m_with = _ADD_CHECKLIST_ITEMS_WITHDUPS_RE.match(stripped)
    if m_with is not None:
        try:
            return AddChecklistItemsWithDups(
                added_count=int(m_with.group("n")),
                duplicate_count=int(m_with.group("m")),
                checklist_id=m_with.group("cid"),
            )
        except ValidationError:
            pass
    m_no = _ADD_CHECKLIST_ITEMS_NODUPS_RE.match(stripped)
    if m_no is not None:
        try:
            return AddChecklistItemsOk(
                added_count=int(m_no.group("n")),
                checklist_id=m_no.group("cid"),
            )
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="add_checklist_items",
        timestamp=datetime.now(timezone.utc),
    )


# 39. move_task_to_checklist
class MoveTaskMovedOk(BaseModel):
    """Happy path: task cancelled + new checklist item created."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["moved"] = "moved"
    item_id: ChecklistItemId
    checklist_id: ChecklistId


class MoveTaskMovedDup(BaseModel):
    """Idempotent path: task cancelled but checklist already had
    a matching item — no new item created. ``item_id`` is the
    sentinel literal ``"existing"`` (not a real clitem_<24 hex>
    because runtime doesn't echo the matched item's id, only
    that one existed)."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["moved_dup"] = "moved_dup"
    checklist_id: ChecklistId


class MoveTaskPartialFailure(BaseModel):
    """Codex Sub-A4 checklists R3 MINOR: typed partial-failure
    variant — runtime is NOT atomic (see MOVE_TASK_TO_CHECKLIST_SPEC
    description); ``task_service.cancel`` commits first, then
    checklist writes commit separately. If the post-cancel step
    fails, the task is already cancelled with no rollback.

    Parser detects four specific runtime error codes that mean
    «task cancelled but item not created»:
    - ``task_has_empty_title`` — cancel succeeded but title was
      empty (edge case after cancel, housewife_chat_tools.py:2516)
    - ``list_resolve_failed`` — create_list raised after cancel
    - ``internal_add`` — add_items raised after cancel
    - ``nothing_added`` — neither created nor matched existing
      (edge case where items list was unexpectedly empty)

    Planner branches on this status to honestly report «задача
    отменена, но в чек-лист не добавилась — повтори
    add_checklist_items вручную» instead of «не получилось».
    """
    model_config = ConfigDict(extra="forbid")
    status: Literal["partial_failure"] = "partial_failure"
    error_code: Literal[
        "task_has_empty_title",
        "list_resolve_failed",
        "internal_add",
        "nothing_added",
    ]
    message: str = Field(min_length=1)


MoveTaskToChecklistOutput = Annotated[
    Union[
        MoveTaskMovedOk,
        MoveTaskMovedDup,
        MoveTaskPartialFailure,
        HousewifeToolError,
    ],
    Field(discriminator="status"),
]


_MOVE_TASK_NEW_RE = re.compile(
    r"^ok:moved:item_id=(?P<item_id>clitem_[0-9a-f]{24}):"
    r"list=(?P<cid>checklist_[0-9a-f]{24})$"
)
_MOVE_TASK_DUP_RE = re.compile(
    r"^ok:moved:item_id=existing:list=(?P<cid>checklist_[0-9a-f]{24}):dup$"
)


_MOVE_TASK_PARTIAL_FAILURE_CODES = frozenset({
    "task_has_empty_title",
    "list_resolve_failed",
    "internal_add",
    "nothing_added",
})


def parse_move_task_to_checklist(
    raw: str,
) -> (
    MoveTaskMovedOk
    | MoveTaskMovedDup
    | MoveTaskPartialFailure
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        # Codex Sub-A4 checklists R3 MINOR: typed partial-failure
        # path. Three error codes from housewife_chat_tools.py:2532,
        # 2541, 2548 all mean «cancel committed but item not created».
        if err.error_code in _MOVE_TASK_PARTIAL_FAILURE_CODES:
            try:
                return MoveTaskPartialFailure(
                    error_code=err.error_code,
                    message=err.message,
                )
            except ValidationError:
                pass
        return err
    stripped = raw.strip()
    m_new = _MOVE_TASK_NEW_RE.match(stripped)
    if m_new is not None:
        try:
            return MoveTaskMovedOk(
                item_id=m_new.group("item_id"),
                checklist_id=m_new.group("cid"),
            )
        except ValidationError:
            pass
    m_dup = _MOVE_TASK_DUP_RE.match(stripped)
    if m_dup is not None:
        try:
            return MoveTaskMovedDup(checklist_id=m_dup.group("cid"))
        except ValidationError:
            pass
    return ToolOutputContractViolation(
        raw_output=raw, tool_name="move_task_to_checklist",
        timestamp=datetime.now(timezone.utc),
    )


# 40. list_checklists
class ListChecklistsRow(BaseModel):
    """One checklist line in ``list_checklists`` output."""
    model_config = ConfigDict(extra="forbid")
    checklist_id: ChecklistId
    title: str = Field(min_length=1, max_length=500)
    pending_count: int = Field(ge=0)
    done_count: int = Field(ge=0)
    total_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_counts(self) -> "ListChecklistsRow":
        # Runtime emits pending + done + total separately; total can
        # legitimately be ≥ pending+done (cancelled items don't fall
        # into either bucket but count toward total). Just sanity-
        # check non-negative + counts make sense.
        if self.total_count < self.pending_count + self.done_count:
            raise ValueError(
                f"total_count={self.total_count} less than "
                f"pending_count={self.pending_count} + "
                f"done_count={self.done_count} — runtime drift."
            )
        return self


class ListChecklistsEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"


class ListChecklistsOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    checklists: list[ListChecklistsRow] = Field(default_factory=list)


ListChecklistsOutput = Annotated[
    Union[ListChecklistsOk, ListChecklistsEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


# Per-row: `[checklist_id] · title · N pending, M done, T total`
_LIST_CHECKLISTS_ROW_RE = re.compile(
    r"^\[(?P<cid>checklist_[0-9a-f]{24})\]\s*·\s*"
    r"(?P<title>.+?)\s*·\s*"
    r"(?P<p>\d+)\s+pending,\s*(?P<d>\d+)\s+done,\s*(?P<t>\d+)\s+total$"
)


def parse_list_checklists(
    raw: str,
) -> (
    ListChecklistsOk
    | ListChecklistsEmpty
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        return err
    stripped = raw.strip()
    if stripped == "no checklists":
        return ListChecklistsEmpty()
    if not stripped or stripped.startswith("error:") or stripped.startswith("ok:"):
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_checklists",
            timestamp=datetime.now(timezone.utc),
        )
    rows: list[ListChecklistsRow] = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        m = _LIST_CHECKLISTS_ROW_RE.match(line)
        if m is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_checklists",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            rows.append(ListChecklistsRow(
                checklist_id=m.group("cid"),
                title=m.group("title").strip(),
                pending_count=int(m.group("p")),
                done_count=int(m.group("d")),
                total_count=int(m.group("t")),
            ))
        except ValidationError:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="list_checklists",
                timestamp=datetime.now(timezone.utc),
            )
    if not rows:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_checklists",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return ListChecklistsOk(checklists=rows)
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="list_checklists",
            timestamp=datetime.now(timezone.utc),
        )


# 41. show_checklist
class ShowChecklistItem(BaseModel):
    """One item line inside a checklist."""
    model_config = ConfigDict(extra="forbid")
    item_id: ChecklistItemId
    item_status: Literal["pending", "done", "cancelled"]
    title: str = Field(min_length=1, max_length=1000)


class ShowChecklistEmpty(BaseModel):
    """List exists but has no items."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["empty"] = "empty"
    checklist_id: ChecklistId
    title: str = Field(min_length=1, max_length=500)


class ShowChecklistOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    checklist_id: ChecklistId
    title: str = Field(min_length=1, max_length=500)
    items: list[ShowChecklistItem] = Field(min_length=1)


ShowChecklistOutput = Annotated[
    Union[ShowChecklistOk, ShowChecklistEmpty, HousewifeToolError],
    Field(discriminator="status"),
]


# Header: `# {title} ({checklist_id})`
_SHOW_CHECKLIST_HEADER_RE = re.compile(
    r"^#\s+(?P<title>.+?)\s+\((?P<cid>checklist_[0-9a-f]{24})\)$"
)
# Item: `[{clitem_id}] ☐ {title}` / `☑` / `✗`
_SHOW_CHECKLIST_ITEM_RE = re.compile(
    r"^\[(?P<iid>clitem_[0-9a-f]{24})\]\s+(?P<mark>[☐☑✗])\s+(?P<title>.+)$"
)
# Empty: `empty: list={id} title='{title}'`
_SHOW_CHECKLIST_EMPTY_RE = re.compile(
    r"^empty:\s+list=(?P<cid>checklist_[0-9a-f]{24})\s+title=(?P<title>.+)$"
)
_MARK_TO_STATUS = {"☐": "pending", "☑": "done", "✗": "cancelled"}


def parse_show_checklist(
    raw: str,
) -> (
    ShowChecklistOk
    | ShowChecklistEmpty
    | HousewifeToolError
    | ToolOutputContractViolation
):
    err = _parse_error(raw)
    if err is not None:
        # Codex Sub-A4 checklists R1 (preempt): runtime emits
        # `error: not_found: '<needle>'` for list lookup miss in
        # show_checklist + archive_checklist (housewife_chat_tools.py
        # :2590, :2692). Other tools (mark/delete) use the more
        # explicit `list_not_found:`. Remap both to one stable code
        # so planner branching is uniform.
        if err.error_code == "not_found" or (
            err.error_code == "unknown"
            and err.message.lower().startswith("not_found")
        ):
            return HousewifeToolError(
                error_code="checklist_list_not_found", message=err.message,
            )
        return err
    stripped = raw.strip()
    # Empty path
    m_empty = _SHOW_CHECKLIST_EMPTY_RE.match(stripped.splitlines()[0] if stripped else "")
    if m_empty is not None:
        # Title in runtime is `repr()` quoted (e.g. `'Дача'`) — strip quotes.
        title_raw = m_empty.group("title").strip()
        if (title_raw.startswith("'") and title_raw.endswith("'")) or (
            title_raw.startswith('"') and title_raw.endswith('"')
        ):
            title_raw = title_raw[1:-1]
        try:
            return ShowChecklistEmpty(
                checklist_id=m_empty.group("cid"),
                title=title_raw,
            )
        except ValidationError:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="show_checklist",
                timestamp=datetime.now(timezone.utc),
            )
    # Populated path
    lines = stripped.splitlines()
    if not lines:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="show_checklist",
            timestamp=datetime.now(timezone.utc),
        )
    m_header = _SHOW_CHECKLIST_HEADER_RE.match(lines[0])
    if m_header is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="show_checklist",
            timestamp=datetime.now(timezone.utc),
        )
    items: list[ShowChecklistItem] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        m_item = _SHOW_CHECKLIST_ITEM_RE.match(line)
        if m_item is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="show_checklist",
                timestamp=datetime.now(timezone.utc),
            )
        mark = m_item.group("mark")
        item_status = _MARK_TO_STATUS.get(mark)
        if item_status is None:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="show_checklist",
                timestamp=datetime.now(timezone.utc),
            )
        try:
            items.append(ShowChecklistItem(
                item_id=m_item.group("iid"),
                item_status=item_status,
                title=m_item.group("title").strip(),
            ))
        except ValidationError:
            return ToolOutputContractViolation(
                raw_output=raw, tool_name="show_checklist",
                timestamp=datetime.now(timezone.utc),
            )
    if not items:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="show_checklist",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return ShowChecklistOk(
            checklist_id=m_header.group("cid"),
            title=m_header.group("title").strip(),
            items=items,
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="show_checklist",
            timestamp=datetime.now(timezone.utc),
        )


# 42. mark_checklist_item_done
class MarkChecklistItemDoneOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["done"] = "done"
    item_id: ChecklistItemId
    title: str = Field(min_length=1, max_length=1000)
    """Codex Sub-A4 checklists R7 MINOR (HIGH catch): runtime
    add_items caps title at 1000 (checklists.py:463). Pre-R7
    schema cap of 500 would ContractViolation legacy rows with
    501-1000 char titles."""


MarkChecklistItemDoneOutput = Annotated[
    Union[MarkChecklistItemDoneOk, HousewifeToolError],
    Field(discriminator="status"),
]


_MARK_DONE_RE = re.compile(
    r"^ok:done:(?P<iid>clitem_[0-9a-f]{24}):(?P<title>.+)$"
)


def parse_mark_checklist_item_done(
    raw: str,
) -> MarkChecklistItemDoneOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _MARK_DONE_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="mark_checklist_item_done",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return MarkChecklistItemDoneOk(
            item_id=m.group("iid"),
            title=m.group("title").strip(),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="mark_checklist_item_done",
            timestamp=datetime.now(timezone.utc),
        )


# 43. delete_checklist_item
class DeleteChecklistItemOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["deleted"] = "deleted"
    item_id: ChecklistItemId
    title: str = Field(min_length=1, max_length=1000)
    """Codex R7 MINOR: cap matches runtime add_items (checklists.py:463)."""


DeleteChecklistItemOutput = Annotated[
    Union[DeleteChecklistItemOk, HousewifeToolError],
    Field(discriminator="status"),
]


_DELETE_ITEM_RE = re.compile(
    r"^ok:deleted:(?P<iid>clitem_[0-9a-f]{24}):(?P<title>.+)$"
)


def parse_delete_checklist_item(
    raw: str,
) -> DeleteChecklistItemOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _DELETE_ITEM_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="delete_checklist_item",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return DeleteChecklistItemOk(
            item_id=m.group("iid"),
            title=m.group("title").strip(),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="delete_checklist_item",
            timestamp=datetime.now(timezone.utc),
        )


# 44. archive_checklist
class ArchiveChecklistOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["archived"] = "archived"
    checklist_id: ChecklistId


ArchiveChecklistOutput = Annotated[
    Union[ArchiveChecklistOk, HousewifeToolError],
    Field(discriminator="status"),
]


_ARCHIVE_CHECKLIST_RE = re.compile(
    r"^ok:archived:(?P<cid>checklist_[0-9a-f]{24})$"
)


def parse_archive_checklist(
    raw: str,
) -> ArchiveChecklistOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        # Codex Sub-A4 checklists R1 (preempting reviewer): runtime
        # at housewife_chat_tools.py:2692 emits `error: not_found:
        # '<needle>'` for list lookup miss. Remap to the same stable
        # code mark/delete use for list-not-found.
        if err.error_code == "not_found" or (
            err.error_code == "unknown"
            and err.message.lower().startswith("not_found")
        ):
            return HousewifeToolError(
                error_code="checklist_list_not_found", message=err.message,
            )
        return err
    m = _ARCHIVE_CHECKLIST_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="archive_checklist",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return ArchiveChecklistOk(checklist_id=m.group("cid"))
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="archive_checklist",
            timestamp=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# 45-47. Onboarding family (Sub-A4 phase 8) — 3 tools:
#         onboarding_answered / onboarding_deferred / onboarding_complete
#
# Sources of truth: housewife_chat_tools.py:555-660,
# housewife_onboarding.py:85-93 for state/status constants.
#
# 6 topics: addressing, self_intro, family, diet, routine, pain_point.
# 4 topic states: pending, answered, skipped_once, skipped.
# 4 onboarding statuses: not_started, in_progress, complete, abandoned.
#
# Runtime shapes:
#   answered: "ok:answered:<topic>:next=<topic|none>:status=<status>"
#   deferred: "ok:deferred:<topic>:topic_state=<state>:next=<topic|none>:status=<status>"
#   complete: "ok:complete:status=<status>"
# ---------------------------------------------------------------------------


OnboardingTopic = Literal[
    "addressing", "self_intro", "family", "diet", "routine", "pain_point",
]
"""Codex Sub-A4 onboarding R1 MAJOR #1: schema mirrors what
runtime ``TOPIC_DESCRIPTIONS`` validates (all 6 topics — runtime
accepts them via ``housewife_chat_tools.py:581``). HOWEVER,
runtime ``TOPIC_ORDER`` is currently reduced to just
``("addressing",)`` (housewife_onboarding.py:68) — only addressing
is in the ACTIVE flow as of 2026-04-27. Other 5 topics persist
as valid identifiers but won't be re-asked or counted by the
progression engine. Planner should call onboarding_answered/
deferred ONLY for ``addressing`` until TOPIC_ORDER expands again."""
OnboardingTopicOrNone = Literal[
    "addressing", "self_intro", "family", "diet", "routine", "pain_point", "none",
]
OnboardingStatus = Literal["not_started", "in_progress", "complete", "abandoned"]
OnboardingTopicState = Literal[
    "pending", "answered", "skipped_once", "skipped",
]


class OnboardingAnsweredOk(BaseModel):
    """Happy-path response from onboarding_answered. Carries the
    next-topic pointer (or 'none' when all topics closed) AND the
    overall onboarding status so the planner can branch on
    «продолжать опрос» vs «уже закончили»."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["answered"] = "answered"
    topic: OnboardingTopic
    next_topic: OnboardingTopicOrNone
    onboarding_status: OnboardingStatus


OnboardingAnsweredOutput = Annotated[
    Union[OnboardingAnsweredOk, HousewifeToolError],
    Field(discriminator="status"),
]


_ONBOARDING_ANSWERED_RE = re.compile(
    r"^ok:answered:"
    r"(?P<topic>addressing|self_intro|family|diet|routine|pain_point):"
    r"next=(?P<next>addressing|self_intro|family|diet|routine|pain_point|none):"
    r"status=(?P<st>not_started|in_progress|complete|abandoned)$"
)


def parse_onboarding_answered(
    raw: str,
) -> OnboardingAnsweredOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ONBOARDING_ANSWERED_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="onboarding_answered",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return OnboardingAnsweredOk(
            topic=m.group("topic"),
            next_topic=m.group("next"),
            onboarding_status=m.group("st"),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="onboarding_answered",
            timestamp=datetime.now(timezone.utc),
        )


class OnboardingDeferredOk(BaseModel):
    """Happy-path response from onboarding_deferred. Topic state
    distinguishes «first skip — still in retry queue» (skipped_once)
    from «second skip — permanently dropped» (skipped). Planner
    branches on this to know whether to re-ask later."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["deferred"] = "deferred"
    topic: OnboardingTopic
    topic_state: OnboardingTopicState
    next_topic: OnboardingTopicOrNone
    onboarding_status: OnboardingStatus


OnboardingDeferredOutput = Annotated[
    Union[OnboardingDeferredOk, HousewifeToolError],
    Field(discriminator="status"),
]


_ONBOARDING_DEFERRED_RE = re.compile(
    r"^ok:deferred:"
    r"(?P<topic>addressing|self_intro|family|diet|routine|pain_point):"
    r"topic_state=(?P<ts>pending|answered|skipped_once|skipped):"
    r"next=(?P<next>addressing|self_intro|family|diet|routine|pain_point|none):"
    r"status=(?P<st>not_started|in_progress|complete|abandoned)$"
)


def parse_onboarding_deferred(
    raw: str,
) -> OnboardingDeferredOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ONBOARDING_DEFERRED_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="onboarding_deferred",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return OnboardingDeferredOk(
            topic=m.group("topic"),
            topic_state=m.group("ts"),
            next_topic=m.group("next"),
            onboarding_status=m.group("st"),
        )
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="onboarding_deferred",
            timestamp=datetime.now(timezone.utc),
        )


class OnboardingCompleteOk(BaseModel):
    """Happy-path response from onboarding_complete.

    Codex Sub-A4 onboarding R1 MAJOR #2: runtime ``mark_complete``
    ALWAYS sets status to ``STATUS_COMPLETE`` regardless of prior
    state (housewife_onboarding.py:373). Pre-R1 schema accepted
    all 4 status values — too broad. Tightened to
    ``Literal["complete"]`` so any other value triggers
    ContractViolation (real runtime drift).
    """

    model_config = ConfigDict(extra="forbid")
    status: Literal["complete"] = "complete"
    onboarding_status: Literal["complete"]


OnboardingCompleteOutput = Annotated[
    Union[OnboardingCompleteOk, HousewifeToolError],
    Field(discriminator="status"),
]


_ONBOARDING_COMPLETE_RE = re.compile(
    r"^ok:complete:status=(?P<st>complete)$"
)


def parse_onboarding_complete(
    raw: str,
) -> OnboardingCompleteOk | HousewifeToolError | ToolOutputContractViolation:
    err = _parse_error(raw)
    if err is not None:
        return err
    m = _ONBOARDING_COMPLETE_RE.match(raw.strip())
    if m is None:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="onboarding_complete",
            timestamp=datetime.now(timezone.utc),
        )
    try:
        return OnboardingCompleteOk(onboarding_status=m.group("st"))
    except ValidationError:
        return ToolOutputContractViolation(
            raw_output=raw, tool_name="onboarding_complete",
            timestamp=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Parser registry — wrapper looks tool_name up here
# ---------------------------------------------------------------------------


PARSERS = {
    "add_shopping_items": parse_add_shopping_items,
    "schedule_reminder": parse_schedule_reminder,
    "list_shopping": parse_list_shopping,
    "list_reminders": parse_list_reminders,
    "get_recipe": parse_get_recipe,
    "mark_shopping_bought": parse_mark_shopping_bought,
    "remove_shopping_items": parse_remove_shopping_items,
    "update_shopping_item": parse_update_shopping_item,
    "update_shopping_items_category": parse_update_shopping_items_category,
    "clear_bought_shopping": parse_clear_bought_shopping,
    "update_reminder": parse_update_reminder,
    "cancel_reminder": parse_cancel_reminder,
    "save_recipe": parse_save_recipe,
    "save_recipes_batch": parse_save_recipes_batch,
    "search_recipes": parse_search_recipes,
    "delete_recipe": parse_delete_recipe,
    "plan_week_menu": parse_plan_week_menu,
    "update_menu_item": parse_update_menu_item,
    "list_menu": parse_list_menu,
    "generate_shopping_from_menu": parse_generate_shopping_from_menu,
    "clear_menu": parse_clear_menu,
    "add_family_members": parse_add_family_members,
    "list_family_members": parse_list_family_members,
    "update_family_member": parse_update_family_member,
    "remove_family_member": parse_remove_family_member,
    "add_task": parse_add_task,
    "list_tasks": parse_list_tasks,
    "update_task": parse_update_task,
    "complete_task": parse_complete_task,
    "uncomplete_task": parse_uncomplete_task,
    "cancel_task": parse_cancel_task,
    "delete_task": parse_delete_task,
    "attach_reminder": parse_attach_reminder,
    "detach_reminder": parse_detach_reminder,
    "link_task_to_checklist": parse_link_task_to_checklist,
    "unlink_task": parse_unlink_task,
    "create_checklist": parse_create_checklist,
    "add_checklist_items": parse_add_checklist_items,
    "move_task_to_checklist": parse_move_task_to_checklist,
    "list_checklists": parse_list_checklists,
    "show_checklist": parse_show_checklist,
    "mark_checklist_item_done": parse_mark_checklist_item_done,
    "delete_checklist_item": parse_delete_checklist_item,
    "archive_checklist": parse_archive_checklist,
    "onboarding_answered": parse_onboarding_answered,
    "onboarding_deferred": parse_onboarding_deferred,
    "onboarding_complete": parse_onboarding_complete,
}


def parse_tool_output(tool_name: str, raw: str) -> BaseModel:
    """Dispatch to the right parser for ``tool_name``.

    Tools not yet covered return ``ToolOutputContractViolation`` so the
    fail-closed contract holds — the registry is the only "list of
    accepted shapes" and adding a tool requires a parser.
    """
    parser = PARSERS.get(tool_name)
    if parser is None:
        return ToolOutputContractViolation(
            raw_output=raw,
            tool_name=tool_name,
            timestamp=datetime.now(timezone.utc),
        )
    return parser(raw)


__all__ = [
    "AddFamilyMembersOk",
    "AddFamilyMembersOutput",
    "AddShoppingItemsAdded",
    "AddShoppingItemsEmpty",
    "AddShoppingItemsOutput",
    "ClearBoughtShoppingOk",
    "ClearBoughtShoppingOutput",
    "GetRecipeFound",
    "GetRecipeOutput",
    "HousewifeToolError",
    "ListRemindersEmpty",
    "ListRemindersItem",
    "ListRemindersList",
    "ListRemindersOutput",
    "ListShoppingEmpty",
    "ListShoppingItem",
    "ListShoppingItems",
    "ListShoppingOutput",
    "MarkShoppingBoughtOk",
    "MarkShoppingBoughtOutput",
    "PARSERS",
    "RemoveShoppingItemsOk",
    "RemoveShoppingItemsOutput",
    "ScheduleReminderOutput",
    "ScheduleReminderScheduled",
    "UpdateShoppingItemOk",
    "UpdateShoppingItemOutput",
    "UpdateShoppingItemsCategoryOk",
    "UpdateShoppingItemsCategoryOutput",
    "parse_add_shopping_items",
    "parse_clear_bought_shopping",
    "parse_get_recipe",
    "parse_list_reminders",
    "parse_list_shopping",
    "parse_mark_shopping_bought",
    "parse_remove_shopping_items",
    "parse_schedule_reminder",
    "parse_tool_output",
    "parse_update_shopping_item",
    "parse_update_shopping_items_category",
]
