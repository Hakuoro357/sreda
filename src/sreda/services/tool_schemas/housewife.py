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
from sreda.services.tool_schemas.common import ReminderId, ShoppingItemId


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
]


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
        # Special-case ``error: recipe '<id>' not found`` — the recipe_id
        # is unique per call so the generic first-token code is useless
        # for the planner to match. Normalize to ``not_found`` so the
        # plan can branch on it deterministically.
        if "not found" in err.message:
            return HousewifeToolError(error_code="not_found", message=err.message)
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
