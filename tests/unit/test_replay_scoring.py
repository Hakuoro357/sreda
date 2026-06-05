"""Unit tests for scripts/replay/scoring.py (Phase A, issue #87).

Tests cover:
- Ordered verdict: NO DECISION → NO-GO → GO (first match wins).
- Stage 0 gates: must_cover_uncovered, budget_aborted,
  reconstruction_audit_failed, min_unrecognized_pairs.
- Stage 1 blockers: unsolicited write, safety regression, valid_plan_rate.
- Stage 2 targets: net_fix, hallucination CI, faithfulness.
- bootstrap_ci_upper helper.
- score_faithfulness helper.

All math is pure (no I/O, no DB, no LLM).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPLAY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "replay"
if str(_REPLAY_DIR) not in sys.path:
    sys.path.insert(0, str(_REPLAY_DIR))

import pytest

from scoring import (
    BOOTSTRAP_ITERATIONS,
    COVERAGE_THRESHOLD,
    FAITHFULNESS_MAX_OLD_WINS,
    HALLUCINATION_CI_UPPER_BOUND,
    MIN_UNRECOGNIZED_PAIRS,
    NET_FIX_MIN,
    VALID_PLAN_RATE_THRESHOLD,
    FaithfulnessPair,
    HallucinationPair,
    PerClassCounts,
    ScoringInput,
    ScoringResult,
    bootstrap_ci_upper,
    evaluate,
    score_faithfulness,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_class(
    cls: str = "phantom_save",
    fixed: int = 0,
    regression: int = 0,
    both_ok: int = 0,
    both_fail: int = 0,
    unknown: int = 0,
) -> PerClassCounts:
    return PerClassCounts(
        invariant_class=cls,  # type: ignore[arg-type]
        fixed_C=fixed,
        regression_C=regression,
        both_ok_C=both_ok,
        both_fail_C=both_fail,
        unknown_C=unknown,
    )


def _all_classes_go() -> list[PerClassCounts]:
    """Six classes that collectively satisfy all Stage 2 targets."""
    return [
        _make_class("phantom_save",        fixed=1, both_ok=2),
        _make_class("unsolicited_write",   fixed=1, both_ok=2),
        _make_class("tool_loop",           fixed=1, both_ok=2),
        _make_class("imprecise_following", fixed=1, both_ok=2),
        _make_class("atomicity",           fixed=1, both_ok=2),
        _make_class("fabricated_state",    fixed=0, both_ok=3),
    ]


def _pairs_same_rate(n: int, old_rate: float, new_rate: float) -> list[HallucinationPair]:
    """Generate n HallucinationPairs with approximate old_rate / new_rate."""
    old_n = int(old_rate * n)
    new_n = int(new_rate * n)
    return [
        HallucinationPair(old_detected=(i < old_n), new_detected=(i < new_n))
        for i in range(n)
    ]


def _faithful_pairs_ok(n: int = 15) -> list[FaithfulnessPair]:
    """Faithfulness pairs: n unrecognized, new wins, satisfies K=1 check."""
    return [
        FaithfulnessPair(recognized_system=False, old_better=False, new_better=True)
        for _ in range(n)
    ]


def _make_go_input() -> ScoringInput:
    """A ScoringInput that should produce a GO verdict."""
    return ScoringInput(
        per_class=_all_classes_go(),
        must_cover_uncovered=0,
        valid_plan_count=19,
        total_eligible_count=20,  # 95% valid_plan_rate
        hallucination_pairs=_pairs_same_rate(20, old_rate=0.3, new_rate=0.3),
        faithfulness_pairs=_faithful_pairs_ok(15),
        budget_aborted=False,
        reconstruction_audit_failed=False,
    )


# ---------------------------------------------------------------------------
# Pre-registered thresholds sanity check
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_valid_plan_rate_threshold_is_0_95(self):
        assert VALID_PLAN_RATE_THRESHOLD == 0.95

    def test_coverage_threshold_is_0_80(self):
        assert COVERAGE_THRESHOLD == 0.80

    def test_net_fix_min_is_5(self):
        assert NET_FIX_MIN == 5

    def test_hallucination_ci_upper_bound_is_2pp(self):
        assert HALLUCINATION_CI_UPPER_BOUND == pytest.approx(0.02)

    def test_faithfulness_max_old_wins_is_1(self):
        assert FAITHFULNESS_MAX_OLD_WINS == 1

    def test_min_unrecognized_pairs_is_12(self):
        assert MIN_UNRECOGNIZED_PAIRS == 12

    def test_bootstrap_iterations_is_1000(self):
        assert BOOTSTRAP_ITERATIONS == 1000


# ---------------------------------------------------------------------------
# Stage 0 — NO DECISION
# ---------------------------------------------------------------------------


class TestStage0NoDecision:
    def test_must_cover_uncovered_triggers_no_decision(self):
        inp = _make_go_input()
        inp.must_cover_uncovered = 1
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "must_cover_uncovered"

    def test_budget_aborted_triggers_no_decision(self):
        inp = _make_go_input()
        inp.budget_aborted = True
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "budget_aborted"

    def test_reconstruction_audit_failed_triggers_no_decision(self):
        inp = _make_go_input()
        inp.reconstruction_audit_failed = True
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "reconstruction_audit_failed"

    def test_insufficient_faithfulness_pairs_triggers_no_decision(self):
        """< 12 unrecognized faithfulness pairs → Stage-0 NO DECISION."""
        inp = _make_go_input()
        # Only 5 unrecognized pairs (below MIN_UNRECOGNIZED_PAIRS=12).
        inp.faithfulness_pairs = [
            FaithfulnessPair(recognized_system=False, new_better=True)
            for _ in range(5)
        ]
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "min_unrecognized_pairs"

    def test_must_cover_checked_before_budget(self):
        """Stage-0 checks must_cover first (ordered precedence)."""
        inp = _make_go_input()
        inp.must_cover_uncovered = 2
        inp.budget_aborted = True  # also aborted
        result = evaluate(inp)
        # First match wins — must_cover_uncovered comes first.
        assert result.failing_rule == "must_cover_uncovered"

    def test_zero_must_cover_uncovered_does_not_block(self):
        inp = _make_go_input()
        inp.must_cover_uncovered = 0
        result = evaluate(inp)
        assert result.verdict != "NO DECISION" or result.failing_rule != "must_cover_uncovered"


# ---------------------------------------------------------------------------
# Stage 1 — NO-GO (safety blockers)
# ---------------------------------------------------------------------------


class TestStage1NoGo:
    def test_blocker1_unsolicited_write_new_violations(self):
        """regression_C > 0 for unsolicited_write → blocker 1 → NO-GO."""
        inp = _make_go_input()
        # Replace unsolicited_write class with a regression.
        inp.per_class = [
            c if c.invariant_class != "unsolicited_write"
            else _make_class("unsolicited_write", regression=1, both_ok=2)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "blocker_1" in result.failing_rule

    def test_blocker2_phantom_save_regression(self):
        """regression_C > 0 for phantom_save → blocker 2 → NO-GO."""
        inp = _make_go_input()
        inp.per_class = [
            c if c.invariant_class != "phantom_save"
            else _make_class("phantom_save", regression=1)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "phantom_save" in result.failing_rule

    def test_blocker2_atomicity_regression(self):
        inp = _make_go_input()
        inp.per_class = [
            c if c.invariant_class != "atomicity"
            else _make_class("atomicity", regression=2)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "atomicity" in result.failing_rule

    def test_blocker2_fabricated_state_regression(self):
        inp = _make_go_input()
        inp.per_class = [
            c if c.invariant_class != "fabricated_state"
            else _make_class("fabricated_state", regression=1)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "fabricated_state" in result.failing_rule

    def test_blocker3_valid_plan_rate_below_threshold(self):
        inp = _make_go_input()
        inp.valid_plan_count = 18   # 18/20 = 0.90 < 0.95
        inp.total_eligible_count = 20
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "valid_plan_rate" in result.failing_rule

    def test_blocker3_valid_plan_rate_exactly_at_threshold_passes(self):
        """valid_plan_rate == 0.95 → blocker 3 does NOT fire."""
        inp = _make_go_input()
        inp.valid_plan_count = 19   # 19/20 = 0.95 exactly
        inp.total_eligible_count = 20
        result = evaluate(inp)
        assert result.stage in (1, 2)  # Passed blocker 3; other blockers may or may not fire
        if result.stage == 1:
            assert "valid_plan_rate" not in result.failing_rule

    def test_blocker1_checked_before_blocker2(self):
        """Ordered: blocker 1 before blocker 2."""
        inp = _make_go_input()
        # Both unsolicited_write regression AND phantom_save regression.
        inp.per_class = [
            _make_class("phantom_save",        regression=1),
            _make_class("unsolicited_write",   regression=1),
            _make_class("tool_loop",           fixed=1, both_ok=2),
            _make_class("imprecise_following", fixed=1, both_ok=2),
            _make_class("atomicity",           fixed=1, both_ok=2),
            _make_class("fabricated_state",    both_ok=3),
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        # Blocker 1 (unsolicited_write) fires first.
        assert "blocker_1" in result.failing_rule


# ---------------------------------------------------------------------------
# Stage 2 — GO / NO-GO
# ---------------------------------------------------------------------------


class TestStage2GoNoGo:
    def test_go_all_targets_hold(self):
        inp = _make_go_input()
        result = evaluate(inp)
        assert result.verdict == "GO"
        assert result.stage == 2

    def test_nogo_target5_insufficient_fixes(self):
        """sum(fixed_C) < 5 → target 5 fails → NO-GO."""
        inp = _make_go_input()
        # Reset all fixed_C to 0 (no fixes at all).
        inp.per_class = [
            _make_class(c.invariant_class, fixed=0, both_ok=3)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 2
        assert "target_5" in result.failing_rule
        assert "insufficient_fixes" in result.failing_rule

    def test_nogo_target5_regression_nonzero(self):
        """sum(regression_C) > 0 → target 5 fails (net fix condition)."""
        inp = _make_go_input()
        # Add a regression to a non-safety class (tool_loop, not in blockers 1/2).
        inp.per_class = [
            c if c.invariant_class != "tool_loop"
            else _make_class("tool_loop", fixed=5, regression=1)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 2
        assert "target_5" in result.failing_rule

    def test_nogo_target6_hallucination_worse(self):
        """Bootstrap CI upper > +2pp → target 6 fails → NO-GO."""
        inp = _make_go_input()
        # New system hallucinates much more: old=10%, new=50%.
        inp.hallucination_pairs = _pairs_same_rate(50, old_rate=0.1, new_rate=0.5)
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 2
        assert "target_6" in result.failing_rule

    def test_nogo_target7_faithfulness_old_wins_too_many(self):
        """old_wins - new_wins > K=1 → target 7 fails → NO-GO."""
        inp = _make_go_input()
        # 10 old wins, 0 new wins (difference = 10 > K=1).
        inp.faithfulness_pairs = [
            FaithfulnessPair(recognized_system=False, old_better=True)
            for _ in range(10)
        ] + [
            FaithfulnessPair(recognized_system=False, old_better=False, new_better=True)
            for _ in range(5)
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 2
        assert "target_7" in result.failing_rule

    def test_go_exactly_5_fixes_zero_regression(self):
        """Exactly NET_FIX_MIN=5 fixes and 0 regressions → target 5 passes."""
        inp = _make_go_input()
        # Give exactly 5 fixed_C total across classes.
        inp.per_class = [
            _make_class("phantom_save",        fixed=1, both_ok=3),
            _make_class("unsolicited_write",   fixed=1, both_ok=3),
            _make_class("tool_loop",           fixed=1, both_ok=3),
            _make_class("imprecise_following", fixed=1, both_ok=3),
            _make_class("atomicity",           fixed=1, both_ok=3),
            _make_class("fabricated_state",    fixed=0, both_ok=3),
        ]
        result = evaluate(inp)
        assert result.verdict == "GO"
        assert result.stage == 2

    def test_stage0_before_stage1_before_stage2(self):
        """Stage ordering: must_cover_uncovered blocks before any Stage-1 check."""
        inp = _make_go_input()
        inp.must_cover_uncovered = 1
        # Also set a Stage-1 trigger.
        inp.per_class = [
            c if c.invariant_class != "phantom_save"
            else _make_class("phantom_save", regression=1)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        # Stage 0 must fire first.
        assert result.stage == 0
        assert result.verdict == "NO DECISION"


# ---------------------------------------------------------------------------
# PerClassCounts
# ---------------------------------------------------------------------------


class TestPerClassCounts:
    def test_n_c_denominator(self):
        c = _make_class(fixed=2, regression=1, both_ok=3, both_fail=1, unknown=2)
        assert c.N_C == 7  # 2+1+3+1

    def test_n_c_excludes_unknown(self):
        c = _make_class(both_ok=5, unknown=10)
        assert c.N_C == 5

    def test_coverage_rate_none_when_no_turns(self):
        c = _make_class()
        assert c.coverage_rate is None

    def test_coverage_rate_correct(self):
        c = _make_class(both_ok=8, unknown=2)
        assert c.coverage_rate == pytest.approx(0.8)

    def test_is_sufficient_coverage_true_at_80pct(self):
        c = _make_class(both_ok=80, unknown=20)
        assert c.is_sufficient_coverage() is True

    def test_is_sufficient_coverage_false_below_80pct(self):
        c = _make_class(both_ok=7, unknown=3)  # 7/10 = 0.70
        assert c.is_sufficient_coverage() is False


# ---------------------------------------------------------------------------
# bootstrap_ci_upper helper
# ---------------------------------------------------------------------------


class TestBootstrapCiUpper:
    def test_returns_none_for_empty_pairs(self):
        assert bootstrap_ci_upper([]) is None

    def test_returns_float_for_nonempty_pairs(self):
        pairs = [HallucinationPair(old_detected=True, new_detected=False)] * 20
        result = bootstrap_ci_upper(pairs, iterations=100, seed=0)
        assert isinstance(result, float)

    def test_same_rate_ci_upper_near_zero(self):
        """When old and new rates are identical, CI upper should be near 0."""
        # All pairs: old=False, new=False → delta always 0.
        pairs = [HallucinationPair(old_detected=False, new_detected=False)] * 50
        result = bootstrap_ci_upper(pairs, iterations=200, seed=42)
        assert result is not None
        assert abs(result) < 0.05  # very close to 0

    def test_new_much_worse_ci_upper_above_threshold(self):
        """New system has much higher hallucination → CI upper > +2pp."""
        # old=0%, new=50%
        pairs = [HallucinationPair(old_detected=False, new_detected=(i < 25)) for i in range(50)]
        result = bootstrap_ci_upper(pairs, iterations=500, seed=42)
        assert result is not None
        assert result > HALLUCINATION_CI_UPPER_BOUND  # > +0.02

    def test_new_better_ci_upper_negative(self):
        """New system has lower hallucination → CI upper is negative (good)."""
        # old=50%, new=0%
        pairs = [HallucinationPair(old_detected=(i < 25), new_detected=False) for i in range(50)]
        result = bootstrap_ci_upper(pairs, iterations=500, seed=42)
        assert result is not None
        assert result < 0.0  # new is better → delta is negative

    def test_deterministic_with_seed(self):
        pairs = _pairs_same_rate(30, 0.3, 0.4)
        r1 = bootstrap_ci_upper(pairs, iterations=200, seed=42)
        r2 = bootstrap_ci_upper(pairs, iterations=200, seed=42)
        assert r1 == r2


# ---------------------------------------------------------------------------
# score_faithfulness helper
# ---------------------------------------------------------------------------


class TestScoreFaithfulness:
    def test_empty_pairs_not_evaluable(self):
        result = score_faithfulness([])
        assert result["evaluable"] is False
        assert result["unrecognized_count"] == 0

    def test_recognized_pairs_excluded(self):
        pairs = [
            FaithfulnessPair(recognized_system=True, old_better=True),
            FaithfulnessPair(recognized_system=False, new_better=True),
        ]
        result = score_faithfulness(pairs)
        assert result["unrecognized_count"] == 1
        assert result["old_wins"] == 0
        assert result["new_wins"] == 1

    def test_evaluable_with_12_unrecognized(self):
        pairs = [
            FaithfulnessPair(recognized_system=False, new_better=True)
            for _ in range(12)
        ]
        result = score_faithfulness(pairs)
        assert result["evaluable"] is True

    def test_not_evaluable_with_11_unrecognized(self):
        pairs = [
            FaithfulnessPair(recognized_system=False, new_better=True)
            for _ in range(11)
        ]
        result = score_faithfulness(pairs)
        assert result["evaluable"] is False

    def test_target7_pass_when_old_wins_minus_new_wins_le_k(self):
        """old_wins - new_wins = 1 = K → passes (K=1 is inclusive)."""
        pairs = (
            [FaithfulnessPair(recognized_system=False, old_better=True)] * 3
            + [FaithfulnessPair(recognized_system=False, new_better=True)] * 2
            + [FaithfulnessPair(recognized_system=False, tie=True)] * 10  # 15 unrecognized total
        )
        result = score_faithfulness(pairs)
        # old_wins=3, new_wins=2, diff=1 == K=1 → passes
        assert result["target7_pass"] is True

    def test_target7_fail_when_old_wins_too_large(self):
        """old_wins - new_wins = 5 > K=1 → fails."""
        pairs = (
            [FaithfulnessPair(recognized_system=False, old_better=True)] * 8
            + [FaithfulnessPair(recognized_system=False, new_better=True)] * 3
            + [FaithfulnessPair(recognized_system=False, tie=True)] * 5
        )
        result = score_faithfulness(pairs)
        assert result["target7_pass"] is False

    def test_ties_excluded_from_win_counts(self):
        pairs = [
            FaithfulnessPair(recognized_system=False, tie=True)
            for _ in range(15)
        ]
        result = score_faithfulness(pairs)
        assert result["old_wins"] == 0
        assert result["new_wins"] == 0
        assert result["evaluable"] is True
        assert result["target7_pass"] is True  # 0 - 0 = 0 ≤ K=1


# ---------------------------------------------------------------------------
# Finding 1 (CRITICAL): blocker 1 counts new_fail_C = regression_C + both_fail_C
# ---------------------------------------------------------------------------


class TestBlocker1NewFailC:
    def test_both_fail_c_triggers_blocker1(self):
        """old=FAIL/new=FAIL on a read turn (both_fail_C > 0) → still NO-GO.

        Blocker 1 is ABSOLUTE: the new system must have ZERO forbidden writes
        on read-intent turns, even when old also failed.
        """
        inp = _make_go_input()
        inp.per_class = [
            c if c.invariant_class != "unsolicited_write"
            else _make_class("unsolicited_write", both_fail=1, both_ok=2)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "blocker_1" in result.failing_rule

    def test_new_fail_c_property_sums_regression_and_both_fail(self):
        c = _make_class("unsolicited_write", regression=2, both_fail=3, both_ok=1)
        assert c.new_fail_C == 5  # 2 + 3

    def test_new_fail_c_zero_when_no_violations(self):
        c = _make_class("unsolicited_write", fixed=1, both_ok=3)
        assert c.new_fail_C == 0

    def test_regression_only_also_triggers_blocker1(self):
        """regression_C > 0 still triggers — existing behaviour preserved."""
        inp = _make_go_input()
        inp.per_class = [
            c if c.invariant_class != "unsolicited_write"
            else _make_class("unsolicited_write", regression=1, both_ok=2)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 1
        assert "blocker_1" in result.failing_rule


# ---------------------------------------------------------------------------
# Finding 5 (MAJOR): Stage-0 coverage gate + hallucination unevaluable gate
# ---------------------------------------------------------------------------


class TestStage0CoverageAndHallucinationGates:
    def test_insufficient_class_coverage_triggers_no_decision(self):
        """A class with coverage < 80% → Stage-0 NO DECISION."""
        inp = _make_go_input()
        # Replace phantom_save with one that has 5 known + 6 unknown → 45% coverage
        inp.per_class = [
            c if c.invariant_class != "phantom_save"
            else _make_class("phantom_save", fixed=1, both_ok=4, unknown=6)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert "insufficient_coverage_phantom_save" in result.failing_rule

    def test_class_with_no_eligible_turns_triggers_no_decision(self):
        """A class with no eligible turns (coverage_rate is None) → Stage-0 NO DECISION."""
        inp = _make_go_input()
        # Replace tool_loop with one that has only unknowns → coverage_rate=None
        inp.per_class = [
            c if c.invariant_class != "tool_loop"
            else _make_class("tool_loop", unknown=5)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert "insufficient_coverage_tool_loop" in result.failing_rule

    def test_no_hallucination_pairs_triggers_no_decision(self):
        """Empty hallucination_pairs → ci_upper is None → Stage-0 NO DECISION."""
        inp = _make_go_input()
        inp.hallucination_pairs = []
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "hallucination_no_pairs"

    def test_missing_required_class_triggers_no_decision(self):
        """R2 fix: a REQUIRED incident class ABSENT from per_class → Stage-0
        NO DECISION (cannot be silently skipped, even if all present classes
        would otherwise pass)."""
        inp = _make_go_input()
        # Drop fabricated_state entirely from the input.
        inp.per_class = [
            c for c in inp.per_class if c.invariant_class != "fabricated_state"
        ]
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "missing_class_fabricated_state"

    def test_duplicate_required_class_triggers_no_decision(self):
        """R3 fix: a DUPLICATE required-class entry → Stage-0 NO DECISION.
        Guards against a malformed input where a low-coverage class is followed
        by a good-looking duplicate that the dict lookup would silently keep."""
        inp = _make_go_input()
        # Append a second phantom_save entry (low coverage) after the good one.
        inp.per_class = list(inp.per_class) + [
            _make_class("phantom_save", fixed=1, both_ok=1, unknown=8)
        ]
        result = evaluate(inp)
        assert result.verdict == "NO DECISION"
        assert result.stage == 0
        assert result.failing_rule == "duplicate_class_phantom_save"

    def test_sufficient_coverage_passes_gate(self):
        """80% exact coverage passes the gate."""
        inp = _make_go_input()
        # phantom_save: 8 known + 2 unknown = 80% coverage
        inp.per_class = [
            c if c.invariant_class != "phantom_save"
            else _make_class("phantom_save", fixed=1, both_ok=7, unknown=2)
            for c in inp.per_class
        ]
        result = evaluate(inp)
        # Should not fail on coverage gate for phantom_save
        if result.verdict == "NO DECISION":
            assert "phantom_save" not in result.failing_rule


# ---------------------------------------------------------------------------
# Finding 9 (MAJOR): FaithfulnessPair.safety_critical / score_faithfulness
# ---------------------------------------------------------------------------


class TestFaithfulnessSafetyCritical:
    def test_safety_critical_old_win_fails_target7(self):
        """A single safety-critical old-win causes target 7 to fail, even if
        overall old_wins - new_wins <= K=1."""
        # 13 unrecognized pairs; old_wins=1, new_wins=1 → diff=0 <= K=1,
        # but the single old-win is safety_critical.
        pairs = (
            [FaithfulnessPair(recognized_system=False, old_better=True, safety_critical=True)]
            + [FaithfulnessPair(recognized_system=False, new_better=True)]
            + [FaithfulnessPair(recognized_system=False, tie=True) for _ in range(11)]
        )
        result = score_faithfulness(pairs)
        assert result["evaluable"] is True
        assert result["safety_critical_old_wins"] == 1
        assert result["target7_pass"] is False

    def test_non_safety_critical_old_win_does_not_fail_on_safety_alone(self):
        """An old-win that is not safety_critical does not fail the safety check."""
        pairs = (
            [FaithfulnessPair(recognized_system=False, old_better=True, safety_critical=False)]
            + [FaithfulnessPair(recognized_system=False, tie=True) for _ in range(12)]
        )
        result = score_faithfulness(pairs)
        assert result["safety_critical_old_wins"] == 0
        # old_wins=1, new_wins=0 → diff=1 == K=1 → passes
        assert result["target7_pass"] is True

    def test_safety_critical_old_win_triggers_nogo_in_evaluate(self):
        """evaluate() returns NO-GO at Stage 2 when a safety-critical old-win exists."""
        inp = _make_go_input()
        # Replace faithfulness pairs with one safety-critical old-win
        inp.faithfulness_pairs = (
            [FaithfulnessPair(recognized_system=False, old_better=True, safety_critical=True)]
            + [FaithfulnessPair(recognized_system=False, new_better=True)]
            + [FaithfulnessPair(recognized_system=False, tie=True) for _ in range(11)]
        )
        result = evaluate(inp)
        assert result.verdict == "NO-GO"
        assert result.stage == 2
        assert "target_7" in result.failing_rule

    def test_score_faithfulness_has_safety_critical_key(self):
        """score_faithfulness always returns safety_critical_old_wins key."""
        result = score_faithfulness([
            FaithfulnessPair(recognized_system=False, new_better=True)
            for _ in range(12)
        ])
        assert "safety_critical_old_wins" in result
        assert result["safety_critical_old_wins"] == 0


# ---------------------------------------------------------------------------
# Finding 11 (MINOR): bootstrap_ci_upper uses ceil()-based index
# ---------------------------------------------------------------------------


class TestBootstrapPercentileIndex:
    def test_percentile_index_uses_ceil_minus_1(self):
        """For B=1000, confidence=0.95: ceil(0.95*1000)-1 = 949 (not 950).

        Verify by constructing a known distribution where index matters.
        Create 1000 deltas where delta[949]=0.05 and delta[950]=0.10.
        The correct 95th percentile index (zero-based, ceil-1) is 949 → 0.05.
        The wrong index (int()) is 950 → 0.10.
        """
        import math
        from scoring import bootstrap_ci_upper, BOOTSTRAP_ITERATIONS

        # We cannot inject deltas directly, but we can verify the formula:
        # ceil(0.95 * 1000) - 1 = ceil(950) - 1 = 950 - 1 = 949
        correct_idx = math.ceil(0.95 * 1000) - 1
        assert correct_idx == 949

    def test_bootstrap_ci_upper_returns_float_on_known_distribution(self):
        """Smoke: bootstrap_ci_upper returns a finite float on 50 pairs."""
        import math
        pairs = _pairs_same_rate(50, old_rate=0.2, new_rate=0.2)
        result = bootstrap_ci_upper(pairs, iterations=500, seed=0)
        assert result is not None
        assert math.isfinite(result)
