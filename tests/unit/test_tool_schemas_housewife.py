"""Tests for top-5 housewife tool output parsers (Sub-A4, Epic #74).

Each parser converts a legacy ``str`` output line into a typed pydantic
discriminated union. Unknown patterns return ``ToolOutputContractViolation``
(fail-closed contract per Group 6.5) — the executor halts and writes
to ``planner_gaps`` so the gap can be patched via GEPA.

Parser coverage matrix (one happy + one or two error variants per tool;
``test_<parser>_unknown_returns_contract_violation`` for the fail-closed
contract).
"""

from __future__ import annotations

import pytest

from sreda.services.tool_schemas import ToolOutputContractViolation, parse_tool_output
from pydantic import ValidationError

from sreda.services.tool_schemas.housewife import (
    AddShoppingItemsAdded,
    AddShoppingItemsEmpty,
    GetRecipeFound,
    HousewifeToolError,
    ListRemindersEmpty,
    ListRemindersList,
    ListShoppingEmpty,
    ListShoppingItems,
    PARSERS,
    ScheduleReminderScheduled,
    ScheduleReminderSkippedPast,
    parse_add_shopping_items,
    parse_get_recipe,
    parse_list_reminders,
    parse_list_shopping,
    parse_schedule_reminder,
)


# ---------------------------------------------------------------------------
# add_shopping_items
# ---------------------------------------------------------------------------


def test_add_shopping_items_added_with_ids() -> None:
    result = parse_add_shopping_items("ok:added:3:ids=[sh_1,sh_2,sh_3]")
    assert isinstance(result, AddShoppingItemsAdded)
    assert result.added_count == 3
    assert result.item_ids == ["sh_1", "sh_2", "sh_3"]


def test_add_shopping_items_added_single_id() -> None:
    result = parse_add_shopping_items("ok:added:1:ids=[sh_abc]")
    assert isinstance(result, AddShoppingItemsAdded)
    assert result.added_count == 1


def test_add_shopping_items_empty_means_all_duplicates() -> None:
    result = parse_add_shopping_items("ok:added:0")
    assert isinstance(result, AddShoppingItemsEmpty)
    assert result.added_count == 0


def test_add_shopping_items_error_empty_items_list() -> None:
    result = parse_add_shopping_items("error: empty items list")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "empty_items_list"


def test_add_shopping_items_error_internal() -> None:
    result = parse_add_shopping_items("error: internal")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "internal"


def test_add_shopping_items_unknown_pattern_returns_violation() -> None:
    result = parse_add_shopping_items("ok:weird:format:42")
    assert isinstance(result, ToolOutputContractViolation)
    assert result.tool_name == "add_shopping_items"
    assert result.raw_output == "ok:weird:format:42"


# ---------------------------------------------------------------------------
# schedule_reminder
# ---------------------------------------------------------------------------


def test_schedule_reminder_scheduled() -> None:
    raw = "ok:scheduled:rem_abc123:2026-05-26T18:00:00+03:00"
    result = parse_schedule_reminder(raw)
    assert isinstance(result, ScheduleReminderScheduled)
    assert result.reminder_id == "rem_abc123"
    assert result.trigger_at_iso == "2026-05-26T18:00:00+03:00"


def test_schedule_reminder_parse_error() -> None:
    raw = "error: cannot parse trigger_iso='завтра'"
    result = parse_schedule_reminder(raw)
    assert isinstance(result, HousewifeToolError)
    # First token before ":" is the canonical code → "cannot_parse_trigger_iso='завтра'"
    # is fine but we collapse the equals-style detail too
    assert "cannot" in result.error_code


def test_schedule_reminder_internal_error() -> None:
    result = parse_schedule_reminder("error: internal")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "internal"


def test_schedule_reminder_unknown_returns_violation() -> None:
    result = parse_schedule_reminder("scheduled at noon")
    assert isinstance(result, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# list_shopping
# ---------------------------------------------------------------------------


def test_list_shopping_empty() -> None:
    result = parse_list_shopping("no shopping items")
    assert isinstance(result, ListShoppingEmpty)


def test_list_shopping_real_production_format() -> None:
    """Real housewife_chat_tools.py list_shopping() format — header +
    category groups + indented item rows (code-reviewer CRITICAL #1)."""
    raw = (
        "pending shopping items:\n"
        "[молочные]\n"
        "  [sh_abc] молоко (1 л)\n"
        "  [sh_def] хлеб\n"
        "[бакалея]\n"
        "  [sh_xyz] сахар (1 кг)"
    )
    result = parse_list_shopping(raw)
    assert isinstance(result, ListShoppingItems)
    assert len(result.items) == 3
    assert result.items[0].item_id == "sh_abc"
    assert result.items[0].category == "молочные"
    assert result.items[1].category == "молочные"
    assert result.items[2].category == "бакалея"


def test_list_shopping_header_only_returns_violation() -> None:
    """``pending shopping items:`` with no rows is malformed —
    production short-circuits to ``no shopping items`` for empty."""
    result = parse_list_shopping("pending shopping items:")
    assert isinstance(result, ToolOutputContractViolation)


def test_list_shopping_missing_header_returns_violation() -> None:
    """Flat list without the ``pending shopping items:`` header is not
    the real format and would have silently passed in the v1 parser."""
    raw = "[sh_1] хлеб 1\n[sh_2] молоко 2"
    result = parse_list_shopping(raw)
    assert isinstance(result, ToolOutputContractViolation)


def test_list_shopping_item_before_category_returns_violation() -> None:
    raw = "pending shopping items:\n  [sh_abc] молоко (1 л)"
    result = parse_list_shopping(raw)
    assert isinstance(result, ToolOutputContractViolation)


def test_list_shopping_error() -> None:
    result = parse_list_shopping("error: no user_id context")
    assert isinstance(result, HousewifeToolError)
    # error_code is now lowercase (Codex Low)
    assert result.error_code == "no_user_id_context"


# ---------------------------------------------------------------------------
# list_reminders
# ---------------------------------------------------------------------------


def test_list_reminders_empty() -> None:
    result = parse_list_reminders("no active reminders")
    assert isinstance(result, ListRemindersEmpty)


def test_list_reminders_with_items() -> None:
    raw = (
        "active reminders:\n"
        "[rem_1] купить хлеб → 2026-05-26 18:00\n"
        "[rem_2] забрать ребёнка → 2026-05-26 18:30"
    )
    result = parse_list_reminders(raw)
    assert isinstance(result, ListRemindersList)
    assert len(result.items) == 2
    assert result.items[0].reminder_id == "rem_1"
    assert "хлеб" in result.items[0].raw_line


def test_list_reminders_missing_header_returns_violation() -> None:
    raw = "[rem_1] что-то → когда-то"  # no "active reminders:" header
    result = parse_list_reminders(raw)
    assert isinstance(result, ToolOutputContractViolation)


def test_list_reminders_error() -> None:
    result = parse_list_reminders("error: internal")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "internal"


# ---------------------------------------------------------------------------
# get_recipe
# ---------------------------------------------------------------------------


def test_get_recipe_found_returns_raw_text() -> None:
    raw = "Борщ\nПорций: 4\nИнгредиенты:\n- свёкла 2 шт\n- картофель 3 шт"
    result = parse_get_recipe(raw)
    assert isinstance(result, GetRecipeFound)
    assert "Борщ" in result.raw_text


def test_get_recipe_not_found_error_is_normalized() -> None:
    """``error: recipe '<id>' not found`` collapses to error_code='not_found'
    so the planner can branch on a stable code (the recipe_id varies per call)."""
    result = parse_get_recipe("error: recipe 'mystery' not found")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "not_found"
    assert "not found" in result.message


def test_get_recipe_empty_returns_violation() -> None:
    """Empty output is unexpected — should hit the violation path."""
    result = parse_get_recipe("")
    assert isinstance(result, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------


def test_parse_tool_output_dispatches_to_registered_parser() -> None:
    result = parse_tool_output("add_shopping_items", "ok:added:0")
    assert isinstance(result, AddShoppingItemsEmpty)


def test_parse_tool_output_unknown_tool_returns_violation() -> None:
    result = parse_tool_output("some_future_tool_not_registered", "anything")
    assert isinstance(result, ToolOutputContractViolation)
    assert result.tool_name == "some_future_tool_not_registered"


# ---------------------------------------------------------------------------
# Code-reviewer 2026-05-26 follow-ups — variants the v1 parser missed
# ---------------------------------------------------------------------------


def test_schedule_reminder_skipped_past_parsed() -> None:
    """Real housewife_chat_tools.py:373 — when trigger is in the past,
    tool emits ``skipped:past:<iso>:late_by_<n>min``. v1 parser treated
    this as ContractViolation (CRITICAL #2)."""
    raw = "skipped:past:2026-05-26T15:00:00+03:00:late_by_42min"
    result = parse_schedule_reminder(raw)
    assert isinstance(result, ScheduleReminderSkippedPast)
    assert result.trigger_at_iso == "2026-05-26T15:00:00+03:00"
    assert result.late_by_minutes == 42


def test_add_shopping_count_ids_mismatch_rejected() -> None:
    """``ok:added:5:ids=[sh_1,sh_2,sh_3]`` is internally inconsistent —
    planner uses count for branch decisions and ids for refs (Codex M3 /
    code-reviewer MINOR)."""
    with pytest.raises(ValidationError):
        AddShoppingItemsAdded(added_count=5, item_ids=["sh_1", "sh_2", "sh_3"])


def test_bare_error_normalized_to_unknown() -> None:
    """``error:`` with no payload doesn't raise — collapses to unknown
    so the planner branch can still match (Codex Low)."""
    result = parse_list_shopping("error:")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "unknown"


def test_error_code_is_lowercase() -> None:
    """error_code must be lowercase for planner's deterministic match —
    Codex Low. Real tools currently emit lowercase, but parser must
    normalize defensively."""
    result = parse_list_shopping("error: INTERNAL")
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "internal"


def test_parsers_registry_covers_top_5() -> None:
    """Sanity check: the five tools called out in Sub-A4 are wired."""
    expected = {
        "add_shopping_items",
        "schedule_reminder",
        "list_shopping",
        "list_reminders",
        "get_recipe",
    }
    assert expected <= set(PARSERS.keys())
