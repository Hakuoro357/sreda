"""#115 — add_task returns the task NAME (+ container item names)."""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import (
    AddTaskCreated,
    AddTaskCreatedWithChecklist,
    AddTaskCreatedWithReminder,
    parse_add_task,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

TASK = "task_" + "a" * 24
CHK = "checklist_" + "b" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


def test_created_okv2_name():
    parsed = parse_add_task(encode_tool_ok("created", {"task_id": TASK, "task_title": "Позвонить врачу"}))
    assert isinstance(parsed, AddTaskCreated)
    assert parsed.task_title == "Позвонить врачу"
    assert "Позвонить врачу" in parsed.display_summary and TASK not in parsed.display_summary


def test_created_with_reminder_okv2():
    parsed = parse_add_task(
        encode_tool_ok("created_with_reminder", {"task_id": TASK, "reminder_offset_minutes": 15, "task_title": "Лекарство"})
    )
    assert isinstance(parsed, AddTaskCreatedWithReminder)
    assert parsed.reminder_offset_minutes == 15
    assert "Лекарство" in parsed.display_summary


def test_created_with_checklist_okv2_shows_task_and_items():
    parsed = parse_add_task(
        encode_tool_ok("created_with_checklist", {
            "task_id": TASK, "checklist_id": CHK, "task_title": "Сборы",
            "details_added": ["паспорт", "билеты", "зарядка"],
        })
    )
    assert isinstance(parsed, AddTaskCreatedWithChecklist)
    assert parsed.details_added == ["паспорт", "билеты", "зарядка"]
    s = parsed.display_summary
    assert "Сборы" in s and "паспорт" in s and "билеты" in s
    text = render_display_text("add_task", parsed.model_dump(), domain_status="created_with_checklist")
    assert "Сборы" in text and "паспорт" in text and TASK not in text


def test_legacy_positional_variants():
    p1 = parse_add_task(f"ok:created:{TASK}")
    assert isinstance(p1, AddTaskCreated) and p1.task_title is None and p1.display_summary == "Готово."
    p2 = parse_add_task(f"ok:created:{TASK}:reminder=за 15мин")
    assert isinstance(p2, AddTaskCreatedWithReminder) and p2.reminder_offset_minutes == 15
    p3 = parse_add_task(f"ok:created:{TASK}:checklist={CHK}")
    assert isinstance(p3, AddTaskCreatedWithChecklist)


def test_blank_or_malformed_fails_closed():
    assert isinstance(parse_add_task(encode_tool_ok("created", {"task_id": TASK, "task_title": "  "})), ToolOutputContractViolation)
    assert isinstance(parse_add_task(encode_tool_ok("created", {"task_id": TASK})), ToolOutputContractViolation)  # no title
    assert isinstance(parse_add_task("okv2:created:{bad json"), ToolOutputContractViolation)


def test_container_empty_details_fails_closed():
    # Codex #115 [MAJOR]: okv2 created_with_checklist MUST carry non-empty item names.
    for payload in (
        {"task_id": TASK, "checklist_id": CHK, "task_title": "Сборы", "details_added": []},
        {"task_id": TASK, "checklist_id": CHK, "task_title": "Сборы"},  # key missing
    ):
        assert isinstance(
            parse_add_task(encode_tool_ok("created_with_checklist", payload)),
            ToolOutputContractViolation,
        )


def test_container_blank_detail_item_fails_closed():
    raw = encode_tool_ok("created_with_checklist", {
        "task_id": TASK, "checklist_id": CHK, "task_title": "Сборы",
        "details_added": ["паспорт", "   "],  # one blank item
    })
    assert isinstance(parse_add_task(raw), ToolOutputContractViolation)
