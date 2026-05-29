"""Composer entry point — Sub-A12 Phase D.1 (R1 A/B fixes applied).

Single function ``compose(call, execution_log, ...)`` that renders the
final user-facing reply for a planner run. Sits between Phase C
(executor → ExecutionLog) and Phase E (LangGraph composer_node that
writes to outbox).

What this owns
==============

1. **Multi-override policy** (Codex Phase D.1 R1 HIGH convergent A+B) —
   ``terminal_composes()`` returns ALL matched-branch composes in
   compiled log order. Policy:
     - 0 overrides → plan-level ``call`` (default)
     - 1 override → that override wins (single-branch path)
     - 2+ overrides → fall back to plan-level ``call`` (no policy yet
       for combining; planner should not produce conflicting terminals)

2. **Ref resolution** — ``template_data`` may contain ``${s1.field}``
   references. Walks the dict via Sub-A1 ``resolve_refs`` against
   ``execution_log.step_outputs`` (built from successful step outputs
   only). KNOWN LIMITATION (Task #30): flat namespace; a ref to a
   step from a different intent_group resolves if that step also
   succeeded. Today's safe behavior: any unresolvable ref falls
   through to ``partial_with_compose_error``. Task #30 will add
   intent-scoped filtering.

3. **Registry snapshot race protection** (Codex Phase D.1 R1 MEDIUM,
   Group 6.5) — optional ``expected_registry_snapshot_hash`` arg lets
   Phase E pass the hash recorded at Phase B validation; if the
   current registry differs (template removed / changed body), we
   fall through to ``compose_failure_after_execution``.

4. **Dispatch by kind** — ``kind='template'`` calls
   ``ComposerRegistry.render``; ``kind='llm'`` delegates to an
   injected ``llm_composer`` callable receiving a ``ComposerContext``
   (Codex Phase D.1 R1 MEDIUM #5). D.1 ships template path; D.2
   wires the LLM composer.

5. **Failure fall-through** — UnknownTemplateError (race against
   registry deploy) and Jinja TemplateError surface as
   ``partial_with_compose_error``. Missing ``llm_composer`` /
   ``llm_composer`` raise / unsupported kind → ``generic_tool_error``.
   ``template_id`` missing on kind='template' → ``generic_tool_error``
   (Codex Phase D.1 R1 LOW #6 — invalid internal ComposerCall, not a
   registry race).

6. **Rich return type** (Codex Phase D.1 R1 HIGH #2) — returns
   ``ComposeResult(text, fallback_used, error_code, effective_*)`` so
   Phase E can distinguish normal-completed vs fell-through reply
   when writing ``planner_executions.execution_status`` /
   ``composer_path``.

What this does NOT own
======================

* Choosing WHICH ComposerCall to render for aborted plans — the
  orchestrator builds that based on plan outcome (e.g. picks
  ``invalid_plan_fallback`` for invalid_plan, ``generic_tool_error``
  for transport failure). For ``completed`` / ``partial_failure`` it
  passes the plan-level ``compose``.
* Persistence of the rendered reply — Phase E composer_node writes to
  outbox and ``planner_executions.final_reply_chars``.
* LLM composer implementation — Phase D.2.
* Intent-scoped ref namespacing — Task #30.
* Per-tool natural-language summaries in fallback — Task for D.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from jinja2 import TemplateError

from sreda.runtime.planner.executor import ExecutionLog
from sreda.runtime.planner.interpolation import resolve_refs
from sreda.runtime.planner.schemas import ComposerCall
from sreda.services.composer.prompts_registry import (
    LLM_PROMPT_REGISTRY,
    LLMPromptRegistry,
)
from sreda.services.composer.registry import (
    REGISTRY,
    ComposerRegistry,
    UnknownTemplateError,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Composer context (Codex Phase D.1 R1 MEDIUM #5 — LLMComposer protocol)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComposerContext:
    """Per-request metadata the LLM composer (Phase D.2) needs but
    that doesn't belong in the rendered template_data.

    Adding the field now avoids coupling D.2 implementation to
    orchestrator globals later (Codex Phase D.1 R1 MEDIUM #5).

    For ``kind='template'`` calls, context is unused — the template
    sees only resolved ``template_data``.

    Sensible defaults so D.1 unit tests can call compose() without
    wiring a context; production callers (Phase E orchestrator) MUST
    pass real values.
    """

    tenant_id: str = "unknown"
    run_id: str = "unknown"
    feature_key: str = "housewife_assistant"
    user_message: str = ""
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"


# ---------------------------------------------------------------------------
# Compose result (Codex Phase D.1 R1 HIGH #2 — rich return)
# ---------------------------------------------------------------------------


FallbackUsed = Literal[None, "compose_error", "generic_error"]


@dataclass(frozen=True)
class ComposeResult:
    """Output of ``compose()`` — text + metadata for Phase E.

    Phase E persistence uses ``fallback_used`` to set
    ``planner_executions.composer_path`` (e.g. ``"template:<id>"`` for
    successful renders, ``"fallback:compose_error"`` for race / Jinja
    failures).

    Metrics: ``fallback_used != None`` → planner_gaps candidate (the
    planner picked a template that can't render OR the LLM composer
    failed; both are signals for GEPA training).
    """

    text: str
    """Plain-text reply for Telegram outbox (NOT HTML — registry uses
    ``autoescape=False``)."""

    fallback_used: FallbackUsed = None
    """``None`` = primary path rendered cleanly. ``'compose_error'`` =
    fell through to ``partial_with_compose_error`` (UnknownTemplateError
    / TemplateError / ref-resolve failure / registry hash mismatch).
    ``'generic_error'`` = fell through to ``generic_tool_error``
    (missing template_id / LLM composer not wired / unsupported kind)."""

    error_code: str | None = None
    """Diagnostic identifier for the fallback path (e.g.
    ``'unknown_template'`` / ``'ref_resolve_error'`` /
    ``'registry_hash_mismatch'`` / ``'llm_composer_not_wired'`` /
    ``'llm_composer_error:RuntimeError'``). None on success."""

    effective_template_id: str | None = None
    """The ``template_id`` actually used to render (after override
    resolution, before fallback). None when ``kind='llm'`` was the
    effective path or when the primary call had no template_id."""

    effective_llm_prompt_key: str | None = None
    """The ``llm_prompt_key`` actually used. None when ``kind='template'``
    was effective."""

    # Codex Phase D.2 R1 MAJOR A#8/B#3 — composer LLM cost metadata for
    # Phase E persistence. Populated only when the LLM path ran AND the
    # composer returned an LLMComposerResult (None for template path,
    # for str-returning test stubs, and for fallbacks).
    composer_provider: str | None = None
    """LLM provider used by the composer (kind='llm' success only)."""

    composer_model: str | None = None
    """Resolved model name of the composer call (kind='llm' success only)."""

    composer_latency_ms: int | None = None
    """Wall-clock latency of the composer LLM call (kind='llm' success only)."""


class LLMComposer(Protocol):
    """Phase D.2 callable interface — invoked when the chosen
    ComposerCall has ``kind='llm'``. Returns the rendered reply.

    Codex Phase D.1 R1 MEDIUM #5: added ``ctx`` parameter so D.2 can
    access tenant/run/profile/locale without reaching into orchestrator
    globals.

    Return type (Codex D.2 R1 MAJOR A#8/B#3): the production composer
    returns an ``LLMComposerResult`` (text + provider/model/latency for
    Phase E observability). compose() duck-types the return — it reads
    ``.text`` if present, else treats the value as the reply string —
    so lightweight test stubs may still return a bare ``str``. Declared
    as ``Any`` here to avoid a compose→llm_composer import cycle.
    """

    def __call__(
        self,
        *,
        llm_prompt_key: str,
        template_data: dict[str, Any],
        execution_log: ExecutionLog,
        ctx: ComposerContext,
    ) -> Any: ...


# Fallback template ids — used when the primary compose path raises.
# Both already in HOUSEWIFE_TEMPLATES (Sub-A5 foundation).
_FALLBACK_COMPOSE_ERROR = "partial_with_compose_error"
_FALLBACK_GENERIC_ERROR = "generic_tool_error"


def compose(
    call: ComposerCall,
    execution_log: ExecutionLog,
    *,
    registry: ComposerRegistry = REGISTRY,
    llm_composer: LLMComposer | Callable[..., Any] | None = None,
    ctx: ComposerContext | None = None,
    expected_registry_snapshot_hash: str | None = None,
    llm_prompt_registry: LLMPromptRegistry = LLM_PROMPT_REGISTRY,
    expected_llm_prompt_registry_snapshot_hash: str | None = None,
) -> ComposeResult:
    """Render the final user-facing reply.

    Parameters
    ----------
    call :
        Plan-level ``ComposerCall`` from ``Plan.compose``. Used unless
        exactly one matched-branch override is present in
        ``execution_log`` (then override wins). Multiple overrides →
        plan-level call wins (no merge policy yet).
    execution_log :
        Output of ``executor.execute_plan``. Source of step outputs
        for ref resolution + matched-branch compose overrides (if any).
    registry :
        Defaults to module-level singleton. Tests inject a custom
        registry to verify failure paths.
    llm_composer :
        Phase D.2 callable for ``kind='llm'``. If None, the LLM path
        returns ``ComposeResult(fallback_used='generic_error')`` so
        D.1 can ship without D.2 being wired.
    ctx :
        Per-request metadata for the LLM composer. Defaults to
        ``ComposerContext()`` for D.1 unit tests; Phase E orchestrator
        MUST pass real values.
    expected_registry_snapshot_hash :
        Codex Phase D.1 R1 MEDIUM #3 — if Phase B recorded the
        registry hash at validation, Phase E passes it here. If the
        current registry's snapshot_hash differs (template removed /
        body changed between Phase B and Phase D), fall through to
        ``partial_with_compose_error``. None = no check.

    Returns
    -------
    ComposeResult :
        Text + fallback metadata. ``fallback_used=None`` → primary
        path rendered cleanly. Non-None values signal a degraded
        render that Phase E should record in
        ``planner_executions.composer_path`` and surface as a
        planner_gaps candidate.

    Notes
    -----
    * Override priority (Codex Phase D.1 R1 HIGH #1): zero overrides →
      plan-level call. One override → use it. Multiple overrides → use
      plan-level (avoid policy ambiguity until product needs surface).
    * Aborted plans (Codex Phase D.1 R1 LOW #8): compose() does NOT
      know whether a ``completed`` or ``aborted_partial`` plan reached
      it. Phase E orchestrator MUST swap the call to an explicit
      fallback template (``invalid_plan_fallback``,
      ``partial_with_compose_error``, etc.) for aborted outcomes
      before calling compose(). compose() trusts what it's given.
    """
    ctx_provided = ctx is not None
    ctx = ctx or ComposerContext()

    # 1. Pick effective ComposerCall (multi-override policy, outcome-aware)
    effective_call = _pick_effective_call(call, execution_log)

    # 2. Registry snapshot race check (Group 6.5) — Codex Phase D.1 R3
    # high MED #2 + R4 high MED: hash check fires for PLANNER-OWNED
    # compose paths (Phase B-validated plan-level call):
    #
    #   - 'completed'        → orchestrator passes Plan.compose         → CHECK
    #   - 'partial_failure'  → orchestrator passes Plan.compose         → CHECK
    #   - 'failed' / 'aborted' / 'aborted_partial' → orchestrator SWAPS
    #     to fallback template (invalid_plan_fallback /
    #     partial_with_compose_error / generic_tool_error) which was
    #     NOT Phase B-validated → SKIP (forcing hash check would
    #     override orchestrator's fallback choice with compose_error).
    #
    # Phase E contract: orchestrator passes expected_registry_snapshot_hash
    # iff the call originated from Plan.compose (i.e. Phase B validated
    # it). For orchestrator-owned fallbacks, pass None.
    _PLANNER_OWNED_OUTCOMES = ("completed", "partial_failure")
    if (
        expected_registry_snapshot_hash is not None
        and execution_log.outcome in _PLANNER_OWNED_OUTCOMES
    ):
        actual_hash = registry.snapshot_hash()
        if actual_hash != expected_registry_snapshot_hash:
            logger.warning(
                "composer: registry snapshot mismatch — Phase B recorded "
                "hash=%s but current=%s. Falling through to %s.",
                expected_registry_snapshot_hash[:12], actual_hash[:12],
                _FALLBACK_COMPOSE_ERROR,
            )
            # Codex Phase D.1 R4 medium MED — preserve effective call
            # metadata so Phase E composer_path persistence shows which
            # template_id/llm_prompt_key was attempted before the race.
            return _result_compose_error(
                registry, execution_log,
                error_code="registry_hash_mismatch",
                effective_template_id=effective_call.template_id,
                effective_llm_prompt_key=effective_call.llm_prompt_key,
            )

    # 3. Codex Phase D.1 R3 MED #1 (A) — pre-dispatch ctx guard for
    # kind='llm' BEFORE ref resolution. Otherwise a ${missing.ref} on a
    # kind='llm' call without ctx would surface as compose_error
    # 'ref_resolve_error' instead of generic_error 'llm_context_missing',
    # hiding Phase E ctx-wiring bugs as planner / template failures in
    # telemetry. Also handles R3 MED #3 (B): strict ctx — reject default
    # 'unknown' tenant_id/run_id values for production llm path.
    if effective_call.kind == "llm":
        if not ctx_provided or _ctx_has_unknown_identity(ctx):
            logger.error(
                "composer: kind='llm' with llm_prompt_key=%r requires real "
                "ctx (tenant_id/run_id not 'unknown') — caller passed %s. "
                "Falling through to %s.",
                effective_call.llm_prompt_key,
                "None" if not ctx_provided else "ctx with default identity",
                _FALLBACK_GENERIC_ERROR,
            )
            return _result_generic_error(
                registry, error_code="llm_context_missing",
                effective_llm_prompt_key=effective_call.llm_prompt_key,
            )
        # Codex Phase D.1 R3 LOW #4 (B) — symmetric invalid_llm_call
        # check matching invalid_template_call. Empty llm_prompt_key
        # means the planner emitted an invalid ComposerCall (pydantic
        # validator should have rejected; defensive guard for
        # model_construct bypass / future schema relaxation).
        if not effective_call.llm_prompt_key:
            logger.error(
                "composer: kind='llm' but llm_prompt_key is empty/None — "
                "invalid internal ComposerCall. Falling through to %s.",
                _FALLBACK_GENERIC_ERROR,
            )
            return _result_generic_error(
                registry, error_code="invalid_llm_call",
            )
        # Codex Phase D.2 R1 MAJOR A#3/B#1 — LLM prompt registry race
        # guard, symmetric to the template hash check above. Phase B
        # records the LLM registry hash at validation; if a prompt was
        # edited/removed between then and now, the system prompt the
        # planner validated against no longer matches → fall through to
        # compose_error rather than narrate on a changed prompt. Same
        # planner-owned-outcome gate as the template check.
        if (
            expected_llm_prompt_registry_snapshot_hash is not None
            and execution_log.outcome in _PLANNER_OWNED_OUTCOMES
        ):
            actual_llm_hash = llm_prompt_registry.snapshot_hash()
            if actual_llm_hash != expected_llm_prompt_registry_snapshot_hash:
                logger.warning(
                    "composer: LLM prompt registry snapshot mismatch — "
                    "Phase B recorded hash=%s but current=%s. Falling "
                    "through to %s.",
                    expected_llm_prompt_registry_snapshot_hash[:12],
                    actual_llm_hash[:12], _FALLBACK_COMPOSE_ERROR,
                )
                return _result_compose_error(
                    registry, execution_log,
                    error_code="llm_registry_hash_mismatch",
                    effective_llm_prompt_key=effective_call.llm_prompt_key,
                )

    # 4. Build step_outputs view for ref resolution
    step_outputs = _step_outputs_for_refs(execution_log)

    # 5. Resolve refs in template_data
    try:
        resolved_data = resolve_refs(
            effective_call.template_data, step_outputs,
        )
    except Exception as exc:  # noqa: BLE001 — fall-through to safety template
        logger.warning(
            "composer: ref resolution failed for template_id=%r / "
            "llm_prompt_key=%r — falling through to %s. Cause: %s",
            effective_call.template_id,
            effective_call.llm_prompt_key,
            _FALLBACK_COMPOSE_ERROR,
            exc,
        )
        return _result_compose_error(
            registry, execution_log,
            error_code=f"ref_resolve_error:{type(exc).__name__}",
            effective_template_id=effective_call.template_id,
            effective_llm_prompt_key=effective_call.llm_prompt_key,
        )

    # 5. Dispatch by kind
    if effective_call.kind == "template":
        return _render_template_result(
            registry,
            template_id=effective_call.template_id,
            data=resolved_data,
            execution_log=execution_log,
        )
    if effective_call.kind == "llm":
        return _render_llm_result(
            llm_composer,
            registry=registry,
            llm_prompt_key=effective_call.llm_prompt_key or "",
            data=resolved_data,
            execution_log=execution_log,
            ctx=ctx,
            ctx_provided=ctx_provided,
        )
    # Schema only allows {template, llm}; defensive guard for future
    # kinds that might land without a renderer.
    logger.error(
        "composer: unsupported kind=%r — falling through to %s",
        effective_call.kind, _FALLBACK_GENERIC_ERROR,
    )
    return _result_generic_error(
        registry, error_code=f"unsupported_kind:{effective_call.kind}",
    )


# ---------------------------------------------------------------------------
# Internals — override policy
# ---------------------------------------------------------------------------


def _pick_effective_call(
    plan_call: ComposerCall, execution_log: ExecutionLog,
) -> ComposerCall:
    """Codex Phase D.1 R1 HIGH #1 + R2 HIGH (outcome-blind) — pick
    effective ComposerCall using terminal_composes() plural with
    explicit policy:

    - Outcome != 'completed' (partial_failure / aborted / aborted_partial
      / failed) → plan-level call ALWAYS. A successful early step's
      terminal-branch compose would hide the real outcome from the
      user; the orchestrator should have swapped the plan_call to an
      explicit fallback template (partial_with_compose_error,
      invalid_plan_fallback, generic_tool_error) before calling
      compose(). Codex Phase D.1 R2 HIGH (B-side).
    - 0 overrides → plan-level call (normal: planner's plan.compose).
    - 1 override  → that override (single-branch path; planner expects
                    this to fire and the plan-level is just aggregate
                    fallback).
    - 2+ overrides → plan-level call (multi-step plan with several
                    terminal branches — no merge policy yet; planner
                    contract says aggregate response uses Plan.compose,
                    terminal compose only for mutually exclusive
                    branches — Codex R2 MED B-side planner-prompt note).
    """
    if execution_log.outcome != "completed":
        logger.debug(
            "composer: execution_log.outcome=%r — ignoring terminal "
            "overrides, using plan-level call (orchestrator-owned "
            "fallback path expected).",
            execution_log.outcome,
        )
        return plan_call
    overrides = execution_log.terminal_composes()
    if not overrides:
        return plan_call
    if len(overrides) == 1:
        return overrides[0][1]
    logger.info(
        "composer: %d terminal-branch composes matched — using plan-level "
        "call (no multi-override merge policy yet). Override step_ids=%s",
        len(overrides),
        [step_id for step_id, _ in overrides],
    )
    return plan_call


# ---------------------------------------------------------------------------
# Internals — refs + render
# ---------------------------------------------------------------------------


def _step_outputs_for_refs(execution_log: ExecutionLog) -> dict[str, Any]:
    """Build the dict consumed by ``resolve_refs``: ``step_id → parsed_output``.

    Only successful steps contribute. Skipped / failed / unknown_outcome
    steps don't have meaningful output for refs — referencing them
    raises (which the caller catches and falls through to the safety
    template).

    KNOWN LIMITATION (Task #30): flat namespace allows cross-intent
    ref leakage. Currently safe because unresolvable refs fall through,
    but a coincidental successful ref from a different intent_group
    would resolve silently. Task #30 will add intent-scoped filtering.
    """
    return {
        step.step_id: step.parsed_output or {}
        for step in execution_log.steps
        if step.status == "ok" and step.parsed_output is not None
    }


def _render_template_result(
    registry: ComposerRegistry,
    *,
    template_id: str | None,
    data: dict[str, Any],
    execution_log: ExecutionLog,
) -> ComposeResult:
    """Render via registry; fall through on any failure.

    Codex Phase D.1 R1 LOW #6 — missing template_id (kind='template'
    with None somehow) is an invalid internal ComposerCall, not a
    registry race. Route to generic_error not compose_error.
    """
    if not template_id:
        logger.error(
            "composer: kind='template' but template_id is empty/None — "
            "this is an invalid internal ComposerCall (pydantic should "
            "have rejected). Falling through to %s.",
            _FALLBACK_GENERIC_ERROR,
        )
        return _result_generic_error(
            registry, error_code="invalid_template_call",
        )

    try:
        text = registry.render(template_id, data)
        return ComposeResult(
            text=text,
            fallback_used=None,
            error_code=None,
            effective_template_id=template_id,
        )
    except UnknownTemplateError:
        logger.warning(
            "composer: unknown template_id=%r — likely Phase B/D registry "
            "race (Group 6.5). Falling through to %s.",
            template_id, _FALLBACK_COMPOSE_ERROR,
        )
        return _result_compose_error(
            registry, execution_log,
            error_code="unknown_template",
            effective_template_id=template_id,
        )
    except TemplateError as exc:
        logger.warning(
            "composer: render of template_id=%r failed (%s). "
            "Falling through to %s.",
            template_id, exc, _FALLBACK_COMPOSE_ERROR,
        )
        return _result_compose_error(
            registry, execution_log,
            error_code=f"template_render_error:{type(exc).__name__}",
            effective_template_id=template_id,
        )


def _ctx_has_unknown_identity(ctx: ComposerContext) -> bool:
    """Codex Phase D.1 R3 MED #3 (B) — flag default-valued ctx as
    'identity missing'. Phase E orchestrator must populate real
    tenant_id + run_id; default 'unknown' values would silently
    pollute LLM-trace billing / audit / personalization.

    Returns True if either tenant_id OR run_id is the placeholder
    'unknown' (the dataclass default). Other defaults (locale='ru-RU',
    timezone='Europe/Moscow') are acceptable production defaults so
    we don't gate on them."""
    return ctx.tenant_id == "unknown" or ctx.run_id == "unknown"


def _render_llm_result(
    llm_composer: LLMComposer | Callable[..., str] | None,
    *,
    registry: ComposerRegistry,
    llm_prompt_key: str,
    data: dict[str, Any],
    execution_log: ExecutionLog,
    ctx: ComposerContext,
    ctx_provided: bool,
) -> ComposeResult:
    """Dispatch to the injected LLM composer; fall through to generic
    error if not wired (D.1 ships without LLM composer; D.2 wires it).

    The pre-dispatch caller (compose() main flow) already enforces
    ctx_provided + non-default identity per Codex Phase D.1 R3 MED #1
    + R3 MED #3. The redundant check below is defense-in-depth in case
    a future refactor calls _render_llm_result directly.
    """
    if not ctx_provided or _ctx_has_unknown_identity(ctx):
        # Defense-in-depth — main flow should have caught this.
        logger.error(
            "composer: _render_llm_result reached without real ctx "
            "(provided=%s, identity_ok=%s) — main-flow pre-dispatch guard "
            "should have fired. Falling through to %s.",
            ctx_provided, not _ctx_has_unknown_identity(ctx),
            _FALLBACK_GENERIC_ERROR,
        )
        return _result_generic_error(
            registry, error_code="llm_context_missing",
            effective_llm_prompt_key=llm_prompt_key,
        )
    if llm_composer is None:
        logger.warning(
            "composer: kind='llm' with llm_prompt_key=%r but no llm_composer "
            "injected — falling through to %s",
            llm_prompt_key, _FALLBACK_GENERIC_ERROR,
        )
        return _result_generic_error(
            registry, error_code="llm_composer_not_wired",
            effective_llm_prompt_key=llm_prompt_key,
        )
    try:
        raw = llm_composer(
            llm_prompt_key=llm_prompt_key,
            template_data=data,
            execution_log=execution_log,
            ctx=ctx,
        )
        # Codex Phase D.2 R1 MAJOR A#8/B#3 — duck-type the composer
        # return: production composer yields an LLMComposerResult (has
        # .text + provider/model/latency_ms); test stubs may return a
        # bare str. Pull metadata when present for Phase E persistence.
        if isinstance(raw, str):
            text = raw
            provider = model = None
            latency_ms = None
        else:
            text = getattr(raw, "text", "")
            provider = getattr(raw, "provider", None)
            model = getattr(raw, "model", None)
            latency_ms = getattr(raw, "latency_ms", None)
        # A composer that returns blank text is a contract breach —
        # the production composer raises ComposerEmptyOutput instead,
        # but a stub might not. Treat blank as generic_error.
        if not text or not text.strip():
            logger.warning(
                "composer: llm_composer returned blank text for "
                "prompt_key=%r — falling through to %s",
                llm_prompt_key, _FALLBACK_GENERIC_ERROR,
            )
            return _result_generic_error(
                registry, error_code="llm_composer_blank_output",
                effective_llm_prompt_key=llm_prompt_key,
            )
        return ComposeResult(
            text=text,
            fallback_used=None,
            error_code=None,
            effective_llm_prompt_key=llm_prompt_key,
            composer_provider=provider,
            composer_model=model,
            composer_latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 — LLM call can fail many ways
        logger.exception(
            "composer: llm_composer raised for prompt_key=%r — falling "
            "through to %s",
            llm_prompt_key, _FALLBACK_GENERIC_ERROR,
        )
        return _result_generic_error(
            registry,
            error_code=f"llm_composer_error:{type(exc).__name__}",
            effective_llm_prompt_key=llm_prompt_key,
        )


# ---------------------------------------------------------------------------
# Internals — fallback rendering
# ---------------------------------------------------------------------------


def _result_compose_error(
    registry: ComposerRegistry,
    execution_log: ExecutionLog,
    *,
    error_code: str,
    effective_template_id: str | None = None,
    effective_llm_prompt_key: str | None = None,
) -> ComposeResult:
    """Render ``partial_with_compose_error`` with a brief execution
    summary so the user knows what landed before the compose hiccup."""
    summary = _execution_summary(execution_log)
    try:
        text = registry.render(
            _FALLBACK_COMPOSE_ERROR,
            {"execution_summary": summary} if summary else {},
        )
    except (UnknownTemplateError, TemplateError) as exc:
        logger.exception(
            "composer: fallback %s also failed: %s",
            _FALLBACK_COMPOSE_ERROR, exc,
        )
        text = (
            "Сделала что просила, но с финальным сообщением что-то "
            "пошло не так. Действия выполнены."
        )
    return ComposeResult(
        text=text,
        fallback_used="compose_error",
        error_code=error_code,
        effective_template_id=effective_template_id,
        effective_llm_prompt_key=effective_llm_prompt_key,
    )


def _result_generic_error(
    registry: ComposerRegistry,
    *,
    error_code: str,
    effective_template_id: str | None = None,
    effective_llm_prompt_key: str | None = None,
) -> ComposeResult:
    """Last-resort generic_tool_error template."""
    try:
        text = registry.render(
            _FALLBACK_GENERIC_ERROR, {"error_code": error_code},
        )
    except (UnknownTemplateError, TemplateError) as exc:
        logger.exception(
            "composer: fallback %s also failed: %s",
            _FALLBACK_GENERIC_ERROR, exc,
        )
        text = (
            "Что-то пошло не так с моей внутренней логикой. "
            "Попробуй ещё раз через минуту."
        )
    return ComposeResult(
        text=text,
        fallback_used="generic_error",
        error_code=error_code,
        effective_template_id=effective_template_id,
        effective_llm_prompt_key=effective_llm_prompt_key,
    )


def _execution_summary(execution_log: ExecutionLog) -> str:
    """Brief human-readable summary of what succeeded — used by
    ``partial_with_compose_error`` so the user sees what landed before
    the compose hiccup.

    D.1: tool name list (good enough for "don't lie to the user").
    D.3 (Task: future): richer per-tool natural-language summaries
    («добавила X, поставила Y») via a mapping or per-tool summary hook.
    """
    ok_tools = [step.tool for step in execution_log.steps if step.status == "ok"]
    if not ok_tools:
        return ""
    if len(ok_tools) == 1:
        return ok_tools[0]
    return ", ".join(ok_tools[:-1]) + " и " + ok_tools[-1]


__all__ = [
    "ComposeResult",
    "ComposerContext",
    "FallbackUsed",
    "LLMComposer",
    "compose",
]
