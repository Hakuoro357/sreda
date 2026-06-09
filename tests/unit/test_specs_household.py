"""Integration tests for the household family ToolSpec instances
(Sub-A4 phase 5 — Plan-Execute Epic).

Mirrors the shopping/reminders/recipes/menu patterns (all at NSC).

Coverage:
- All 4 HOUSEHOLD_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: household-family entries exact match
- Per-tool: input_model rejects extra keys, parsers produce
  output_model on canonical "ok:..." strings
- Tight FamilyMemberId aliases
- FamilyRole Literal enforcement
- Cross-family mutex_note reference scope (search_recipes /
  plan_week_menu / generate_shopping_from_menu are visible)
- Sentinel boundary regression
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    AddFamilyMembersOk,
    HousewifeToolError,
    ListFamilyMembersEmpty,
    ListFamilyMembersOk,
    ListFamilyMembersRow,
    PARSERS,
    RemoveFamilyMemberOk,
    UpdateFamilyMemberOk,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
)
from sreda.services.tool_schemas.specs_household import (
    ADD_FAMILY_MEMBERS_SPEC,
    AddFamilyMembersInput,
    FamilyMemberDraft,
    HOUSEHOLD_SPECS,
    LIST_FAMILY_MEMBERS_SPEC,
    ListFamilyMembersInput,
    REMOVE_FAMILY_MEMBER_SPEC,
    RemoveFamilyMemberInput,
    UPDATE_FAMILY_MEMBER_SPEC,
    UpdateFamilyMemberInput,
)

# Real-shape IDs (24 hex chars).
FM_A = "fm_aaaaaaaaaaaaaaaaaaaaaaaa"
FM_B = "fm_bbbbbbbbbbbbbbbbbbbbbbbb"
FM_C = "fm_cccccccccccccccccccccccc"


# ---------------------------------------------------------------------------
# Family-level invariants
# ---------------------------------------------------------------------------


def test_all_household_specs_construct() -> None:
    assert len(HOUSEHOLD_SPECS) == 4
    names = {s.name for s in HOUSEHOLD_SPECS}
    assert names == {
        "add_family_members",
        "list_family_members",
        "update_family_member",
        "remove_family_member",
    }


def test_household_family_passes_production_quality_strict() -> None:
    """Codex Sub-A4 menu R1 MAJOR #2: scope is HOUSEHOLD_SPECS,
    `migrated_specs=MIGRATED_TOOL_SPECS` so cross-family references
    to other migrated tools (e.g. search_recipes,
    plan_week_menu) don't false-positive."""
    assert_production_registry_quality(HOUSEHOLD_SPECS)


def test_manifest_matches_household_specs() -> None:
    manifest_household = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "household"
    }
    spec_names = {s.name for s in HOUSEHOLD_SPECS}
    assert manifest_household == spec_names, (
        f"manifest household entries {manifest_household!r} must "
        f"match HOUSEHOLD_SPECS {spec_names!r}"
    )


@pytest.mark.parametrize("spec", HOUSEHOLD_SPECS, ids=lambda s: s.name)
def test_every_household_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS, (
        f"{spec.name} declared in HOUSEHOLD_SPECS but no parser "
        f"registered — wrapper would fail-closed on every invocation"
    )


# ---------------------------------------------------------------------------
# Input model — FamilyMemberDraft
# ---------------------------------------------------------------------------


def test_family_member_draft_minimal() -> None:
    parsed = FamilyMemberDraft.model_validate({"name": "Маша", "role": "child"})
    assert parsed.name == "Маша"
    assert parsed.role == "child"
    assert parsed.birth_year is None


def test_family_member_draft_all_fields() -> None:
    parsed = FamilyMemberDraft.model_validate({
        "name": "Маша",
        "role": "child",
        "birth_year": 2017,
        "age_hint": "8 лет",
        "notes": "аллергия на горчицу",
    })
    assert parsed.birth_year == 2017


def test_family_member_draft_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError) as exc:
        FamilyMemberDraft.model_validate({"name": "Маша", "role": "friend"})
    assert "friend" in str(exc.value) or "role" in str(exc.value)


def test_family_member_draft_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        FamilyMemberDraft.model_validate({"name": "   ", "role": "child"})


def test_family_member_draft_rejects_implausible_birth_year() -> None:
    with pytest.raises(ValidationError):
        FamilyMemberDraft.model_validate({
            "name": "Маша", "role": "child", "birth_year": 1800,
        })
    with pytest.raises(ValidationError):
        FamilyMemberDraft.model_validate({
            "name": "Маша", "role": "child", "birth_year": 2200,
        })


def test_family_member_draft_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        FamilyMemberDraft.model_validate({
            "name": "Маша", "role": "child", "favorite_color": "blue",
        })


# ---------------------------------------------------------------------------
# Input model — AddFamilyMembersInput
# ---------------------------------------------------------------------------


def test_add_family_members_input_accepts_one() -> None:
    parsed = AddFamilyMembersInput.model_validate({
        "members": [{"name": "Маша", "role": "child"}]
    })
    assert len(parsed.members) == 1


def test_add_family_members_input_accepts_batch() -> None:
    parsed = AddFamilyMembersInput.model_validate({
        "members": [
            {"name": "Катя", "role": "spouse"},
            {"name": "Никита", "role": "child", "birth_year": 2015},
            {"name": "Маша", "role": "child", "age_hint": "8 лет"},
        ]
    })
    assert len(parsed.members) == 3


def test_add_family_members_input_rejects_empty_batch() -> None:
    with pytest.raises(ValidationError) as exc:
        AddFamilyMembersInput.model_validate({"members": []})
    assert "min_length" in str(exc.value) or "at least" in str(exc.value)


def test_add_family_members_input_rejects_oversized_batch() -> None:
    # max 20
    with pytest.raises(ValidationError):
        AddFamilyMembersInput.model_validate({
            "members": [
                {"name": f"Member{i}", "role": "other"} for i in range(21)
            ]
        })


# ---------------------------------------------------------------------------
# Input model — UpdateFamilyMemberInput
# ---------------------------------------------------------------------------


def test_update_family_member_input_accepts_single_field() -> None:
    parsed = UpdateFamilyMemberInput.model_validate({
        "member_id": FM_A,
        "notes": "новая аллергия",
    })
    assert parsed.member_id == FM_A
    assert parsed.notes == "новая аллергия"


def test_update_family_member_input_accepts_all_fields() -> None:
    parsed = UpdateFamilyMemberInput.model_validate({
        "member_id": FM_A,
        "name": "Маша",
        "role": "child",
        "birth_year": 2017,
        "age_hint": "8 лет",
        "notes": "аллергия",
    })
    assert parsed.role == "child"


def test_update_family_member_input_rejects_empty_update() -> None:
    """At least ONE updatable field must be non-None — empty
    update is a no-op runtime + planner mistake."""
    with pytest.raises(ValidationError) as exc:
        UpdateFamilyMemberInput.model_validate({"member_id": FM_A})
    assert "at least one" in str(exc.value)


def test_update_family_member_input_rejects_bad_member_id() -> None:
    with pytest.raises(ValidationError):
        UpdateFamilyMemberInput.model_validate({
            "member_id": "fm_short", "notes": "x"
        })


def test_update_family_member_input_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        UpdateFamilyMemberInput.model_validate({
            "member_id": FM_A, "role": "uncle"
        })


def test_update_family_member_input_accepts_empty_notes_as_clear() -> None:
    """Codex Sub-A4 household R1 MAJOR #4: empty string in
    ``notes`` represents «clear the notes field» («убери аллергию
    у Никиты»). The clearable variant (no min_length) allows it
    while the add-path FamilyMemberDraft.notes still requires
    min_length=1."""
    parsed = UpdateFamilyMemberInput.model_validate({
        "member_id": FM_A, "notes": "",
    })
    assert parsed.notes == ""


def test_update_family_member_input_accepts_empty_age_hint_as_clear() -> None:
    """Same clearable semantics for age_hint."""
    parsed = UpdateFamilyMemberInput.model_validate({
        "member_id": FM_A, "age_hint": "",
    })
    assert parsed.age_hint == ""


def test_update_family_member_input_rejects_empty_name() -> None:
    """name is NOT clearable — empty would break the dedup invariant
    (normalised-name uniqueness). Empty string rejected."""
    with pytest.raises(ValidationError):
        UpdateFamilyMemberInput.model_validate({
            "member_id": FM_A, "name": "",
        })


# ---------------------------------------------------------------------------
# Input model — RemoveFamilyMemberInput / ListFamilyMembersInput
# ---------------------------------------------------------------------------


def test_remove_family_member_input_accepts_real_id() -> None:
    parsed = RemoveFamilyMemberInput.model_validate({"member_id": FM_B})
    assert parsed.member_id == FM_B


def test_remove_family_member_input_rejects_bad_id() -> None:
    with pytest.raises(ValidationError):
        RemoveFamilyMemberInput.model_validate({"member_id": "fm_xxx"})


def test_remove_family_member_input_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        RemoveFamilyMemberInput.model_validate({
            "member_id": FM_A, "reason": "moved out"
        })


def test_list_family_members_input_accepts_empty() -> None:
    parsed = ListFamilyMembersInput.model_validate({})
    assert parsed is not None


def test_list_family_members_input_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        ListFamilyMembersInput.model_validate({"unused": "x"})


# ---------------------------------------------------------------------------
# Parsers — add_family_members
# ---------------------------------------------------------------------------


def test_add_family_members_parser_returns_added() -> None:
    """Codex Sub-A4 household R1 CRITICAL #1: collapsed shape —
    both happy path and all-duplicate use ``AddFamilyMembersOk``
    discriminated by ``added_count``."""
    parsed = parse_tool_output(
        "add_family_members",
        f"ok:added:2:skipped_as_duplicate:1:ids=[{FM_A},{FM_B}]",
    )
    assert isinstance(parsed, AddFamilyMembersOk)
    assert parsed.added_count == 2
    assert parsed.skipped_as_duplicate == 1
    assert parsed.member_ids == [FM_A, FM_B]


def test_add_family_members_parser_all_duplicate_path() -> None:
    """``ok:added:0:skipped_as_duplicate:N`` (no ids segment) →
    same ``AddFamilyMembersOk`` model with ``added_count=0``.
    Planner branches on ``added_count == 0`` to say «эти уже
    есть, ничего не добавила»."""
    parsed = parse_tool_output(
        "add_family_members", "ok:added:0:skipped_as_duplicate:3"
    )
    assert isinstance(parsed, AddFamilyMembersOk)
    assert parsed.added_count == 0
    assert parsed.skipped_as_duplicate == 3
    assert parsed.member_ids == []


def test_add_family_members_parser_rejects_both_zeros() -> None:
    """Codex Sub-A4 household R1 (validator addition): runtime never
    emits ``ok:added:0:skipped_as_duplicate:0`` — empty batch is
    rejected upstream with ``error: empty batch``. The malformed
    both-zeros shape fails the model validator → ContractViolation."""
    parsed = parse_tool_output(
        "add_family_members", "ok:added:0:skipped_as_duplicate:0"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_add_family_members_parser_zero_count_with_ids_is_violation() -> None:
    """Runtime invariant: ``added_count==0`` MUST have empty ids
    segment. Mismatch is malformed → ToolOutputContractViolation."""
    parsed = parse_tool_output(
        "add_family_members",
        f"ok:added:0:skipped_as_duplicate:1:ids=[{FM_A}]",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_add_family_members_parser_count_id_mismatch_is_violation() -> None:
    """Cross-field invariant: added_count must equal len(member_ids).
    Runtime always emits a matching list; mismatch is malformed."""
    parsed = parse_tool_output(
        "add_family_members",
        f"ok:added:2:skipped_as_duplicate:0:ids=[{FM_A}]",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_add_family_members_parser_error_empty_batch() -> None:
    parsed = parse_tool_output("add_family_members", "error: empty batch")
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "empty_batch"


def test_add_family_members_parser_garbage_is_violation() -> None:
    parsed = parse_tool_output("add_family_members", "хз что вернул")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Parsers — list_family_members
# ---------------------------------------------------------------------------


def test_list_family_members_parser_empty_path() -> None:
    parsed = parse_tool_output(
        "list_family_members", "no family members recorded"
    )
    assert isinstance(parsed, ListFamilyMembersEmpty)
    assert parsed.status == "empty"


def test_list_family_members_parser_returns_structured_rows() -> None:
    """Codex Sub-A4 household R1 MAJOR #7: list now returns
    structured rows with member_id, name, role, age_text, notes —
    the planner uses ``members[i].member_id`` directly for
    update/remove rather than parsing prose."""
    raw = (
        "2 member(s):\n"
        f"  [{FM_A}] Маша (child, 8 лет) — аллергия на горчицу\n"
        f"  [{FM_B}] Никита (child, 10 лет)"
    )
    parsed = parse_tool_output("list_family_members", raw)
    assert isinstance(parsed, ListFamilyMembersOk)
    assert len(parsed.members) == 2
    assert parsed.members[0] == ListFamilyMembersRow(
        member_id=FM_A,
        name="Маша",
        role="child",
        age_text="8 лет",
        notes="аллергия на горчицу",
    )
    assert parsed.members[1] == ListFamilyMembersRow(
        member_id=FM_B,
        name="Никита",
        role="child",
        age_text="10 лет",
        notes=None,
    )


def test_list_family_members_parser_handles_minimal_row() -> None:
    """Member with no age, no notes — runtime emits just
    ``[fm_id] name (role)``."""
    raw = (
        "1 member(s):\n"
        f"  [{FM_A}] Маша (self)"
    )
    parsed = parse_tool_output("list_family_members", raw)
    assert isinstance(parsed, ListFamilyMembersOk)
    assert parsed.members[0].member_id == FM_A
    assert parsed.members[0].name == "Маша"
    assert parsed.members[0].role == "self"
    assert parsed.members[0].age_text is None
    assert parsed.members[0].notes is None


def test_list_family_members_parser_rejects_zero_header() -> None:
    """Codex Sub-A4 household R1 MINOR #1: ``0 member(s):`` is
    contract drift — the runtime emits ``no family members
    recorded`` for the empty path. Tightened regex rejects the
    numeric-zero header → ContractViolation."""
    parsed = parse_tool_output("list_family_members", "0 member(s):")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_list_family_members_parser_rejects_count_mismatch() -> None:
    """Header says 2 but only 1 row → ContractViolation. Catches
    runtime dump-format drift."""
    raw = (
        "2 member(s):\n"
        f"  [{FM_A}] Маша (child, 8 лет)"
    )
    parsed = parse_tool_output("list_family_members", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_list_family_members_parser_rejects_bad_role() -> None:
    """Row with role outside the FAMILY_ROLES whitelist →
    ContractViolation. Catches runtime drift."""
    raw = (
        "1 member(s):\n"
        f"  [{FM_A}] Маша (sister, 8 лет)"
    )
    parsed = parse_tool_output("list_family_members", raw)
    assert isinstance(parsed, ToolOutputContractViolation)


def test_list_family_members_parser_garbage_is_violation() -> None:
    parsed = parse_tool_output("list_family_members", "totally unknown")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Parsers — update_family_member / remove_family_member
# ---------------------------------------------------------------------------


def test_update_family_member_parser_ok() -> None:
    parsed = parse_tool_output("update_family_member", "ok:updated")
    assert isinstance(parsed, UpdateFamilyMemberOk)


def test_update_family_member_parser_not_found() -> None:
    parsed = parse_tool_output(
        "update_family_member",
        f"error: member '{FM_C}' not found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "member_not_found"


def test_update_family_member_parser_garbage_is_violation() -> None:
    parsed = parse_tool_output("update_family_member", "ok:whatever")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_remove_family_member_parser_ok() -> None:
    parsed = parse_tool_output("remove_family_member", "ok:removed")
    assert isinstance(parsed, RemoveFamilyMemberOk)


def test_remove_family_member_parser_not_found() -> None:
    parsed = parse_tool_output(
        "remove_family_member",
        f"error: member '{FM_C}' not found",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "member_not_found"


def test_remove_family_member_parser_garbage_is_violation() -> None:
    parsed = parse_tool_output("remove_family_member", "ok:gone")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Aggregator + quality gate
# ---------------------------------------------------------------------------


def test_migrated_tool_specs_aggregate_includes_household() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    household_names = {s.name for s in HOUSEHOLD_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert household_names.issubset(migrated_names)


def test_migrated_tool_specs_pass_strict_with_household() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)


# ---------------------------------------------------------------------------
# Codex R2 MAJOR (new) — runtime cap parity
# ---------------------------------------------------------------------------


def test_family_member_draft_accepts_200_char_name() -> None:
    """Codex R2 MAJOR (new): name cap widened to 200 to match
    runtime truncation at housewife_family.py:99. Legacy Mini App
    rows with long names must not block list_family_members."""
    long_name = "А" * 200
    parsed = FamilyMemberDraft.model_validate({
        "name": long_name, "role": "other",
    })
    assert len(parsed.name) == 200


def test_family_member_draft_rejects_201_char_name() -> None:
    with pytest.raises(ValidationError):
        FamilyMemberDraft.model_validate({
            "name": "А" * 201, "role": "other",
        })


def test_family_member_draft_accepts_64_char_age_hint() -> None:
    """Codex R2 MAJOR (new): age_hint cap widened to 64 to match
    runtime ``[:64]`` AND DB column ``String(64)``."""
    parsed = FamilyMemberDraft.model_validate({
        "name": "Маша", "role": "child", "age_hint": "x" * 64,
    })
    assert len(parsed.age_hint) == 64


def test_family_member_draft_accepts_500_char_notes() -> None:
    """Codex R2 MAJOR (new): notes cap widened to 500 to match
    runtime ``[:500]``."""
    parsed = FamilyMemberDraft.model_validate({
        "name": "Маша", "role": "child", "notes": "x" * 500,
    })
    assert len(parsed.notes) == 500


def test_list_family_members_parser_accepts_long_runtime_values() -> None:
    """Codex R2 MAJOR (new): row parser must accept name up to 200
    chars (matches runtime truncation) — pre-R2 the 80-char cap
    would have ContractViolation'd legacy rows."""
    long_name = "А" * 200
    raw = (
        "1 member(s):\n"
        f"  [{FM_A}] {long_name} (other, x)"
    )
    parsed = parse_tool_output("list_family_members", raw)
    assert isinstance(parsed, ListFamilyMembersOk)
    assert len(parsed.members[0].name) == 200


# ---------------------------------------------------------------------------
# Codex R2 MAJOR (new) — clear_birth_year semantics
# ---------------------------------------------------------------------------


def test_update_family_member_input_accepts_clear_birth_year_true() -> None:
    """Codex R2 MAJOR (new): clear_birth_year=True is the explicit
    mechanism to clear the birth_year column — replaces the
    destructive «remove + re-add» workaround."""
    parsed = UpdateFamilyMemberInput.model_validate({
        "member_id": FM_A, "clear_birth_year": True,
    })
    assert parsed.clear_birth_year is True
    assert parsed.birth_year is None


def test_update_family_member_input_rejects_birth_year_and_clear_together() -> None:
    """Mutually exclusive: setting both is a planner mistake."""
    with pytest.raises(ValidationError) as exc:
        UpdateFamilyMemberInput.model_validate({
            "member_id": FM_A,
            "birth_year": 2010,
            "clear_birth_year": True,
        })
    assert "mutually exclusive" in str(exc.value)


def test_update_family_member_input_clear_birth_year_satisfies_at_least_one() -> None:
    """clear_birth_year=True alone counts as «at least one field
    set» — no other field is required."""
    parsed = UpdateFamilyMemberInput.model_validate({
        "member_id": FM_A, "clear_birth_year": True,
    })
    assert parsed.clear_birth_year is True


def test_update_family_member_input_clear_birth_year_false_doesnt_count() -> None:
    """``clear_birth_year=False`` (default) is not «doing something»
    — must still pass at-least-one-field check via another non-None."""
    with pytest.raises(ValidationError) as exc:
        UpdateFamilyMemberInput.model_validate({
            "member_id": FM_A, "clear_birth_year": False,
        })
    assert "at least one" in str(exc.value)


# ---------------------------------------------------------------------------
# Codex R2 MINOR (prior-not-closed) — TypeAdapter parser→output_model parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,raw", [
    ("add_family_members", f"ok:added:1:skipped_as_duplicate:0:ids=[{FM_A}]"),
    ("add_family_members", "ok:added:0:skipped_as_duplicate:2"),
    ("add_family_members", "error: empty batch"),
    ("list_family_members", "no family members recorded"),
    ("list_family_members", f"1 member(s):\n  [{FM_A}] Маша (child, 8 лет)"),
    ("update_family_member", "ok:updated"),
    ("update_family_member", f"error: member '{FM_C}' not found"),
    ("remove_family_member", "ok:removed"),
    ("remove_family_member", f"error: member '{FM_C}' not found"),
])
def test_household_parser_outputs_are_valid_against_spec_output_model(tool, raw):
    """Codex Sub-A4 household R1 MINOR (prior-not-closed) +
    R2 MINOR: every parser result must roundtrip through
    ``TypeAdapter(spec.output_model).validate_python(parsed.model_dump())``.
    This catches the executor-side failure mode that surfaced as
    the R1 CRITICAL discriminator collision — TypeAdapter validation
    is the same path the executor uses to type-check ToolOutput dicts."""
    from pydantic import TypeAdapter
    spec = next(s for s in HOUSEHOLD_SPECS if s.name == tool)
    parsed = parse_tool_output(tool, raw)
    assert not isinstance(parsed, ToolOutputContractViolation), (
        f"unexpected violation for {tool} / {raw!r}"
    )
    adapter = TypeAdapter(spec.output_model)
    # #115: exclude @computed_field (output-only, e.g. display_summary) — mirrors
    # dispatch_typed_output; extra='forbid' variants reject them as input.
    computed = set(getattr(type(parsed), "model_computed_fields", {}))
    validated = adapter.validate_python(parsed.model_dump(exclude=computed))
    # Roundtrip must produce the same status discriminator.
    assert validated.status == parsed.status


# ---------------------------------------------------------------------------
# Codex R3 MAJOR (prior-not-closed) — clear_birth_year runtime parity
# ---------------------------------------------------------------------------


def test_update_member_service_clears_birth_year_when_flag_set():
    """Codex Sub-A4 household R3 MAJOR: schema-side flag must
    actually clear the column. Integration test against real
    SQLAlchemy session + HousewifeFamilyService."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.models.housewife import Base, FamilyMember
    from sreda.services.housewife_family import HousewifeFamilyService

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        svc = HousewifeFamilyService(session)
        created = svc.add_members_batch(
            tenant_id="t1", user_id="u1",
            members=[{"name": "Маша", "role": "child", "birth_year": 2017}],
        )
        member_id = created[0].id

        # Confirm baseline
        assert created[0].birth_year == 2017

        # Clear via flag
        row = svc.update_member(
            tenant_id="t1", user_id="u1", member_id=member_id,
            clear_birth_year=True,
        )
        assert row is not None
        assert row.birth_year is None


def test_update_member_service_rejects_birth_year_and_clear_together():
    """Service-level mutual-exclusion guard mirrors the schema one
    so direct service callers (Mini App, admin endpoints) get the
    same protection."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.models.housewife import Base
    from sreda.services.housewife_family import HousewifeFamilyService

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        svc = HousewifeFamilyService(session)
        created = svc.add_members_batch(
            tenant_id="t1", user_id="u1",
            members=[{"name": "Маша", "role": "child"}],
        )
        with pytest.raises(ValueError) as exc:
            svc.update_member(
                tenant_id="t1", user_id="u1", member_id=created[0].id,
                birth_year=2010, clear_birth_year=True,
            )
        assert "mutually exclusive" in str(exc.value)


def test_update_family_member_chat_tool_propagates_clear_birth_year():
    """End-to-end: LangChain tool accepts clear_birth_year and
    plumbs it through to service. Catches the R3 schema-vs-runtime
    gap that the prior R2 commit left open."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.models.housewife import Base
    from sreda.services.housewife_chat_tools import build_housewife_tools

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        tools = {
            t.name: t for t in build_housewife_tools(
                session=session, tenant_id="t1", user_id="u1",
            )
        }
        result = tools["add_family_members"].invoke({
            "members": [{
                "name": "Маша", "role": "child", "birth_year": 2017,
            }],
        })
        # #115: add_family_members now emits okv2 — parse to get the member id.
        member_id = parse_tool_output("add_family_members", result).member_ids[0]  # #115 okv2

        # Clear via the chat tool
        out = tools["update_family_member"].invoke({
            "member_id": member_id, "clear_birth_year": True,
        })
        assert out.startswith("okv2:updated:")  # #115 okv2 wire carries the name


def test_update_family_member_chat_tool_rejects_birth_year_and_clear_together():
    """End-to-end: mutual-exclusion surfaces as ``error: ...``
    rather than silently picking one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sreda.db.models.housewife import Base
    from sreda.services.housewife_chat_tools import build_housewife_tools

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        tools = {
            t.name: t for t in build_housewife_tools(
                session=session, tenant_id="t1", user_id="u1",
            )
        }
        result = tools["add_family_members"].invoke({
            "members": [{"name": "Маша", "role": "child"}],
        })
        member_id = parse_tool_output("add_family_members", result).member_ids[0]  # #115 okv2

        out = tools["update_family_member"].invoke({
            "member_id": member_id,
            "birth_year": 2010,
            "clear_birth_year": True,
        })
        assert out.startswith("error:")
        assert "mutually exclusive" in out


def test_household_typeadapter_rejects_sentinel() -> None:
    """ContractViolation must NOT be accepted by any output_model
    discriminator union — fail-closed semantic."""
    from pydantic import TypeAdapter
    for spec in HOUSEHOLD_SPECS:
        adapter = TypeAdapter(spec.output_model)
        with pytest.raises(ValidationError):
            adapter.validate_python({
                "status": "contract_violation",
                "raw_output": "garbage",
                "tool_name": spec.name,
                "timestamp": "2026-05-27T00:00:00Z",
            })
