"""Integration tests for the checklists family ToolSpec instances
(Sub-A4 phase 7 — Plan-Execute Epic).

Mirrors the shopping/reminders/recipes/menu/household/tasks patterns.

Coverage:
- All 8 CHECKLISTS_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: checklists-family entries exact match
- Per-tool: input_model rejects extra keys, parsers produce
  output_model on canonical "ok:..." strings
- Tight ChecklistId / ChecklistItemId aliases
- TypeAdapter parser→output_model parity
- Structured list/show outputs (rows + items)
- ContractViolation sentinel rejection
- list_not_found / item_not_found stable error code remapping
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    AddChecklistItemsOk,
    AddChecklistItemsWithDups,
    ArchiveChecklistOk,
    CreateChecklistOk,
    DeleteChecklistItemOk,
    HousewifeToolError,
    ListChecklistsEmpty,
    ListChecklistsOk,
    ListChecklistsRow,
    MarkChecklistItemDoneOk,
    MoveTaskMovedDup,
    MoveTaskMovedOk,
    PARSERS,
    ShowChecklistEmpty,
    ShowChecklistItem,
    ShowChecklistOk,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
)
from sreda.services.tool_schemas.specs_checklists import (
    ADD_CHECKLIST_ITEMS_SPEC,
    ARCHIVE_CHECKLIST_SPEC,
    AddChecklistItemsInput,
    ArchiveChecklistInput,
    CHECKLISTS_SPECS,
    CREATE_CHECKLIST_SPEC,
    CreateChecklistInput,
    DELETE_CHECKLIST_ITEM_SPEC,
    DeleteChecklistItemInput,
    LIST_CHECKLISTS_SPEC,
    ListChecklistsInput,
    MARK_CHECKLIST_ITEM_DONE_SPEC,
    MOVE_TASK_TO_CHECKLIST_SPEC,
    MarkChecklistItemDoneInput,
    MoveTaskToChecklistInput,
    SHOW_CHECKLIST_SPEC,
    ShowChecklistInput,
)

# Real-shape IDs.
CL_A = "checklist_aaaaaaaaaaaaaaaaaaaaaaaa"
CL_B = "checklist_bbbbbbbbbbbbbbbbbbbbbbbb"
IT_A = "clitem_aaaaaaaaaaaaaaaaaaaaaaaa"
IT_B = "clitem_bbbbbbbbbbbbbbbbbbbbbbbb"
TASK_A = "task_cccccccccccccccccccccccc"


# ---------------------------------------------------------------------------
# Family-level invariants
# ---------------------------------------------------------------------------


def test_all_checklists_specs_construct() -> None:
    assert len(CHECKLISTS_SPECS) == 8
    names = {s.name for s in CHECKLISTS_SPECS}
    assert names == {
        "create_checklist", "add_checklist_items", "move_task_to_checklist",
        "list_checklists", "show_checklist",
        "mark_checklist_item_done", "delete_checklist_item", "archive_checklist",
    }


def test_checklists_family_passes_production_quality_strict() -> None:
    assert_production_registry_quality(CHECKLISTS_SPECS)


def test_manifest_matches_checklists_specs() -> None:
    manifest_checklists = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "checklists"
    }
    spec_names = {s.name for s in CHECKLISTS_SPECS}
    assert manifest_checklists == spec_names


@pytest.mark.parametrize("spec", CHECKLISTS_SPECS, ids=lambda s: s.name)
def test_every_checklists_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS


# ---------------------------------------------------------------------------
# Input models — rejection invariants
# ---------------------------------------------------------------------------


def test_create_checklist_input_real_title() -> None:
    parsed = CreateChecklistInput.model_validate({"title": "План кроя"})
    assert parsed.title == "План кроя"


def test_create_checklist_input_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        CreateChecklistInput.model_validate({"title": "   "})


def test_create_checklist_input_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        CreateChecklistInput.model_validate({"title": "x", "extra": "y"})


def test_add_checklist_items_input_real() -> None:
    parsed = AddChecklistItemsInput.model_validate({
        "list_id_or_title": "План кроя",
        "items": ["Лаванда 298", "Шампань 202"],
    })
    assert len(parsed.items) == 2


def test_add_checklist_items_input_accepts_id() -> None:
    parsed = AddChecklistItemsInput.model_validate({
        "list_id_or_title": CL_A,
        "items": ["x"],
    })
    assert parsed.list_id_or_title == CL_A


def test_add_checklist_items_input_rejects_empty_list() -> None:
    with pytest.raises(ValidationError):
        AddChecklistItemsInput.model_validate({
            "list_id_or_title": "x", "items": [],
        })


def test_add_checklist_items_input_rejects_oversized_list() -> None:
    with pytest.raises(ValidationError):
        AddChecklistItemsInput.model_validate({
            "list_id_or_title": "x",
            "items": [f"item{i}" for i in range(101)],
        })


def test_move_task_to_checklist_input_real() -> None:
    parsed = MoveTaskToChecklistInput.model_validate({
        "task_id": TASK_A, "list_id_or_title": "Дача",
    })
    assert parsed.task_id == TASK_A


def test_move_task_to_checklist_input_rejects_bad_task_id() -> None:
    with pytest.raises(ValidationError):
        MoveTaskToChecklistInput.model_validate({
            "task_id": "task_short", "list_id_or_title": "x",
        })


def test_list_checklists_input_no_args() -> None:
    parsed = ListChecklistsInput.model_validate({})
    assert parsed is not None


def test_list_checklists_input_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        ListChecklistsInput.model_validate({"foo": "bar"})


def test_show_checklist_input_real() -> None:
    parsed = ShowChecklistInput.model_validate({"list_id_or_title": "План кроя"})
    assert parsed.list_id_or_title == "План кроя"


def test_mark_checklist_item_done_input_real() -> None:
    parsed = MarkChecklistItemDoneInput.model_validate({
        "list_id_or_title": "План кроя", "item_title_match": "Лаванда",
    })
    assert parsed.item_title_match == "Лаванда"


def test_delete_checklist_item_input_real() -> None:
    parsed = DeleteChecklistItemInput.model_validate({
        "list_id_or_title": "План кроя", "item_title_match": "Шампань",
    })
    assert parsed is not None


def test_archive_checklist_input_real() -> None:
    parsed = ArchiveChecklistInput.model_validate({"list_id_or_title": "Дача"})
    assert parsed is not None


# ---------------------------------------------------------------------------
# Parsers — happy paths
# ---------------------------------------------------------------------------


def test_create_checklist_parser_ok() -> None:
    parsed = parse_tool_output("create_checklist", f"ok:created:{CL_A}:План кроя")
    assert isinstance(parsed, CreateChecklistOk)
    assert parsed.checklist_id == CL_A
    assert parsed.title == "План кроя"


def test_add_checklist_items_parser_nodups() -> None:
    parsed = parse_tool_output("add_checklist_items", f"ok:added:3:list={CL_A}")
    assert isinstance(parsed, AddChecklistItemsOk)
    assert parsed.added_count == 3
    assert parsed.duplicate_count == 0


def test_add_checklist_items_parser_withdups() -> None:
    parsed = parse_tool_output(
        "add_checklist_items", f"ok:added:2:dups:1:list={CL_A}",
    )
    assert isinstance(parsed, AddChecklistItemsWithDups)
    assert parsed.added_count == 2
    assert parsed.duplicate_count == 1


def test_add_checklist_items_parser_empty_items_error() -> None:
    parsed = parse_tool_output("add_checklist_items", "error: empty items")
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "empty_items"


def test_move_task_parser_new_item() -> None:
    parsed = parse_tool_output(
        "move_task_to_checklist", f"ok:moved:item_id={IT_A}:list={CL_A}",
    )
    assert isinstance(parsed, MoveTaskMovedOk)
    assert parsed.item_id == IT_A
    assert parsed.checklist_id == CL_A


def test_move_task_parser_dup() -> None:
    parsed = parse_tool_output(
        "move_task_to_checklist", f"ok:moved:item_id=existing:list={CL_A}:dup",
    )
    assert isinstance(parsed, MoveTaskMovedDup)
    assert parsed.checklist_id == CL_A


def test_move_task_parser_task_not_found() -> None:
    parsed = parse_tool_output("move_task_to_checklist", "error: task_not_found")
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "task_not_found"


@pytest.mark.parametrize("code", [
    "list_resolve_failed",
    "internal_add",
    "nothing_added",
])
def test_move_task_parser_partial_failure_variants(code) -> None:
    """Codex R3 MINOR (prior-not-closed): three runtime error codes
    mean «task cancelled but item not created». Parser routes them
    to typed MoveTaskPartialFailure so planner can branch and tell
    user honestly «задача отменена, но добавление в чек-лист
    провалилось»."""
    from sreda.services.tool_schemas.housewife import MoveTaskPartialFailure
    parsed = parse_tool_output("move_task_to_checklist", f"error: {code}")
    assert isinstance(parsed, MoveTaskPartialFailure)
    assert parsed.error_code == code


def test_move_task_partial_failure_typed_in_output_union() -> None:
    """MoveTaskToChecklistOutput discriminator union must include
    MoveTaskPartialFailure so TypeAdapter accepts it executor-side."""
    from pydantic import TypeAdapter
    from sreda.services.tool_schemas.housewife import MoveTaskPartialFailure
    adapter = TypeAdapter(MOVE_TASK_TO_CHECKLIST_SPEC.output_model)
    validated = adapter.validate_python({
        "status": "partial_failure",
        "error_code": "internal_add",
        "message": "internal_add",
    })
    assert validated.status == "partial_failure"


# ---------------------------------------------------------------------------
# list_checklists structured parsing
# ---------------------------------------------------------------------------


def test_list_checklists_parser_empty() -> None:
    parsed = parse_tool_output("list_checklists", "no checklists")
    assert isinstance(parsed, ListChecklistsEmpty)


def test_list_checklists_parser_single_row() -> None:
    raw = f"[{CL_A}] · План кроя · 5 pending, 2 done, 7 total"
    parsed = parse_tool_output("list_checklists", raw)
    assert isinstance(parsed, ListChecklistsOk)
    assert len(parsed.checklists) == 1
    r = parsed.checklists[0]
    assert isinstance(r, ListChecklistsRow)
    assert r.checklist_id == CL_A
    assert r.title == "План кроя"
    assert r.pending_count == 5
    assert r.done_count == 2
    assert r.total_count == 7


def test_list_checklists_parser_multiple_rows() -> None:
    raw = (
        f"[{CL_A}] · План кроя · 5 pending, 2 done, 7 total\n"
        f"[{CL_B}] · Дача · 0 pending, 3 done, 3 total"
    )
    parsed = parse_tool_output("list_checklists", raw)
    assert isinstance(parsed, ListChecklistsOk)
    assert len(parsed.checklists) == 2


def test_list_checklists_parser_rejects_total_less_than_pending_plus_done() -> None:
    """Cross-field invariant: total >= pending + done."""
    raw = f"[{CL_A}] · X · 5 pending, 5 done, 7 total"  # 5+5=10 > 7
    parsed = parse_tool_output("list_checklists", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# show_checklist structured parsing
# ---------------------------------------------------------------------------


def test_show_checklist_parser_populated() -> None:
    raw = (
        f"# План кроя ({CL_A})\n"
        f"[{IT_A}] ☐ Лаванда 298\n"
        f"[{IT_B}] ☑ Шампань 202"
    )
    parsed = parse_tool_output("show_checklist", raw)
    assert isinstance(parsed, ShowChecklistOk)
    assert parsed.checklist_id == CL_A
    assert parsed.title == "План кроя"
    assert len(parsed.items) == 2
    assert parsed.items[0].item_id == IT_A
    assert parsed.items[0].item_status == "pending"
    assert parsed.items[1].item_status == "done"


def test_show_checklist_parser_with_cancelled_mark() -> None:
    raw = f"# X ({CL_A})\n[{IT_A}] ✗ cancelled item"
    parsed = parse_tool_output("show_checklist", raw)
    assert isinstance(parsed, ShowChecklistOk)
    assert parsed.items[0].item_status == "cancelled"


def test_show_checklist_parser_empty_list() -> None:
    raw = f"empty: list={CL_A} title='План кроя'"
    parsed = parse_tool_output("show_checklist", raw)
    assert isinstance(parsed, ShowChecklistEmpty)
    assert parsed.checklist_id == CL_A
    assert parsed.title == "План кроя"


def test_show_checklist_parser_list_not_found_remapped() -> None:
    """Codex Sub-A4 checklists R1 (preempt): runtime emits generic
    `error: not_found: '<needle>'` for list-resolve miss
    (housewife_chat_tools.py:2590). Remap to checklist_list_not_found
    so planner branches uniformly across mark/delete/show/archive."""
    parsed = parse_tool_output("show_checklist", "error: not_found: 'дача'")
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "checklist_list_not_found"


# ---------------------------------------------------------------------------
# mark / delete / archive
# ---------------------------------------------------------------------------


def test_mark_done_parser_ok() -> None:
    parsed = parse_tool_output(
        "mark_checklist_item_done", f"ok:done:{IT_A}:Лаванда 298",
    )
    assert isinstance(parsed, MarkChecklistItemDoneOk)
    assert parsed.item_id == IT_A
    assert parsed.title == "Лаванда 298"


def test_mark_done_parser_list_not_found() -> None:
    parsed = parse_tool_output(
        "mark_checklist_item_done", "error: list_not_found: 'дача'",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "checklist_list_not_found"


def test_mark_done_parser_item_not_found() -> None:
    parsed = parse_tool_output(
        "mark_checklist_item_done", "error: item_not_found: 'X'",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "checklist_item_not_found"


def test_delete_item_parser_ok() -> None:
    parsed = parse_tool_output(
        "delete_checklist_item", f"ok:deleted:{IT_A}:Лаванда",
    )
    assert isinstance(parsed, DeleteChecklistItemOk)
    assert parsed.item_id == IT_A


def test_archive_parser_ok() -> None:
    parsed = parse_tool_output("archive_checklist", f"ok:archived:{CL_A}")
    assert isinstance(parsed, ArchiveChecklistOk)
    assert parsed.checklist_id == CL_A


def test_archive_parser_not_found_remapped() -> None:
    """Codex Sub-A4 checklists R1 (preempt): same generic `not_found:'<X>'`
    runtime shape (housewife_chat_tools.py:2692). Remap to
    checklist_list_not_found."""
    parsed = parse_tool_output("archive_checklist", "error: not_found: 'дача'")
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "checklist_list_not_found"


# ---------------------------------------------------------------------------
# TypeAdapter parser→output_model parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,raw", [
    ("create_checklist", f"ok:created:{CL_A}:План кроя"),
    ("create_checklist", "error: title too long"),
    ("add_checklist_items", f"ok:added:3:list={CL_A}"),
    ("add_checklist_items", f"ok:added:2:dups:1:list={CL_A}"),
    ("add_checklist_items", "error: empty items"),
    ("move_task_to_checklist", f"ok:moved:item_id={IT_A}:list={CL_A}"),
    ("move_task_to_checklist", f"ok:moved:item_id=existing:list={CL_A}:dup"),
    ("move_task_to_checklist", "error: task_not_found"),
    ("list_checklists", "no checklists"),
    ("list_checklists", f"[{CL_A}] · X · 1 pending, 0 done, 1 total"),
    ("show_checklist", f"empty: list={CL_A} title='X'"),
    ("show_checklist", f"# X ({CL_A})\n[{IT_A}] ☐ Y"),
    ("show_checklist", "error: not_found: 'X'"),
    ("mark_checklist_item_done", f"ok:done:{IT_A}:Y"),
    ("mark_checklist_item_done", "error: list_not_found: 'X'"),
    ("delete_checklist_item", f"ok:deleted:{IT_A}:Y"),
    ("archive_checklist", f"ok:archived:{CL_A}"),
    ("archive_checklist", "error: not_found: 'X'"),
])
def test_checklists_parser_outputs_validate_against_spec_output_model(tool, raw):
    """Every parser result must roundtrip through
    TypeAdapter(spec.output_model)."""
    spec = next(s for s in CHECKLISTS_SPECS if s.name == tool)
    parsed = parse_tool_output(tool, raw)
    assert not isinstance(parsed, ToolOutputContractViolation), (
        f"unexpected violation for {tool} / {raw!r}"
    )
    adapter = TypeAdapter(spec.output_model)
    validated = adapter.validate_python(parsed.model_dump())
    assert validated.status == parsed.status


def test_checklists_typeadapter_rejects_sentinel() -> None:
    for spec in CHECKLISTS_SPECS:
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


def test_migrated_tool_specs_aggregate_includes_checklists() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    checklists_names = {s.name for s in CHECKLISTS_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert checklists_names.issubset(migrated_names)


def test_migrated_tool_specs_pass_strict_with_checklists() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
