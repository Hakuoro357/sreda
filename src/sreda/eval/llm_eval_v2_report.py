"""Report builders for llm_eval_v2."""

from __future__ import annotations

from typing import Any

from sreda.eval.llm_eval_v2_runner import ScenarioResult


def build_json_report(
    *,
    core_results: list[ScenarioResult],
    preflight_results: list[dict[str, Any]],
    harness_safety_results: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for result in core_results if result.verdict == "PASS")
    denominator = len(core_results)
    return {
        "core_llm": [_serialise_result(result) for result in core_results],
        "preflight": preflight_results,
        "harness_safety": harness_safety_results,
        "summary": {
            "core_passed": passed,
            "core_denominator": denominator,
            "core_score": (passed / denominator) if denominator else None,
        },
    }


def build_markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# llm_eval_v2 Report",
        "",
        "## Core LLM Score",
        "",
        f"Passed: {summary['core_passed']} / {summary['core_denominator']}",
        f"Score: {_format_score(summary['core_score'])}",
        "",
        "## Scenario Results",
        "",
    ]
    for result in report["core_llm"]:
        reason = result.get("failure_reason") or "-"
        lines.append(f"- `{result['scenario_id']}` [{result['provider']}]: {result['verdict']} ({reason})")
    if not report["core_llm"]:
        lines.append("- none")
    lines.extend(["", "## Preflight", ""])
    lines.extend(_dict_rows(report["preflight"]))
    lines.extend(["", "## Harness Safety", ""])
    lines.extend(_dict_rows(report["harness_safety"]))
    lines.extend(["", "## Summary", "", f"Core denominator: {summary['core_denominator']}"])
    return "\n".join(lines) + "\n"


def _serialise_result(result: ScenarioResult) -> dict[str, Any]:
    return {
        "scenario_id": result.scenario_id,
        "provider": result.provider,
        "verdict": result.verdict,
        "failure_reason": result.failure_reason,
        "tool_calls_per_turn": result.tool_calls_per_turn,
        "final_text": result.final_text,
    }


def _format_score(score: float | None) -> str:
    if score is None:
        return "n/a"
    return f"{score:.3f}"


def _dict_rows(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- `{item.get('name', 'item')}`: {item.get('verdict', 'UNKNOWN')}" for item in items]
