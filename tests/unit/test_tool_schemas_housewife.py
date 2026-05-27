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
# Codex R2 MAJOR #1: tight ID aliases enforce ``^sh_[0-9a-f]{24}$`` /
# ``^rem_[0-9a-f]{24}$`` etc. matching the runtime
# ``f"sh_{uuid4().hex[:24]}"`` factories. Short fakes like ``sh_1``
# are no longer valid output IDs — they trigger the fail-closed sentinel.
# ---------------------------------------------------------------------------

SH_A = "sh_aaaaaaaaaaaaaaaaaaaaaaaa"
SH_B = "sh_bbbbbbbbbbbbbbbbbbbbbbbb"
SH_C = "sh_cccccccccccccccccccccccc"
SH_D = "sh_dddddddddddddddddddddddd"
SH_E = "sh_eeeeeeeeeeeeeeeeeeeeeeee"
SH_F = "sh_ffffffffffffffffffffffff"
REM_A = "rem_1111111111111111aaaaaaaa"[:28]  # 'rem_' + 24 chars
REM_B = "rem_2222222222222222bbbbbbbb"[:28]


# ---------------------------------------------------------------------------
# add_shopping_items
# ---------------------------------------------------------------------------


def test_add_shopping_items_added_with_ids() -> None:
    result = parse_add_shopping_items(f"ok:added:3:ids=[{SH_A},{SH_B},{SH_C}]")
    assert isinstance(result, AddShoppingItemsAdded)
    assert result.added_count == 3
    assert result.item_ids == [SH_A, SH_B, SH_C]


def test_add_shopping_items_added_single_id() -> None:
    result = parse_add_shopping_items(f"ok:added:1:ids=[{SH_A}]")
    assert isinstance(result, AddShoppingItemsAdded)
    assert result.added_count == 1


def test_add_shopping_items_malformed_ids_returns_violation() -> None:
    """Codex R2 MAJOR #4: short IDs like ``sh_1`` no longer pass the
    tight ``^sh_[0-9a-f]{24}$`` constraint → fail-closed via sentinel."""
    result = parse_add_shopping_items("ok:added:2:ids=[sh_1,sh_2]")
    assert isinstance(result, ToolOutputContractViolation)


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
    raw = f"ok:scheduled:{REM_A}:2026-05-26T18:00:00+03:00"
    result = parse_schedule_reminder(raw)
    assert isinstance(result, ScheduleReminderScheduled)
    assert result.reminder_id == REM_A
    assert result.trigger_at_iso == "2026-05-26T18:00:00+03:00"


def test_schedule_reminder_malformed_id_returns_violation() -> None:
    """Codex R2 MAJOR #4: short reminder id like ``rem_abc123`` no
    longer satisfies the tight ``^rem_[0-9a-f]{24}$`` constraint."""
    raw = "ok:scheduled:rem_abc123:2026-05-26T18:00:00+03:00"
    result = parse_schedule_reminder(raw)
    assert isinstance(result, ToolOutputContractViolation)


def test_schedule_reminder_parse_error() -> None:
    """Codex R2 MAJOR #6: stable code regardless of dynamic value."""
    raw = "error: cannot parse trigger_iso='завтра'"
    result = parse_schedule_reminder(raw)
    assert isinstance(result, HousewifeToolError)
    assert result.error_code == "cannot_parse_trigger_iso"


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
        f"  [{SH_A}] молоко (1 л)\n"
        f"  [{SH_B}] хлеб\n"
        "[бакалея]\n"
        f"  [{SH_C}] сахар (1 кг)"
    )
    result = parse_list_shopping(raw)
    assert isinstance(result, ListShoppingItems)
    assert len(result.items) == 3
    assert result.items[0].item_id == SH_A
    assert result.items[0].category == "молочные"
    assert result.items[1].category == "молочные"
    assert result.items[2].category == "бакалея"


def test_list_shopping_malformed_id_returns_violation() -> None:
    """Codex R2 MAJOR #4: bracketed token starts with ``sh_`` but isn't
    a valid 24-hex suffix → tight pattern rejects, parser fail-closes."""
    raw = (
        "pending shopping items:\n"
        "[молочные]\n"
        "  [sh_abc] молоко (1 л)\n"
    )
    result = parse_list_shopping(raw)
    assert isinstance(result, ToolOutputContractViolation)


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
        f"[{REM_A}] купить хлеб → 2026-05-26 18:00\n"
        f"[{REM_B}] забрать ребёнка → 2026-05-26 18:30"
    )
    result = parse_list_reminders(raw)
    assert isinstance(result, ListRemindersList)
    assert len(result.items) == 2
    assert result.items[0].reminder_id == REM_A
    assert "хлеб" in result.items[0].raw_line


def test_list_reminders_malformed_id_returns_violation() -> None:
    """Codex R2 MAJOR #4: short reminder id like ``rem_1`` no longer
    passes ``^rem_[0-9a-f]{24}$`` — parser fail-closes via sentinel
    rather than emit a bad id to the planner."""
    raw = "active reminders:\n[rem_1] купить хлеб → 2026-05-26 18:00"
    result = parse_list_reminders(raw)
    assert isinstance(result, ToolOutputContractViolation)


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
    """``ok:added:5:ids=[<3 ids>]`` is internally inconsistent — planner
    uses count for branch decisions and ids for refs (Codex M3 /
    code-reviewer MINOR)."""
    with pytest.raises(ValidationError):
        AddShoppingItemsAdded(added_count=5, item_ids=[SH_A, SH_B, SH_C])


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
