"""Unit tests for scripts/eval_planner_live.py — Sub-A12 Phase B.7.

The eval script is MANUAL — it calls real LLM, costs money, gates Phase E
rollout. Tests here cover ONLY the offline-safe parts:

* aggregate() — percentile / rate math
* EvalReport.thresholds_met / to_markdown
* _parse_args — CLI surface

We do NOT call orchestrator.run() with a real LLM from tests. Snapshot
tests (Phase B.6) already cover the orchestrator end-to-end with mocked
LLM.
"""

from __future__ import annotations

from pathlib import Path

from scripts.eval_planner_live import (
    ScenarioOutcome,
    _parse_args,
    aggregate,
)


# ---------------------------------------------------------------------------
# aggregate()
# ---------------------------------------------------------------------------


def _outcome(
    *,
    success: bool = True,
    attempt: int = 1,
    latency_ms: int = 100,
    err: str | None = None,
) -> ScenarioOutcome:
    return ScenarioOutcome(
        label="t",
        user_message="x",
        success=success,
        final_attempt_no=attempt,
        latency_ms=latency_ms,
        error_summary=err,
    )


def test_aggregate_empty_outcomes() -> None:
    """Zero scenarios → zero rates, zero latencies, no crash."""
    report = aggregate([], require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.valid_rate == 0.0
    assert report.retry_rate == 0.0
    assert report.p50_latency_ms == 0
    assert report.p95_latency_ms == 0


def test_aggregate_all_valid() -> None:
    outcomes = [_outcome(success=True, latency_ms=200) for _ in range(5)]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.valid_rate == 1.0
    assert report.retry_rate == 0.0


def test_aggregate_mix_valid_and_invalid() -> None:
    outcomes = [
        _outcome(success=True), _outcome(success=True),
        _outcome(success=False, err="invalid_plan_after_retry"),
        _outcome(success=True), _outcome(success=True),
    ]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.valid_rate == 0.8  # 4/5


def test_aggregate_retry_rate_counts_attempt_gt_1() -> None:
    outcomes = [
        _outcome(attempt=1), _outcome(attempt=1),
        _outcome(attempt=2),  # retry happened
        _outcome(attempt=2),
    ]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.retry_rate == 0.5


def test_aggregate_p95_latency_for_small_n() -> None:
    """p95 idx = ceil(0.95 * n) - 1. For n=5 → idx = 4 → last item."""
    outcomes = [_outcome(latency_ms=ms) for ms in [100, 200, 300, 400, 500]]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.p95_latency_ms == 500


def test_aggregate_thresholds_met_happy_path() -> None:
    outcomes = [_outcome(success=True, latency_ms=100) for _ in range(10)]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.thresholds_met is True


def test_aggregate_thresholds_failed_on_valid_rate() -> None:
    outcomes = [
        _outcome(success=True), _outcome(success=True),
        _outcome(success=False, err="x"),  # 2/3 = 0.67
    ]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.thresholds_met is False


def test_aggregate_thresholds_failed_on_p95_latency() -> None:
    outcomes = [_outcome(success=True, latency_ms=ms) for ms in [100, 200, 300, 400, 20000]]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    assert report.thresholds_met is False
    assert report.p95_latency_ms == 20000


# ---------------------------------------------------------------------------
# EvalReport.to_markdown
# ---------------------------------------------------------------------------


def test_to_markdown_includes_summary_and_per_scenario_rows() -> None:
    outcomes = [_outcome(success=True, latency_ms=150)]
    report = aggregate(outcomes, require_valid_rate=0.9, require_p95_latency_ms=15000)
    md = report.to_markdown()
    assert "Planner live-eval report" in md
    assert "valid rate" in md
    assert "user_message" in md
    assert "OK" in md


def test_to_markdown_handles_pipe_in_user_message() -> None:
    """Pipes break markdown tables — must be replaced."""
    o = ScenarioOutcome(
        label="t", user_message="купи | хлеб", success=True,
        final_attempt_no=1, latency_ms=100, error_summary=None,
    )
    report = aggregate([o], require_valid_rate=0.9, require_p95_latency_ms=15000)
    md = report.to_markdown()
    table_line = [line for line in md.split("\n") if "1 |" in line][0]
    # The user_message column shouldn't contain a bare pipe that would
    # split the cell
    cells = table_line.split("|")
    assert len(cells) == 8  # leading + 6 cols + trailing empty


def test_to_markdown_failure_rows_show_error() -> None:
    o = _outcome(success=False, err="timeout")
    report = aggregate([o], require_valid_rate=0.9, require_p95_latency_ms=15000)
    md = report.to_markdown()
    assert "FAIL" in md
    assert "timeout" in md


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = _parse_args([])
    assert args.limit == 5
    assert args.require_valid_rate == 0.90
    assert args.require_p95_latency_ms == 15000
    assert args.tenant_id == "eval_local"
    assert args.output is None


def test_parse_args_thresholds_overrides() -> None:
    args = _parse_args([
        "--limit", "10",
        "--require-valid-rate", "0.95",
        "--require-p95-latency-ms", "8000",
        "--tenant-id", "tenant_test",
    ])
    assert args.limit == 10
    assert args.require_valid_rate == 0.95
    assert args.require_p95_latency_ms == 8000
    assert args.tenant_id == "tenant_test"


def test_parse_args_output_path(tmp_path: Path) -> None:
    args = _parse_args(["--output", str(tmp_path / "report.md")])
    assert args.output == tmp_path / "report.md"
