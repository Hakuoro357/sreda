"""Unit tests for scripts/replay/manifest.py (Phase A, issue #87).

Tests cover:
- ReplayExpectations.memory_eligible predicate for independent vs
  dependent × status combinations.
- ReplayCase.is_memory_eligible shortcut.
- Frozen dataclass immutability.
- Field defaults produce sensible values.

All fixtures use synthetic placeholder data (tenant_tg_<SMOKE>).
No prod data, no real chat ids.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts/replay to sys.path so we can import Phase A modules.
_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest

from manifest import ReplayCase, ReplayExpectations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_expectations(**kwargs) -> ReplayExpectations:
    """Build a ReplayExpectations with sensible defaults."""
    defaults: dict = {
        "expected_intent": "read",
        "memory_dependence": "independent",
        "current_memory_status": "not_applicable",
        "eligibility_reason": "test fixture",
        "label_source": "manual",
        "labeler": "test",
        "confidence": 1.0,
    }
    defaults.update(kwargs)
    return ReplayExpectations(**defaults)


def _make_case(**expectations_kwargs) -> ReplayCase:
    """Build a ReplayCase with a synthetic placeholder tenant."""
    return ReplayCase(
        turn_id_hash="abcdef0123456789",  # opaque 16-char hex prefix
        tenant_placeholder="tenant_tg_<SMOKE>",
        expectations=_make_expectations(**expectations_kwargs),
    )


# ---------------------------------------------------------------------------
# memory_eligible predicate — independent turns
# ---------------------------------------------------------------------------


class TestMemoryEligibleIndependent:
    """Independent turns are ALWAYS eligible regardless of memory_status."""

    def test_independent_not_applicable_is_eligible(self):
        exp = _make_expectations(
            memory_dependence="independent",
            current_memory_status="not_applicable",
        )
        assert exp.memory_eligible is True

    def test_independent_valid_is_eligible(self):
        """Even with valid status, independent = eligible (status ignored)."""
        exp = _make_expectations(
            memory_dependence="independent",
            current_memory_status="valid",
        )
        assert exp.memory_eligible is True

    def test_independent_stale_is_still_eligible(self):
        """Stale status on an independent turn must NOT exclude it (R5 fix)."""
        exp = _make_expectations(
            memory_dependence="independent",
            current_memory_status="stale",
        )
        assert exp.memory_eligible is True

    def test_independent_missing_is_eligible(self):
        exp = _make_expectations(
            memory_dependence="independent",
            current_memory_status="missing",
        )
        assert exp.memory_eligible is True

    def test_independent_contradicted_is_eligible(self):
        exp = _make_expectations(
            memory_dependence="independent",
            current_memory_status="contradicted",
        )
        assert exp.memory_eligible is True

    def test_independent_unknown_status_is_eligible(self):
        exp = _make_expectations(
            memory_dependence="independent",
            current_memory_status="unknown",
        )
        assert exp.memory_eligible is True


# ---------------------------------------------------------------------------
# memory_eligible predicate — dependent turns
# ---------------------------------------------------------------------------


class TestMemoryEligibleDependent:
    """Dependent turns: eligible ONLY when current_memory_status == 'valid'."""

    def test_dependent_valid_is_eligible(self):
        exp = _make_expectations(
            memory_dependence="dependent",
            current_memory_status="valid",
        )
        assert exp.memory_eligible is True

    def test_dependent_stale_is_not_eligible(self):
        exp = _make_expectations(
            memory_dependence="dependent",
            current_memory_status="stale",
        )
        assert exp.memory_eligible is False

    def test_dependent_missing_is_not_eligible(self):
        exp = _make_expectations(
            memory_dependence="dependent",
            current_memory_status="missing",
        )
        assert exp.memory_eligible is False

    def test_dependent_contradicted_is_not_eligible(self):
        exp = _make_expectations(
            memory_dependence="dependent",
            current_memory_status="contradicted",
        )
        assert exp.memory_eligible is False

    def test_dependent_unknown_status_is_not_eligible(self):
        exp = _make_expectations(
            memory_dependence="dependent",
            current_memory_status="unknown",
        )
        assert exp.memory_eligible is False

    def test_dependent_not_applicable_is_not_eligible(self):
        """'not_applicable' is semantically wrong for dependent turns,
        but the predicate treats it as non-valid → not eligible."""
        exp = _make_expectations(
            memory_dependence="dependent",
            current_memory_status="not_applicable",
        )
        assert exp.memory_eligible is False


# ---------------------------------------------------------------------------
# ReplayCase.is_memory_eligible shortcut
# ---------------------------------------------------------------------------


class TestReplayCaseShortcut:
    def test_shortcut_delegates_to_expectations(self):
        case = _make_case(
            memory_dependence="independent",
            current_memory_status="not_applicable",
        )
        assert case.is_memory_eligible is True

    def test_shortcut_false_for_dependent_stale(self):
        case = _make_case(
            memory_dependence="dependent",
            current_memory_status="stale",
        )
        assert case.is_memory_eligible is False


# ---------------------------------------------------------------------------
# Frozen dataclass immutability
# ---------------------------------------------------------------------------


class TestFrozenImmutability:
    def test_expectations_is_frozen(self):
        exp = _make_expectations()
        with pytest.raises((AttributeError, TypeError)):
            exp.memory_dependence = "dependent"  # type: ignore[misc]

    def test_case_is_frozen(self):
        case = _make_case()
        with pytest.raises((AttributeError, TypeError)):
            case.turn_id_hash = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Field defaults
# ---------------------------------------------------------------------------


class TestFieldDefaults:
    def test_required_outcomes_empty_by_default(self):
        exp = _make_expectations()
        assert exp.required_outcomes == ()

    def test_acceptable_action_sets_empty_by_default(self):
        exp = _make_expectations()
        assert exp.acceptable_action_sets == ()

    def test_forbidden_write_targets_empty_by_default(self):
        exp = _make_expectations()
        assert exp.forbidden_write_targets == ()

    def test_requires_clarification_false_by_default(self):
        exp = _make_expectations()
        assert exp.requires_clarification is False

    def test_must_cover_false_by_default(self):
        exp = _make_expectations()
        assert exp.must_cover is False

    def test_confidence_default_one(self):
        exp = _make_expectations()
        assert exp.confidence == 1.0

    def test_reconstruction_status_none_before_phase_b(self):
        case = _make_case()
        assert case.reconstruction_status is None


# ---------------------------------------------------------------------------
# must_cover + incident_id
# ---------------------------------------------------------------------------


class TestMustCover:
    def test_must_cover_with_incident_id(self):
        exp = _make_expectations(
            must_cover=True,
            incident_id="#59",
        )
        assert exp.must_cover is True
        assert exp.incident_id == "#59"

    def test_must_cover_false_by_default(self):
        exp = _make_expectations()
        assert exp.must_cover is False
        assert exp.incident_id == ""
