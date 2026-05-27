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


def test_list_tasks_parser_structured_row() -> None:
    """Codex R1 MAJOR #3: structured rows (was raw_text). Parser
    decomposes ``_fmt_task_for_llm`` output into typed fields so
    update/complete/cancel/delete can reference ``${list.tasks[i].task_id}``."""
    from sreda.services.tool_schemas.housewife import ListTasksRow
    raw = (
        f"[{TASK_A}] · утренняя разминка · on 2026-05-28 07:00–07:30 "
        "· recurring=FREQ=DAILY · reminder=за 15мин "
        "· notes=не забудь воду"
    )
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ListTasksOk)
    assert len(parsed.tasks) == 1
    t = parsed.tasks[0]
    assert isinstance(t, ListTasksRow)
    assert t.task_id == TASK_A
    assert t.title == "утренняя разминка"
    assert t.scheduled_date_iso == "2026-05-28"
    assert t.time_start == "07:00"
    assert t.time_end == "07:30"
    assert t.recurrence_rule == "FREQ=DAILY"
    assert t.reminder_offset_minutes == 15
    assert t.notes == "не забудь воду"


def test_list_tasks_parser_multiple_rows() -> None:
    """One task per line."""
    raw = (
        f"[{TASK_A}] · разминка · on 2026-05-28 07:00\n"
        f"[{TASK_B}] · зарядка · status=completed"
    )
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ListTasksOk)
    assert len(parsed.tasks) == 2
    assert parsed.tasks[1].runtime_status == "completed"


def test_list_tasks_parser_inbox_task_no_date() -> None:
    """Inbox tasks have no `on YYYY-MM-DD` segment — runtime
    suppresses the date when scheduled_date is None."""
    parsed = parse_tool_output("list_tasks", f"[{TASK_A}] · read book")
    assert isinstance(parsed, ListTasksOk)
    assert parsed.tasks[0].scheduled_date_iso is None
    assert parsed.tasks[0].time_start is None


def test_list_tasks_parser_unknown_segment_becomes_title_part() -> None:
    """Codex R2 MAJOR (new): robust parser walks title segments
    until it finds the FIRST known suffix prefix. Unknown segments
    BEFORE any known prefix are concatenated as part of the title
    (preserves legitimate ` · ` in user input). To trigger
    ContractViolation, a known prefix with bad value is needed
    (see test_list_tasks_parser_rejects_bad_runtime_status)."""
    raw = f"[{TASK_A}] · разминка · totally_weird_segment"
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ListTasksOk)
    # Unknown segment merged into title.
    assert parsed.tasks[0].title == "разминка · totally_weird_segment"


def test_list_tasks_parser_rejects_segment_after_known_prefix() -> None:
    """Once a known suffix prefix appears, subsequent unrecognised
    segments ARE drift (suffix region is structured). For example
    `on YYYY-MM-DD · foo=bar` — `foo=bar` doesn't match any known
    prefix and comes AFTER `on` → ContractViolation."""
    raw = f"[{TASK_A}] · разминка · on 2026-05-28 · weird_segment"
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_list_tasks_parser_rejects_bad_task_id_shape() -> None:
    parsed = parse_tool_output("list_tasks", "[task_short] · foo")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_list_tasks_parser_rejects_bad_runtime_status() -> None:
    """status=unknown is runtime drift — only completed/cancelled
    are valid (pending is implicit when omitted)."""
    raw = f"[{TASK_A}] · разминка · status=unknown"
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


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
    ("list_tasks", f"[{TASK_A}] · разминка · on 2026-05-28 07:00"),
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


# ---------------------------------------------------------------------------
# Codex R1 fixes — boundary tests
# ---------------------------------------------------------------------------


def test_add_task_input_rejects_empty_details_list() -> None:
    """Codex R1 MAJOR #4: empty details_items list would be a no-op
    runtime-side (truthy check), but planner thinks it asked for a
    checklist. Reject at schema time."""
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({
            "title": "x",
            "details_items": [],
        })


def test_add_task_input_rejects_reminder_above_week() -> None:
    """Codex R1 MINOR #6: cap matches AttachReminderInput at 10080
    (7 days). Inconsistency with AttachReminder was the bug."""
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({
            "title": "x",
            "scheduled_date": "tomorrow",
            "time_start": "08:00",
            "reminder_offset_minutes": 10081,
        })


def test_update_task_input_rejects_inbox_scheduled_date() -> None:
    """Codex R1 MAJOR #2: ``scheduled_date='inbox'`` in update is a
    silent no-op runtime-side (None means «leave as-is»). Reject
    until runtime gets explicit clear-date sentinel."""
    with pytest.raises(ValidationError) as exc:
        UpdateTaskInput.model_validate({
            "task_id": TASK_A,
            "scheduled_date": "inbox",
        })
    assert "inbox" in str(exc.value).lower()


def test_update_task_spec_has_required_any_non_null_args() -> None:
    """Codex R1 MAJOR #1: validator-driven Phase 1.d guard catches
    refs-bearing no-op updates that model_validator misses (plan
    validator skips Pydantic model_validators for ref'd args)."""
    assert UPDATE_TASK_SPEC.required_any_non_null_args == [
        "title",
        "scheduled_date",
        "time_start",
        "time_end",
        "recurrence_rule",
        "notes",
    ]


@pytest.mark.parametrize("raw,expected_code", [
    (
        f"error: task_already_linked:{TASK_A}:checklist_aaaaaaaaaaaaaaaaaaaaaaaa. "
        "Unlink сначала через unlink_task.",
        "task_already_linked",
    ),
    (
        "error: checklist_already_linked_to_task_aaaaaaaaaaaaaaaaaaaaaaaaaaaa. "
        "Сначала unlink другую задачу через unlink_task.",
        "checklist_already_linked",
    ),
])
def test_link_task_conflict_error_codes_are_stable(raw, expected_code):
    """Codex R1 MAJOR #5: link/unlink conflict messages embed
    dynamic task/checklist ids; without stable patterns the
    fallback `_parse_error` produces per-id codes like
    `task_already_linked_task_X_checklist_Y` — planner branching
    becomes nondeterministic. Stable patterns map all dynamic
    variants to one branch."""
    parsed = parse_tool_output("link_task_to_checklist", raw)
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == expected_code


def test_link_task_not_found_remapped_to_task() -> None:
    """Codex R3 MINOR: use ACTUAL runtime shape from
    ``TaskService.link_to_checklist`` (tasks.py:238) — emits
    ``("error:not_found", "task_not_found")`` → chat_tool wraps to
    ``error:not_found: task_not_found``. Pre-R3 test used a fictional
    prose string; brittle to runtime wording changes."""
    parsed = parse_tool_output(
        "link_task_to_checklist",
        "error:not_found: task_not_found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "link_task_not_found"


def test_link_task_not_found_remapped_to_checklist() -> None:
    """Actual runtime shape from tasks.py:250 — ``("error:not_found",
    "checklist_not_found")`` → ``error:not_found: checklist_not_found``."""
    parsed = parse_tool_output(
        "link_task_to_checklist",
        "error:not_found: checklist_not_found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "link_checklist_not_found"


def test_link_task_not_found_ambiguous_keeps_generic_code() -> None:
    """Fictional shape — if runtime ever emits a both-keywords or
    no-keyword `not_found:...`, planner gets the ambiguous code
    and asks the user. Defensive coverage."""
    parsed = parse_tool_output(
        "link_task_to_checklist",
        "error:not_found: relation between task X and checklist Y broken",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "link_target_not_found"


def test_link_task_archived_stable_code() -> None:
    """Actual runtime shape from tasks.py:252 — ``("error:archived",
    f"checklist_status={chk.status}")`` → ``error:archived:
    checklist_status=archived``."""
    parsed = parse_tool_output(
        "link_task_to_checklist", "error:archived: checklist_status=archived",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "checklist_archived"


def test_link_task_remapping_does_not_leak_to_other_tools() -> None:
    """Codex R2 MAJOR (new-introduced): generic `not_found:*` /
    `archived:*` patterns are NOT in _STABLE_ERROR_PATTERNS to
    avoid hijacking other tools' messages. Verify update_task
    `task '...' not found` still resolves via the
    `task_not_found` stable pattern, not the link-scoped remap."""
    parsed = parse_tool_output(
        "update_task", f"error: task '{TASK_B}' not found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "task_not_found"  # NOT link_task_not_found


# ---------------------------------------------------------------------------
# Codex R2 MAJOR (new) — ListTasksOk robustness
# ---------------------------------------------------------------------------


def test_list_tasks_parser_title_with_middot() -> None:
    """Codex R2 MAJOR (new): title can contain ` · ` (Russian
    middot is a legitimate character). Parser walks left-to-right
    consuming title segments until first known suffix prefix."""
    raw = f"[{TASK_A}] · купить · хлеб и молоко · on 2026-05-28"
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ListTasksOk)
    assert parsed.tasks[0].title == "купить · хлеб и молоко"
    assert parsed.tasks[0].scheduled_date_iso == "2026-05-28"


def test_list_tasks_parser_notes_with_middot() -> None:
    """Notes is always last per `_fmt_task_for_llm`. Everything
    after the first ` · notes=` token is the notes payload —
    can contain `·` freely (multi-step note bodies)."""
    raw = f"[{TASK_A}] · разминка · notes=шаг 1 · шаг 2 · шаг 3"
    parsed = parse_tool_output("list_tasks", raw)
    assert isinstance(parsed, ListTasksOk)
    assert parsed.tasks[0].notes == "шаг 1 · шаг 2 · шаг 3"


def test_list_tasks_parser_documented_limitation_title_lookalike() -> None:
    """Codex R3 MAJOR (acknowledged fundamental limitation):
    `_fmt_task_for_llm` emits unescaped ` · ` between segments
    AND title comes from free-form user input. If a real user
    types «купить · status=blocked» as a title, the parser
    cannot tell user content from a real `status=blocked` segment.

    This is the same class of ambiguity Codex R2 #4 raised — solving
    it fully requires either machine-readable output (JSON / TLV) or
    escaping `·` in user content at runtime. Both are runtime
    changes; Sub-A4 boundary stops at planner contracts.

    DOCUMENTED LIMITATION (Phase B follow-up):
    - If title ENDS with ` · status=...` (or any known suffix
      prefix), the parser claims that suffix and shortens title.
    - The planner may receive runtime_status=`blocked` (rejected
      as bad-runtime-status → ContractViolation) for a task that
      really had pending status and user-typed title.

    This test pins the current behavior so future fixes are
    explicit. Fix path: emit `_fmt_task_for_llm` as JSON, OR
    escape `·` in title/notes at runtime, OR add explicit
    segment markers.
    """
    # User-typed title that LOOKS LIKE a status segment.
    raw = f"[{TASK_A}] · купить · status=completed"
    parsed = parse_tool_output("list_tasks", raw)
    # Current (limited) behavior: parses as status=completed,
    # title='купить'. The planner sees runtime_status=completed
    # for what was actually a pending task with funny title.
    assert isinstance(parsed, ListTasksOk)
    assert parsed.tasks[0].title == "купить"
    assert parsed.tasks[0].runtime_status == "completed"
    # When this test starts failing because the fix landed, update
    # it to assert ContractViolation OR the correct interpretation
    # (title='купить · status=completed', runtime_status=None).


def test_list_tasks_ok_allows_empty_tasks_list() -> None:
    """Codex R2 MAJOR (new): ListTasksOk.tasks no longer has
    min_length=1. Runtime routes empty rows via the «no tasks»
    string (→ ListTasksEmpty), but defensive contract allows
    `tasks=[]` here too so future runtime drift doesn't
    ContractViolation."""
    parsed = ListTasksOk(tasks=[])
    assert parsed.tasks == []


# ---------------------------------------------------------------------------
# Codex R2 MAJOR (new) — non-destructive inbox-update error message
# ---------------------------------------------------------------------------


def test_add_task_rejects_recurrence_without_scheduled_date() -> None:
    """A/B study finding (HIGH-reasoning catch MEDIUM missed):
    runtime expander filters recurring tasks by
    ``scheduled_date.isnot(None)`` (tasks.py:701). A recurring task
    without a date would be a silent orphan — never expand into
    any view. Reject at schema time."""
    with pytest.raises(ValidationError) as exc:
        AddTaskInput.model_validate({
            "title": "ежедневная медитация",
            "recurrence_rule": "FREQ=DAILY",
        })
    assert "recurrence_rule" in str(exc.value)
    assert "scheduled_date" in str(exc.value)


def test_add_task_rejects_recurrence_with_inbox() -> None:
    """inbox-recurring task = same orphan problem."""
    with pytest.raises(ValidationError):
        AddTaskInput.model_validate({
            "title": "ежедневная медитация",
            "scheduled_date": "inbox",
            "recurrence_rule": "FREQ=DAILY",
        })


def test_add_task_accepts_recurrence_with_dated_schedule() -> None:
    """Happy path: recurrence + dated schedule (any concrete date,
    today, or tomorrow) is the only valid shape for runtime
    expansion."""
    for date_val in ("today", "tomorrow", "2026-05-30"):
        parsed = AddTaskInput.model_validate({
            "title": "ежедневная медитация",
            "scheduled_date": date_val,
            "time_start": "07:00",
            "recurrence_rule": "FREQ=DAILY",
        })
        assert parsed.recurrence_rule == "FREQ=DAILY"


def test_recurrence_rule_cap_matches_db_column_255() -> None:
    """A/B study finding (HIGH-reasoning catch MEDIUM missed):
    DB columns ``housewife.py:62`` and ``tasks.py:90`` are both
    String(255). Pre-A/B alias cap was 512 → silent DB truncation
    on long RRULEs. Verify schema cap is now 255."""
    from sreda.services.tool_schemas.common import RecurrenceRule
    from pydantic import TypeAdapter
    adapter = TypeAdapter(RecurrenceRule)
    # Construct rule longer than 255 chars — must reject (silent
    # DB truncation prevented). 30 × INTERVAL=1 = 330 chars > 255.
    rule_too_long = "FREQ=DAILY" + ";INTERVAL=1" * 30
    assert len(rule_too_long) > 255
    with pytest.raises(ValidationError):
        adapter.validate_python(rule_too_long)
    # Sanity: short valid rule still works.
    adapter.validate_python("FREQ=DAILY")
    adapter.validate_python("FREQ=WEEKLY;BYDAY=MO,WE,FR")


def test_inbox_update_rejection_does_not_recommend_destructive_action() -> None:
    """Codex R2 MAJOR (new): R1 error message said «делай
    delete_task + add_task» which is destructive (loses
    task_id, reminder, checklist, recurrence). R2 wording must
    NOT recommend that — only suggest «ask user» / wait for
    explicit clear-date sentinel."""
    with pytest.raises(ValidationError) as exc:
        UpdateTaskInput.model_validate({
            "task_id": TASK_A, "scheduled_date": "inbox",
        })
    msg = str(exc.value)
    # Must mention asking the user — non-destructive path.
    assert "ASK" in msg.upper() or "спроси" in msg.lower(), (
        "Error message must direct the planner to ASK the user "
        "rather than recommend a destructive workaround"
    )
    # Must NOT recommend «delete + add» as a path to follow.
    # (Allowed to mention it AS the destructive thing to AVOID.)
    msg_lower = msg.lower()
    assert "destructive" in msg_lower or "потер" in msg_lower, (
        "Message must explicitly label the «delete+add» workaround "
        "as destructive so the planner doesn't follow it"
    )
