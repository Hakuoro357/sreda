"""Integration tests for the reminders family ToolSpec instances
(Sub-A4 phase 2 — Plan-Execute Epic).

Mirrors the shopping family's R1→R7 test structure. Coverage:
- All 4 REMINDERS_SPECS construct without ValidationError
- assert_production_registry_quality passes strict policy
- Manifest cross-check: every reminders-family entry in
  TOOL_FAMILY_MANIFEST has a matching ToolSpec
- Per-tool: input_model rejects extra keys, parser produces
  output_model on canonical "ok:..." strings
- Tight ID aliases for ReminderId
- update_reminder no-op rejector + required_any_non_null_args
- Sentinel boundary regression
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.runtime.planner.schemas import (
    Action,
    ComposerCall,
    OutcomeBranch,
    Plan,
    TurnClassification,
)
from sreda.runtime.planner.validator import validate_plan
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    CancelReminderOk,
    HousewifeToolError,
    PARSERS,
    ScheduleReminderScheduled,
    UpdateReminderOk,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
    validate_tool_registry_quality,
)
from sreda.services.tool_schemas.specs_reminders import (
    CANCEL_REMINDER_SPEC,
    LIST_REMINDERS_SPEC,
    REMINDERS_SPECS,
    SCHEDULE_REMINDER_SPEC,
    UPDATE_REMINDER_SPEC,
    CancelReminderInput,
    ListRemindersInput,
    ScheduleReminderInput,
    UpdateReminderInput,
)

# Real-shape IDs matching f"rem_{uuid4().hex[:24]}".
REM_A = "rem_aaaaaaaaaaaaaaaaaaaaaaaa"
REM_B = "rem_bbbbbbbbbbbbbbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# Registry-level invariants
# ---------------------------------------------------------------------------


def test_reminders_specs_count_is_four() -> None:
    assert len(REMINDERS_SPECS) == 4


def test_reminders_specs_all_pass_strict_quality_lint() -> None:
    violations = validate_tool_registry_quality(REMINDERS_SPECS, strict=True)
    assert violations == [], (
        f"Strict quality lint surfaced {len(violations)} violation(s): "
        f"{[(v.tool_name, v.code, v.message[:80]) for v in violations]}"
    )


def test_reminders_specs_pass_assert_production_gate() -> None:
    assert_production_registry_quality(REMINDERS_SPECS)


def test_reminders_specs_match_tool_family_manifest() -> None:
    manifest_reminders = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "reminders"
    }
    spec_names = {s.name for s in REMINDERS_SPECS}
    assert spec_names == manifest_reminders, (
        f"Mismatch.\nIn manifest only: {manifest_reminders - spec_names}\n"
        f"In specs only: {spec_names - manifest_reminders}"
    )
    for spec in REMINDERS_SPECS:
        expected = TOOL_FAMILY_MANIFEST[spec.name]
        assert spec.family == expected


def test_reminders_spec_names_are_unique() -> None:
    names = [s.name for s in REMINDERS_SPECS]
    assert len(names) == len(set(names))


def test_reminders_write_tools_declare_reminders_write_domain() -> None:
    for spec in REMINDERS_SPECS:
        if spec.effect == "write":
            assert "reminders" in spec.write_domains, (
                f"{spec.name}: effect=write but 'reminders' not in "
                f"write_domains={spec.write_domains!r}"
            )


# ---------------------------------------------------------------------------
# Per-tool input model rejection of extra keys
# ---------------------------------------------------------------------------


def test_schedule_reminder_input_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "купить хлеб",
            "trigger_iso": "2026-05-27T18:00:00+03:00",
            "hallucinated_extra": "value",
        })


def test_schedule_reminder_input_accepts_minimal() -> None:
    parsed = ScheduleReminderInput.model_validate({
        "title": "купить хлеб",
        "trigger_iso": "2026-05-27T18:00:00+03:00",
    })
    assert parsed.title == "купить хлеб"
    assert parsed.recurrence_rule is None


def test_schedule_reminder_input_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "   ",
            "trigger_iso": "2026-05-27T18:00:00+03:00",
        })


def test_schedule_reminder_input_rejects_over_200_title() -> None:
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "x" * 201,
            "trigger_iso": "2026-05-27T18:00:00+03:00",
        })


def test_list_reminders_input_accepts_empty_dict() -> None:
    parsed = ListRemindersInput.model_validate({})
    assert isinstance(parsed, ListRemindersInput)


def test_list_reminders_input_rejects_any_arg() -> None:
    with pytest.raises(ValidationError):
        ListRemindersInput.model_validate({"unexpected": "value"})


def test_update_reminder_input_accepts_real_shape_id() -> None:
    parsed = UpdateReminderInput.model_validate({
        "reminder_id": REM_A,
        "title": "новый текст",
    })
    assert parsed.reminder_id == REM_A
    assert parsed.title == "новый текст"


def test_update_reminder_input_rejects_short_id() -> None:
    """Tight ``^rem_[0-9a-f]{24}$`` rejects ``rem_1``."""
    with pytest.raises(ValidationError):
        UpdateReminderInput.model_validate({
            "reminder_id": "rem_1",
            "title": "x",
        })


def test_update_reminder_input_rejects_typo_id() -> None:
    with pytest.raises(ValidationError):
        UpdateReminderInput.model_validate({
            "reminder_id": "rem-abc",
            "title": "x",
        })


def test_cancel_reminder_input_accepts_real_shape_id() -> None:
    parsed = CancelReminderInput.model_validate({"reminder_id": REM_A})
    assert parsed.reminder_id == REM_A


def test_cancel_reminder_input_rejects_extra_key() -> None:
    with pytest.raises(ValidationError):
        CancelReminderInput.model_validate({
            "reminder_id": REM_A,
            "reason": "perhaps",
        })


# ---------------------------------------------------------------------------
# End-to-end: parser returns the spec's output_model variant
# ---------------------------------------------------------------------------


def test_schedule_reminder_parser_returns_scheduled() -> None:
    parsed = parse_tool_output(
        "schedule_reminder", f"ok:scheduled:{REM_A}:2026-05-27T18:00:00+00:00"
    )
    assert isinstance(parsed, ScheduleReminderScheduled)
    assert parsed.reminder_id == REM_A


def test_update_reminder_parser_returns_updated_with_iso() -> None:
    parsed = parse_tool_output(
        "update_reminder", f"ok:updated:{REM_A}:2026-05-27T18:00:00+00:00"
    )
    assert isinstance(parsed, UpdateReminderOk)
    assert parsed.reminder_id == REM_A
    assert parsed.next_trigger_at_iso == "2026-05-27T18:00:00+00:00"


def test_update_reminder_parser_maps_literal_none_to_field_none() -> None:
    """``ok:updated:rem_<id>:none`` means recurrence cleared and no
    future trigger remains. Parser maps the literal string ``"none"``
    to ``None`` so the planner can branch on the absence."""
    parsed = parse_tool_output("update_reminder", f"ok:updated:{REM_A}:none")
    assert isinstance(parsed, UpdateReminderOk)
    assert parsed.next_trigger_at_iso is None


def test_update_reminder_parser_returns_sentinel_for_malformed_id() -> None:
    parsed = parse_tool_output(
        "update_reminder", "ok:updated:rem_garbage:2026-05-27T18:00:00+00:00"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_cancel_reminder_parser_returns_cancelled() -> None:
    parsed = parse_tool_output("cancel_reminder", "ok:cancelled")
    assert isinstance(parsed, CancelReminderOk)


def test_cancel_reminder_parser_returns_sentinel_for_unknown() -> None:
    parsed = parse_tool_output("cancel_reminder", "ok:cancelled:rem_xxx")
    assert isinstance(parsed, ToolOutputContractViolation)


# ---------------------------------------------------------------------------
# Stable error codes — schedule + update share the cannot_parse_trigger_iso
# pattern; cancel + update share the reminder_not_found pattern.
# ---------------------------------------------------------------------------


def test_schedule_reminder_cannot_parse_trigger_iso_is_stable() -> None:
    a = parse_tool_output(
        "schedule_reminder", "error: cannot parse trigger_iso='завтра'"
    )
    b = parse_tool_output(
        "schedule_reminder", "error: cannot parse trigger_iso='вчера в три'"
    )
    assert isinstance(a, HousewifeToolError)
    assert isinstance(b, HousewifeToolError)
    assert a.error_code == "cannot_parse_trigger_iso"
    assert b.error_code == "cannot_parse_trigger_iso"


def test_cancel_reminder_not_found_is_stable() -> None:
    a = parse_tool_output("cancel_reminder", "error: reminder 'rem_42' not found")
    b = parse_tool_output("cancel_reminder", "error: reminder 'rem_7' not found")
    assert isinstance(a, HousewifeToolError)
    assert isinstance(b, HousewifeToolError)
    assert a.error_code == "reminder_not_found"
    assert b.error_code == "reminder_not_found"


def test_update_reminder_not_found_is_stable() -> None:
    a = parse_tool_output("update_reminder", "error: reminder 'rem_42' not found")
    b = parse_tool_output("update_reminder", "error: reminder 'rem_7' not found")
    assert a.error_code == "reminder_not_found"
    assert b.error_code == "reminder_not_found"


# ---------------------------------------------------------------------------
# Parser/output_model compatibility for every reminders spec
# ---------------------------------------------------------------------------


_PARSER_HAPPY_PATH = {
    "schedule_reminder": f"ok:scheduled:{REM_A}:2026-05-27T18:00:00+00:00",
    "list_reminders": "no active reminders",
    "update_reminder": f"ok:updated:{REM_A}:2026-05-27T18:00:00+00:00",
    "cancel_reminder": "ok:cancelled",
}


@pytest.mark.parametrize("spec", REMINDERS_SPECS, ids=lambda s: s.name)
def test_every_reminders_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS, (
        f"Tool {spec.name!r} has a ToolSpec but no parser in PARSERS."
    )


@pytest.mark.parametrize("spec", REMINDERS_SPECS, ids=lambda s: s.name)
def test_parser_output_validates_against_spec_output_model(spec) -> None:
    raw = _PARSER_HAPPY_PATH[spec.name]
    parsed = parse_tool_output(spec.name, raw)
    TypeAdapter(spec.output_model).validate_python(parsed.model_dump())


# ---------------------------------------------------------------------------
# Sentinel boundary regression — sentinel must NOT validate against any
# reminders output_model union (executor catches BEFORE output validation).
# ---------------------------------------------------------------------------


def test_sentinel_is_not_valid_against_any_reminders_output_model() -> None:
    sentinel = parse_tool_output("update_reminder", "totally unparseable")
    assert isinstance(sentinel, ToolOutputContractViolation)
    sentinel_dump = sentinel.model_dump()
    for spec in REMINDERS_SPECS:
        with pytest.raises(ValidationError):
            TypeAdapter(spec.output_model).validate_python(sentinel_dump)


# ---------------------------------------------------------------------------
# UPDATE_REMINDER_SPEC required_any_non_null_args — refs-aware no-op
# rejection via the REAL runtime/planner/validator.py
# ---------------------------------------------------------------------------


def _plan_with_actions(actions: dict[str, Action]) -> Plan:
    return Plan(
        turn_classification=TurnClassification(
            is_new_turn=True, reason="test"
        ),
        actions=actions,
        compose=ComposerCall(kind="template", template_id="reminder_set"),
    )


def _action(
    tool: str,
    args: dict,
    *,
    depends_on: list[str] | None = None,
    outcome_status: str = "ok",
) -> Action:
    """Build an Action with an outcome branch that matches the tool's
    likely happy-status. Codex R1 MINOR #7: previously hard-coded
    ``"updated"`` for every action, which made ``list_reminders``
    actions semantically dirty (list returns ``ok`` / ``empty`` /
    ``error``)."""
    return Action(
        tool=tool,
        args=args,
        expected_outcomes=[OutcomeBranch(match={"status": outcome_status})],
        depends_on=depends_on or [],
    )


def test_update_reminder_spec_declares_required_any_non_null() -> None:
    assert UPDATE_REMINDER_SPEC.required_any_non_null_args == [
        "title",
        "trigger_iso",
        "recurrence_rule",
        "clear_recurrence",
    ]


def test_update_reminder_rejects_refs_only_no_mutable_in_real_validator() -> None:
    """End-to-end via the REAL ``runtime/planner/validator.py``.

    Plan with ``update_reminder(reminder_id="${s1.items[0].reminder_id}")`` and
    no mutable fields → Phase 1.d ``_phase1_check_required_any_non_null``
    must emit ``silent_noop_call``."""
    plan = _plan_with_actions({
        "s1": _action("list_reminders", {}, outcome_status="ok"),
        "s2": _action(
            "update_reminder",
            {"reminder_id": "${s1.items[0].reminder_id}"},
            depends_on=["s1"],
            outcome_status="updated",
        ),
    })
    registry = {
        "list_reminders": LIST_REMINDERS_SPEC,
        "update_reminder": UPDATE_REMINDER_SPEC,
        "schedule_reminder": SCHEDULE_REMINDER_SPEC,
        "cancel_reminder": CANCEL_REMINDER_SPEC,
    }
    violations = validate_plan(plan, registry)
    silent_noop = [
        v for v in violations
        if v.code == "silent_noop_call" and v.step_id == "s2"
    ]
    assert silent_noop, (
        f"Expected silent_noop_call on s2 but got: "
        f"{[(v.code, v.message[:80]) for v in violations]}"
    )


def test_update_reminder_accepts_ref_on_mutable_field() -> None:
    plan = _plan_with_actions({
        "s1": _action("list_reminders", {}, outcome_status="ok"),
        "s2": _action(
            "update_reminder",
            {
                "reminder_id": "${s1.items[0].reminder_id}",
                "trigger_iso": "${s1.items[0].next_trigger_at_iso}",
            },
            depends_on=["s1"],
            outcome_status="updated",
        ),
    })
    registry = {
        "list_reminders": LIST_REMINDERS_SPEC,
        "update_reminder": UPDATE_REMINDER_SPEC,
    }
    violations = validate_plan(plan, registry)
    silent_noop = [v for v in violations if v.code == "silent_noop_call"]
    assert not silent_noop


# ---------------------------------------------------------------------------
# Codex R1 follow-up tests (R2 candidates) — MAJOR #1/#2/#3/#4 + MINOR #8
# ---------------------------------------------------------------------------


def test_update_reminder_input_rejects_clear_recurrence_false() -> None:
    """Codex R1 MAJOR #1: ``clear_recurrence=False`` is a semantic
    no-op (runtime branches only on True). ``Literal[True] | None``
    rejects False explicitly."""
    with pytest.raises(ValidationError):
        UpdateReminderInput.model_validate({
            "reminder_id": REM_A,
            "clear_recurrence": False,
        })


def test_update_reminder_input_accepts_clear_recurrence_true() -> None:
    """``clear_recurrence=True`` is the real «turn off recurrence»
    intent — accepted."""
    parsed = UpdateReminderInput.model_validate({
        "reminder_id": REM_A,
        "clear_recurrence": True,
    })
    assert parsed.clear_recurrence is True


def test_update_reminder_validator_rejects_false_only(monkeypatch) -> None:
    """Codex R1 MAJOR #1: end-to-end via the real validator. A plan
    that sends only ``{reminder_id, clear_recurrence: False}`` must
    be rejected — both at the schema level AND at the planner
    validator level (defense-in-depth)."""
    # The schema rejects directly; validator path also catches via
    # Phase 2's input_model.model_validate when no refs are present.
    plan = _plan_with_actions({
        "s1": _action(
            "update_reminder",
            {"reminder_id": REM_A, "clear_recurrence": False},
            outcome_status="updated",
        ),
    })
    registry = {"update_reminder": UPDATE_REMINDER_SPEC}
    violations = validate_plan(plan, registry)
    assert violations, "Expected validation violations for clear_recurrence=False"


def test_schedule_reminder_input_rejects_malformed_trigger_iso() -> None:
    """Codex R1 MAJOR #2 + #3: ``TriggerIso`` regex rejects obvious
    non-ISO strings at plan time."""
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "купить хлеб",
            "trigger_iso": "tomorrow",
        })


def test_schedule_reminder_input_rejects_huge_trigger_iso() -> None:
    """Codex R1 MAJOR #3: ``TriggerIso`` caps at 64 chars."""
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "купить хлеб",
            "trigger_iso": "2026-05-27T18:00:00+03:00" + "x" * 100,
        })


def test_schedule_reminder_input_accepts_offset_aware_iso() -> None:
    """``+03:00`` accepted — runtime converts to UTC. UTC enforcement
    is the runtime's responsibility per docstring."""
    parsed = ScheduleReminderInput.model_validate({
        "title": "x",
        "trigger_iso": "2026-05-27T18:00:00+03:00",
    })
    assert parsed.trigger_iso == "2026-05-27T18:00:00+03:00"


def test_schedule_reminder_input_accepts_z_suffix() -> None:
    parsed = ScheduleReminderInput.model_validate({
        "title": "x",
        "trigger_iso": "2026-05-27T15:00:00Z",
    })
    assert parsed.trigger_iso == "2026-05-27T15:00:00Z"


def test_schedule_reminder_input_rejects_malformed_recurrence_rule() -> None:
    """Codex R1 MAJOR #3: ``RecurrenceRule`` requires ``FREQ=`` start."""
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "x",
            "trigger_iso": "2026-05-27T15:00:00Z",
            "recurrence_rule": "weekly_tuesday",
        })


def test_schedule_reminder_input_rejects_multiline_recurrence_rule() -> None:
    with pytest.raises(ValidationError):
        ScheduleReminderInput.model_validate({
            "title": "x",
            "trigger_iso": "2026-05-27T15:00:00Z",
            "recurrence_rule": "FREQ=WEEKLY\nBYDAY=TU",
        })


def test_schedule_reminder_input_accepts_valid_rrule() -> None:
    parsed = ScheduleReminderInput.model_validate({
        "title": "x",
        "trigger_iso": "2026-05-27T15:00:00Z",
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU;BYHOUR=13;BYMINUTE=0",
    })
    assert parsed.recurrence_rule == "FREQ=WEEKLY;BYDAY=TU;BYHOUR=13;BYMINUTE=0"


def test_update_reminder_parser_rejects_malformed_next_trigger() -> None:
    """Codex R1 MAJOR #4: parser previously accepted any non-``none``
    payload as ``next_trigger_at_iso``. With ``TriggerIso`` validation
    on the output model, malformed values fall through to sentinel."""
    parsed = parse_tool_output(
        "update_reminder", f"ok:updated:{REM_A}:tomorrow"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_update_reminder_parser_accepts_real_iso_next_trigger() -> None:
    """ISO-shaped next_trigger continues to work."""
    parsed = parse_tool_output(
        "update_reminder", f"ok:updated:{REM_A}:2026-05-27T18:00:00Z"
    )
    assert isinstance(parsed, UpdateReminderOk)
    assert parsed.next_trigger_at_iso == "2026-05-27T18:00:00Z"


# ---------------------------------------------------------------------------
# Codex R1 MINOR #8 — explicit blank-input coverage
# ---------------------------------------------------------------------------


def test_update_reminder_input_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        UpdateReminderInput.model_validate({
            "reminder_id": REM_A,
            "title": "   ",
        })


def test_update_reminder_input_rejects_blank_trigger_iso() -> None:
    with pytest.raises(ValidationError):
        UpdateReminderInput.model_validate({
            "reminder_id": REM_A,
            "trigger_iso": "",
        })


def test_update_reminder_input_rejects_blank_recurrence_rule() -> None:
    with pytest.raises(ValidationError):
        UpdateReminderInput.model_validate({
            "reminder_id": REM_A,
            "recurrence_rule": "",
        })


# ---------------------------------------------------------------------------
# Codex R1 MAJOR #6 — registry-level mutex-note reference linter
# ---------------------------------------------------------------------------


def test_reminders_mutex_notes_do_not_reference_unmigrated_tools() -> None:
    """Codex R1 MAJOR #6: the new registry linter
    ``validate_mutex_note_references`` would catch a mutex_note
    that names a tool from ``TOOL_FAMILY_MANIFEST`` which isn't in
    the migrated specs. Reminders specs were rewritten to avoid
    naming ``tasks.attach_reminder`` (not yet migrated)."""
    from sreda.services.tool_schemas.registry_quality import (
        validate_mutex_note_references,
    )
    violations = validate_mutex_note_references(
        REMINDERS_SPECS, manifest=TOOL_FAMILY_MANIFEST
    )
    assert violations == [], (
        f"reminders mutex_notes reference unmigrated tools: "
        f"{[(v.tool_name, v.code, v.message[:80]) for v in violations]}"
    )


def test_mutex_note_reference_linter_detects_unmigrated_reference() -> None:
    """Synthetic check: a mutex_note that names a tool from
    ``TOOL_FAMILY_MANIFEST`` but not in the migrated specs surfaces
    a violation."""
    from sreda.services.tool_schemas.registry_quality import (
        validate_mutex_note_references,
    )
    # SCHEDULE_REMINDER_SPEC is in the migrated set. Build a
    # synthetic «poisoned» spec that names an unmigrated tool by
    # using model_copy.
    poisoned = SCHEDULE_REMINDER_SPEC.model_copy(update={
        "mutex_notes": [
            "Используй для standalone-напоминаний. Если юзер просит «напомни про задачу T-X» — это tasks.attach_reminder, не schedule_reminder.",
        ],
    })
    violations = validate_mutex_note_references(
        [poisoned], manifest=TOOL_FAMILY_MANIFEST
    )
    assert violations, "Expected violation for attach_reminder reference"
    assert any(
        v.code == "mutex_note_references_unmigrated_tool"
        and "attach_reminder" in v.message
        for v in violations
    )


# ---------------------------------------------------------------------------
# Central registry aggregator
# ---------------------------------------------------------------------------


def test_migrated_tool_specs_aggregate_includes_reminders() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    reminders_names = {s.name for s in REMINDERS_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert reminders_names.issubset(migrated_names)


def test_migrated_tool_specs_pass_strict_production_quality() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    assert_production_registry_quality(MIGRATED_TOOL_SPECS)
