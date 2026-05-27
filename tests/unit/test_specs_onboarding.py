"""Integration tests for the onboarding family ToolSpec instances
(Sub-A4 phase 8 — Plan-Execute Epic)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST
from sreda.services.tool_schemas.housewife import (
    HousewifeToolError,
    OnboardingAnsweredOk,
    OnboardingCompleteOk,
    OnboardingDeferredOk,
    PARSERS,
    parse_tool_output,
)
from sreda.services.tool_schemas.registry_quality import (
    assert_production_registry_quality,
)
from sreda.services.tool_schemas.specs_onboarding import (
    ONBOARDING_ANSWERED_SPEC,
    ONBOARDING_COMPLETE_SPEC,
    ONBOARDING_DEFERRED_SPEC,
    ONBOARDING_SPECS,
    OnboardingAnsweredInput,
    OnboardingCompleteInput,
    OnboardingDeferredInput,
)


# ---------------------------------------------------------------------------
# Family-level invariants
# ---------------------------------------------------------------------------


def test_all_onboarding_specs_construct() -> None:
    assert len(ONBOARDING_SPECS) == 3
    names = {s.name for s in ONBOARDING_SPECS}
    assert names == {
        "onboarding_answered", "onboarding_deferred", "onboarding_complete",
    }


def test_onboarding_family_passes_production_quality_strict() -> None:
    assert_production_registry_quality(ONBOARDING_SPECS)


def test_manifest_matches_onboarding_specs() -> None:
    manifest = {
        name for name, family in TOOL_FAMILY_MANIFEST.items()
        if family == "onboarding"
    }
    spec_names = {s.name for s in ONBOARDING_SPECS}
    assert manifest == spec_names


@pytest.mark.parametrize("spec", ONBOARDING_SPECS, ids=lambda s: s.name)
def test_every_onboarding_spec_has_a_parser(spec) -> None:
    assert spec.name in PARSERS


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


def test_answered_input_minimal() -> None:
    parsed = OnboardingAnsweredInput.model_validate({
        "topic": "addressing", "summary": "Борис",
    })
    assert parsed.topic == "addressing"
    assert parsed.summary == "Борис"


def test_answered_input_rejects_unknown_topic() -> None:
    with pytest.raises(ValidationError):
        OnboardingAnsweredInput.model_validate({
            "topic": "weird", "summary": "x",
        })


def test_answered_input_rejects_blank_summary() -> None:
    with pytest.raises(ValidationError):
        OnboardingAnsweredInput.model_validate({
            "topic": "addressing", "summary": "   ",
        })


def test_answered_input_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        OnboardingAnsweredInput.model_validate({
            "topic": "addressing", "summary": "x", "extra": "y",
        })


def test_deferred_input_minimal() -> None:
    """Codex R2 MAJOR #1: topic restricted to active-flow «addressing»."""
    parsed = OnboardingDeferredInput.model_validate({
        "topic": "addressing", "reason": "потом",
    })
    assert parsed.reason == "потом"


def test_deferred_input_rejects_unknown_topic() -> None:
    with pytest.raises(ValidationError):
        OnboardingDeferredInput.model_validate({
            "topic": "xxx", "reason": "x",
        })


def test_deferred_input_rejects_non_addressing_topic() -> None:
    """Codex R2 MAJOR #1: non-addressing topics (legacy 5 topics
    that persist in TOPIC_DESCRIPTIONS for output validation but
    aren't in active TOPIC_ORDER) must NOT be accepted as planner
    input — only addressing is in the active flow."""
    for t in ("self_intro", "family", "diet", "routine", "pain_point"):
        with pytest.raises(ValidationError):
            OnboardingDeferredInput.model_validate({
                "topic": t, "reason": "потом",
            })


def test_complete_input_no_args() -> None:
    parsed = OnboardingCompleteInput.model_validate({})
    assert parsed is not None


def test_complete_input_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        OnboardingCompleteInput.model_validate({"foo": "bar"})


# ---------------------------------------------------------------------------
# Parsers — all 6 topics × all 4 statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("topic", [
    "addressing", "self_intro", "family", "diet", "routine", "pain_point",
])
def test_answered_parser_all_topics(topic) -> None:
    parsed = parse_tool_output(
        "onboarding_answered",
        f"ok:answered:{topic}:next=none:status=complete",
    )
    assert isinstance(parsed, OnboardingAnsweredOk)
    assert parsed.topic == topic
    assert parsed.next_topic == "none"
    assert parsed.onboarding_status == "complete"


@pytest.mark.parametrize("st", [
    "not_started", "in_progress", "complete", "abandoned",
])
def test_answered_parser_all_statuses(st) -> None:
    parsed = parse_tool_output(
        "onboarding_answered",
        f"ok:answered:addressing:next=self_intro:status={st}",
    )
    assert isinstance(parsed, OnboardingAnsweredOk)
    assert parsed.onboarding_status == st


def test_answered_parser_next_is_none() -> None:
    """Final topic answered → next=none."""
    parsed = parse_tool_output(
        "onboarding_answered",
        "ok:answered:pain_point:next=none:status=complete",
    )
    assert isinstance(parsed, OnboardingAnsweredOk)
    assert parsed.next_topic == "none"


def test_answered_parser_rejects_unknown_topic() -> None:
    parsed = parse_tool_output(
        "onboarding_answered",
        "ok:answered:weird:next=none:status=complete",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_answered_parser_rejects_unknown_status() -> None:
    parsed = parse_tool_output(
        "onboarding_answered",
        "ok:answered:addressing:next=none:status=weird",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_answered_parser_error_unknown_topic_via_runtime() -> None:
    parsed = parse_tool_output(
        "onboarding_answered", "error: unknown topic 'weird'",
    )
    assert isinstance(parsed, HousewifeToolError)


def test_answered_parser_error_empty_summary() -> None:
    parsed = parse_tool_output(
        "onboarding_answered", "error: empty summary",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "empty_summary"


def test_answered_parser_topic_not_in_active_flow() -> None:
    """Codex Sub-A4 onboarding R5 MAJOR (HIGH catch): runtime
    rejects inactive topics with `error: topic_not_in_active_flow 'X'`.
    Stable error code for planner branching."""
    parsed = parse_tool_output(
        "onboarding_answered",
        "error: topic_not_in_active_flow 'family'",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "topic_not_in_active_flow"


def test_deferred_parser_topic_not_in_active_flow() -> None:
    parsed = parse_tool_output(
        "onboarding_deferred",
        "error: topic_not_in_active_flow 'diet'",
    )
    assert isinstance(parsed, HousewifeToolError)
    assert parsed.error_code == "topic_not_in_active_flow"


@pytest.mark.parametrize("ts", [
    "pending", "answered", "skipped_once", "skipped",
])
def test_deferred_parser_all_topic_states(ts) -> None:
    parsed = parse_tool_output(
        "onboarding_deferred",
        f"ok:deferred:family:topic_state={ts}:next=diet:status=in_progress",
    )
    assert isinstance(parsed, OnboardingDeferredOk)
    assert parsed.topic_state == ts


def test_deferred_parser_rejects_unknown_topic_state() -> None:
    parsed = parse_tool_output(
        "onboarding_deferred",
        "ok:deferred:family:topic_state=weird:next=diet:status=in_progress",
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_complete_parser_status_complete() -> None:
    """Codex R1 MAJOR #2: runtime mark_complete ALWAYS sets
    STATUS_COMPLETE (housewife_onboarding.py:373). Schema accepts
    ONLY `complete`."""
    parsed = parse_tool_output("onboarding_complete", "ok:complete:status=complete")
    assert isinstance(parsed, OnboardingCompleteOk)
    assert parsed.onboarding_status == "complete"


def test_complete_parser_rejects_non_complete_status() -> None:
    """Codex R1 MAJOR #2: any status besides `complete` is runtime
    drift → ContractViolation. Even `abandoned` (which we previously
    allowed) doesn't appear in actual runtime."""
    for st in ("not_started", "in_progress", "abandoned", "weird"):
        parsed = parse_tool_output(
            "onboarding_complete", f"ok:complete:status={st}",
        )
        assert isinstance(parsed, ToolOutputContractViolation), (
            f"status={st!r} should be ContractViolation (runtime "
            f"only emits 'complete')"
        )


# ---------------------------------------------------------------------------
# TypeAdapter parser→output_model parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,raw", [
    ("onboarding_answered", "ok:answered:addressing:next=self_intro:status=in_progress"),
    ("onboarding_answered", "ok:answered:pain_point:next=none:status=complete"),
    ("onboarding_answered", "error: empty summary"),
    ("onboarding_deferred", "ok:deferred:family:topic_state=skipped_once:next=diet:status=in_progress"),
    ("onboarding_deferred", "ok:deferred:diet:topic_state=skipped:next=routine:status=in_progress"),
    ("onboarding_complete", "ok:complete:status=complete"),
])
def test_onboarding_parser_outputs_validate_against_spec_output_model(tool, raw):
    spec = next(s for s in ONBOARDING_SPECS if s.name == tool)
    parsed = parse_tool_output(tool, raw)
    assert not isinstance(parsed, ToolOutputContractViolation), (
        f"unexpected violation for {tool} / {raw!r}"
    )
    adapter = TypeAdapter(spec.output_model)
    validated = adapter.validate_python(parsed.model_dump())
    assert validated.status == parsed.status


def test_onboarding_typeadapter_rejects_sentinel() -> None:
    for spec in ONBOARDING_SPECS:
        adapter = TypeAdapter(spec.output_model)
        with pytest.raises(ValidationError):
            adapter.validate_python({
                "status": "contract_violation",
                "raw_output": "garbage",
                "tool_name": spec.name,
                "timestamp": "2026-05-27T00:00:00Z",
            })


def test_migrated_tool_specs_aggregate_includes_onboarding() -> None:
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    onboarding_names = {s.name for s in ONBOARDING_SPECS}
    migrated_names = {s.name for s in MIGRATED_TOOL_SPECS}
    assert onboarding_names.issubset(migrated_names)
