"""Tests for runtime/planner/executor.py — Sub-A12 Phase C.

Covers:
- _match_branch matrix (status + multi-field AND + no-match)
- _output_as_dict normalisation (BaseModel / dict / None)
- execute_plan happy path (single step, chain with ref, parallel layer)
- execute_plan timeout per ToolSpec.timeout_seconds
- execute_plan unknown_outcome aborts plan
- execute_plan honest_partial: failed group stops, other groups continue
- execute_plan partial: failure does not stop other steps
- execute_plan empty (clarification mode → no steps)
- ExecutionLog summary outcome string
- ExecutorError when tool missing from registry / tools_by_name

Uses a synthetic ``_FakeTool`` so we don't depend on real LangChain
tooling or real HTTP/DB.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from sreda.runtime.planner.executor import (
    ExecutorError,
    StepResult,
    _match_branch,
    _output_as_dict,
    _summarise_outcome,
    execute_plan,
)
from sreda.runtime.planner.plan_compiler import compile as compile_plan
from sreda.runtime.planner.schemas import OutcomeBranch, Plan
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS


REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}


# ---------------------------------------------------------------------------
# Fake tools + helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal LangChain-tool-shaped object. ``ainvoke`` returns a
    canned raw string per call (must match the tool's real parser
    regex) OR raises a configured exception."""

    def __init__(
        self, name: str, *, return_raw: str = "ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]",
        delay: float = 0.0, raises: Exception | None = None,
    ):
        self.name = name
        self._return_raw = return_raw
        self._delay = delay
        self._raises = raises

    async def ainvoke(self, args: dict) -> str:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._return_raw


def _plan(actions: dict, *, compose: dict | None = None,
          clarity: str = "clear", clarity_reason: str | None = None) -> Plan:
    if compose is None:
        compose = {
            "kind": "template",
            "template_id": "shopping_added_ok",
            "template_data": {"items": ["x"]},
        }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test"},
        "clarity": clarity,
        "actions": actions,
        "compose": compose,
    }
    if clarity_reason is not None:
        payload["clarity_reason"] = clarity_reason
    return Plan.model_validate(payload)


def _action(tool: str, *, args: dict | None = None,
            outcomes: list[dict] | None = None,
            intent_group: str = "default",
            depends_on: list[str] | None = None) -> dict:
    if args is None:
        args = {"items": [{"title": "x"}]} if tool == "add_shopping_items" else {}
    if outcomes is None:
        outcomes = [
            {"match": {"status": "added"}, "next": None,
             "compose": {"kind": "template", "template_id": "shopping_added_ok",
                         "template_data": {"items": ["x"]}}},
        ]
    return {
        "tool": tool,
        "args": args,
        "expected_outcomes": outcomes,
        "intent_group": intent_group,
        "depends_on": depends_on or [],
    }


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _match_branch
# ---------------------------------------------------------------------------


def test_match_branch_single_status_match() -> None:
    branches = [
        OutcomeBranch.model_validate(
            {"match": {"status": "added"}, "next": None}
        ),
    ]
    out = _match_branch(branches, {"status": "added"})
    assert out is not None
    idx, branch = out
    assert idx == 0


def test_match_branch_picks_first_when_multiple() -> None:
    branches = [
        OutcomeBranch.model_validate(
            {"match": {"status": "added"}, "next": None}
        ),
        OutcomeBranch.model_validate(
            {"match": {"status": "error"}, "next": None}
        ),
    ]
    out = _match_branch(branches, {"status": "added"})
    assert out is not None and out[0] == 0


def test_match_branch_multi_field_AND() -> None:
    branches = [
        OutcomeBranch.model_validate(
            {"match": {"status": "added", "added_count": 2}, "next": None}
        ),
    ]
    # Same status but wrong count → no match
    assert _match_branch(branches, {"status": "added", "added_count": 3}) is None
    # Match both
    out = _match_branch(branches, {"status": "added", "added_count": 2})
    assert out is not None


def test_match_branch_no_match_returns_none() -> None:
    branches = [
        OutcomeBranch.model_validate(
            {"match": {"status": "added"}, "next": None}
        ),
    ]
    assert _match_branch(branches, {"status": "error"}) is None


# ---------------------------------------------------------------------------
# _output_as_dict
# ---------------------------------------------------------------------------


def test_output_as_dict_from_basemodel() -> None:
    class _Out(BaseModel):
        status: str = "ok"
        count: int = 3

    assert _output_as_dict(_Out()) == {"status": "ok", "count": 3}


def test_output_as_dict_from_dict_passthrough() -> None:
    assert _output_as_dict({"x": 1}) == {"x": 1}


def test_output_as_dict_from_none() -> None:
    assert _output_as_dict(None) == {}


# ---------------------------------------------------------------------------
# execute_plan — happy paths
# ---------------------------------------------------------------------------


def test_execute_plan_empty_actions_clarification_mode() -> None:
    """Plan with clarity='needs_clarification' + actions={} → execute_plan
    returns ExecutionLog(steps=(), outcome='completed') without running
    any tools."""
    plan = _plan(
        {}, clarity="needs_clarification",
        clarity_reason="no time given",
        compose={"kind": "template", "template_id": "ask_when_to_remind",
                 "template_data": {"what": "x"}},
    )
    ep = compile_plan(plan, REGISTRY)

    log = _run(execute_plan(ep, tools_by_name={}, registry=REGISTRY))
    assert log.steps == ()
    assert log.outcome == "completed"


def test_execute_plan_single_step_happy() -> None:
    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile_plan(plan, REGISTRY)

    fake = _FakeTool("add_shopping_items",
                     return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    log = _run(execute_plan(
        ep, tools_by_name={"add_shopping_items": fake}, registry=REGISTRY,
    ))
    assert len(log.steps) == 1
    assert log.steps[0].status == "ok"
    assert log.steps[0].step_id == "s1"
    assert log.outcome == "completed"


def test_execute_plan_passes_resolved_refs_to_tool() -> None:
    """${s1.item_ids} in s2's args must be resolved before tool call.
    list_shopping uses status=empty path (simpler raw format)."""
    plan = _plan({
        "s1": _action("add_shopping_items"),
        "s2": _action(
            "list_shopping",
            args={},
            outcomes=[{
                "match": {"status": "empty"}, "next": None,
                "compose": {"kind": "template",
                            "template_id": "shopping_list_empty",
                            "template_data": {}},
            }],
        ),
    })
    ep = compile_plan(plan, REGISTRY)

    fake_add = _FakeTool("add_shopping_items",
                         return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    fake_list = _FakeTool("list_shopping",
                          return_raw='no shopping items')
    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add,
        "list_shopping": fake_list,
    }, registry=REGISTRY))
    assert log.outcome == "completed"
    by_id = log.by_step_id()
    assert by_id["s1"].status == "ok"
    assert by_id["s2"].status == "ok"


# ---------------------------------------------------------------------------
# execute_plan — unknown_outcome
# ---------------------------------------------------------------------------


def test_execute_plan_unknown_outcome_aborts() -> None:
    """If tool returns a status not matched by any expected_outcomes,
    executor stops the plan and reports 'aborted'."""
    plan = _plan({
        "s1": _action("add_shopping_items"),
        "s2": _action("list_shopping",
                      args={},
                      outcomes=[{"match": {"status": "ok"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "shopping_list_show",
                                             "template_data": {"items": ["x"]}}}]),
    })
    ep = compile_plan(plan, REGISTRY)

    # add_shopping_items returns status="empty" (count=0 raw 'ok:added:0')
    # but plan only expects "added" → unknown_outcome path.
    fake_add = _FakeTool("add_shopping_items",
                         return_raw='ok:added:0')
    fake_list = _FakeTool("list_shopping", return_raw='no shopping items')
    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add,
        "list_shopping": fake_list,
    }, registry=REGISTRY))

    # Codex Phase C R6 A/B convergent — add_shopping_items is a WRITE
    # tool; unknown_outcome status is in POST_INVOKE_COMMITTED_STATUSES
    # → may-have-committed → aborted_partial (NOT plain aborted).
    assert log.outcome == "aborted_partial"
    by_id = log.by_step_id()
    assert by_id["s1"].status == "unknown_outcome"
    # s2 was skipped due to abort
    assert by_id["s2"].status == "skipped"


# ---------------------------------------------------------------------------
# execute_plan — timeout
# ---------------------------------------------------------------------------


def test_execute_plan_step_timeout_returns_timeout_status() -> None:
    """Write-tool timeout → StepResult(status='timeout') + ESCALATES to
    abort (Codex Phase C R7-high CRITICAL): asyncio.to_thread cannot
    cancel sync tool's thread; write may commit after we record timeout.
    Recovery MUST treat as may-have-committed → aborted_partial outcome."""
    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile_plan(plan, REGISTRY)

    # Tool sleeps for 2s; we'll patch ToolSpec.timeout_seconds=0.05 to force timeout
    fake = _FakeTool("add_shopping_items", delay=2.0)

    # Build a registry copy with shortened timeout
    spec = REGISTRY["add_shopping_items"].model_copy(
        update={"timeout_seconds": 1},
    )
    fast_registry = dict(REGISTRY)
    fast_registry["add_shopping_items"] = spec

    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=fast_registry,
                            timeout_seconds_default=1.0))
    assert log.steps[0].status == "timeout"
    assert "timeout after" in (log.steps[0].error_summary or "")
    # Write timeout escalates to abort + counts as may-have-committed
    # → aborted_partial (NOT failed which would tell recovery "safe to
    # retry without verification").
    assert log.outcome == "aborted_partial"


def test_read_tool_timeout_stays_failed_not_aborted() -> None:
    """Codex Phase C R7-high CRITICAL refinement — read tool timeout
    does NOT escalate to abort (no commit risk for read effect).
    Outcome stays 'failed' (or 'partial_failure' if other steps OK)."""
    plan = _plan({
        "s1": _action("list_shopping", args={},
                      outcomes=[{"match": {"status": "empty"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "shopping_list_empty",
                                             "template_data": {}}}]),
    })
    ep = compile_plan(plan, REGISTRY)
    fake = _FakeTool("list_shopping", delay=2.0)
    spec = REGISTRY["list_shopping"].model_copy(update={"timeout_seconds": 1})
    fast_registry = dict(REGISTRY)
    fast_registry["list_shopping"] = spec

    log = _run(execute_plan(ep, tools_by_name={"list_shopping": fake},
                            registry=fast_registry,
                            timeout_seconds_default=1.0))
    assert log.steps[0].status == "timeout"
    # Read timeout: no commit risk → stays plain 'failed', not aborted
    assert log.outcome == "failed"


# ---------------------------------------------------------------------------
# fail_mode — honest_partial
# ---------------------------------------------------------------------------


def test_execute_plan_honest_partial_stops_group_on_failure() -> None:
    """honest_partial: failed step in intent_group X → skip remaining
    X-steps. Other intent_groups continue normally."""
    # Two intent groups; g1 has 2 steps. First fails (error) → second
    # of g1 should be skipped. g2 still runs.
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g1"),
        "s2": _action("add_shopping_items", intent_group="g1",
                      args={"items": [{"title": "y"}]}),
        "s3": _action(
            "schedule_reminder", intent_group="g2",
            args={"title": "test", "trigger_iso": "2026-12-31T18:00:00+00:00"},
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)
    # Multi-intent_group with disjoint write_domains: g1=shopping, g2=reminders
    # → fail_mode for g1 SHOULD be 'partial' (no overlap, multi group).
    # But within-group failures still need handling — let me check this is
    # an honest_partial scenario by checking the fail_modes result.
    # For honest_partial test we need same group with internal overlap or
    # use a single intent_group setup.

    # SIMPLER setup: single intent_group with two shopping writes
    # (which is honest_partial per Group 2 rule).
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g1"),
        "s2": _action("add_shopping_items", intent_group="g1",
                      args={"items": [{"title": "y"}]}),
        # add g2 so g1 still has "other groups present" — but g1 same-domain
        # overlap still → honest_partial.
        "s3": _action(
            "schedule_reminder", intent_group="g2",
            args={"title": "test", "trigger_iso": "2026-12-31T18:00:00+00:00"},
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)
    # Verify g1 is honest_partial (same domain overlap)
    assert ep.fail_modes["g1"] == "honest_partial"

    # s1 returns error; s2 should be skipped; s3 should run.
    fake_add_fail = _FakeTool(
        "add_shopping_items",
        raises=RuntimeError("db_unavailable"),
    )
    fake_reminder = _FakeTool("schedule_reminder",
                              return_raw='ok:scheduled:rem_aaaaaaaaaaaaaaaaaaaaaaaa:2026-12-31T18:00:00+00:00')

    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add_fail,
        "schedule_reminder": fake_reminder,
    }, registry=REGISTRY))

    by_id = log.by_step_id()
    # s1 failed
    assert by_id["s1"].status == "error"
    # s2 skipped because g1 failed
    assert by_id["s2"].status == "skipped"
    assert "honest_partial" in (by_id["s2"].error_summary or "")
    # s3 ran (different group)
    assert by_id["s3"].status == "ok"

    # Outcome: partial_failure (some ok, some failed)
    assert log.outcome == "partial_failure"


# ---------------------------------------------------------------------------
# fail_mode — partial
# ---------------------------------------------------------------------------


def test_execute_plan_partial_keeps_going_on_failure() -> None:
    """partial: failure of one step doesn't stop the rest. Requires
    different intent_groups + disjoint write_domains per Group 2 rule."""
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g_shop"),
        "s2": _action(
            "schedule_reminder", intent_group="g_rem",
            args={"title": "test",
                  "trigger_iso": "2026-12-31T18:00:00+00:00"},
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)
    assert ep.fail_modes["g_shop"] == "partial"
    assert ep.fail_modes["g_rem"] == "partial"

    fake_add_fail = _FakeTool(
        "add_shopping_items",
        raises=RuntimeError("db_unavailable"),
    )
    fake_reminder = _FakeTool("schedule_reminder",
                              return_raw='ok:scheduled:rem_aaaaaaaaaaaaaaaaaaaaaaaa:2026-12-31T18:00:00+00:00')

    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add_fail,
        "schedule_reminder": fake_reminder,
    }, registry=REGISTRY))

    by_id = log.by_step_id()
    assert by_id["s1"].status == "error"
    # s2 ran successfully despite s1 failure
    assert by_id["s2"].status == "ok"
    assert log.outcome == "partial_failure"


# ---------------------------------------------------------------------------
# Sync tool support
# ---------------------------------------------------------------------------


def test_execute_plan_calls_sync_invoke_when_no_ainvoke() -> None:
    """If tool only exposes sync .invoke, executor wraps it in to_thread."""
    class _SyncTool:
        name = "add_shopping_items"

        def invoke(self, args: dict) -> str:
            return 'ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]'

    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile_plan(plan, REGISTRY)

    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": _SyncTool(),
    }, registry=REGISTRY))
    assert log.steps[0].status == "ok"
    assert log.outcome == "completed"


# ---------------------------------------------------------------------------
# ExecutorError on registry gaps
# ---------------------------------------------------------------------------


def test_execute_plan_raises_on_tool_missing_from_tools_map() -> None:
    """Validator should have caught this — if we get here, caller
    didn't validate. Raise ExecutorError (not silent skip)."""
    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile_plan(plan, REGISTRY)

    with pytest.raises(ExecutorError, match="not in tools_by_name"):
        _run(execute_plan(ep, tools_by_name={}, registry=REGISTRY))


# ---------------------------------------------------------------------------
# Outcome string summary
# ---------------------------------------------------------------------------


def test_outcome_string_completed_when_all_ok() -> None:
    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile_plan(plan, REGISTRY)
    fake = _FakeTool("add_shopping_items", return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=REGISTRY))
    assert log.outcome == "completed"


def test_outcome_string_failed_when_no_success() -> None:
    plan = _plan({"s1": _action("add_shopping_items")})
    ep = compile_plan(plan, REGISTRY)
    fake = _FakeTool("add_shopping_items", raises=RuntimeError("x"))
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=REGISTRY))
    assert log.outcome == "failed"


def test_outcome_aborted_vs_aborted_partial_distinction() -> None:
    """Codex Phase C R3-high + R6 A/B MAJOR — plain ``aborted`` only
    when NO step potentially committed a write before the abort.

    To get plain aborted we use a READ-only tool that hits
    unknown_outcome (read effect means it cannot have committed,
    regardless of post-invoke status).
    """
    plan = _plan({
        "s1": _action(
            "list_shopping", args={},
            outcomes=[{"match": {"status": "ok"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "shopping_list_show",
                                   "template_data": {"items": ["x"]}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)
    # 'no shopping items' → status='empty', expected only 'ok' →
    # unknown_outcome. Read tool → not in committed_writes → plain aborted.
    fake = _FakeTool("list_shopping", return_raw='no shopping items')
    log = _run(execute_plan(ep, tools_by_name={"list_shopping": fake},
                            registry=REGISTRY))
    assert log.outcome == "aborted"  # read-only abort → no committed effect


def test_write_tool_unknown_outcome_classified_aborted_partial() -> None:
    """Codex Phase C R6 A/B convergent MAJOR — close false-negative.

    A write tool can MUTATE THE DB and still produce a status the
    planner didn't list in expected_outcomes → step status becomes
    ``unknown_outcome`` (not ``ok``) and plan aborts. Previously this
    persisted as plain ``aborted``, losing the committed fact and
    risking recovery skipping verification of a real write.

    Now: ``unknown_outcome`` on a write-effect tool counts as
    "may have committed" → outcome ``aborted_partial``.
    """
    plan = _plan({
        "s1": {
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "x"}]},
            "expected_outcomes": [
                # Plan ONLY lists 'error' but tool returns 'added' →
                # unknown_outcome → abort. The tool DID mutate.
                {"match": {"status": "error"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_added_ok",
                             "template_data": {"items": ["x"]}}},
            ],
            "intent_group": "default",
            "depends_on": [],
        },
    })
    ep = compile_plan(plan, REGISTRY)
    fake = _FakeTool(
        "add_shopping_items",
        return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]',
    )
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=REGISTRY))
    s1 = log.by_step_id()["s1"]
    # Tool returned 'added', branch only listed 'error' → no match
    assert s1.status == "unknown_outcome"
    # CRITICAL fix: write-tool unknown_outcome counts as may-have-committed
    # → aborted_partial (NOT plain aborted, which would lose the fact
    # that the row was inserted and risk recovery double-write)
    assert log.outcome == "aborted_partial"


def test_outcome_aborted_partial_requires_committed_write() -> None:
    """Codex Phase C R4-high MAJOR refinement — ``aborted_partial`` is
    side-effect-aware. A READ-ONLY success (list_shopping) before abort
    is NOT a committed side effect → outcome stays plain ``aborted``.
    A WRITE success (add_shopping_items) before abort → ``aborted_partial``.
    """
    # Read-only success + read-only abort → plain aborted.
    # BOTH steps must be read-only — a write tool with unknown_outcome
    # would count as may-have-committed under R6 fix.
    plan_ro = _plan({
        "s1": _action("list_shopping", args={},
                      outcomes=[{"match": {"status": "empty"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "shopping_list_empty",
                                             "template_data": {}}}]),
        "s2": _action(
            "list_reminders", args={},
            depends_on=["s1"],
            # Only 'ok' listed; tool returns 'empty' → unknown_outcome
            outcomes=[{"match": {"status": "ok"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminders_list_show",
                                   "template_data": {"items": []}}}],
        ),
    })
    ep_ro = compile_plan(plan_ro, REGISTRY)
    fake_list = _FakeTool("list_shopping", return_raw='no shopping items')
    fake_rem = _FakeTool("list_reminders", return_raw='no active reminders')
    log_ro = _run(execute_plan(ep_ro, tools_by_name={
        "list_shopping": fake_list,
        "list_reminders": fake_rem,
    }, registry=REGISTRY))
    # s1 ok (read-only), s2 unknown_outcome (read-only) → aborted
    # (read-only steps cannot have committed)
    assert log_ro.outcome == "aborted"

    # Write success before abort → aborted_partial
    plan_w = _plan({
        "s1": _action("add_shopping_items",
                      args={"items": [{"title": "x"}]}),
        "s2": _action("add_shopping_items",
                      args={"items": [{"title": "y"}]},
                      depends_on=["s1"]),
    })
    ep_w = compile_plan(plan_w, REGISTRY)
    # Force s1 to return ok (committed write), s2 to return unknown
    # via a counter-tool that switches output on call count.
    class _CountTool:
        name = "add_shopping_items"
        def __init__(self):
            self.calls = 0
        async def ainvoke(self, args):
            self.calls += 1
            if self.calls == 1:
                return 'ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]'
            return 'ok:added:0'  # empty → unknown_outcome
    fake_counter = _CountTool()
    log_w = _run(execute_plan(ep_w, tools_by_name={
        "add_shopping_items": fake_counter,
    }, registry=REGISTRY))
    by_id = log_w.by_step_id()
    assert by_id["s1"].status == "ok"
    assert by_id["s2"].status == "unknown_outcome"
    # s1 committed a WRITE before s2 abort → aborted_partial
    assert log_w.outcome == "aborted_partial"


# ---------------------------------------------------------------------------
# Codex Phase C R1 CRITICAL #1 — branch-driven execution
# ---------------------------------------------------------------------------


def test_branch_next_selects_only_matched_path() -> None:
    """s1 has two branches: added → s2 (was s2_added); (fallback) → s3 (was s2_error).
    s1 returns status='added' → s2 MUST run, s3 MUST be
    skipped with reason 'branch_not_selected'."""
    plan = _plan({
        "s1": {
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "x"}]},
            "expected_outcomes": [
                {"match": {"status": "added"}, "next": "s2"},   # was s2_added: the 'added' branch
                {"match": {"status": "empty"}, "next": "s3"},   # was s2_error: the 'error' branch
            ],
            "intent_group": "g_path",
            "depends_on": [],
        },
        "s2": _action(  # was s2_added: the 'added' branch
            "schedule_reminder", intent_group="g_path",
            args={"title": "ok", "trigger_iso": "2026-12-31T18:00:00+00:00"},
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
        "s3": _action(  # was s2_error: the 'error' branch
            "schedule_reminder", intent_group="g_path",
            args={"title": "err", "trigger_iso": "2026-12-31T19:00:00+00:00"},
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)

    fake_add = _FakeTool("add_shopping_items",
                         return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    fake_rem = _FakeTool("schedule_reminder",
                         return_raw='ok:scheduled:rem_aaaaaaaaaaaaaaaaaaaaaaaa:2026-12-31T18:00:00+00:00')

    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add,
        "schedule_reminder": fake_rem,
    }, registry=REGISTRY))

    by_id = log.by_step_id()
    assert by_id["s1"].status == "ok"
    assert by_id["s2"].status == "ok"    # was s2_added
    assert by_id["s3"].status == "skipped"  # was s2_error
    assert by_id["s3"].error_summary == "branch_not_selected"


def test_terminal_branch_compose_override_surfaces_via_step_result() -> None:
    """If matched branch has its own compose, it's exposed via
    StepResult.selected_compose for the composer to honour over the
    plan-level compose."""
    plan = _plan({
        "s1": {
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "x"}]},
            "expected_outcomes": [
                {"match": {"status": "added"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_added_ok",
                             "template_data": {"items": ["x"]}}},
            ],
            "intent_group": "default",
            "depends_on": [],
        },
    })
    ep = compile_plan(plan, REGISTRY)

    fake = _FakeTool("add_shopping_items",
                     return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=REGISTRY))
    s1 = log.by_step_id()["s1"]
    assert s1.selected_compose is not None
    assert s1.selected_compose.template_id == "shopping_added_ok"
    # ExecutionLog.terminal_compose() returns it for composer
    assert log.terminal_compose() is not None
    assert log.terminal_compose().template_id == "shopping_added_ok"


# ---------------------------------------------------------------------------
# Codex Phase C R1 CRITICAL #3 — plan_gap from process_output sentinel
# ---------------------------------------------------------------------------


def test_contract_violation_becomes_plan_gap_and_aborts() -> None:
    """add_shopping_items returning a string the parser can't handle
    (e.g. raw with mismatched ids vs count) → ToolOutputContractViolation
    → process_output raises PlannerGapError → step status='plan_gap'
    → plan aborts."""
    plan = _plan({
        "s1": _action("add_shopping_items"),
        "s2": _action("add_shopping_items",
                      args={"items": [{"title": "y"}]}),
    })
    ep = compile_plan(plan, REGISTRY)
    # Raw 'ok:added:0:ids=[sh_...]' is contract-violation per
    # housewife.py:280 (count=0 with non-empty ids).
    fake = _FakeTool(
        "add_shopping_items",
        return_raw='ok:added:0:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]',
    )
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=REGISTRY))
    by_id = log.by_step_id()
    assert by_id["s1"].status == "plan_gap"
    assert "contract_violation" in (by_id["s1"].error_summary or "")
    # s2 should be skipped because plan aborted
    assert by_id["s2"].status == "skipped"
    # Codex Phase C R6 A/B convergent — write-tool with plan_gap status
    # is in POST_INVOKE_COMMITTED_STATUSES (parser raised AFTER tool
    # invoke; tool may have committed before parse failed) → aborted_partial
    assert log.outcome == "aborted_partial"


# ---------------------------------------------------------------------------
# Codex Phase C R1 CRITICAL #2 — execute-time arg validation
# ---------------------------------------------------------------------------


def test_arg_violation_aborts_plan() -> None:
    """If validate_args_at_execute_time raises ValidationError on
    resolved args (e.g. missing required field), executor records
    status='arg_violation' and aborts."""
    # update_shopping_item is a known refs-aware tool — passing it
    # all-None refs trips its no-op rejector. Use it to force a
    # ValidationError on validated args.
    plan = _plan(
        actions={
            "s1": {
                "tool": "update_shopping_item",
                # No-op: only item_id, no title/category/quantity update fields
                # — the model rejects "no-op update".
                "args": {"item_id": "sh_aaaaaaaaaaaaaaaaaaaaaaaa"},
                "expected_outcomes": [
                    {"match": {"status": "updated"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "shopping_updated_ok",
                                 "template_data": {"item_title": "x"}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        compose={"kind": "template",
                 "template_id": "shopping_updated_ok",
                 "template_data": {"item_title": "x"}},
    )
    ep = compile_plan(plan, REGISTRY)
    fake = _FakeTool("update_shopping_item", return_raw='ok:updated:sh_aaaaaaaaaaaaaaaaaaaaaaaa')
    log = _run(execute_plan(ep, tools_by_name={"update_shopping_item": fake},
                            registry=REGISTRY))
    s1 = log.by_step_id()["s1"]
    assert s1.status == "arg_violation"
    assert "execute_time_arg_violation" in (s1.error_summary or "")
    assert log.outcome == "aborted"


# ---------------------------------------------------------------------------
# Codex Phase C R1 MAJOR #5 — same-group honest_partial sequenced in layer
# ---------------------------------------------------------------------------


def test_same_group_honest_partial_sequenced_within_layer() -> None:
    """Two same-intent_group honest_partial steps in the same compiled
    layer: if first fails, second MUST be skipped (not also fired in
    parallel via gather).

    Setup: two add_shopping_items steps in intent_group='g1'. They write
    to same domain ('shopping') so the compiler already places them in
    SEPARATE sub-layers via parallel-safety — but if any future change
    lets two same-group steps into one layer, the executor's batching
    must still sequence them. We assert by checking that on s1 failure,
    s2 is skipped with reason 'honest_partial_group_failed'."""
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g1"),
        "s2": _action("add_shopping_items", intent_group="g1",
                      args={"items": [{"title": "y"}]}),
    })
    ep = compile_plan(plan, REGISTRY)
    assert ep.fail_modes["g1"] == "honest_partial"

    fake_fail = _FakeTool("add_shopping_items",
                          raises=RuntimeError("db_unavailable"))
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake_fail},
                            registry=REGISTRY))
    by_id = log.by_step_id()
    assert by_id["s1"].status == "error"
    assert by_id["s2"].status == "skipped"
    assert "honest_partial" in (by_id["s2"].error_summary or "")


# ---------------------------------------------------------------------------
# Codex Phase C R1 MAJOR #8 — catch-all branch enforced as last
# ---------------------------------------------------------------------------


def test_match_branch_misplaced_catchall_does_not_swallow() -> None:
    """A catch-all branch (empty match) at a non-final index must NOT
    fire as a wildcard — defensive guard against planner misformat."""
    from sreda.runtime.planner.schemas import OutcomeBranch
    branches = [
        OutcomeBranch.model_validate({"match": {}, "next": "wrong"}),
        OutcomeBranch.model_validate({"match": {"status": "added"}, "next": "right"}),
    ]
    # 'added' should still match the SECOND (correctly-fielded) branch,
    # not the misplaced catch-all at index 0.
    out = _match_branch(branches, {"status": "added"})
    assert out is not None
    assert out[0] == 1


# ---------------------------------------------------------------------------
# Codex Phase C R1 MINOR #9 — step_outputs snapshot per layer
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex Phase C R2 MAJOR — abort preserves same-batch siblings in audit log
# ---------------------------------------------------------------------------


def test_abort_in_batch_preserves_sibling_results_and_skips_later_layer() -> None:
    """When step S1 in a batch returns unknown_outcome, sibling S2 in
    the SAME batch is already committed by gather — its StepResult MUST
    appear in the log. Later-layer step S3 MUST be skipped with reason
    'halted_unknown_outcome'.

    Uses two read-only tools (read/read = always parallel-safe per
    can_run_parallel) so they land in the SAME batch."""
    plan = _plan({
        # Both reads (parallel-safe) → same compiled layer + batch.
        "s1": _action("list_shopping", intent_group="g_a", args={},
                      outcomes=[{"match": {"status": "ok"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "shopping_list_show",
                                             "template_data": {"items": ["x"]}}}]),
        "s2": _action("list_reminders", intent_group="g_b", args={},
                      outcomes=[{"match": {"status": "empty"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "reminders_list_empty",
                                             "template_data": {}}}]),
        # s3 in later layer, depends_on s2 → only runs after layer 0 done.
        "s3": _action("list_shopping", intent_group="g_a", args={},
                      depends_on=["s2"],
                      outcomes=[{"match": {"status": "empty"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "shopping_list_empty",
                                             "template_data": {}}}]),
    })
    ep = compile_plan(plan, REGISTRY)
    # Sanity: s1 + s2 should share a layer/batch (both read).
    assert "s1" in ep.layers[0] and "s2" in ep.layers[0], (
        f"expected s1+s2 in same layer, got layers={ep.layers}"
    )

    # s1: raw status='empty' but expected_outcomes only lists 'ok'
    # → unknown_outcome triggering abort
    fake_shop = _FakeTool("list_shopping", return_raw='no shopping items')
    fake_rem = _FakeTool("list_reminders", return_raw='no active reminders')
    log = _run(execute_plan(ep, tools_by_name={
        "list_shopping": fake_shop,
        "list_reminders": fake_rem,
    }, registry=REGISTRY))

    by_id = log.by_step_id()
    # s1 aborted with unknown_outcome
    assert by_id["s1"].status == "unknown_outcome"
    # s2 was concurrent in same batch — its result MUST still be in log
    # (gather already ran it; the executor must not drop it).
    assert "s2" in by_id
    assert by_id["s2"].status == "ok"
    # s3 in later layer → skipped because plan aborted
    assert by_id["s3"].status == "skipped"
    assert "halted" in (by_id["s3"].error_summary or "")
    # Codex Phase C R4-high MAJOR refinement: s1+s2 are list_shopping +
    # list_reminders (both READ-only). Read-only successes are NOT
    # committed side effects → plain ``aborted``, not ``aborted_partial``.
    # (See _summarise_outcome side-effect-aware semantics.)
    assert log.outcome == "aborted"


# ---------------------------------------------------------------------------
# Codex Phase C R1-high CRITICAL — cascade skip on upstream skipped
# ---------------------------------------------------------------------------


def test_step_with_skipped_upstream_dep_is_cascade_skipped() -> None:
    """If s1 has branch.next=s2 (no fallback) and s1 returns 'empty' →
    no branch matches → s1 unknown_outcome → aborts.

    Make s3 depend on s2 via depends_on. Since s2 is branch_not_selected
    (downstream of unreached branch path), s3 should be cascade-skipped
    with reason 'upstream_skipped:s2', NOT fail with ref_resolve_error.

    Simpler version: s1 root with terminal branch (next=None). s2 has
    depends_on=[s_other] where s_other was skipped because its enable
    didn't fire. We construct s_other as a branch.next-target that
    never gets enabled.
    """
    plan = _plan({
        # s1: root, only matches 'added' → 'unmatched'_path step
        "s1": {
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "x"}]},
            "expected_outcomes": [
                # Only matches 'added'; next='s2' (was s_unreached).
                {"match": {"status": "added"}, "next": "s2"},  # was s_unreached
            ],
            "intent_group": "default",
            "depends_on": [],
        },
        "s2": {  # was s_unreached
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "y"}]},
            "expected_outcomes": [
                {"match": {"status": "added"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_added_ok",
                             "template_data": {"items": ["y"]}}},
            ],
            "intent_group": "default",
            "depends_on": [],
        },
        # s3 (was s_downstream) uses depends_on=s2 → should cascade skip
        "s3": {  # was s_downstream
            "tool": "list_shopping",
            "args": {},
            "expected_outcomes": [
                {"match": {"status": "empty"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_list_empty",
                             "template_data": {}}},
            ],
            "intent_group": "default",
            "depends_on": ["s2"],  # was s_unreached
        },
    })
    ep = compile_plan(plan, REGISTRY)

    # s1 returns 'empty' (count=0) → status='empty' but branches only
    # have 'added' → unknown_outcome → aborts. But our test target
    # is whether s3 (was s_downstream) gets cascade-skipped via upstream rule.
    # Use raw='ok:added:1:...' so s1 succeeds + enables s2 (was s_unreached).
    # Then s2 returns empty → unknown_outcome → abort.
    # In that case s3 would also be drained as halted, not
    # cascade-skipped. So instead make s1 succeed with a status NOT
    # matching → unknown_outcome on s1 directly → s2 and
    # s3 both get drained as halted.
    # For cascade test specifically, we need s2 to be
    # branch_not_selected (not aborted). Setup s1 with two branches:
    # 'added' → enable nothing; 'empty' → enable s2 (was s_unreached).
    plan = _plan({
        "s1": {
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "x"}]},
            "expected_outcomes": [
                {"match": {"status": "added"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_added_ok",
                             "template_data": {"items": ["x"]}}},
                {"match": {"status": "empty"}, "next": "s2"},  # was s_unreached
            ],
            "intent_group": "default",
            "depends_on": [],
        },
        "s2": {  # was s_unreached
            "tool": "add_shopping_items",
            "args": {"items": [{"title": "y"}]},
            "expected_outcomes": [
                {"match": {"status": "added"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_added_ok",
                             "template_data": {"items": ["y"]}}},
            ],
            "intent_group": "default",
            "depends_on": [],
        },
        "s3": {  # was s_downstream
            "tool": "list_shopping",
            "args": {},
            "expected_outcomes": [
                {"match": {"status": "empty"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_list_empty",
                             "template_data": {}}},
            ],
            "intent_group": "default",
            "depends_on": ["s2"],  # was s_unreached
        },
    })
    ep = compile_plan(plan, REGISTRY)

    # s1 returns 'added' → matches first branch → s2 (was s_unreached) NOT enabled
    # → s3 (was s_downstream) depends_on s2 → cascade skip
    fake_add = _FakeTool("add_shopping_items",
                         return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    fake_list = _FakeTool("list_shopping", return_raw='no shopping items')
    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add,
        "list_shopping": fake_list,
    }, registry=REGISTRY))

    by_id = log.by_step_id()
    assert by_id["s1"].status == "ok"
    assert by_id["s2"].status == "skipped"               # was s_unreached
    assert by_id["s2"].error_summary == "branch_not_selected"
    assert by_id["s3"].status == "skipped"               # was s_downstream
    # Cascade reason mentions s2 (was s_unreached) as the missing upstream
    assert "upstream_skipped" in (by_id["s3"].error_summary or "")
    assert "s2" in (by_id["s3"].error_summary or "")     # was "s_unreached"


# ---------------------------------------------------------------------------
# Codex Phase C R2 CRITICAL — cascade includes failed (error/timeout), not just skipped
# ---------------------------------------------------------------------------


def test_step_with_failed_upstream_dep_is_cascade_skipped() -> None:
    """If s1 fails (error/timeout) under PARTIAL fail_mode, s2 with
    depends_on=[s1] or ${s1.x} ref MUST cascade-skip with reason
    'upstream_unavailable:error:s1' — not run on a broken precondition
    (depends_on) or hit misleading arg_violation (refs)."""
    plan = _plan({
        "s1": _action("add_shopping_items", intent_group="g_shop",
                      args={"items": [{"title": "x"}]}),
        "s2": _action(
            "schedule_reminder",
            intent_group="g_rem",  # different group → partial fail_mode
            args={"title": "ok", "trigger_iso": "2026-12-31T18:00:00+00:00"},
            depends_on=["s1"],
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)
    # Confirm partial (different intent groups + disjoint write domains)
    assert ep.fail_modes["g_shop"] == "partial"
    assert ep.fail_modes["g_rem"] == "partial"

    fake_add_fail = _FakeTool(
        "add_shopping_items",
        raises=RuntimeError("db_unavailable"),
    )
    fake_rem = _FakeTool(
        "schedule_reminder",
        return_raw='ok:scheduled:rem_aaaaaaaaaaaaaaaaaaaaaaaa:2026-12-31T18:00:00+00:00',
    )
    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add_fail,
        "schedule_reminder": fake_rem,
    }, registry=REGISTRY))

    by_id = log.by_step_id()
    assert by_id["s1"].status == "error"
    # s2 MUST cascade-skip via failed_step_ids, NOT run on broken
    # depends_on contract
    assert by_id["s2"].status == "skipped"
    assert "upstream_unavailable" in (by_id["s2"].error_summary or "")
    assert "s1" in (by_id["s2"].error_summary or "")
    assert "error" in (by_id["s2"].error_summary or "")


# ---------------------------------------------------------------------------
# Codex Phase C R1-high MAJOR #2 — abort drains later batches same layer
# ---------------------------------------------------------------------------


def test_abort_drains_later_batches_in_same_layer() -> None:
    """When abort triggers in batch 1 of a multi-batch layer, batch 2's
    steps MUST be drained as halted skips, not silently dropped.

    Construct layer with two same-group honest_partial steps that
    compiler/executor split into separate batches. Step in batch 1 hits
    unknown_outcome; step in batch 2 should appear as halted skip.
    """
    plan = _plan({
        # Two add_shopping_items in same intent_group → split into
        # separate batches by _build_layer_batches (honest_partial).
        "s1": _action("add_shopping_items", intent_group="g1"),
        "s2": _action("add_shopping_items", intent_group="g1",
                      args={"items": [{"title": "y"}]}),
    })
    ep = compile_plan(plan, REGISTRY)

    # s1 returns count=0 → status='empty' → no matching branch (only 'added')
    # → unknown_outcome → abort. s2 in next batch should be drained.
    fake = _FakeTool("add_shopping_items", return_raw='ok:added:0')
    log = _run(execute_plan(ep, tools_by_name={"add_shopping_items": fake},
                            registry=REGISTRY))

    by_id = log.by_step_id()
    assert by_id["s1"].status == "unknown_outcome"
    assert by_id["s2"].status == "skipped"
    assert "halted_unknown_outcome" in (by_id["s2"].error_summary or "")
    # Codex Phase C R6 A/B convergent — s1 is add_shopping_items (write)
    # with unknown_outcome status → may have committed → aborted_partial
    assert log.outcome == "aborted_partial"


# ---------------------------------------------------------------------------
# Codex Phase C R1-high MAJOR #3 — ref_resolve → arg_violation aborts
# ---------------------------------------------------------------------------


def test_unresolvable_ref_becomes_arg_violation_not_error() -> None:
    """If ${s1.missing_field} fails to resolve at execute time, this is
    a contract failure (planner/runtime bug), not a tool error. Must
    classify as arg_violation → halt the plan."""
    plan = _plan({
        "s1": _action("add_shopping_items"),
        "s2": _action(
            "schedule_reminder",
            args={"title": "${s1.nonexistent_field}",
                  "trigger_iso": "2026-12-31T18:00:00+00:00"},
            outcomes=[{"match": {"status": "scheduled"}, "next": None,
                       "compose": {"kind": "template",
                                   "template_id": "reminder_set_ok",
                                   "template_data": {"what": "x",
                                                     "when_phrase": "y"}}}],
        ),
    })
    ep = compile_plan(plan, REGISTRY)
    fake_add = _FakeTool("add_shopping_items",
                         return_raw='ok:added:1:ids=[sh_aaaaaaaaaaaaaaaaaaaaaaaa]')
    fake_rem = _FakeTool(
        "schedule_reminder",
        return_raw='ok:scheduled:rem_aaaaaaaaaaaaaaaaaaaaaaaa:2026-12-31T18:00:00+00:00',
    )
    log = _run(execute_plan(ep, tools_by_name={
        "add_shopping_items": fake_add,
        "schedule_reminder": fake_rem,
    }, registry=REGISTRY))
    by_id = log.by_step_id()
    assert by_id["s1"].status == "ok"
    assert by_id["s2"].status == "arg_violation"
    assert "ref_resolve_error" in (by_id["s2"].error_summary or "")
    # Codex Phase C R3-high MAJOR: s1 committed before s2 abort → aborted_partial
    assert log.outcome == "aborted_partial"


# ---------------------------------------------------------------------------
# Codex Phase C R1-high MAJOR #4 — terminal_composes() returns all matches
# ---------------------------------------------------------------------------


def test_terminal_composes_returns_all_branch_composes_in_order() -> None:
    """When multiple steps match branches with their own compose, all
    are exposed via ExecutionLog.terminal_composes() so Phase D can
    apply its own policy (deepest reachable, error priority, etc.)."""
    plan = _plan({
        "s1": {
            "tool": "list_shopping",
            "args": {},
            "expected_outcomes": [
                {"match": {"status": "empty"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "shopping_list_empty",
                             "template_data": {}}},
            ],
            "intent_group": "g_a",
            "depends_on": [],
        },
        "s2": {
            "tool": "list_reminders",
            "args": {},
            "expected_outcomes": [
                {"match": {"status": "empty"}, "next": None,
                 "compose": {"kind": "template",
                             "template_id": "reminders_list_empty",
                             "template_data": {}}},
            ],
            "intent_group": "g_b",
            "depends_on": [],
        },
    })
    ep = compile_plan(plan, REGISTRY)
    fake_s = _FakeTool("list_shopping", return_raw='no shopping items')
    fake_r = _FakeTool("list_reminders", return_raw='no active reminders')
    log = _run(execute_plan(ep, tools_by_name={
        "list_shopping": fake_s,
        "list_reminders": fake_r,
    }, registry=REGISTRY))

    composes = log.terminal_composes()
    # Both steps matched a branch with its own compose
    assert len(composes) == 2
    ids = [c[0] for c in composes]
    templates = [c[1].template_id for c in composes]
    assert set(ids) == {"s1", "s2"}
    assert "shopping_list_empty" in templates
    assert "reminders_list_empty" in templates


def test_same_layer_steps_see_only_pre_layer_outputs() -> None:
    """Two independent root-steps in the same layer must NOT see each
    other's outputs via ref resolution — only outputs from EARLIER
    layers are visible (snapshot semantics).

    This is enforced by ``snapshot = dict(state.step_outputs)`` taken
    at layer entry; we test the contract indirectly by ensuring two
    same-layer steps both succeed without ref dependency on each other.
    """
    plan = _plan({
        # Both roots, different intent_groups → can parallel.
        "s1": _action("list_shopping", intent_group="g_a", args={},
                      outcomes=[{"match": {"status": "empty"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "shopping_list_empty",
                                             "template_data": {}}}]),
        "s2": _action("list_reminders", intent_group="g_b", args={},
                      outcomes=[{"match": {"status": "empty"}, "next": None,
                                 "compose": {"kind": "template",
                                             "template_id": "reminders_list_empty",
                                             "template_data": {}}}]),
    })
    ep = compile_plan(plan, REGISTRY)
    fake_s = _FakeTool("list_shopping", return_raw='no shopping items')
    fake_r = _FakeTool("list_reminders", return_raw='no active reminders')
    log = _run(execute_plan(ep, tools_by_name={
        "list_shopping": fake_s,
        "list_reminders": fake_r,
    }, registry=REGISTRY))
    by_id = log.by_step_id()
    assert by_id["s1"].status == "ok"
    assert by_id["s2"].status == "ok"
    assert log.outcome == "completed"


# ---------------------------------------------------------------------------
# #29 — committed_statuses: precise aborted vs aborted_partial. A write tool
# whose matched 'ok' status is an idempotent no-op (add_shopping_items→empty,
# link_task_to_checklist→already_linked, ...) did NOT commit, so an abort
# after it stays plain `aborted`, not `aborted_partial`. Tests target
# _summarise_outcome directly with the real registry (now annotated).
# ---------------------------------------------------------------------------


def _sr(
    tool: str,
    status: str,
    parsed_status: str | None = None,
    *,
    matched_status: str | None = None,
) -> StepResult:
    """Build a StepResult. ``parsed_status`` populates parsed_output['status']
    (the REAL tool status the classifier reads); ``matched_status`` is the
    branch-match status (deliberately independent — a catch-all/non-status
    branch leaves it None even for a real write)."""
    return StepResult(
        step_id="s1",
        tool=tool,
        status=status,
        parsed_output={"status": parsed_status} if parsed_status else None,
        matched_status=matched_status,
    )


def test_29_no_op_write_status_stays_aborted() -> None:
    """add_shopping_items parsed 'empty' (count=0) → not in
    committed_statuses={'added'} → no committed write → plain aborted."""
    results = [_sr("add_shopping_items", "ok", "empty")]
    assert _summarise_outcome(results, aborted=True, registry=REGISTRY) == "aborted"


def test_29_real_write_status_is_aborted_partial() -> None:
    """add_shopping_items parsed 'added' ∈ committed_statuses → committed
    write before abort → aborted_partial."""
    results = [_sr("add_shopping_items", "ok", "added")]
    assert (
        _summarise_outcome(results, aborted=True, registry=REGISTRY)
        == "aborted_partial"
    )


def test_29_catch_all_branch_real_write_still_committed() -> None:
    """Codex #29 R1 high MAJOR: a real write matched via a catch-all ({}) or
    non-status branch has matched_status=None, but parsed_output.status is
    'added'. The classifier MUST key on parsed status, not matched_status —
    else a real committed write is under-reported as plain aborted."""
    results = [_sr("add_shopping_items", "ok", "added", matched_status=None)]
    assert (
        _summarise_outcome(results, aborted=True, registry=REGISTRY)
        == "aborted_partial"
    )


def test_29_unreadable_parsed_status_fails_closed() -> None:
    """ok write step with no parsed status → fail closed (may have
    committed) → aborted_partial."""
    results = [_sr("add_shopping_items", "ok", None)]
    assert (
        _summarise_outcome(results, aborted=True, registry=REGISTRY)
        == "aborted_partial"
    )


def test_29_link_already_linked_noop_stays_aborted() -> None:
    results = [_sr("link_task_to_checklist", "ok", "already_linked")]
    assert _summarise_outcome(results, aborted=True, registry=REGISTRY) == "aborted"


def test_29_unlink_not_linked_noop_stays_aborted() -> None:
    results = [_sr("unlink_task", "ok", "not_linked")]
    assert _summarise_outcome(results, aborted=True, registry=REGISTRY) == "aborted"


def test_29_schedule_reminder_skipped_past_stays_aborted() -> None:
    results = [_sr("schedule_reminder", "ok", "skipped_past")]
    assert _summarise_outcome(results, aborted=True, registry=REGISTRY) == "aborted"


def test_29_save_recipe_duplicate_noop_stays_aborted() -> None:
    """save_recipe parsed 'duplicate' (recipe already exists) inserts
    nothing → not in committed_statuses={'saved'} → plain aborted."""
    results = [_sr("save_recipe", "ok", "duplicate")]
    assert _summarise_outcome(results, aborted=True, registry=REGISTRY) == "aborted"


def test_29_unannotated_write_over_approximates() -> None:
    """mark_shopping_bought has no committed_statuses (None) → safe
    over-approximation preserved: any 'ok' write → aborted_partial."""
    results = [_sr("mark_shopping_bought", "ok", "bought")]
    assert (
        _summarise_outcome(results, aborted=True, registry=REGISTRY)
        == "aborted_partial"
    )


def test_29_unknown_outcome_on_annotated_write_still_committed() -> None:
    """R6 preserved: committed_statuses refines ONLY the 'ok' case. An
    annotated write tool with status='unknown_outcome' stays fail-closed
    may-have-committed → aborted_partial (even though parsed status, if
    any, is irrelevant for non-'ok' statuses)."""
    results = [_sr("add_shopping_items", "unknown_outcome", "added")]
    assert (
        _summarise_outcome(results, aborted=True, registry=REGISTRY)
        == "aborted_partial"
    )
