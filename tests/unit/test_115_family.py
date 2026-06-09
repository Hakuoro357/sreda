"""#115 — family tools return affected NAMES (red-before-impl).

add_family_members: batch outcome-object (created / duplicates_existing /
duplicates_in_batch / invalid by name). update_family_member: the updated name.
Tested at the wire→parser→model→presenter surface (#74 deliverable); the okv2 wire
string is exactly what the chat-tool emits. Plus a service-level test of the new
add_members_batch_detailed bucket attribution (housewife_family imports cleanly).
"""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import (
    AddFamilyMembersOk,
    UpdateFamilyMemberOk,
    parse_add_family_members,
    parse_update_family_member,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

FM_A = "fm_" + "a" * 24
FM_B = "fm_" + "b" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


def test_add_okv2_groups_by_name():
    raw = encode_tool_ok(
        "added",
        {
            "added_count": 2,
            "skipped_as_duplicate": 2,
            "member_ids": [FM_A, FM_B],
            "created": ["Маша", "Петя"],
            "duplicates_existing": ["Аня"],
            "duplicates_in_batch": ["Маша"],
            "invalid": [],
        },
    )
    parsed = parse_add_family_members(raw)
    assert isinstance(parsed, AddFamilyMembersOk)
    assert parsed.created == ["Маша", "Петя"]
    assert parsed.duplicates_existing == ["Аня"]
    assert parsed.duplicates_in_batch == ["Маша"]
    s = parsed.display_summary
    assert "Маша" in s and "Петя" in s and "Аня" in s
    assert FM_A not in s


def test_add_all_duplicate_okv2():
    raw = encode_tool_ok(
        "added",
        {
            "added_count": 0,
            "skipped_as_duplicate": 1,
            "member_ids": [],
            "created": [],
            "duplicates_existing": ["Аня"],
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    parsed = parse_add_family_members(raw)
    assert isinstance(parsed, AddFamilyMembersOk)
    assert "Аня" in parsed.display_summary
    assert "Уже были" in parsed.display_summary


def test_add_legacy_positional_still_parses():
    parsed = parse_add_family_members(
        f"ok:added:2:skipped_as_duplicate:1:ids=[{FM_A},{FM_B}]"
    )
    assert isinstance(parsed, AddFamilyMembersOk)
    assert parsed.member_ids == [FM_A, FM_B]
    assert parsed.created == []  # legacy carried no names


def test_add_created_count_name_mismatch_fails_closed():
    raw = encode_tool_ok(
        "added",
        {
            "added_count": 2,
            "skipped_as_duplicate": 0,
            "member_ids": [FM_A, FM_B],
            "created": ["Маша"],  # 1 name for added_count=2
            "duplicates_existing": [],
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    assert isinstance(parse_add_family_members(raw), ToolOutputContractViolation)


def test_add_presenter_shows_names():
    raw = encode_tool_ok(
        "added",
        {
            "added_count": 1, "skipped_as_duplicate": 0, "member_ids": [FM_A],
            "created": ["Маша"], "duplicates_existing": [],
            "duplicates_in_batch": [], "invalid": [],
        },
    )
    parsed = parse_add_family_members(raw)
    text = render_display_text("add_family_members", parsed.model_dump(), domain_status="added")
    assert "Маша" in text and FM_A not in text


def test_update_okv2_shows_name():
    parsed = parse_update_family_member(encode_tool_ok("updated", {"name": "Маша"}))
    assert isinstance(parsed, UpdateFamilyMemberOk)
    assert parsed.name == "Маша"
    assert "Маша" in parsed.display_summary


def test_update_legacy_still_parses():
    parsed = parse_update_family_member("ok:updated")
    assert isinstance(parsed, UpdateFamilyMemberOk)
    assert parsed.name is None
    assert parsed.display_summary == "Готово."


def test_update_okv2_blank_name_fails_closed():
    # Codex #115 [MAJOR]: okv2 must carry a usable name (only legacy is nameless).
    # Includes sanitizer-only-empty names (R4): control-only / guillemet-only.
    for bad in ({}, {"name": ""}, {"name": "   "}, {"name": "\x07\x07"}, {"name": "«»"}):
        assert isinstance(
            parse_update_family_member(encode_tool_ok("updated", bad)),
            ToolOutputContractViolation,
        ), bad


def test_add_okv2_missing_buckets_fails_closed():
    # Codex #115 [MAJOR]: okv2 must send all four name-bucket keys.
    raw = encode_tool_ok(
        "added",
        {"added_count": 0, "skipped_as_duplicate": 1, "member_ids": []},
    )
    assert isinstance(parse_add_family_members(raw), ToolOutputContractViolation)


def test_add_okv2_blank_name_fails_closed():
    # Codex #115 R2 [MAJOR]: a blank name passes length checks but the voice
    # would drop it → fail closed.
    raw = encode_tool_ok(
        "added",
        {
            "added_count": 1,
            "skipped_as_duplicate": 0,
            "member_ids": [FM_A],
            "created": ["   "],  # blank
            "duplicates_existing": [],
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    assert isinstance(parse_add_family_members(raw), ToolOutputContractViolation)


def test_add_okv2_control_only_name_fails_closed():
    # Codex #115 R3 [MAJOR]: a name that survives strip() but sanitizes to empty
    # (control chars / guillemets only) must fail closed — aligned to the renderer.
    for bad in ("\x07\x07", "«»"):
        raw = encode_tool_ok(
            "added",
            {
                "added_count": 1,
                "skipped_as_duplicate": 0,
                "member_ids": [FM_A],
                "created": [bad],
                "duplicates_existing": [],
                "duplicates_in_batch": [],
                "invalid": [],
            },
        )
        assert isinstance(parse_add_family_members(raw), ToolOutputContractViolation)


def test_add_okv2_bucket_overflow_fails_closed():
    # named skipped buckets cannot exceed the aggregate skipped count
    raw = encode_tool_ok(
        "added",
        {
            "added_count": 0,
            "skipped_as_duplicate": 1,
            "member_ids": [],
            "created": [],
            "duplicates_existing": ["Аня", "Маша", "Петя"],  # 3 > skipped=1
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    assert isinstance(parse_add_family_members(raw), ToolOutputContractViolation)


# --- service-level bucket attribution (no chat-tool / pymorphy3) ---

def test_add_members_batch_detailed_buckets(db_session):
    from sreda.services.housewife_family import HousewifeFamilyService

    svc = HousewifeFamilyService(db_session)
    # seed an existing member
    svc.add_member(tenant_id="t1", user_id="u1", name="Аня", role="child")
    result = svc.add_members_batch_detailed(
        tenant_id="t1",
        user_id="u1",
        members=[
            {"name": "Маша", "role": "child"},      # created
            {"name": "Маша", "role": "child"},      # within-batch dup
            {"name": "Аня", "role": "child"},       # existing dup
            {"name": "Петя", "role": "bad_role"},   # invalid (bad role)
        ],
    )
    assert [m.name for m in result.created] == ["Маша"]
    assert result.duplicates_in_batch == ["Маша"]
    assert result.duplicates_existing == ["Аня"]
    assert result.invalid == ["Петя"]
    # back-compat wrapper still returns just the created rows
    created_only = svc.add_members_batch(
        tenant_id="t1", user_id="u1", members=[{"name": "Вова", "role": "child"}]
    )
    assert [m.name for m in created_only] == ["Вова"]
