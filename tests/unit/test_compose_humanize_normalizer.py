"""Unit + integration tests for the humanize_result Layer-2 normalizer (#110 Phase 3).

The normalizer (``_normalize_humanize_result`` in ``compose.py``) intercepts the
NEW ``actions:[{step_id}]`` narration form and builds the strict internal form
``{intent, actions:[{user_visible_summary, status}]}`` in code via the presenter,
branching strictly by ``StepResult.status``. Every other (current) form falls
through to the unchanged resolve_refs path → zero regression.

These tests drive the production shapes: real ``ComposerCall`` + ``ExecutionLog``
+ ``StepResult``. Direct tests exercise the branching; ``compose()`` integration
tests prove the wiring (normalized → LLM with SAFE data; short-circuit → no LLM;
fall-through → original data → LLM).
"""

from __future__ import annotations

import pytest

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.runtime.planner.schemas import ComposerCall
from sreda.services.composer import presenters
from sreda.services.composer.compose import (
    HUMANIZE_NORMALIZER_METRICS,
    ComposerContext,
    _is_step_id_action,
    _normalize_humanize_result,
    compose,
)
from sreda.services.composer.presenters import PRESENTER_FALLBACK_COUNTS


@pytest.fixture(autouse=True)
def _reset_metrics_and_map():
    HUMANIZE_NORMALIZER_METRICS.clear()
    PRESENTER_FALLBACK_COUNTS.clear()
    # Hermetic: empty display map → only the overrides (web_search/recall_memory)
    # resolve; any other tool denies-by-default. Map projection itself is covered
    # in test_presenters.
    presenters.set_display_field_map({})
    # Value-based status allowlist (R2 MAJOR): only schema-declared statuses pass.
    presenters.set_valid_status_map({"web_search": frozenset({"results"})})
    yield
    HUMANIZE_NORMALIZER_METRICS.clear()
    PRESENTER_FALLBACK_COUNTS.clear()
    presenters._DISPLAY_FIELD_MAP = None  # let other modules lazy-build from registry
    presenters._VALID_STATUS_MAP = None


# --- builders ---------------------------------------------------------------


def _ok(sid: str, tool: str, parsed: dict) -> StepResult:
    return StepResult(step_id=sid, tool=tool, status="ok", parsed_output=parsed)


def _skip(sid: str, reason: str) -> StepResult:
    return StepResult(
        step_id=sid, tool="t", status="skipped", parsed_output=None, error_summary=reason
    )


def _fail(sid: str, tool: str, status: str, parsed: dict | None = None) -> StepResult:
    return StepResult(
        step_id=sid, tool=tool, status=status, parsed_output=parsed,
        error_summary="internal: boom secret-token",
    )


def _log(*steps: StepResult, outcome: str = "completed") -> ExecutionLog:
    return ExecutionLog(steps=tuple(steps), outcome=outcome)


def _td(actions: object, intent: str = "показать результат") -> dict:
    return {"intent": intent, "actions": actions}


def _call(template_data: dict) -> ComposerCall:
    return ComposerCall(
        kind="llm", llm_prompt_key="humanize_result", template_data=template_data
    )


def _ctx() -> ComposerContext:
    return ComposerContext(tenant_id="t1", run_id="r1")


class _CapturingComposer:
    """Stub llm_composer: records what data it was handed, returns a fixed reply."""

    def __init__(self, reply: str = "СОБРАННЫЙ ОТВЕТ") -> None:
        self.calls: list[dict] = []
        self.reply = reply

    def __call__(self, *, llm_prompt_key, template_data, execution_log, ctx):
        self.calls.append({"llm_prompt_key": llm_prompt_key, "template_data": template_data})
        return self.reply


# --- _is_step_id_action discrimination --------------------------------------


def test_is_step_id_action_recognizes_only_pure_step_id_dict() -> None:
    assert _is_step_id_action({"step_id": "s1"}) is True
    # not the form → fall through
    assert _is_step_id_action({"step_id": "s1", "status": "ok"}) is False  # extra key
    assert _is_step_id_action({"user_visible_summary": "hi", "status": "ok"}) is False
    assert _is_step_id_action({"step_id": ""}) is False  # empty
    assert _is_step_id_action({"step_id": 5}) is False  # non-str
    assert _is_step_id_action("${s1}") is False  # item-ref
    assert _is_step_id_action({}) is False


# --- new form: ok → domain presenter ----------------------------------------


def test_new_form_ok_projects_only_safe_field_no_id_leak() -> None:
    log = _log(_ok("s1", "web_search", {
        "status": "results", "raw_text": "Нашла рецепт борща", "url": "https://secret-internal",
    }))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "normalized"
    assert data["actions"] == [{"user_visible_summary": "Нашла рецепт борща", "status": "results"}]
    rendered = data["actions"][0]["user_visible_summary"]
    assert "secret-internal" not in rendered
    assert "https://" not in rendered
    assert HUMANIZE_NORMALIZER_METRICS["new_form_ok"] == 1


def test_new_form_intent_is_preserved() -> None:
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "ок"}))
    _, data = _normalize_humanize_result(_td([{"step_id": "s1"}], intent="мой интент"), log)
    assert data["intent"] == "мой интент"


def test_new_form_unmapped_tool_denies_by_default_and_metrics() -> None:
    log = _log(_ok("s1", "mystery_tool", {
        "status": "ok", "raw_text": "СКРЫТО", "internal_id": "iid-9",
    }))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "normalized"
    summary = data["actions"][0]["user_visible_summary"]
    assert summary == "Не могу безопасно показать результат."
    assert "СКРЫТО" not in summary and "iid-9" not in summary
    # presenter lookup uses the REAL status "ok"; the OUTGOING status is sanitized
    # — mystery_tool's "ok" is not a schema-declared status → "unknown".
    assert data["actions"][0]["status"] == "unknown"
    assert PRESENTER_FALLBACK_COUNTS[("mystery_tool", "ok")] == 1


def test_new_form_non_str_domain_status_coerced_to_unknown_then_denied() -> None:
    log = _log(_ok("s1", "mystery_tool", {"status": 123, "raw_text": "x"}))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "normalized"
    assert data["actions"][0]["status"] == "unknown"
    assert data["actions"][0]["user_visible_summary"] == "Не могу безопасно показать результат."
    assert PRESENTER_FALLBACK_COUNTS[("mystery_tool", "unknown")] == 1


# --- new form: executor failures (NOT domain ok) ----------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("error", "Не получилось выполнить действие. Попробуй ещё раз позже."),
        ("timeout", "Действие заняло слишком много времени. Попробуй ещё раз."),
        ("plan_gap", "Не получилось корректно подготовить действие. Попробуй переформулировать."),
        ("arg_violation", "Не получилось выполнить действие из-за неверных параметров."),
    ],
)
def test_new_form_executor_failure_uses_safe_phrase(status: str, expected: str) -> None:
    log = _log(_fail("s1", "list_shopping", status))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "normalized"
    assert data["actions"] == [{"user_visible_summary": expected, "status": "error"}]
    assert "secret-token" not in data["actions"][0]["user_visible_summary"]
    assert HUMANIZE_NORMALIZER_METRICS["new_form_failure"] == 1


def test_unknown_outcome_with_parsed_output_goes_to_failure_not_domain() -> None:
    # unknown_outcome carries a non-ok status AND a parsed_output — must branch
    # by StepResult.status (failure), NEVER treat the parsed_output as domain ok.
    log = _log(StepResult(
        step_id="s1", tool="web_search", status="unknown_outcome",
        parsed_output={"status": "weird", "raw_text": "LEAK", "secret_id": "x9"},
        error_summary="no branch matched",
    ))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "normalized"
    assert data["actions"][0]["status"] == "error"
    assert data["actions"][0]["user_visible_summary"] == (
        "Действие выполнилось, но результат не удалось безопасно распознать."
    )
    assert "LEAK" not in data["actions"][0]["user_visible_summary"]
    assert "x9" not in data["actions"][0]["user_visible_summary"]
    assert HUMANIZE_NORMALIZER_METRICS["new_form_failure"] == 1
    assert "new_form_ok" not in HUMANIZE_NORMALIZER_METRICS


# --- new form: skips --------------------------------------------------------


def test_branch_not_selected_silently_omitted_no_disclaimer() -> None:
    log = _log(
        _ok("s1", "web_search", {"status": "results", "raw_text": "норм"}),
        _skip("s2", "branch_not_selected"),
    )
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "normalized"
    # only the selected branch narrates; no error/disclaimer for the non-taken one
    assert data["actions"] == [{"user_visible_summary": "норм", "status": "results"}]
    assert HUMANIZE_NORMALIZER_METRICS["omit_branch_not_selected"] == 1
    assert "partial_disclaimer" not in HUMANIZE_NORMALIZER_METRICS


def test_mutually_exclusive_branches_non_selected_is_not_user_visible_error() -> None:
    # root compose referencing two mutually-exclusive branch steps; only one fired.
    log = _log(
        _skip("s1", "branch_not_selected"),
        _ok("s2", "web_search", {"status": "results", "raw_text": "ветка Б"}),
    )
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "normalized"
    assert data["actions"] == [{"user_visible_summary": "ветка Б", "status": "results"}]


@pytest.mark.parametrize("reason", ["halted_arg_violation", "upstream_skipped:s1", "honest_partial_group_failed"])
def test_failure_skip_omits_and_appends_one_disclaimer(reason: str) -> None:
    log = _log(
        _ok("s1", "web_search", {"status": "results", "raw_text": "часть готова"}),
        _skip("s2", reason),
    )
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "normalized"
    assert data["actions"] == [
        {"user_visible_summary": "часть готова", "status": "results"},
        {"user_visible_summary": "Часть действий выполнить не удалось.", "status": "fallback"},
    ]
    assert HUMANIZE_NORMALIZER_METRICS["omit_failure"] == 1
    assert HUMANIZE_NORMALIZER_METRICS["partial_disclaimer"] == 1


def test_unknown_skip_reason_treated_as_failure_omit() -> None:
    log = _log(
        _ok("s1", "web_search", {"status": "results", "raw_text": "ок"}),
        _skip("s2", "some_future_reason"),
    )
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "normalized"
    assert HUMANIZE_NORMALIZER_METRICS["omit_skip_unknown_reason"] == 1
    assert HUMANIZE_NORMALIZER_METRICS["partial_disclaimer"] == 1


# --- new form: missing step -------------------------------------------------


def test_missing_step_id_renders_safe_fallback() -> None:
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "ок"}))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s9"}]), log)
    assert outcome == "normalized"
    assert data["actions"] == [{
        "user_visible_summary": "Не получилось выполнить действие. Попробуй ещё раз позже.",
        "status": "fallback",
    }]
    assert HUMANIZE_NORMALIZER_METRICS["deny_fallback_missing_step"] == 1


# --- new form: short-circuit (all omitted) ----------------------------------


def test_all_branch_not_selected_short_circuits_neutral() -> None:
    log = _log(_skip("s1", "branch_not_selected"), _skip("s2", "branch_not_selected"))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "short_circuit"
    assert data is None
    assert HUMANIZE_NORMALIZER_METRICS["short_circuit_neutral"] == 1


def test_all_failure_skip_short_circuits_failure() -> None:
    log = _log(_skip("s1", "halted_timeout"))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "short_circuit"
    assert HUMANIZE_NORMALIZER_METRICS["short_circuit_failure"] == 1


# --- fall-through forms (NOT intercepted) -----------------------------------


@pytest.mark.parametrize(
    "actions",
    [
        [{"user_visible_summary": "литерал", "status": "ok"}],  # literal summary
        [{"user_visible_summary": "${s1.raw_text}", "status": "results"}],  # OLD full-ref
        "${s1.actions}",  # top-level full-ref
        ["${s1}"],  # item-ref
        [{"step_id": "s1", "status": "ok"}],  # mixed/extra key → not pure {step_id}
        [{"step_id": "s1"}, {"user_visible_summary": "x", "status": "ok"}],  # mixed list
        [],  # empty
    ],
)
def test_fall_through_forms_are_not_normalized(actions: object) -> None:
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "x"}))
    outcome, data = _normalize_humanize_result(_td(actions), log)
    assert outcome == "fall_through"
    assert data is None


def test_non_dict_template_data_falls_through() -> None:
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "x"}))
    outcome, _ = _normalize_humanize_result("${s1}", log)  # type: ignore[arg-type]
    assert outcome == "fall_through"


# --- compose() integration: wiring + ordering -------------------------------


def test_compose_normalized_reaches_llm_with_safe_data_only() -> None:
    stub = _CapturingComposer()
    log = _log(_ok("s1", "web_search", {
        "status": "results", "raw_text": "Безопасный текст", "url": "https://leak",
    }))
    result = compose(_call(_td([{"step_id": "s1"}])), log, llm_composer=stub, ctx=_ctx())
    assert result.fallback_used is None
    assert result.text == "СОБРАННЫЙ ОТВЕТ"
    assert len(stub.calls) == 1
    sent = stub.calls[0]["template_data"]
    # LLM received the strict normalized form — step_id gone, only safe summary
    assert sent == {
        "intent": "показать результат",
        "actions": [{"user_visible_summary": "Безопасный текст", "status": "results"}],
    }
    assert "leak" not in str(sent)


def test_compose_short_circuit_does_not_invoke_llm() -> None:
    stub = _CapturingComposer()
    log = _log(_skip("s1", "branch_not_selected"))
    result = compose(_call(_td([{"step_id": "s1"}])), log, llm_composer=stub, ctx=_ctx())
    assert stub.calls == []  # LLM never called
    assert result.fallback_used == "generic_error"
    assert result.error_code == "humanize_all_actions_omitted"


def test_compose_fall_through_passes_original_data_to_llm() -> None:
    stub = _CapturingComposer()
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "x"}))
    literal = _td([{"user_visible_summary": "литеральный ответ", "status": "ok"}])
    result = compose(_call(literal), log, llm_composer=stub, ctx=_ctx())
    assert result.fallback_used is None
    assert len(stub.calls) == 1
    # fall-through → resolve_refs on a literal is a no-op → original data reaches LLM
    assert stub.calls[0]["template_data"] == literal


# --- R1 fixes: strict intent / top-level recognition (MAJOR, both A/B) -------


@pytest.mark.parametrize(
    "template_data",
    [
        {"actions": [{"step_id": "s1"}]},  # missing intent
        {"intent": "", "actions": [{"step_id": "s1"}]},  # blank intent
        {"intent": "   ", "actions": [{"step_id": "s1"}]},  # whitespace intent
        {"intent": 5, "actions": [{"step_id": "s1"}]},  # non-str intent
        {"intent": "${s1.intent}", "actions": [{"step_id": "s1"}]},  # full-ref intent
        {"intent": "покажи ${s1.raw_text}", "actions": [{"step_id": "s1"}]},  # EMBEDDED ref (R2)
        {"intent": "глянь ${}", "actions": [{"step_id": "s1"}]},  # EMPTY ${} token (R3 MAJOR medium)
        {"intent": "ok", "actions": [{"step_id": "s1"}], "extra": "x"},  # extra top key
    ],
)
def test_malformed_intent_or_extra_top_key_falls_through(template_data: dict) -> None:
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "x"}))
    outcome, data = _normalize_humanize_result(template_data, log)
    assert outcome == "fall_through"
    assert data is None


# --- R1 fix: branch_not_selected EXACT, not prefix (MAJOR/MINOR, A/B) --------


def test_branch_not_selected_prefixed_variant_is_failure_omit_not_silent() -> None:
    log = _log(
        _ok("s1", "web_search", {"status": "results", "raw_text": "готово"}),
        _skip("s2", "branch_not_selected_after_halt"),  # prefixed unknown variant
    )
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "normalized"
    # prefixed unknown → failure-omit (disclaimer appended), NOT silent
    assert data["actions"][-1]["user_visible_summary"] == "Часть действий выполнить не удалось."
    assert HUMANIZE_NORMALIZER_METRICS.get("omit_branch_not_selected") is None
    assert HUMANIZE_NORMALIZER_METRICS["omit_skip_unknown_reason"] == 1
    assert HUMANIZE_NORMALIZER_METRICS["partial_disclaimer"] == 1


# --- R1 fix: outgoing status sanitized separately from lookup (MAJOR, A/B) ---


@pytest.mark.parametrize(
    "bad_status",
    [
        "member_id:sh_9", "a" * 50, "has space", "id/123", "tab\tval",
        # alnum/underscore id-like values a SHAPE regex would WRONGLY allow — the
        # value-based allowlist rejects them (Codex R2 MAJOR, both A/B).
        "member_id_755682022", "sh_9", "user_12345", "secret_token",
    ],
)
def test_id_like_or_oversized_domain_status_sanitized_in_payload(bad_status: str) -> None:
    # web_search override narrates raw_text regardless of status, so the summary
    # stays safe; the OUTGOING status must be downgraded to "unknown".
    log = _log(_ok("s1", "web_search", {"status": bad_status, "raw_text": "безопасно"}))
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert outcome == "normalized"
    assert data["actions"][0]["status"] == "unknown"
    assert data["actions"][0]["user_visible_summary"] == "безопасно"
    assert bad_status not in str(data["actions"][0])
    assert HUMANIZE_NORMALIZER_METRICS["status_sanitized"] == 1


def test_clean_domain_status_passes_through_unchanged() -> None:
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "ок"}))
    _, data = _normalize_humanize_result(_td([{"step_id": "s1"}]), log)
    assert data["actions"][0]["status"] == "results"
    assert "status_sanitized" not in HUMANIZE_NORMALIZER_METRICS


# --- R1 MINOR: compose-level fall-through for old full-ref + mixed -----------


def test_compose_old_full_ref_takes_resolve_refs_path() -> None:
    # OLD form {user_visible_summary: "${s1.raw_text}"} must fall through and be
    # resolved by resolve_refs (unchanged current behavior), NOT normalized.
    stub = _CapturingComposer()
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "РАЗРЕШЁННОЕ"}))
    old = _td([{"user_visible_summary": "${s1.raw_text}", "status": "results"}])
    compose(_call(old), log, llm_composer=stub, ctx=_ctx())
    assert len(stub.calls) == 1
    # resolve_refs replaced the ref → the step's raw_text reached the LLM
    assert stub.calls[0]["template_data"]["actions"][0]["user_visible_summary"] == "РАЗРЕШЁННОЕ"


def test_compose_mixed_list_falls_through_unnormalized() -> None:
    stub = _CapturingComposer()
    log = _log(_ok("s1", "web_search", {"status": "results", "raw_text": "x"}))
    mixed = _td([{"step_id": "s1"}, {"user_visible_summary": "литерал", "status": "ok"}])
    compose(_call(mixed), log, llm_composer=stub, ctx=_ctx())
    assert len(stub.calls) == 1
    # not normalized → the {step_id} item is still present verbatim (no presenter)
    assert stub.calls[0]["template_data"]["actions"][0] == {"step_id": "s1"}


# --- R1 MINOR: "${...}" in presenter text is preserved (skip-resolve_refs) ---


def test_compose_normalized_preserves_dollar_brace_in_presenter_text() -> None:
    # The whole reason for skipping resolve_refs on the normalized path: presenter
    # text may legitimately contain "${...}" (e.g. fetched web/code content). It
    # must reach the LLM unchanged, never be (mis)resolved.
    stub = _CapturingComposer()
    log = _log(_ok("s1", "web_search", {
        "status": "results", "raw_text": "цена ${not.a.ref} за кг",
    }))
    result = compose(_call(_td([{"step_id": "s1"}])), log, llm_composer=stub, ctx=_ctx())
    assert result.fallback_used is None
    assert stub.calls[0]["template_data"]["actions"][0]["user_visible_summary"] == (
        "цена ${not.a.ref} за кг"
    )


# --- R1 MINOR: normalized payload satisfies the real strict contract ---------


def test_normalized_payload_passes_real_validate_data() -> None:
    from sreda.services.composer_contracts import validate_humanize_result_payload

    log = _log(
        _ok("s1", "web_search", {"status": "results", "raw_text": "ок"}),
        _fail("s2", "list_shopping", "timeout"),
    )
    outcome, data = _normalize_humanize_result(_td([{"step_id": "s1"}, {"step_id": "s2"}]), log)
    assert outcome == "normalized"
    # The strict runtime contract (allow_refs=False — fully resolved) accepts it.
    assert validate_humanize_result_payload(data, allow_refs=False) == []
