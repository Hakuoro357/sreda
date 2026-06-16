"""#155: planner_chat diagnostic-persistence extraction (pure, synthetic).

Covers ``_build_diag_persist_args`` + ``_step_to_log`` — the live-path extraction
of ``persist_completed_turn`` kwargs from the planner result + execution log —
without a full chat harness. The write→read roundtrip is in
test_replay_trace_bundle.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sreda.runtime.planner_chat import _build_diag_persist_args, _step_to_log


@dataclass
class _Step:
    step_id: str
    tool: str
    status: str
    parsed_output: dict | None = None
    raw_output: str | None = None
    matched_status: str | None = None
    error_summary: str | None = None
    latency_ms: int | None = None
    matched_branch_index: int | None = None
    selected_compose: object | None = None


def _plan_result(
    *, success=True, execution_id="pe_1", actions=None, error=None,
    execution_plan=True, compose_template=None, compose_llm=None,
):
    plan = None
    if actions is not None or compose_template or compose_llm:
        plan = SimpleNamespace(
            model_dump=lambda mode="json": {"actions": actions or {}},
            compose=SimpleNamespace(
                template_id=compose_template, llm_prompt_key=compose_llm
            ),
        )
    return SimpleNamespace(
        success=success, execution_id=execution_id, plan=plan, error_summary=error,
        execution_plan=object() if execution_plan else None,
    )


def test_extract_valid_plan_with_execution() -> None:
    pr = _plan_result(
        actions={"s1": {"tool": "list_reminders", "args": {}}},
        compose_template="reminders_list_show",
    )
    el = SimpleNamespace(
        outcome="completed",
        steps=[_Step("s1", "list_reminders", "ok", {"items": 2})],
    )
    args = _build_diag_persist_args(
        pr, el, run_id="run_1", tenant_id="t1", feature_key="hw",
        planner_provider="inception-mercury2",
        planner_prompt_version=3, tool_registry_version="trv-1",
        composer_registry_snapshot_hash="snap-1",
    )
    assert args["planner_status"] == "valid"
    assert args["execution_status"] == "completed"
    assert args["plan_json"] == {"actions": {"s1": {"tool": "list_reminders", "args": {}}}}
    assert args["execution_log"][0]["tool"] == "list_reminders"
    assert args["execution_log"][0]["parsed_output"] == {"items": 2}
    assert args["composer_path"] == "template:reminders_list_show"
    assert args["planner_prompt_version"] == 3
    assert args["tool_registry_version"] == "trv-1"
    assert args["composer_registry_snapshot_hash"] == "snap-1"


def test_step_to_log_serialises_selected_compose() -> None:
    # The CRITICAL bug: a real StepResult carries a Pydantic ComposerCall in
    # selected_compose; asdict() left it un-serialisable. _step_to_log must
    # model_dump it to a plain dict.
    sc = SimpleNamespace(model_dump=lambda mode="json": {"kind": "template", "template_id": "x"})
    step = _Step("s1", "list_menu", "ok", {"ok": True}, selected_compose=sc)
    log = _step_to_log(step)
    assert log["selected_compose"] == {"kind": "template", "template_id": "x"}
    assert log["tool"] == "list_menu" and log["parsed_output"] == {"ok": True}
    # The whole record must be JSON-serialisable (what JSONEncryptedString does).
    import json
    json.dumps(log)


def test_step_to_log_coerces_non_json_parsed_output() -> None:
    # parsed_output is built upstream with model_dump(mode="python") → may carry
    # datetime/Decimal; _step_to_log must coerce so JSONEncryptedString.json.dumps
    # never fails and silently loses the row (subagent R2 MAJOR, reproduced).
    import json
    from datetime import datetime

    step = _Step("s1", "x", "failed", parsed_output={"when": datetime(2026, 6, 17, 9)},
                 error_summary="contract_violation: bad date")
    log = _step_to_log(step)
    json.dumps(log)  # must NOT raise
    assert isinstance(log["parsed_output"]["when"], str)  # coerced to str
    assert log["error_summary"] == "contract_violation: bad date"  # per-step reason kept


def test_extract_valid_requires_execution_plan() -> None:
    # success=True but no execution_plan (invariant violation) → invalid, NOT valid.
    pr = _plan_result(success=True, execution_plan=False, actions={})
    args = _build_diag_persist_args(
        pr, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x"
    )
    assert args["planner_status"] == "invalid"


def test_extract_execute_crash_override() -> None:
    pr = _plan_result(actions={"s1": {"tool": "x", "args": {}}})
    args = _build_diag_persist_args(
        pr, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x",
        execution_status_override="failed",
    )
    assert args["execution_status"] == "failed"
    assert args["execution_log"] == []


def test_extract_composer_path_llm() -> None:
    pr = _plan_result(actions={}, compose_llm="recipe_narrative")
    args = _build_diag_persist_args(
        pr, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x"
    )
    assert args["composer_path"] == "llm:recipe_narrative"


def test_extract_invalid_plan_no_execution() -> None:
    pr = _plan_result(success=False, execution_plan=False, actions=None,
                      error="schema mismatch on s1")
    args = _build_diag_persist_args(
        pr, None, run_id="run_1", tenant_id="t1", feature_key="hw",
        planner_provider="x",
    )
    assert args["planner_status"] == "invalid"
    assert args["plan_json"] is None
    assert args["execution_log"] == []
    assert args["execution_status"] == "pending"
    assert args["validation_errors"] == "schema mismatch on s1"
    assert args["composer_path"] is None


def test_extract_none_when_no_result() -> None:
    assert _build_diag_persist_args(
        None, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x"
    ) is None


def test_extract_none_when_no_execution_id() -> None:
    pr = SimpleNamespace(success=True, execution_id="", plan=None,
                         error_summary=None, execution_plan=object())
    assert _build_diag_persist_args(
        pr, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x"
    ) is None
