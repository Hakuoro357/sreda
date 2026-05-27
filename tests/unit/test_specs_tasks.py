"""Integration tests for the tasks family ToolSpec instances
(Sub-A4 phase 6 — Plan-Execute Epic).

Mirrors the shopping/reminders/recipes/menu/household patterns.

Coverage:
- All 11 TASKS_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: tasks-family entries exact match
- Per-tool: input_model rejects extra keys, parsers produce
  output_model on canonical "ok:..." strings
- Tight TaskId aliases, ChecklistId / ReminderId cross-family
- HHMM regex enforcement, RRULE validation
- Cross-field invariants (reminder requires schedule, reminder
  XOR details_items, time_end > time_start, at-least-one update)
- Sentinel boundary regression
- TypeAdapter parser→output_model parity
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    AddTaskCreated,
    AddTaskCreatedWithChecklist,
    AddTaskCreatedWithReminder,
    AttachReminderOk,
    CancelTaskOk,
    CompleteTaskOk,
    DeleteTaskOk,
    DetachReminderOk,
    HousewifeToolError,
    LinkTaskToChecklistAlreadyLinked,
    LinkTaskToChecklistLinked,
    ListTasksEmpty,
    ListTasksOk,
    PARSERS,
    UncompleteTaskOk,
    UnlinkTaskNotLinked,
    UnlinkTaskUnlinked,
    UpdateTaskOk,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
)
from sreda.services.tool_schemas.specs_tasks import (
    ADD_TASK_SPEC,
    ATTACH_REMINDER_SPEC,
    AddTaskInput,
    AttachReminderInput,
    CANCEL_TASK_SPEC,
    COMPLETE_TASK_SPEC,
    CancelTaskInput,
    CompleteTaskInput,
    DELETE_TASK_SPEC,
    DETACH_REMINDER_SPEC,
    DeleteTaskInput,
    DetachReminderInput,
    LINK_TASK_TO_CHECKLIST_SPEC,
    LIST_TASKS_SPEC,
    LinkTaskToChecklistInput,
    ListTasksInput,
    TASKS_SPECS,
    UNCOMPLETE_TASK_SPEC,
    UNLINK_TASK_SPEC,
    UPDATE_TASK_SPEC,
    UncompleteTaskInput,
    UnlinkTaskInput,
    UpdateTaskInput,
)

# Real-shape IDs.
TASK_A = "task_aaaaaaaaaaaaaaaaaaaaaaaa"
TASK_B = "task_bbbbbbbbbbbbbbbbbbbbbbbb"
TASK_C = "task_cccccccccccccccccccccccc"
CHK_A = "checklist_aaaaaaaaaaaaaaaaaaaaaaaa"
REM_A = "rem_aaaaaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Family-level invariants
# ---------------------------------------------------------------------------


def test_all_tasks_specs_construct() -> None:
    assert len(TASKS_SPECS) == 11
    names = {s.name for s in TASKS_SPECS}
    assert names == {
        "add_task", "list_tasks", "update_task",
        "complete_task", "uncomplete_task", "cancel_task", "delete_task",
        "attach_reminder", "detach_reminder",
        "link_task_to_checklist", "unlink_task",
    }


def test_tasks_family_passes_production_quality_strict() -> None:
    assert_production_registry_quality(TASKS_SPECS)


def test_manifest_matches_tasks_specs() -> None:
    manifest_tasks = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "tasks"
    }
    spec_names = {s.name for s in TASKS_SPECS}
    assert manifest_tasks == spec_names


@pytest.mark.parametrize("spec", TASKS_SPECS, ids=lambda s: s.name)
def test_every_tasks_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS


# ---------------------------------------------------------------------------
# Input model — AddTaskInput
# ---------------------------------------------------------------------------


def test_add_task_input_minimal() -> None:
    parsed = AddTaskInput.model_validate({"title": "разминка"})
    assert parsed.title == "разминка"
    assert parsed.scheduled_date is None


def test_add_task_input_full() -> None:
    parsed = AddTaskInput.model_validate({
        "title": "встреча с врачом",
        "scheduled_date": "2026-05-30",
        "time_start": "14:00",
        "time_end": "14:30",
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
        "notes": "взять документы",
        "reminder_offset_minutes": 30,
    })
    assert parsed.reminder_offset_minutes == 30


def test_add_task_input_rejects_reminder_without_schedule() -> None:
    """Codex Sub-A4 tasks R1: cross-field rule — reminder needs
    scheduled_date + time_start."""
    with pytest.raises(ValidationError) as exc:
        AddTaskInput.model_validate({
            "title": "x",
            "reminder_offset_minutes": 15,
        })
    assert "scheduled_date" in str(exc.value)


def test_add_task_input_rejects_reminder_without_time_start() -> None:
    with pytest.raises(ValidationError) as exc:
        AddTaskInput.model_validate({
            "title": "x",
            "scheduled_date": "tomorrow",
            "reminder_offset_minutes": 15,
        })
    assert "time_start" in str(exc.value)


def test_add_task_input_rejects_reminder_for_inbox() -> None:
    """Inbox tasks have no schedule — reminder makes no sense."""
    with pytest.raises(ValidationError) as exc:
        AddTaskInput.model_validate({
            "title": "x",
            "scheduled_date": "inbox",
            "time_start": "08:00",
            "reminder_offset_minutes": 15,
        })
    assert "scheduled_date" in str(exc.value) or "inbox" in str(exc.value)


def test_add_task_input_rejects_reminder_with_details() -> None:
    """Codex Sub-A4 tasks R1: reminder + details_items NOT compatible
    (composite path doesn't attach reminders)."""
    with pytest.raises(ValidationError) as exc:
        AddTaskInput.model_validate({
            "title": "купить",
            "scheduled_date": "tomorrow",
            "time_start": "10:00",
            "reminder_offset_minutes": 15,
            "details_items": ["хлеб", "молоко"],
        })
    assert "details_items" in str(exc.value)


def test_add_task_input_rejects_time_end_before_start() -> None:
    with pytest.raises(ValidationError) as exc:
        AddTaskInput.model_validate({
            "title": "x",
            "scheduled_date": "tomorrow",
            "time_start": "14:00",
            "time_end": "13:00",
        })
    assert "time_end" in str(exc.value)


def test_add_task_input_rejects_bad_hhmm() -> None:
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({"title": "x", "time_start": "25:99"})
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({"title": "x", "time_start": "7:5"})


def test_add_task_input_rejects_impossible_date() -> None:
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({
            "title": "x",
            "scheduled_date": "2026-02-31",
        })


def test_add_task_input_accepts_today_tomorrow_inbox() -> None:
    for d in ("today", "tomorrow", "inbox"):
        parsed = AddTaskInput.model_validate({"title": "x", "scheduled_date": d})
        assert parsed.scheduled_date == d


def test_add_task_input_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({"title": "x", "unknown": "y"})


def test_add_task_input_rejects_zero_offset_minutes() -> None:
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({
            "title": "x",
            "scheduled_date": "tomorrow",
            "time_start": "08:00",
            "reminder_offset_minutes": 0,
        })


def test_add_task_input_rejects_oversized_details() -> None:
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({
            "title": "купить",
            "details_items": [f"item{i}" for i in range(51)],
        })


# ---------------------------------------------------------------------------
# Input model — ListTasksInput
# ---------------------------------------------------------------------------


def test_list_tasks_input_defaults() -> None:
    parsed = ListTasksInput.model_validate({})
    assert parsed.date == "today"
    assert parsed.status == "pending"


def test_list_tasks_input_accepts_all() -> None:
    parsed = ListTasksInput.model_validate({"date": "all", "status": "all"})
    assert parsed.date == "all"


def test_list_tasks_input_accepts_iso() -> None:
    parsed = ListTasksInput.model_validate({"date": "2026-05-30"})
    assert parsed.date == "2026-05-30"


def test_list_tasks_input_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        ListTasksInput.model_validate({"status": "weird"})


# ---------------------------------------------------------------------------
# Input model — UpdateTaskInput
# ---------------------------------------------------------------------------


def test_update_task_input_single_field() -> None:
    parsed = UpdateTaskInput.model_validate({
        "task_id": TASK_A, "title": "новое имя",
    })
    assert parsed.title == "новое имя"


def test_update_task_input_rejects_empty_payload() -> None:
    with pytest.raises(ValidationError) as exc:
        UpdateTaskInput.model_validate({"task_id": TASK_A})
    assert "at least one" in str(exc.value)


def test_update_task_input_rejects_bad_task_id() -> None:
    with pytest.raises(ValidationError):
        UpdateTaskInput.model_validate({"task_id": "task_short", "title": "x"})


def test_update_task_input_rejects_time_end_before_start() -> None:
    with pytest.raises(ValidationError):
        UpdateTaskInput.model_validate({
            "task_id": TASK_A, "time_start": "14:00", "time_end": "13:00",
        })


# ---------------------------------------------------------------------------
# Simple ID-only inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("klass", [
    CompleteTaskInput, UncompleteTaskInput, CancelTaskInput, DeleteTaskInput,
    DetachReminderInput, UnlinkTaskInput,
])
def test_id_only_inputs_accept_real_id(klass) -> None:
    parsed = klass.model_validate({"task_id": TASK_A})
    assert parsed.task_id == TASK_A


@pytest.mark.parametrize("klass", [
    CompleteTaskInput, UncompleteTaskInput, CancelTaskInput, DeleteTaskInput,
    DetachReminderInput, UnlinkTaskInput,
])
def test_id_only_inputs_reject_bad_id(klass) -> None:
    with pytest.raises(ValidationError):
        klass.model_validate({"task_id": "task_short"})


@pytest.mark.parametrize("klass", [
    CompleteTaskInput, UncompleteTaskInput, CancelTaskInput, DeleteTaskInput,
    DetachReminderInput, UnlinkTaskInput,
])
def test_id_only_inputs_reject_extra(klass) -> None:
    with pytest.raises(ValidationError):
        klass.model_validate({"task_id": TASK_A, "extra": "x"})


# ---------------------------------------------------------------------------
# AttachReminderInput
# ---------------------------------------------------------------------------


def test_attach_reminder_input_real_id_and_offset() -> None:
    parsed = AttachReminderInput.model_validate({
        "task_id": TASK_A, "offset_minutes": 30,
    })
    assert parsed.offset_minutes == 30


def test_attach_reminder_input_rejects_zero_offset() -> None:
    with pytest.raises(ValidationError):
        AttachReminderInput.model_validate({
            "task_id": TASK_A, "offset_minutes": 0,
        })


def test_attach_reminder_input_rejects_above_week() -> None:
    """10080 mins = 7 days — upper bound to catch likely planner
    mistakes (year-long reminders almost never user intent)."""
    with pytest.raises(ValidationError):
        AttachReminderInput.model_validate({
            "task_id": TASK_A, "offset_minutes": 10081,
        })


# ---------------------------------------------------------------------------
# Link inputs
# ---------------------------------------------------------------------------


def test_link_task_to_checklist_input_real_ids() -> None:
    parsed = LinkTaskToChecklistInput.model_validate({
        "task_id": TASK_A, "checklist_id": CHK_A,
    })
    assert parsed.checklist_id == CHK_A


def test_link_task_to_checklist_input_rejects_bad_checklist_id() -> None:
    with pytest.raises(ValidationError):
        LinkTaskToChecklistInput.model_validate({
            "task_id": TASK_A, "checklist_id": "checklist_short",
        })


# ---------------------------------------------------------------------------
# Parsers — add_task three variants + error path
# ---------------------------------------------------------------------------


def test_add_task_parser_plain_created() -> None:
    parsed = parse_tool_output("add_task", f"ok:created:{TASK_A}")
    assert isinstance(parsed, AddTaskCreated)
    assert parsed.task_id == TASK_A


def test_add_task_parser_with_reminder() -> None:
    parsed = parse_tool_output(
        "add_task",
        f"ok:created:{TASK_A}:reminder=за 15мин",
    )
    assert isinstance(parsed, AddTaskCreatedWithReminder)
    assert parsed.reminder_offset_minutes == 15


def test_add_task_parser_with_checklist() -> None:
    parsed = parse_tool_output(
        "add_task",
        f"ok:created:{TASK_A}:checklist={CHK_A}",
    )
    assert isinstance(parsed, AddTaskCreatedWithChecklist)
    assert parsed.checklist_id == CHK_A


def test_add_task_parser_error_dynamic_message() -> None:
    parsed = parse_tool_output(
        "add_task",
        "error: reminder requires scheduled_date + time_start",
    )
    assert isinstance(parsed, HousewifeToolError)


def test_add_task_parser_garbage_is_violation() -> None:
    parsed = parse_tool_output("add_task", "ok:fancy:something")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Parsers — list_tasks
# ---------------------------------------------------------------------------


def test_list_tasks_parser_empty() -> None:
    parsed = parse_tool_output("list_tasks", "no tasks")
    assert isinstance(parsed, ListTasksEmpty)


def test_list_tasks_parser_dump() -> None:
    raw = f"[{TASK_A}] утренняя разминка · today · 07:00"
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ListTasksOk)
    assert "разминка" in parsed.raw_text


def test_list_tasks_parser_rejects_status_token() -> None:
    """Lone `ok:` / `error:` tokens are runtime drift, not a tasks
    dump → ContractViolation."""
    parsed = parse_tool_output("list_tasks", "ok:cleared:1")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Parsers — update / status-transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,raw,cls,attr", [
    ("update_task", f"ok:updated:{TASK_A}", UpdateTaskOk, "task_id"),
    ("complete_task", f"ok:completed:{TASK_A}", CompleteTaskOk, "task_id"),
    ("uncomplete_task", f"ok:uncompleted:{TASK_A}", UncompleteTaskOk, "task_id"),
    ("cancel_task", f"ok:cancelled:{TASK_A}", CancelTaskOk, "task_id"),
])
def test_id_returning_parsers_happy(tool, raw, cls, attr) -> None:
    parsed = parse_tool_output(tool, raw)
    assert isinstance(parsed, cls)
    assert getattr(parsed, attr) == TASK_A


@pytest.mark.parametrize("tool", [
    "update_task", "complete_task", "uncomplete_task", "cancel_task",
])
def test_id_returning_parsers_not_found(tool) -> None:
    parsed = parse_tool_output(
        tool, f"error: task '{TASK_B}' not found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "task_not_found"


def test_delete_task_parser_ok() -> None:
    parsed = parse_tool_output("delete_task", "ok:deleted")
    assert isinstance(parsed, DeleteTaskOk)


def test_delete_task_parser_not_found() -> None:
    parsed = parse_tool_output(
        "delete_task", f"error: task '{TASK_B}' not found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "task_not_found"


# ---------------------------------------------------------------------------
# Parsers — attach / detach reminder
# ---------------------------------------------------------------------------


def test_attach_reminder_parser_happy() -> None:
    parsed = parse_tool_output(
        "attach_reminder",
        f"ok:reminder_attached:{REM_A}:за 30мин",
    )
    assert isinstance(parsed, AttachReminderOk)
    assert parsed.reminder_id == REM_A
    assert parsed.offset_minutes == 30


def test_attach_reminder_parser_offset_error() -> None:
    parsed = parse_tool_output(
        "attach_reminder", "error: offset_minutes must be a positive integer",
    )
    assert isinstance(parsed, HousewifeToolError)


def test_detach_reminder_parser_ok() -> None:
    parsed = parse_tool_output("detach_reminder", "ok:reminder_detached")
    assert isinstance(parsed, DetachReminderOk)


def test_detach_reminder_parser_garbage_is_violation() -> None:
    parsed = parse_tool_output("detach_reminder", "ok:gone")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Parsers — link / unlink
# ---------------------------------------------------------------------------


def test_link_task_parser_linked() -> None:
    parsed = parse_tool_output(
        "link_task_to_checklist", f"ok:linked:{TASK_A}:{CHK_A}",
    )
    assert isinstance(parsed, LinkTaskToChecklistLinked)
    assert parsed.task_id == TASK_A
    assert parsed.checklist_id == CHK_A


def test_link_task_parser_already_linked() -> None:
    parsed = parse_tool_output(
        "link_task_to_checklist", f"ok:already_linked:{TASK_A}:{CHK_A}",
    )
    assert isinstance(parsed, LinkTaskToChecklistAlreadyLinked)


def test_unlink_task_parser_unlinked() -> None:
    parsed = parse_tool_output(
        "unlink_task", f"ok:unlinked:{TASK_A}:{CHK_A}",
    )
    assert isinstance(parsed, UnlinkTaskUnlinked)
    assert parsed.checklist_id == CHK_A


def test_unlink_task_parser_not_linked() -> None:
    parsed = parse_tool_output("unlink_task", f"ok:not_linked:{TASK_A}")
    assert isinstance(parsed, UnlinkTaskNotLinked)


# ---------------------------------------------------------------------------
# TypeAdapter parser→output_model parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,raw", [
    ("add_task", f"ok:created:{TASK_A}"),
    ("add_task", f"ok:created:{TASK_A}:reminder=за 15мин"),
    ("add_task", f"ok:created:{TASK_A}:checklist={CHK_A}"),
    ("add_task", "error: reminder requires scheduled_date + time_start"),
    ("list_tasks", "no tasks"),
    ("list_tasks", f"[{TASK_A}] разминка today 07:00"),
    ("update_task", f"ok:updated:{TASK_A}"),
    ("complete_task", f"ok:completed:{TASK_A}"),
    ("uncomplete_task", f"ok:uncompleted:{TASK_A}"),
    ("cancel_task", f"ok:cancelled:{TASK_A}"),
    ("delete_task", "ok:deleted"),
    ("attach_reminder", f"ok:reminder_attached:{REM_A}:за 30мин"),
    ("detach_reminder", "ok:reminder_detached"),
    ("link_task_to_checklist", f"ok:linked:{TASK_A}:{CHK_A}"),
    ("link_task_to_checklist", f"ok:already_linked:{TASK_A}:{CHK_A}"),
    ("unlink_task", f"ok:unlinked:{TASK_A}:{CHK_A}"),
    ("unlink_task", f"ok:not_linked:{TASK_A}"),
])
def test_tasks_parser_outputs_validate_against_spec_output_model(tool, raw):
    """Codex household R1 MINOR pattern — every parser result
    roundtrips through TypeAdapter(spec.output_model).validate_python."""
    spec = next(s for s in TASKS_SPECS if s.name == tool)
    parsed = parse_tool_output(tool, raw)
    assert not isinstance(parsed, ToolOutputContractViolation), (
        f"unexpected violation for {tool} / {raw!r}"
    )
    adapter = TypeAdapter(spec.output_model)
    validated = adapter.validate_python(parsed.model_dump())
    assert validated.status == parsed.status


def test_tasks_typeadapter_rejects_sentinel() -> None:
    for spec in TASKS_SPECS:
        adapter = TypeAdapter(spec.output_model)
        with pytest.raises(ValidationError):
            adapter.validate_python({
                "status": "contract_violation",
                "raw_output": "garbage",
                "tool_name": spec.name,
                "timestamp": "2026-05-27T00:00:00Z",
            })


# ---------------------------------------------------------------------------
# Aggregator + quality gate
# ---------------------------------------------------------------------------


def test_migrated_tool_specs_aggregate_includes_tasks() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    tasks_names = {s.name for s in TASKS_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert tasks_names.issubset(migrated_names)


def test_migrated_tool_specs_pass_strict_with_tasks() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
