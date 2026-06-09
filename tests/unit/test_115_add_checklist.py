"""#115 — add_checklist_items returns added/duplicate item NAMES (red-before-impl)."""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import (
    AddChecklistItemsOk,
    AddChecklistItemsWithDups,
    parse_add_checklist_items,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

CL = "checklist_" + "a" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


def test_added_okv2_shows_names():
    raw = encode_tool_ok(
        "added",
        {"added_count": 2, "duplicate_count": 0, "checklist_id": CL,
         "created": ["Купить муку", "Купить яйца"]},
    )
    parsed = parse_add_checklist_items(raw)
    assert isinstance(parsed, AddChecklistItemsOk)
    assert parsed.created == ["Купить муку", "Купить яйца"]
    s = parsed.display_summary
    assert "Купить муку" in s and "Купить яйца" in s and CL not in s


def test_added_with_dups_okv2_shows_both():
    raw = encode_tool_ok(
        "added_with_dups",
        {"added_count": 1, "duplicate_count": 1, "checklist_id": CL,
         "created": ["Молоко"], "duplicates_existing": ["Хлеб"]},
    )
    parsed = parse_add_checklist_items(raw)
    assert isinstance(parsed, AddChecklistItemsWithDups)
    assert parsed.created == ["Молоко"]
    assert parsed.duplicates_existing == ["Хлеб"]
    s = parsed.display_summary
    assert "Молоко" in s and "Хлеб" in s


def test_presenter_shows_names():
    raw = encode_tool_ok(
        "added", {"added_count": 1, "duplicate_count": 0, "checklist_id": CL, "created": ["Помыть пол"]},
    )
    parsed = parse_add_checklist_items(raw)
    text = render_display_text("add_checklist_items", parsed.model_dump(), domain_status="added")
    assert "Помыть пол" in text and CL not in text


def test_legacy_positional_still_parses():
    parsed = parse_add_checklist_items(f"ok:added:2:list={CL}")
    assert isinstance(parsed, AddChecklistItemsOk)
    assert parsed.added_count == 2
    assert parsed.created == []


def test_legacy_withdups_still_parses():
    parsed = parse_add_checklist_items(f"ok:added:1:dups:2:list={CL}")
    assert isinstance(parsed, AddChecklistItemsWithDups)
    assert parsed.added_count == 1 and parsed.duplicate_count == 2


def test_malformed_okv2_sentinel():
    assert isinstance(parse_add_checklist_items("okv2:added:{bad"), ToolOutputContractViolation)


def test_name_count_mismatch_sentinel():
    raw = encode_tool_ok(
        "added", {"added_count": 2, "duplicate_count": 0, "checklist_id": CL, "created": ["Один"]},
    )
    assert isinstance(parse_add_checklist_items(raw), ToolOutputContractViolation)


def test_blank_name_sentinel():
    raw = encode_tool_ok(
        "added", {"added_count": 1, "duplicate_count": 0, "checklist_id": CL, "created": ["  "]},
    )
    assert isinstance(parse_add_checklist_items(raw), ToolOutputContractViolation)


def test_added_with_nonzero_duplicate_count_sentinel():
    # Codex #115 [MAJOR]: status "added" (no-dups variant) must reject duplicate_count>0.
    raw = encode_tool_ok(
        "added",
        {"added_count": 1, "duplicate_count": 1, "checklist_id": CL, "created": ["Молоко"]},
    )
    assert isinstance(parse_add_checklist_items(raw), ToolOutputContractViolation)


def test_dups_count_mismatch_sentinel():
    raw = encode_tool_ok(
        "added_with_dups",
        {"added_count": 1, "duplicate_count": 2, "checklist_id": CL,
         "created": ["Молоко"], "duplicates_existing": ["Хлеб"]},  # 1 dup name for count=2
    )
    assert isinstance(parse_add_checklist_items(raw), ToolOutputContractViolation)
