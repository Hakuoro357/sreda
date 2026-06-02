"""Tests for services/composer/compose.py — Sub-A12 Phase D.1
(R1 A/B fixes applied: ComposeResult, ComposerContext,
terminal_composes plural policy, snapshot hash race, generic_error
for missing template_id).
"""

from __future__ import annotations

from typing import Any

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.runtime.planner.schemas import ComposerCall
from sreda.services.composer.compose import (
    ComposeResult,
    ComposerContext,
    compose,
)
from sreda.services.composer.registry import REGISTRY, ComposerRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(
    step_id: str = "s1",
    tool: str = "add_shopping_items",
    *,
    status: str = "ok",
    parsed_output: dict | None = None,
    selected_compose: ComposerCall | None = None,
    matched_branch_index: int | None = None,
) -> StepResult:
    return StepResult(
        step_id=step_id,
        tool=tool,
        status=status,                      # type: ignore[arg-type]
        parsed_output=parsed_output,
        matched_branch_index=matched_branch_index,
        selected_compose=selected_compose,
    )


def _log(steps: list[StepResult], outcome: str = "completed") -> ExecutionLog:
    return ExecutionLog(steps=tuple(steps), outcome=outcome)  # type: ignore[arg-type]


def _template_call(template_id: str, data: dict | None = None) -> ComposerCall:
    return ComposerCall(
        kind="template",
        template_id=template_id,
        template_data=data or {},
    )


def _llm_call(llm_prompt_key: str, data: dict | None = None) -> ComposerCall:
    return ComposerCall(
        kind="llm",
        llm_prompt_key=llm_prompt_key,
        template_data=data or {},
    )


# ---------------------------------------------------------------------------
# Template path — happy
# ---------------------------------------------------------------------------


def test_compose_renders_template_with_static_data() -> None:
    call = _template_call("shopping_added_ok", {"items": ["молоко", "хлеб"]})
    res = compose(call, _log([_step(parsed_output={"status": "added"})]))
    assert isinstance(res, ComposeResult)
    assert res.text == "Записала: молоко, хлеб."
    assert res.fallback_used is None
    assert res.error_code is None
    assert res.effective_template_id == "shopping_added_ok"
    assert res.effective_llm_prompt_key is None


def test_compose_resolves_refs_in_template_data_from_execution_log() -> None:
    """${s1.items} in template_data resolves to s1's parsed_output['items']."""
    call = _template_call("shopping_added_ok", {"items": "${s1.items}"})
    log = _log([_step(parsed_output={"status": "added", "items": ["сыр", "яйца"]})])
    res = compose(call, log)
    assert res.text == "Записала: сыр, яйца."
    assert res.fallback_used is None


def test_compose_template_with_empty_data_when_template_supports_it() -> None:
    """shopping_list_empty takes no data — should render fine."""
    call = _template_call("shopping_list_empty", {})
    res = compose(call, _log([]))
    assert res.text == "Список покупок пуст."
    assert res.fallback_used is None


# ---------------------------------------------------------------------------
# Multi-override policy (Codex Phase D.1 R1 HIGH #1 convergent)
# ---------------------------------------------------------------------------


def test_single_terminal_override_wins() -> None:
    """Exactly one matched-branch compose → that override wins."""
    branch_compose = _template_call("shopping_list_empty")
    plan_compose = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log([_step(
        parsed_output={"status": "empty"},
        selected_compose=branch_compose,
        matched_branch_index=0,
    )])
    res = compose(plan_compose, log)
    assert res.text == "Список покупок пуст."
    assert res.effective_template_id == "shopping_list_empty"


def test_zero_overrides_falls_back_to_plan_level() -> None:
    """No matched-branch compose → plan-level call is used."""
    log = _log([_step(parsed_output={"status": "added"})])  # no selected_compose
    call = _template_call("shopping_added_ok", {"items": ["хлеб"]})
    res = compose(call, log)
    assert res.text == "Записала: хлеб."
    assert res.effective_template_id == "shopping_added_ok"


def test_terminal_override_ignored_when_outcome_is_partial_failure() -> None:
    """Codex Phase D.1 R2 HIGH (B-side) — outcome-blind override would
    let a successful early step's terminal compose hide a later failure.
    For outcome=='partial_failure', ignore the override; orchestrator
    is expected to swap plan-level call to an explicit fallback template
    before calling compose()."""
    branch_compose = _template_call("shopping_list_empty")  # the override
    plan_compose = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log(
        [
            _step("s1", parsed_output={"status": "empty"},
                  selected_compose=branch_compose,
                  matched_branch_index=0),
            _step("s2", status="error"),
        ],
        outcome="partial_failure",
    )
    res = compose(plan_compose, log)
    # Plan-level call wins (outcome != 'completed' → override ignored)
    assert res.effective_template_id == "shopping_added_ok"


def test_terminal_override_ignored_when_outcome_is_aborted() -> None:
    """Same as above but for outcome='aborted' (unknown_outcome /
    plan_gap / arg_violation in some step)."""
    branch_compose = _template_call("shopping_list_empty")
    plan_compose = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log(
        [_step(parsed_output={"status": "empty"},
               selected_compose=branch_compose,
               matched_branch_index=0)],
        outcome="aborted",
    )
    res = compose(plan_compose, log)
    assert res.effective_template_id == "shopping_added_ok"


def test_terminal_override_ignored_when_outcome_is_aborted_partial() -> None:
    """outcome='aborted_partial' (committed writes before abort) — same
    rule. Orchestrator picks explicit fallback template; compose()
    honors plan-level call."""
    branch_compose = _template_call("shopping_list_empty")
    plan_compose = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log(
        [_step(parsed_output={"status": "empty"},
               selected_compose=branch_compose,
               matched_branch_index=0)],
        outcome="aborted_partial",
    )
    res = compose(plan_compose, log)
    assert res.effective_template_id == "shopping_added_ok"


def test_multiple_terminal_overrides_falls_back_to_plan_level() -> None:
    """Codex Phase D.1 R1 HIGH #1: with 2+ terminal-branch composes
    we have no merge policy yet — use plan-level call (aggregate)."""
    first_branch = _template_call("shopping_list_empty")
    second_branch = _template_call("reminders_list_empty")
    log = _log([
        _step("s1", tool="list_shopping",
              parsed_output={"status": "empty"},
              selected_compose=first_branch,
              matched_branch_index=0),
        _step("s2", tool="list_reminders",
              parsed_output={"status": "empty"},
              selected_compose=second_branch,
              matched_branch_index=0),
    ])
    plan_compose = _template_call("shopping_added_ok", {"items": ["x"]})
    res = compose(plan_compose, log)
    # Plan-level call wins (NOT first override)
    assert res.text == "Записала: x."
    assert res.effective_template_id == "shopping_added_ok"


# ---------------------------------------------------------------------------
# Registry snapshot race protection (Codex Phase D.1 R1 MEDIUM #3)
# ---------------------------------------------------------------------------


def test_snapshot_hash_match_renders_normally() -> None:
    """Phase B-recorded hash matches current → render proceeds."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log([_step(parsed_output={"status": "added"})])
    current_hash = REGISTRY.snapshot_hash()
    res = compose(call, log, expected_registry_snapshot_hash=current_hash)
    assert res.text == "Записала: x."
    assert res.fallback_used is None


def test_snapshot_hash_mismatch_falls_through_to_compose_error() -> None:
    """Phase B hash differs from current → registry deploy race →
    fall through to compose_error with diagnostic."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log([_step(parsed_output={"status": "added"})])
    res = compose(call, log,
                  expected_registry_snapshot_hash="bogus_old_hash_value")
    assert res.fallback_used == "compose_error"
    assert res.error_code == "registry_hash_mismatch"
    assert "С финальным сообщением" in res.text


def test_snapshot_hash_none_skips_check() -> None:
    """expected_registry_snapshot_hash=None (default) — no check."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log([_step(parsed_output={"status": "added"})])
    res = compose(call, log, expected_registry_snapshot_hash=None)
    assert res.fallback_used is None


# ---------------------------------------------------------------------------
# Failure fall-throughs
# ---------------------------------------------------------------------------


def test_unknown_template_id_falls_through_to_compose_error() -> None:
    """template_id not registered (Phase B/D race per Group 6.5) →
    partial_with_compose_error + diagnostic code."""
    call = _template_call("nonexistent_template_xyz", {})
    log = _log([_step(tool="add_shopping_items",
                      parsed_output={"status": "added"})])
    res = compose(call, log)
    assert res.fallback_used == "compose_error"
    assert res.error_code == "unknown_template"
    assert "С финальным сообщением что-то пошло не так" in res.text
    assert "Сделала что просила" in res.text
    assert "add_shopping_items" in res.text
    assert res.effective_template_id == "nonexistent_template_xyz"


def test_template_error_missing_required_var_falls_through() -> None:
    """StrictUndefined raises when template_data omits required field
    → fall through to compose_error."""
    call = _template_call("shopping_added_ok", {})
    log = _log([_step(parsed_output={"status": "added"})])
    res = compose(call, log)
    assert res.fallback_used == "compose_error"
    assert res.error_code is not None
    assert "template_render_error" in res.error_code


def test_ref_resolution_failure_falls_through_to_compose_error() -> None:
    """Ref pointing to non-existent / skipped step → fall through."""
    call = _template_call("shopping_added_ok", {"items": "${s2.items}"})
    log = _log([_step("s1", parsed_output={"status": "added"})])
    res = compose(call, log)
    assert res.fallback_used == "compose_error"
    assert res.error_code is not None
    assert "ref_resolve_error" in res.error_code


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


def test_llm_path_without_composer_falls_through_to_generic_error() -> None:
    """kind='llm' but no llm_composer injected → generic_error 'llm_composer_not_wired'.
    Caller passes explicit ctx (required for kind='llm' production path)."""
    call = _llm_call("recipe_narrative", {"recipe": "борщ"})
    log = _log([_step(parsed_output={"status": "found"})])
    res = compose(call, log, llm_composer=None, ctx=ComposerContext(
        tenant_id="t_test", run_id="r_test"))
    assert res.fallback_used == "generic_error"
    assert res.error_code == "llm_composer_not_wired"
    # issue #88: diagnostic code stays in res.error_code (logs/triage), NOT in user text.
    assert "llm_composer_not_wired" not in res.text
    assert res.text  # clean canned generic message
    assert res.effective_llm_prompt_key == "recipe_narrative"


def test_llm_path_dispatches_to_injected_composer_with_ctx() -> None:
    """Injected llm_composer receives resolved data + execution_log + ctx
    (Codex Phase D.1 R1 MEDIUM #5)."""
    received: dict[str, Any] = {}

    def fake_llm(*, llm_prompt_key: str, template_data: dict,
                 execution_log: ExecutionLog,
                 ctx: ComposerContext) -> str:
        received["prompt_key"] = llm_prompt_key
        received["data"] = template_data
        received["log_steps"] = len(execution_log.steps)
        received["tenant_id"] = ctx.tenant_id
        received["user_message"] = ctx.user_message
        return "RENDERED_BY_LLM"

    call = _llm_call("recipe_narrative", {"recipe": "${s1.title}"})
    log = _log([_step(parsed_output={"status": "found", "title": "борщ"})])
    test_ctx = ComposerContext(
        tenant_id="t_test", run_id="r_test",
        user_message="приготовь борщ",
    )
    res = compose(call, log, llm_composer=fake_llm, ctx=test_ctx)

    assert res.text == "RENDERED_BY_LLM"
    assert res.fallback_used is None
    assert res.effective_llm_prompt_key == "recipe_narrative"
    assert received["prompt_key"] == "recipe_narrative"
    assert received["data"] == {"recipe": "борщ"}
    assert received["log_steps"] == 1
    assert received["tenant_id"] == "t_test"
    assert received["user_message"] == "приготовь борщ"


def test_llm_composer_exception_falls_through_to_generic_error() -> None:
    """LLM call raises → swallow + render generic_tool_error. The diagnostic
    code lives in res.error_code (logs/triage), NOT in the user-facing text
    (issue #88: no internal-detail leak to the user)."""
    def boom(**_: Any) -> str:
        raise RuntimeError("connection refused")

    call = _llm_call("recipe_narrative", {})
    log = _log([_step(parsed_output={"status": "found"})])
    res = compose(call, log, llm_composer=boom,
                  ctx=ComposerContext(tenant_id="t_test", run_id="r_test"))
    assert res.fallback_used == "generic_error"
    assert res.error_code == "llm_composer_error:RuntimeError"
    assert "llm_composer_error:RuntimeError" not in res.text
    assert res.text  # clean canned generic message


def test_llm_path_requires_explicit_ctx_in_production() -> None:
    """Codex Phase D.1 R2 MED convergent A+B: kind='llm' without an
    explicit ctx falls through to generic_error 'llm_context_missing'.

    Prevents Phase E wiring bugs from polluting LLM traces/billing
    with tenant_id='unknown'."""
    def fake_llm(**_: Any) -> str:
        return "should_never_render"

    call = _llm_call("recipe_narrative", {})
    res = compose(call, _log([_step()]), llm_composer=fake_llm)  # ctx=None default
    assert res.fallback_used == "generic_error"
    assert res.error_code == "llm_context_missing"
    assert res.text  # has fallback text from generic_tool_error template


def test_llm_path_accepts_ctx_with_real_tenant_and_run() -> None:
    """When ctx has non-default tenant_id AND run_id, LLM path runs.
    Codex Phase D.1 R3 MED #3 (B): default 'unknown' identities are
    rejected even if a ctx object is provided."""
    def fake_llm(*, ctx: ComposerContext, **_: Any) -> str:
        return f"ctx.tenant={ctx.tenant_id}/run={ctx.run_id}"

    call = _llm_call("p", {})
    res = compose(call, _log([_step()]),
                  llm_composer=fake_llm,
                  ctx=ComposerContext(tenant_id="t_explicit",
                                      run_id="r_explicit"))
    assert res.fallback_used is None
    assert res.text == "ctx.tenant=t_explicit/run=r_explicit"


def test_llm_path_rejects_ctx_with_default_unknown_identity() -> None:
    """Codex Phase D.1 R3 MED #3 (B): even if caller passes
    ComposerContext() object, default tenant_id='unknown' or
    run_id='unknown' is treated as Phase E wiring bug → generic_error
    'llm_context_missing'."""
    def fake_llm(**_: Any) -> str:
        return "should_never_render"

    call = _llm_call("p", {})
    # tenant_id default, run_id default
    res = compose(call, _log([_step()]),
                  llm_composer=fake_llm,
                  ctx=ComposerContext())
    assert res.fallback_used == "generic_error"
    assert res.error_code == "llm_context_missing"

    # Half-default: tenant_id real, run_id still 'unknown'
    res = compose(call, _log([_step()]),
                  llm_composer=fake_llm,
                  ctx=ComposerContext(tenant_id="t_real"))
    assert res.fallback_used == "generic_error"
    assert res.error_code == "llm_context_missing"


def test_template_path_does_not_require_ctx() -> None:
    """kind='template' path never touches ctx — caller can omit safely
    for the common case."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    res = compose(call, _log([_step()]))  # no ctx
    assert res.text == "Записала: x."
    assert res.fallback_used is None


# ---------------------------------------------------------------------------
# step_outputs view
# ---------------------------------------------------------------------------


def test_step_outputs_excludes_skipped_and_failed_steps() -> None:
    """Refs only see successful steps. A ref to a skipped step's
    output → fall through."""
    call = _template_call("shopping_added_ok", {"items": "${s1.items}"})
    log = _log([
        _step("s1", status="error", parsed_output=None),
        _step("s2", parsed_output={"status": "added", "items": ["x"]}),
    ])
    res = compose(call, log)
    assert res.fallback_used == "compose_error"


def test_step_outputs_includes_only_ok_status_steps() -> None:
    """Two refs — to ok and to skipped. Template uses only the ok one
    → renders fine, skipped step is invisible to the ref engine."""
    call = _template_call("shopping_added_ok", {"items": "${s2.items}"})
    log = _log([
        _step("s1", status="skipped"),
        _step("s2", parsed_output={"status": "added", "items": ["a", "b"]}),
    ])
    res = compose(call, log)
    assert res.text == "Записала: a, b."
    assert res.fallback_used is None


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


def test_custom_registry_can_be_injected() -> None:
    """Caller can pass a custom ComposerRegistry — for unit tests
    that don't want to depend on HOUSEWIFE_TEMPLATES."""
    custom = ComposerRegistry()
    custom.register("my_test_template", "Hello {{ name }}!")
    custom.register("partial_with_compose_error", "Stub compose error.")
    custom.register("generic_tool_error", "Stub generic: {{ error_code }}.")

    call = _template_call("my_test_template", {"name": "Boris"})
    res = compose(call, _log([_step()]), registry=custom)
    assert res.text == "Hello Boris!"
    assert res.fallback_used is None


def test_custom_registry_default_is_module_singleton() -> None:
    """No registry param → REGISTRY module-level singleton."""
    call = _template_call("shopping_list_empty", {})
    res = compose(call, _log([]))
    assert res.text == REGISTRY.render("shopping_list_empty", {})


# ---------------------------------------------------------------------------
# Defensive: invalid ComposerCall surfaces
# ---------------------------------------------------------------------------


def test_unsupported_kind_falls_through_to_generic_error() -> None:
    """Defensive: future kind not in {template, llm} → generic_error.
    pydantic Literal should reject at construction; bypass via
    model_construct for coverage."""
    call = ComposerCall.model_construct(
        kind="other_kind", template_id=None,
        template_data={}, llm_prompt_key=None,
    )
    res = compose(call, _log([_step()]))
    assert res.fallback_used == "generic_error"
    assert "unsupported_kind:other_kind" in res.error_code  # type: ignore[operator]


def test_llm_kind_with_none_prompt_key_falls_through_to_generic_error() -> None:
    """Codex Phase D.1 R3 LOW #4 (B): symmetric invalid_llm_call check —
    empty llm_prompt_key with kind='llm' is an INVALID internal
    ComposerCall (pydantic should have rejected). Defense for
    model_construct bypass."""
    call = ComposerCall.model_construct(
        kind="llm", template_id=None,
        template_data={}, llm_prompt_key="",
    )
    res = compose(call, _log([_step()]),
                  llm_composer=lambda **_: "x",
                  ctx=ComposerContext(tenant_id="t", run_id="r"))
    assert res.fallback_used == "generic_error"
    assert res.error_code == "invalid_llm_call"


def test_llm_ctx_check_fires_before_ref_resolution() -> None:
    """Codex Phase D.1 R3 MED #1 (A): a kind='llm' call WITHOUT ctx and
    WITH a bad ref must surface as 'llm_context_missing' (not
    'ref_resolve_error') — otherwise Phase E ctx-wiring bugs hide as
    template/planner failures in telemetry."""
    call = _llm_call("p", {"recipe": "${missing.ref}"})
    log = _log([_step()])  # no s_missing step → ref unresolvable
    res = compose(call, log, llm_composer=lambda **_: "x", ctx=None)
    # ctx check fires FIRST — error_code is llm_context_missing, NOT ref_resolve
    assert res.fallback_used == "generic_error"
    assert res.error_code == "llm_context_missing"


def test_snapshot_hash_mismatch_skipped_for_orchestrator_owned_outcomes() -> None:
    """Codex Phase D.1 R3 MED #2 (B) + R4 MED B refinement: hash check
    skipped for orchestrator-owned fallback outcomes (failed / aborted
    / aborted_partial). Orchestrator passes its own fallback template
    that wasn't Phase B-validated."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    for outcome in ("failed", "aborted", "aborted_partial"):
        log = _log([_step(parsed_output={"status": "added"})], outcome=outcome)
        res = compose(call, log,
                      expected_registry_snapshot_hash="bogus_hash_xyz")
        assert res.fallback_used is None, (
            f"hash check should skip for outcome={outcome!r}, "
            f"got fallback_used={res.fallback_used!r}"
        )
        assert res.text == "Записала: x."


def test_snapshot_hash_check_fires_for_completed_outcome() -> None:
    """Hash check fires on completed outcome (Phase B-validated
    Plan.compose path)."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log([_step(parsed_output={"status": "added"})], outcome="completed")
    res = compose(call, log,
                  expected_registry_snapshot_hash="bogus_hash_xyz")
    assert res.fallback_used == "compose_error"
    assert res.error_code == "registry_hash_mismatch"
    # Codex Phase D.1 R4 medium MED — preserve effective metadata
    assert res.effective_template_id == "shopping_added_ok"


def test_snapshot_hash_check_fires_for_partial_failure_outcome() -> None:
    """Codex Phase D.1 R4 high MED — partial_failure also uses
    Plan.compose (Phase B-validated, planner-owned). Hash mismatch
    must fire there too, not just on completed."""
    call = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log(
        [
            _step("s1", parsed_output={"status": "added"}),
            _step("s2", status="error"),
        ],
        outcome="partial_failure",
    )
    res = compose(call, log,
                  expected_registry_snapshot_hash="bogus_hash_xyz")
    assert res.fallback_used == "compose_error"
    assert res.error_code == "registry_hash_mismatch"
    assert res.effective_template_id == "shopping_added_ok"


def test_template_kind_with_none_template_id_falls_through_to_generic_error() -> None:
    """Codex Phase D.1 R1 LOW #6: missing template_id on kind='template'
    is an INVALID internal ComposerCall, not a registry race. Must
    route to generic_error (not compose_error)."""
    # Bypass pydantic validator to construct invalid call
    call = ComposerCall.model_construct(
        kind="template", template_id=None,
        template_data={}, llm_prompt_key=None,
    )
    res = compose(call, _log([_step()]))
    assert res.fallback_used == "generic_error"
    assert res.error_code == "invalid_template_call"


# ---------------------------------------------------------------------------
# ComposeResult attributes
# ---------------------------------------------------------------------------


def test_compose_result_carries_effective_template_id_on_success() -> None:
    """ComposeResult.effective_template_id = the id actually rendered
    (after override resolution)."""
    branch_compose = _template_call("shopping_list_empty")
    plan_compose = _template_call("shopping_added_ok", {"items": ["x"]})
    log = _log([_step(
        parsed_output={"status": "empty"},
        selected_compose=branch_compose,
        matched_branch_index=0,
    )])
    res = compose(plan_compose, log)
    # Branch override won → effective is the OVERRIDE's template_id
    assert res.effective_template_id == "shopping_list_empty"


def test_compose_result_carries_diagnostic_on_fallback() -> None:
    """When falling through, ComposeResult exposes error_code +
    effective_* so Phase E can record planner_executions.composer_path
    accurately."""
    call = _template_call("nonexistent_xyz", {})
    res = compose(call, _log([_step()]))
    assert res.fallback_used == "compose_error"
    assert res.error_code == "unknown_template"
    assert res.effective_template_id == "nonexistent_xyz"
