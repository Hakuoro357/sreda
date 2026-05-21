from __future__ import annotations

from sreda.eval.llm_eval_v2_report import build_json_report, build_markdown_report
from sreda.eval.llm_eval_v2_runner import ScenarioResult
from sreda.eval.llm_eval_v2_scenarios import ExpectedState


def _result(verdict: str) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=f"scenario_{verdict.lower()}",
        provider="fake",
        verdict=verdict,
        failure_reason=None if verdict == "PASS" else "boom",
        before_state=ExpectedState(),
        after_state=ExpectedState(),
        tool_calls_per_turn=((),),
        journal=(),
        final_text="ok",
    )


def test_report_separates_core_from_preflight_and_harness_safety() -> None:
    report = build_json_report(
        core_results=[],
        preflight_results=[{"name": "provider_health", "verdict": "FAIL"}],
        harness_safety_results=[{"name": "schema_validation", "verdict": "PASS"}],
    )

    assert "core_llm" in report
    assert "preflight" in report
    assert "harness_safety" in report
    assert report["summary"]["core_denominator"] == 0
    assert report["summary"]["core_passed"] == 0


def test_core_score_ignores_preflight_failures() -> None:
    report = build_json_report(
        core_results=[_result("PASS")],
        preflight_results=[{"name": "provider_health", "verdict": "FAIL"}],
        harness_safety_results=[],
    )

    assert report["summary"]["core_denominator"] == 1
    assert report["summary"]["core_passed"] == 1
    assert report["summary"]["core_score"] == 1.0


def test_markdown_report_has_required_sections() -> None:
    markdown = build_markdown_report(
        build_json_report(
            core_results=[_result("PASS")],
            preflight_results=[],
            harness_safety_results=[],
        )
    )

    assert "## Core LLM Score" in markdown
    assert "## Scenario Results" in markdown
    assert "## Preflight" in markdown
    assert "## Harness Safety" in markdown
    assert "## Summary" in markdown
