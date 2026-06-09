"""#115 — task tools (complete/uncomplete/cancel/update) return the task NAME."""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import (
    CancelTaskOk,
    CompleteTaskOk,
    UncompleteTaskOk,
    UpdateTaskOk,
    parse_cancel_task,
    parse_complete_task,
    parse_uncomplete_task,
    parse_update_task,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

TASK = "task_" + "a" * 24

CASES = [
    ("completed", "complete_task", parse_complete_task, CompleteTaskOk, "Выполнила"),
    ("uncompleted", "uncomplete_task", parse_uncomplete_task, UncompleteTaskOk, "Вернула в работу"),
    ("cancelled", "cancel_task", parse_cancel_task, CancelTaskOk, "Отменила"),
    ("updated", "update_task", parse_update_task, UpdateTaskOk, "Обновила задачу"),
]


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


@pytest.mark.parametrize("status,tool,parse,model_cls,label", CASES)
def test_okv2_carries_title(status, tool, parse, model_cls, label):
    parsed = parse(encode_tool_ok(status, {"task_id": TASK, "title": "Купить молоко"}))
    assert isinstance(parsed, model_cls)
    assert parsed.task_id == TASK
    assert parsed.title == "Купить молоко"
    assert "Купить молоко" in parsed.display_summary
    assert TASK not in parsed.display_summary
    assert label in parsed.display_summary


@pytest.mark.parametrize("status,tool,parse,model_cls,label", CASES)
def test_presenter_shows_name(status, tool, parse, model_cls, label):
    parsed = parse(encode_tool_ok(status, {"task_id": TASK, "title": "Полить цветы"}))
    text = render_display_text(tool, parsed.model_dump(), domain_status=status)
    assert "Полить цветы" in text and TASK not in text


@pytest.mark.parametrize("status,tool,parse,model_cls,label", CASES)
def test_legacy_positional_still_parses(status, tool, parse, model_cls, label):
    parsed = parse(f"ok:{status}:{TASK}")
    assert isinstance(parsed, model_cls)
    assert parsed.task_id == TASK
    assert parsed.title is None
    assert parsed.display_summary == "Готово."


@pytest.mark.parametrize("status,tool,parse,model_cls,label", CASES)
def test_blank_or_malformed_fails_closed(status, tool, parse, model_cls, label):
    assert isinstance(parse(encode_tool_ok(status, {"task_id": TASK, "title": "  "})), ToolOutputContractViolation)
    assert isinstance(parse(f"okv2:{status}:{{bad json"), ToolOutputContractViolation)
    # missing title key → unusable (None) → sentinel
    assert isinstance(parse(encode_tool_ok(status, {"task_id": TASK})), ToolOutputContractViolation)
