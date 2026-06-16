"""#155: planner_chat diagnostic-persistence extraction (pure, synthetic).

Covers ``_build_diag_persist_args`` — the live-path extraction of
``persist_completed_turn`` kwargs from the planner result + execution log —
without a full chat harness. The write→read roundtrip itself is covered by
test_replay_trace_bundle.py (persist_completed_turn ⋈ TraceBundle loader).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sreda.runtime.planner_chat import _build_diag_persist_args


@dataclass
class _Step:
    step_id: str
    tool: str
    status: str
    parsed_output: dict | None = None
    raw_output: str | None = None


def _plan_result(*, success=True, execution_id="pe_1", actions=None, error=None):
    plan = None
    if actions is not None:
        plan = SimpleNamespace(model_dump=lambda mode="json": {"actions": actions})
    return SimpleNamespace(
        success=success, execution_id=execution_id, plan=plan, error_summary=error
    )


def test_extract_valid_plan_with_execution() -> None:
    pr = _plan_result(actions={"s1": {"tool": "list_reminders", "args": {}}})
    el = SimpleNamespace(
        outcome="completed",
        steps=[_Step("s1", "list_reminders", "ok", {"items": 2})],
    )
    args = _build_diag_persist_args(
        pr, el, run_id="run_1", tenant_id="t1", feature_key="hw",
        planner_provider="inception-mercury2",
    )
    assert args["planner_status"] == "valid"
    assert args["execution_status"] == "completed"
    assert args["plan_json"] == {"actions": {"s1": {"tool": "list_reminders", "args": {}}}}
    assert args["execution_log"][0]["tool"] == "list_reminders"
    assert args["execution_log"][0]["parsed_output"] == {"items": 2}
    assert args["execution_id"] == "pe_1"
    assert args["run_id"] == "run_1" and args["tenant_id"] == "t1"


def test_extract_invalid_plan_no_execution() -> None:
    pr = _plan_result(success=False, actions=None, error="schema mismatch on s1")
    args = _build_diag_persist_args(
        pr, None, run_id="run_1", tenant_id="t1", feature_key="hw",
        planner_provider="x",
    )
    assert args["planner_status"] == "invalid"
    assert args["plan_json"] is None
    assert args["execution_log"] == []
    assert args["execution_status"] == "pending"
    assert args["validation_errors"] == "schema mismatch on s1"


def test_extract_none_when_no_result() -> None:
    assert _build_diag_persist_args(
        None, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x"
    ) is None


def test_extract_none_when_no_execution_id() -> None:
    pr = SimpleNamespace(success=True, execution_id="", plan=None, error_summary=None)
    assert _build_diag_persist_args(
        pr, None, run_id="r", tenant_id="t", feature_key="hw", planner_provider="x"
    ) is None
