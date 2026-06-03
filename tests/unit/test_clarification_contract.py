"""Unit tests for ``sreda.services.clarification_contract``.

issue #88 PR-a — Piece 1: Clarification contract.

Coverage:
- Exhaustiveness: every CLARIFICATION_FIELDS code has a CLARIFICATION_FIELD_RU
  entry; clarification_field_ru returns non-empty for all (details → generic).
- validate_clarification_payload: happy paths + rejection paths.
- Template render: every enum code renders without the raw English token;
  empty/absent missing_fields → generic phrase.
- Schema validator: unknown code rejected; valid codes accepted.
- FIX 2: done_summary ref always rejected.
- FIX 3: branch compose with unknown missing_fields code is caught by
  validate_plan via _check_composer_allowlist.
"""

from __future__ import annotations

import pytest

from sreda.services.clarification_contract import (
    CLARIFICATION_FIELD_RU,
    CLARIFICATION_FIELDS,
    GENERIC_CLARIFICATION,
    clarification_field_ru,
    validate_clarification_payload,
)
from sreda.services.composer.registry import render


# ---------------------------------------------------------------------------
# Exhaustiveness
# ---------------------------------------------------------------------------


def test_clarification_field_ru_covers_all_fields() -> None:
    """Every code in CLARIFICATION_FIELDS must have an entry in
    CLARIFICATION_FIELD_RU — the assertion at module import-time already
    enforces this, but we test it explicitly so CI output is clear."""
    assert set(CLARIFICATION_FIELD_RU.keys()) == CLARIFICATION_FIELDS


def test_clarification_field_ru_returns_non_empty_for_all_codes() -> None:
    """``clarification_field_ru`` must return a non-empty string for every
    code — even ``details`` which has an empty RU entry and falls back to
    GENERIC_CLARIFICATION."""
    for code in CLARIFICATION_FIELDS:
        result = clarification_field_ru(code)
        assert result, f"clarification_field_ru({code!r}) returned empty string"
        assert isinstance(result, str)


def test_clarification_field_ru_details_returns_generic() -> None:
    """``details`` maps to empty string in CLARIFICATION_FIELD_RU, so
    ``clarification_field_ru`` must return GENERIC_CLARIFICATION."""
    assert CLARIFICATION_FIELD_RU["details"] == ""
    assert clarification_field_ru("details") == GENERIC_CLARIFICATION


def test_clarification_field_ru_unknown_code_returns_generic() -> None:
    """Any code not in the closed enum → GENERIC_CLARIFICATION.
    Defense-in-depth: the raw code never reaches the user."""
    assert clarification_field_ru("arbitrary_unknown_code") == GENERIC_CLARIFICATION
    assert clarification_field_ru("") == GENERIC_CLARIFICATION
    assert clarification_field_ru("product_brand") == GENERIC_CLARIFICATION


def test_clarification_field_ru_known_code_returns_russian_phrase() -> None:
    """Spot-check a few known codes return their expected Russian fragments."""
    assert "когда" in clarification_field_ru("time")
    assert "кому" in clarification_field_ru("recipient")
    assert "сколько" in clarification_field_ru("quantity")
    assert "что искать" in clarification_field_ru("query")


# ---------------------------------------------------------------------------
# validate_clarification_payload — happy paths
# ---------------------------------------------------------------------------


def test_validate_payload_empty_data_valid() -> None:
    """Completely empty template_data is valid — all fields are optional."""
    assert validate_clarification_payload({}, allow_refs=False) == []


def test_validate_payload_known_codes_valid() -> None:
    """List of known codes → no errors."""
    errors = validate_clarification_payload(
        {"missing_fields": ["time", "recipient"]},
        allow_refs=False,
    )
    assert errors == []


def test_validate_payload_all_known_codes_valid() -> None:
    """All 9 known codes accepted."""
    errors = validate_clarification_payload(
        {"missing_fields": list(CLARIFICATION_FIELDS)},
        allow_refs=False,
    )
    assert errors == []


def test_validate_payload_empty_missing_fields_valid() -> None:
    """Empty list is explicitly allowed (template renders generic fallback)."""
    assert validate_clarification_payload({"missing_fields": []}, allow_refs=False) == []


def test_validate_payload_tuple_missing_fields_valid() -> None:
    """Tuple is accepted as well as list (mirrors schema R4 decision)."""
    assert validate_clarification_payload({"missing_fields": ("time",)}, allow_refs=False) == []


def test_validate_payload_done_summary_valid() -> None:
    """Non-empty literal done_summary is accepted."""
    errors = validate_clarification_payload(
        {"done_summary": "добавила молоко", "missing_fields": ["items"]},
        allow_refs=False,
    )
    assert errors == []


def test_validate_payload_clarity_reason_passthrough() -> None:
    """clarity_reason is allowed as a top-level key (auto-merged by schema)."""
    errors = validate_clarification_payload(
        {"clarity_reason": "не указано время", "missing_fields": ["time"]},
        allow_refs=False,
    )
    assert errors == []


# ---------------------------------------------------------------------------
# validate_clarification_payload — rejection paths
# ---------------------------------------------------------------------------


def test_validate_payload_unknown_missing_fields_code_rejected() -> None:
    """Unknown code in missing_fields → error listing the bad code."""
    errors = validate_clarification_payload(
        {"missing_fields": ["reminder_subject", "unknown_field_xyz"]},
        allow_refs=False,
    )
    assert errors
    assert any("unknown_field_xyz" in e for e in errors)


def test_validate_payload_non_list_missing_fields_rejected() -> None:
    """missing_fields must be a list/tuple — string, int, dict are rejected."""
    for bad in ("time", 1, {"time": True}, 42.5, True):
        errors = validate_clarification_payload({"missing_fields": bad}, allow_refs=False)
        assert errors, f"Expected errors for missing_fields={bad!r}"
        assert any("missing_fields" in e for e in errors)


def test_validate_payload_extra_top_level_key_rejected() -> None:
    """Keys other than the allowed set are rejected."""
    errors = validate_clarification_payload(
        {"missing_fields": ["time"], "extra_key": "surprise"},
        allow_refs=False,
    )
    assert errors
    assert any("extra_key" in e for e in errors)


def test_validate_payload_slots_top_level_key_rejected() -> None:
    """'slots' is no longer a recognised top-level key (deferred to follow-up)."""
    errors = validate_clarification_payload(
        {"missing_fields": ["time"], "slots": {"time": "12:00"}},
        allow_refs=False,
    )
    assert errors
    assert any("slots" in e for e in errors)


def test_validate_payload_done_summary_empty_rejected() -> None:
    """Empty/whitespace done_summary rejected."""
    for bad in ("", "   "):
        errors = validate_clarification_payload({"done_summary": bad}, allow_refs=False)
        assert errors, f"Expected errors for done_summary={bad!r}"


def test_validate_payload_done_summary_ref_rejected_allow_refs_false() -> None:
    """Full-ref done_summary is rejected when allow_refs=False."""
    errors = validate_clarification_payload(
        {"done_summary": "${s1.result}"},
        allow_refs=False,
    )
    assert errors
    assert any("ref" in e.lower() or "${" in e for e in errors)


def test_validate_payload_done_summary_ref_rejected_allow_refs_true() -> None:
    """FIX 2: full-ref done_summary is ALWAYS rejected, even when allow_refs=True.
    A ref can resolve to a dict/list and get stringified raw into the user reply."""
    errors = validate_clarification_payload(
        {"done_summary": "${s1.result}"},
        allow_refs=True,
    )
    assert errors, "Expected done_summary ref to be rejected even with allow_refs=True"
    assert any("ref" in e.lower() or "${" in e for e in errors)


def test_validate_payload_done_summary_mixed_ref_rejected() -> None:
    """Codex PR-a R2 (both): a MIXED string with an embedded ref must also be
    rejected — not just a full-ref. ``"сделала: ${s1.result}"`` would otherwise
    pass, then interpolation stringifies the resolved dict/list into the reply."""
    for bad, allow in (
        ("сделала: ${s1.result}", True),
        ("сделала: ${s1.result}", False),
        ("готово ${s1}", True),
        ("готово ${s1}", False),
    ):
        errors = validate_clarification_payload(
            {"done_summary": bad}, allow_refs=allow
        )
        assert errors, f"Expected embedded-ref done_summary {bad!r} to be rejected (allow_refs={allow})"
        assert any("ref" in e.lower() or "${" in e for e in errors)


# ---------------------------------------------------------------------------
# Template render — no raw English code token in output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(CLARIFICATION_FIELDS))
def test_render_ask_user_for_clarification_no_raw_code(code: str) -> None:
    """For every enum code, rendering with missing_fields=[code] must NOT
    contain the raw English code token in the output, and must be non-empty."""
    out = render(
        "ask_user_for_clarification",
        {"missing_fields": [code]},
    )
    assert out, f"Template rendered empty for code={code!r}"
    # The raw English code must not appear in the output.
    assert code not in out, (
        f"Raw code {code!r} leaked into template output: {out!r}"
    )


@pytest.mark.parametrize("code", sorted(CLARIFICATION_FIELDS))
def test_render_partial_with_clarification_no_raw_code(code: str) -> None:
    """Same check for partial_with_clarification."""
    out = render(
        "partial_with_clarification",
        {"done_summary": "сделала кое-что", "missing_fields": [code]},
    )
    assert out
    assert code not in out, (
        f"Raw code {code!r} leaked into partial_with_clarification output: {out!r}"
    )


def test_render_ask_user_for_clarification_empty_missing_fields_generic() -> None:
    """Empty missing_fields → template renders the generic fallback phrase."""
    out = render("ask_user_for_clarification", {"missing_fields": []})
    assert "подробнее" in out or "Не до конца" in out


def test_render_ask_user_for_clarification_absent_missing_fields_generic() -> None:
    """Absent missing_fields → same generic fallback."""
    out = render("ask_user_for_clarification", {})
    assert "подробнее" in out or "Не до конца" in out


def test_render_details_code_shows_generic_phrase() -> None:
    """``details`` code maps to GENERIC_CLARIFICATION (empty RU entry)."""
    out = render("ask_user_for_clarification", {"missing_fields": ["details"]})
    assert "details" not in out
    # The GENERIC_CLARIFICATION phrase is rendered as a bullet.
    assert "Уточни" in out


# ---------------------------------------------------------------------------
# Schema validator integration (root compose — caught by validate_plan)
# ---------------------------------------------------------------------------


def test_schema_accepts_valid_missing_fields_codes() -> None:
    """Plan with valid enum codes passes schema validation."""
    from sreda.runtime.planner.schemas import ComposerCall, Plan, TurnClassification

    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        clarity="needs_clarification",
        clarity_reason="не указано время",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"missing_fields": ["time", "recipient"]},
        ),
    )
    assert plan.compose.template_data["missing_fields"] == ["time", "recipient"]


def test_root_compose_unknown_missing_fields_code_rejected_by_validate_plan() -> None:
    """Unknown code in root compose missing_fields → validate_plan returns
    a clarification_payload_invalid violation (FIX 3 moves this check from
    schemas.py model_validator to validator._check_composer_allowlist)."""
    from sreda.runtime.planner.schemas import ComposerCall, Plan, TurnClassification
    from sreda.runtime.planner.validator import validate_plan

    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        clarity="needs_clarification",
        clarity_reason="what is unclear",
        actions={},
        compose=ComposerCall(
            kind="template",
            template_id="ask_user_for_clarification",
            template_data={"missing_fields": ["unknown_code_xyz"]},
        ),
    )
    template_ids = frozenset({
        "ask_user_for_clarification",
        "ask_when_to_remind",
        "partial_with_clarification",
    })
    violations = validate_plan(plan, {}, composer_template_ids=template_ids)
    codes = {v.code for v in violations}
    assert "clarification_payload_invalid" in codes, (
        f"Expected clarification_payload_invalid for unknown code in root compose. "
        f"Got: {violations}"
    )
    assert any("unknown_code_xyz" in v.message for v in violations)


# ---------------------------------------------------------------------------
# FIX 3 — branch compose validation via validate_plan
# ---------------------------------------------------------------------------


def _make_clarification_spec():  # type: ignore[return]
    """Build a minimal ToolSpec for save_core_fact for FIX 3 tests."""
    from pydantic import BaseModel, Field as PydanticField
    from sreda.services.tool_schemas.base import ToolSpec
    from typing import Annotated, Literal

    class _SaveOk(BaseModel):
        status: Literal["saved_core"]

    class _SaveErr(BaseModel):
        status: Literal["error"]
        error_code: str

    class _SaveInput(BaseModel):
        content: str

    return ToolSpec(
        name="save_core_fact",
        description="Save a core user fact",
        family="memory",
        effect="write",
        read_domains=[],
        write_domains=["memory"],
        input_model=_SaveInput,
        output_model=Annotated[_SaveOk | _SaveErr, PydanticField(discriminator="status")],
    )


_CLARI_TEMPLATE_IDS = frozenset({
    "ask_user_for_clarification",
    "ask_when_to_remind",
    "partial_with_clarification",
    "generic_tool_error",
})


def test_branch_clarification_unknown_missing_fields_code_rejected() -> None:
    """FIX 3: validate_plan must catch an unknown missing_fields code in a
    BRANCH compose (expected_outcomes[].compose), not only root compose."""
    from sreda.runtime.planner.schemas import (
        Action, ComposerCall, OutcomeBranch, Plan, TurnClassification,
    )
    from sreda.runtime.planner.validator import validate_plan

    spec = _make_clarification_spec()
    registry = {"save_core_fact": spec}

    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        clarity="needs_clarification",
        clarity_reason="need clarification",
        actions={
            "s1": Action(
                tool="save_core_fact",
                args={"content": "some fact"},
                expected_outcomes=[
                    OutcomeBranch(
                        match={"status": "saved_core"},
                        compose=ComposerCall(
                            kind="template",
                            template_id="partial_with_clarification",
                            template_data={
                                "done_summary": "сохранила факт",
                                "missing_fields": ["INVALID_CODE_NOT_IN_ENUM"],
                            },
                        ),
                    ),
                    OutcomeBranch(
                        match={"status": "error"},
                        compose=ComposerCall(
                            kind="template",
                            template_id="generic_tool_error",
                            template_data={"error_code": "${s1.error_code}"},
                        ),
                    ),
                ],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="partial_with_clarification",
            template_data={"done_summary": "сохранила факт"},
        ),
    )

    violations = validate_plan(
        plan, registry,
        composer_template_ids=_CLARI_TEMPLATE_IDS,
    )
    codes = {v.code for v in violations}
    assert "clarification_payload_invalid" in codes, (
        f"Expected clarification_payload_invalid violation for branch with unknown "
        f"missing_fields code. Got: {violations}"
    )


def test_branch_clarification_valid_missing_fields_no_violation() -> None:
    """FIX 3: a valid branch compose with known missing_fields codes passes."""
    from sreda.runtime.planner.schemas import (
        Action, ComposerCall, OutcomeBranch, Plan, TurnClassification,
    )
    from sreda.runtime.planner.validator import validate_plan

    spec = _make_clarification_spec()
    registry = {"save_core_fact": spec}

    plan = Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        clarity="needs_clarification",
        clarity_reason="need clarification",
        actions={
            "s1": Action(
                tool="save_core_fact",
                args={"content": "some fact"},
                expected_outcomes=[
                    OutcomeBranch(
                        match={"status": "saved_core"},
                        compose=ComposerCall(
                            kind="template",
                            template_id="partial_with_clarification",
                            template_data={
                                "done_summary": "сохранила факт",
                                "missing_fields": ["time", "items"],
                            },
                        ),
                    ),
                    OutcomeBranch(
                        match={"status": "error"},
                        compose=ComposerCall(
                            kind="template",
                            template_id="generic_tool_error",
                            template_data={"error_code": "${s1.error_code}"},
                        ),
                    ),
                ],
            ),
        },
        compose=ComposerCall(
            kind="template",
            template_id="partial_with_clarification",
            template_data={"done_summary": "сохранила факт"},
        ),
    )

    violations = validate_plan(
        plan, registry,
        composer_template_ids=_CLARI_TEMPLATE_IDS,
    )
    clari_violations = [v for v in violations if v.code == "clarification_payload_invalid"]
    assert not clari_violations, (
        f"Unexpected clarification_payload_invalid violations: {clari_violations}"
    )
