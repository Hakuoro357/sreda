"""Planner orchestrator — single async entry point.

Sub-A12 Phase B.4. Wires together B.1 (prompt) + B.2 (LLM) + B.3
(compile) + Sub-A1 (schemas, validator, interpolation) + Sub-A7
(planner_executions persistence).

This is a **library function**, not a LangGraph node. Phase E will
wrap ``run()`` in a graph node when integration time comes; for now
the orchestrator is callable from anywhere (worker, eval script,
snapshot test).

Lifecycle (Codex R7 final design — committed-claim pattern)
============================================================

::

    1. caller calls run(ctx)
    2. insert_pending  — own session, COMMIT before LLM
    3. call_planner    — slow; no DB transaction held during LLM call
    4. mark_received   — own session, COMMIT
    5. parse JSON → Plan.model_validate
       failure → mark_invalid (+ retry with feedback OR final)
    6. validate_plan(plan, registry) → if violations → invalid path
    7. compile(plan, registry) → ExecutionPlan
    8. mark_valid      — own session, COMMIT
    9. return PlannerResult(success=True, plan, execution_plan, ...)

Retry policy
============

One retry on parse/validate failure with explicit error feedback in
the prompt suffix. After two failures: PlannerResult(success=False)
+ redacted admin alert.

Out of scope for Phase B.4 (deferred):

* Idempotency-key based replay (needs schema migration)
* Stale reconciler (post-MVP)
* GEPA gap recording — orchestrator writes a planner_gaps row in a
  later phase; for now we just return the failure structure.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import reprlib
from dataclasses import dataclass, field
from typing import Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session

from sreda.config.log_redaction import redact_secrets
from sreda.config.settings import Settings, get_settings
from sreda.runtime.planner.llm import (
    PlannerCallResult,
    PlannerProviderUnavailable,
    PlannerTimeoutError,
    call_planner,
)
from sreda.runtime.planner.persistence import (
    insert_pending,
    make_execution_id,
    mark_invalid,
    mark_received,
    mark_valid,
)
from sreda.runtime.planner.plan_compiler import (
    ExecutionPlan,
    PlanCompileError,
    compile as compile_plan,
)
from sreda.runtime.planner.few_shot_examples import render_few_shot_block
from sreda.runtime.planner.json_parse import parse_planner_json
from sreda.runtime.planner.prompt_builder import (
    NowMoment,
    ProfileSnapshot,
    PromptBudget,
    PromptBudgetExceeded,
    PromptBuildArgs,
    TurnSnapshot,
    VoiceMeta,
    build_prompt,
    MemorySnapshot,
)
from sreda.runtime.planner.schemas import Plan
from sreda.runtime.planner.validator import render_violations, validate_plan
from sreda.services.tool_schemas.base import ToolSpec


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context + Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerContext:
    """Everything orchestrator needs to produce a Plan. Immutable so the
    caller can be sure orchestration doesn't mutate inputs."""

    # Identity
    tenant_id: str
    run_id: str
    feature_key: str
    user_message: str
    voice_meta: VoiceMeta | None

    # Prompt inputs
    now: NowMoment
    profile: ProfileSnapshot
    memories: tuple[MemorySnapshot, ...]
    active_turn: TurnSnapshot | None
    closed_turns: tuple[TurnSnapshot, ...]

    # Registry handles (compiler needs ToolSpec map; prompt needs
    # template_ids + llm_prompt_keys allowlists)
    available_tools: tuple[ToolSpec, ...]
    composer_template_ids: tuple[str, ...]
    composer_llm_prompt_keys: tuple[str, ...]
    composer_registry_snapshot_hash: str
    tool_registry_version: str = "v1"

    # Prompt assembly.
    #
    # ``few_shot_block`` is an OPT-IN TOGGLE, not a verbatim payload
    # (Codex rot-enablement #88 Phase-2 R2 MAJOR-1). The orchestrator does
    # NOT trust a pre-rendered string for the LLM-key gate — a caller that
    # rendered examples before ``effective_llm_keys`` was known could leak a
    # ``kind='llm'`` example for a disabled key, and the validator would then
    # reject greetings as ``unknown_llm_prompt_key``. Instead:
    #   * empty/falsy  → no few-shot examples in the prompt;
    #   * non-empty    → the orchestrator renders the canonical few-shot
    #     block ITSELF, gated to the computed ``effective_llm_keys`` (see
    #     ``_build_prompt_or_raise``). The string's content is ignored; only
    #     its truthiness matters. This guarantees the gate is always enforced
    #     centrally, regardless of what the caller passed.
    few_shot_block: str = ""
    budget: PromptBudget = field(default_factory=PromptBudget)

    # LLM call shape (overrides — usually None → use settings)
    planner_provider: str | None = None
    planner_timeout_seconds: float | None = None
    planner_prompt_version: int = 1


@dataclass(frozen=True)
class PlannerResult:
    """Outcome of one orchestrator run. Caller maps to executor invocation
    (success path) or composer fallback (failure path)."""

    success: bool
    execution_id: str
    final_attempt_no: int

    # Success-path payload
    plan: Plan | None = None
    execution_plan: ExecutionPlan | None = None

    # Failure-path metadata
    error_summary: str | None = None
    raw_responses: tuple[str, ...] = ()

    # F-1 (#151): summed planner LLM token usage across attempts (success path;
    # 0 on failure paths) + the effective provider/model, so
    # run_planner_chat_loop records an accurate skill_ai_executions row.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# PII redaction for admin alerts
# ---------------------------------------------------------------------------


# Codex Sub-A12 B.4 R1 MEDIUM: tighten regexes for real-world inputs.
# Phone: optional country code, optional spaces / dashes / parens.
# Email: dotted local with +/-/_, multi-label TLD (e.g. .co.uk).
_PHONE_RE = re.compile(
    r"\+?[78][\s\-()]*(?:\d[\s\-()]*){10}"
)
_USERNAME_RE = re.compile(r"@\w{5,}")
_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)


def _redact_for_alert(text_value: str, *, max_chars: int = 500) -> str:
    """Codex Sub-A12 R3 #9: redact PII before sending to admin channel.

    Order: email first (more specific), then phone, then username, then
    raw numbers (catches chat_ids). After redaction, hard-cap length."""
    out = _EMAIL_RE.sub("[EMAIL]", text_value)
    out = _PHONE_RE.sub("[PHONE]", out)
    out = _USERNAME_RE.sub("[USERNAME]", out)
    out = _NUMBER_RE.sub("[NUMBER]", out)
    return out[:max_chars]


# ---------------------------------------------------------------------------
# Retry feedback prompt
# ---------------------------------------------------------------------------


_FEEDBACK_PRIORITY_CODES: frozenset[str] = frozenset({
    # #114: нарушения, чьи тексты несут корректирующий словарь — модель может
    # исправиться, только если эта подсказка ДОЕЗЖАЕТ до повтора. Без
    # приоритизации срезы (первые 5 нарушений + 500 знаков) глотали её за
    # многословным шумом (ошибки типов и т.п.) — ход #75 прогона r3.
    "branch_match_status_invented",   # «... Allowed: [настоящие статусы]»
    "compose_ref_unknown_field",      # «... Available top-level fields: [...]»
    "composer_contract_invalid",      # «... Bind a literal or a ${sN.field} ref»
})


def _prioritize_for_feedback(violations: list) -> list:
    """Нарушения с корректирующей подсказкой — вперёд (стабильно), ДО срезов.

    Семантика валидации не меняется (план отвергается тем же набором) —
    меняется только порядок в тексте обратной связи повтора."""
    return sorted(violations, key=lambda v: v.code not in _FEEDBACK_PRIORITY_CODES)


def _build_retry_feedback(previous_response: str, errors: str) -> str:
    """Short error feedback appended to the user message for retry.

    Kept short so retry input doesn't bloat the prompt budget."""
    excerpt = previous_response[:500]
    err_excerpt = errors[:500]
    return (
        f"\n\n[АВТОМАТИЧЕСКИЙ РЕТРАЙ]\n"
        f"Предыдущий ответ невалиден. Извлечение:\n"
        f"<<<\n{excerpt}\n>>>\n"
        f"Ошибки:\n{err_excerpt}\n"
        f"Сгенерируй новый план в строгом JSON-формате, без markdown."
    )


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


# Bounded repr for plan args — caps strings/containers WHILE walking, so a
# pathological args payload never materializes its full repr before truncation
# (Codex obs-review R2). Each string is redacted BEFORE it is shortened (R3) so a
# secret near a reprlib cut boundary can't leak as a fragment the log-layer regex
# would miss. Structural limits below still bound the walk.
class _RedactingRepr(reprlib.Repr):
    def repr_str(self, x, level):  # type: ignore[override]
        return super().repr_str(redact_secrets(x), level)


_ARG_REPR = _RedactingRepr()
_ARG_REPR.maxstring = 200
_ARG_REPR.maxother = 200
_ARG_REPR.maxlist = 12
_ARG_REPR.maxdict = 12
_ARG_REPR.maxlevel = 5


def _cap_for_log(text: str, limit: int = 1500) -> str:
    """Make a value safe for a single observability log line: redact known
    secrets FIRST (so a bot-token can't survive truncation as a partial),
    collapse all whitespace/newlines to single spaces, then cap with an
    explicit marker. NB: redaction (#95) currently covers Telegram bot-tokens
    only — other free-text user content is logged as-is (intentional for the
    internal planner-enabled tenant set)."""
    red = " ".join(redact_secrets(text or "").split())
    if len(red) <= limit:
        return red
    return red[:limit] + f"…[+{len(red) - limit}c]"


def _summarize_plan(plan: Plan) -> str:
    """Compact one-line plan summary for observability logging: clarity +
    action count + each action (tool + args) + the composer target. Budgeted
    PER ACTION (a huge args payload can't blow up the line or the build cost),
    secrets redacted, newlines collapsed. clarity/count/compose live in the
    head so truncation never drops them."""
    comp = plan.compose
    target = comp.template_id or comp.llm_prompt_key or "?"
    head = f"clarity={plan.clarity} actions={len(plan.actions)} compose={target}"
    parts = [
        _cap_for_log(f"{sid}={a.tool}({_ARG_REPR.repr(a.args)})", 300)
        for sid, a in plan.actions.items()
    ]
    body = "; ".join(parts) if parts else "(none)"
    return _cap_for_log(f"{head} [{body}]", 1800)


async def run(
    ctx: PlannerContext,
    *,
    session_factory: Callable[[], Session] | None,
    invalid_retry_enabled: bool = True,
    call_planner_fn: Callable[..., PlannerCallResult] = call_planner,
    admin_alert_fn: Callable[..., None] | None = None,
    settings_factory: Callable[[], Settings] = get_settings,
) -> PlannerResult:
    """Drive one planner turn end-to-end.

    Parameters
    ----------
    ctx :
        Immutable input snapshot (profile, memories, history, message,
        registry handles).
    session_factory :
        Callable returning a fresh SQLAlchemy Session per stage, OR
        ``None`` for **ephemeral mode** (Codex B.7 R1 MAJOR fix):
        skip all DB writes. Used by the live eval script which doesn't
        have a real ``agent_runs`` row to FK against; in ephemeral mode
        the orchestrator still returns a typed PlannerResult so callers
        can aggregate metrics without per-scenario persistence overhead.

        When set: each stage (insert_pending, mark_received,
        mark_valid/invalid) opens its OWN session and COMMITs before
        returning — no transaction is held across the LLM call (Codex
        R7 design).
    invalid_retry_enabled :
        Toggle for the one-shot retry on parse/validate failure. Off
        in tests that need to assert single-attempt behavior.
    call_planner_fn :
        Injectable for tests. Default uses runtime.planner.llm.call_planner.
    admin_alert_fn :
        Injectable redacted-alert hook. None → no alerts (offline mode,
        eval scripts, etc.).

    Returns
    -------
    PlannerResult :
        success=True on valid plan; success=False on retry exhaustion
        or unrecoverable failure (provider unavailable, etc.).
    """
    execution_id = make_execution_id()
    # getattr-default so lightweight test stubs (SimpleNamespace settings)
    # don't need the field; prod Settings always has it (default True).
    _verbose_log = getattr(settings_factory(), "planner_verbose_log", True)

    # Sub-A12 D.2-enable R2 (Codex HIGH) — NON-BYPASSABLE composer-LLM
    # gate. Effective LLM keys = caller-proposed ∩ registry ∩
    # settings-enabled. Computed centrally HERE (not at the caller) so a
    # Phase E / prod caller that passes registry keys cannot bypass the
    # kill-switch, and stale keys absent from the registry are dropped
    # (Codex R2-medium MAJOR #1). Empty settings allow-list → () →
    # planner is shown no LLM key AND validate_plan rejects any
    # kind='llm'. Used for BOTH the prompt and validation below.
    from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
    _registry_llm_keys = set(LLM_PROMPT_REGISTRY.prompt_keys())
    _enabled_llm_keys = settings_factory().composer_llm_enabled_keys
    effective_llm_keys = tuple(sorted(
        set(ctx.composer_llm_prompt_keys)
        & _registry_llm_keys
        & _enabled_llm_keys
    ))
    effective_llm_required = {
        k: LLM_PROMPT_REGISTRY.get(k).required_keys for k in effective_llm_keys
    }

    # Stage 1: persist 'pending' (own session, COMMIT immediately).
    # Ephemeral mode (session_factory=None) skips DB writes — used by
    # live eval script which lacks a real agent_runs FK target.
    try:
        if session_factory is not None:
            with session_factory() as s:
                with s.begin():
                    insert_pending(
                        s,
                        execution_id=execution_id,
                        run_id=ctx.run_id,
                        tenant_id=ctx.tenant_id,
                        feature_key=ctx.feature_key,
                        planner_prompt_version=ctx.planner_prompt_version,
                        planner_provider=ctx.planner_provider or "",
                        planner_model="",  # mark_received writes actual
                        tool_registry_version=ctx.tool_registry_version,
                        composer_registry_snapshot_hash=(
                            ctx.composer_registry_snapshot_hash
                        ),
                    )
    except Exception as exc:  # noqa: BLE001 — persistence failures are operational
        logger.exception("planner orchestrator: insert_pending failed")
        err_msg = f"persistence:insert_pending:{type(exc).__name__}: {exc}"
        # Codex Sub-A12 B.4 R2 MEDIUM: every terminal failure produces
        # an admin alert (when alert_fn provided), not just LLM/invalid
        # paths. Persistence outages need to surface too.
        _maybe_alert(admin_alert_fn, execution_id, ctx, errors=err_msg)
        return PlannerResult(
            success=False,
            execution_id=execution_id,
            final_attempt_no=0,
            error_summary=f"persistence:insert_pending:{type(exc).__name__}",
        )

    cumulative_latency_ms = 0
    raw_responses: list[str] = []
    # F-1 (#151): sum planner token usage across attempts for cost recording.
    planner_prompt_tokens = 0
    planner_completion_tokens = 0
    last_errors = ""
    last_raw = ""

    max_attempts = 2 if invalid_retry_enabled else 1
    for attempt_no in range(1, max_attempts + 1):
        # Codex Sub-A12 R1 MAJOR (HIGH): retry feedback goes THROUGH
        # prompt_builder so the previous raw response is fenced as
        # UNTRUSTED_DATA and the total still respects PromptBudget.
        retry_feedback = None
        if attempt_no > 1:
            retry_feedback = _build_retry_feedback(last_raw, last_errors)
        try:
            prompt = _build_prompt_or_raise(
                ctx, retry_feedback=retry_feedback,
                composer_llm_prompt_keys=effective_llm_keys,
            )
        except PromptBudgetExceeded as exc:
            _terminate_invalid(
                session_factory, execution_id,
                errors=f"prompt_budget_exceeded: {exc}",
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx,
                errors=f"prompt_budget_exceeded: {exc}",
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no - 1,
                error_summary="prompt_budget_exceeded",
                raw_responses=tuple(raw_responses),
            )

        # Stage 2: call LLM (no DB transaction held).
        # Codex Sub-A12 R1 MAJOR (medium): call_planner is sync — wrap in
        # asyncio.to_thread so the event loop isn't blocked for 30-60s
        # while the LLM responds. If caller provides an async test stub,
        # await it directly.
        try:
            kwargs = dict(
                provider=ctx.planner_provider,
                timeout_seconds=ctx.planner_timeout_seconds,
                attempt_no=attempt_no,
            )
            if inspect.iscoroutinefunction(call_planner_fn):
                call_result = await call_planner_fn(prompt, **kwargs)
            else:
                call_result = await asyncio.to_thread(
                    call_planner_fn, prompt, **kwargs,
                )
        except PlannerProviderUnavailable as exc:
            if _verbose_log:
                logger.info(
                    "planner.call_error exec=%s attempt=%d kind=provider_unavailable %s",
                    execution_id, attempt_no, _cap_for_log(str(exc)),
                )
            _terminate_invalid(
                session_factory, execution_id,
                errors=f"provider_unavailable: {exc}",
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx,
                errors=f"provider_unavailable: {exc}",
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary="provider_unavailable",
                raw_responses=tuple(raw_responses),
            )
        except PlannerTimeoutError as exc:
            if _verbose_log:
                logger.info(
                    "planner.call_error exec=%s attempt=%d kind=timeout %s",
                    execution_id, attempt_no, _cap_for_log(str(exc)),
                )
            _terminate_invalid(
                session_factory, execution_id,
                errors=f"timeout: {exc}",
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx, errors=f"timeout: {exc}",
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary="timeout",
                raw_responses=tuple(raw_responses),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "planner orchestrator: LLM call failed attempt=%d", attempt_no,
            )
            err_msg = f"llm_call:{type(exc).__name__}: {exc}"
            _terminate_invalid(
                session_factory, execution_id,
                errors=err_msg,
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx, errors=err_msg,
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary=f"llm_call:{type(exc).__name__}",
                raw_responses=tuple(raw_responses),
            )

        cumulative_latency_ms += call_result.latency_ms
        raw_responses.append(call_result.raw_text)
        planner_prompt_tokens += call_result.prompt_tokens
        planner_completion_tokens += call_result.completion_tokens
        last_raw = call_result.raw_text
        if _verbose_log:
            logger.info(
                "planner.call exec=%s attempt=%d provider=%s model=%s "
                "latency_ms=%d chars=%d",
                execution_id, attempt_no, call_result.provider,
                call_result.model, call_result.latency_ms,
                len(call_result.raw_text or ""),
            )

        # Stage 3: persist 'received' (own session) + audit provider/model
        try:
            if session_factory is not None:
                with session_factory() as s:
                    with s.begin():
                        mark_received(
                            s,
                            execution_id=execution_id,
                            raw_response=call_result.raw_text,
                            latency_ms=cumulative_latency_ms,
                            planner_provider=call_result.provider,
                            planner_model=call_result.model,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.exception("planner orchestrator: mark_received failed")
            err_msg = f"persistence:mark_received:{type(exc).__name__}: {exc}"
            _maybe_alert(admin_alert_fn, execution_id, ctx, errors=err_msg)
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary=f"persistence:mark_received:{type(exc).__name__}",
                raw_responses=tuple(raw_responses),
            )

        # Stage 4: parse JSON (strict envelope-strip — tolerates a single outer
        # ```json fence the planner sometimes adds despite the prompt; #113).
        try:
            payload = parse_planner_json(call_result.raw_text)
        except json.JSONDecodeError as exc:
            last_errors = f"json_decode_error: {exc}"
            if _verbose_log:
                logger.info("planner.invalid exec=%s attempt=%d %s",
                            execution_id, attempt_no, _cap_for_log(last_errors))
            if attempt_no < max_attempts:
                continue
            _terminate_invalid(
                session_factory, execution_id, errors=last_errors,
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx, errors=last_errors,
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary="invalid_plan_after_retry",
                raw_responses=tuple(raw_responses),
            )

        # Stage 5: pydantic validate
        try:
            plan = Plan.model_validate(payload)
        except ValidationError as exc:
            last_errors = f"plan_schema_error: {exc.errors(include_input=False)}"
            if _verbose_log:
                logger.info("planner.invalid exec=%s attempt=%d %s",
                            execution_id, attempt_no, _cap_for_log(last_errors))
            if attempt_no < max_attempts:
                continue
            _terminate_invalid(
                session_factory, execution_id, errors=last_errors,
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx, errors=last_errors,
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary="invalid_plan_after_retry",
                raw_responses=tuple(raw_responses),
            )

        # Stage 6: validator (registry-aware semantic checks)
        # Sub-A12 Phase D.2-enable — pass composer allowlists so an
        # unknown template_id / llm_prompt_key OR a kind='llm' compose
        # missing required template_data is caught here (before any tool
        # runs) rather than as a late compose-time fallback. Uses the
        # GATED ``effective_llm_keys`` (computed once at run() start), so
        # a key the caller proposed but settings didn't enable → not in
        # the allowlist → any kind='llm' using it is rejected here.
        registry_map = {s.name: s for s in ctx.available_tools}
        violations = validate_plan(
            plan,
            registry=registry_map,
            composer_template_ids=frozenset(ctx.composer_template_ids),
            composer_llm_prompt_keys=frozenset(effective_llm_keys),
            llm_prompt_required_keys=effective_llm_required,
        )
        if violations:
            last_errors = "validator_violations: " + "; ".join(
                render_violations(_prioritize_for_feedback(violations))[:5]
            )
            if _verbose_log:
                logger.info("planner.invalid exec=%s attempt=%d violations=%d %s",
                            execution_id, attempt_no, len(violations),
                            _cap_for_log(last_errors))
            if attempt_no < max_attempts:
                continue
            _terminate_invalid(
                session_factory, execution_id, errors=last_errors,
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx, errors=last_errors,
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary="invalid_plan_after_retry",
                raw_responses=tuple(raw_responses),
            )

        # Stage 7: compile to ExecutionPlan
        try:
            execution_plan = compile_plan(plan, registry_map)
        except PlanCompileError as exc:
            last_errors = f"plan_compile_error: {exc}"
            if _verbose_log:
                logger.info("planner.invalid exec=%s attempt=%d %s",
                            execution_id, attempt_no, _cap_for_log(last_errors))
            if attempt_no < max_attempts:
                continue
            _terminate_invalid(
                session_factory, execution_id, errors=last_errors,
            )
            _maybe_alert(
                admin_alert_fn, execution_id, ctx, errors=last_errors,
            )
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary="invalid_plan_after_retry",
                raw_responses=tuple(raw_responses),
            )

        # Stage 8: persist 'valid'
        try:
            if session_factory is not None:
                with session_factory() as s:
                    with s.begin():
                        mark_valid(
                            s,
                            execution_id=execution_id,
                            plan_json=plan.model_dump(mode="json"),
                            execution_plan_json=_execution_plan_to_json(execution_plan),
                            is_new_turn=plan.turn_classification.is_new_turn,
                            turn_classification_reason=plan.turn_classification.reason,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.exception("planner orchestrator: mark_valid failed")
            err_msg = f"persistence:mark_valid:{type(exc).__name__}: {exc}"
            _maybe_alert(admin_alert_fn, execution_id, ctx, errors=err_msg)
            return PlannerResult(
                success=False, execution_id=execution_id,
                final_attempt_no=attempt_no,
                error_summary=f"persistence:mark_valid:{type(exc).__name__}",
                raw_responses=tuple(raw_responses),
            )

        if _verbose_log:
            logger.info(
                "planner.valid exec=%s attempt=%d model=%s %s",
                execution_id, attempt_no, call_result.model,
                _summarize_plan(plan),
            )
        return PlannerResult(
            success=True,
            execution_id=execution_id,
            final_attempt_no=attempt_no,
            plan=plan,
            execution_plan=execution_plan,
            raw_responses=tuple(raw_responses),
            prompt_tokens=planner_prompt_tokens,
            completion_tokens=planner_completion_tokens,
            provider=call_result.provider,
            model=call_result.model,
        )

    # Loop fell through — shouldn't happen
    return PlannerResult(
        success=False, execution_id=execution_id,
        final_attempt_no=max_attempts,
        error_summary="unexpected_loop_exit",
        raw_responses=tuple(raw_responses),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_prompt_or_raise(
    ctx: PlannerContext, *, retry_feedback: str | None,
    composer_llm_prompt_keys: tuple[str, ...],
) -> str:
    """Render base prompt via prompt_builder.build_prompt.

    ``composer_llm_prompt_keys`` is the GATED effective set (Sub-A12
    D.2-enable R2) — NOT ``ctx.composer_llm_prompt_keys`` — so the
    planner is only ever shown keys that settings actually enabled.
    Empty → the LLM-key registry block renders "пусто, не используй".

    Few-shot gating (Codex rot-enablement #88 Phase-2 R2 MAJOR-1): the
    orchestrator renders the canonical few-shot block ITSELF here, gated to
    the same effective ``composer_llm_prompt_keys``, instead of trusting
    ``ctx.few_shot_block`` verbatim. ``ctx.few_shot_block`` is an opt-in
    toggle (truthy → include examples; falsy → none); its content is not
    used. This guarantees no ``kind='llm'`` example for a disabled key ever
    reaches the prompt, regardless of how the caller built the context."""
    few_shot_block = (
        render_few_shot_block(effective_llm_keys=composer_llm_prompt_keys)
        if ctx.few_shot_block
        else ""
    )
    args = PromptBuildArgs(
        tool_specs=ctx.available_tools,
        composer_template_ids=ctx.composer_template_ids,
        composer_llm_prompt_keys=composer_llm_prompt_keys,
        few_shot_block=few_shot_block,
        profile=ctx.profile,
        memories=ctx.memories,
        active_turn=ctx.active_turn,
        closed_turns=ctx.closed_turns,
        now=ctx.now,
        user_message=ctx.user_message + (retry_feedback or ""),
        voice_meta=ctx.voice_meta,
        budget=ctx.budget,
    )
    return build_prompt(args)


def _terminate_invalid(
    session_factory: Callable[[], Session] | None,
    execution_id: str,
    *,
    errors: str,
) -> None:
    """Best-effort persist invalid status. Failure here is logged but
    doesn't override the original failure mode. Ephemeral mode
    (session_factory=None) skips DB writes."""
    if session_factory is None:
        return
    try:
        with session_factory() as s:
            with s.begin():
                mark_invalid(
                    s, execution_id=execution_id,
                    validation_errors=errors[:2000],
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "planner orchestrator: failed to mark_invalid for %s", execution_id,
        )


def _maybe_alert(
    alert_fn: Callable[..., None] | None,
    execution_id: str,
    ctx: PlannerContext,
    *,
    errors: str,
) -> None:
    """Send P1 admin alert with PII redaction. No-op if alert_fn is None."""
    if alert_fn is None:
        return
    redacted_user = _redact_for_alert(ctx.user_message, max_chars=200)
    redacted_errors = _redact_for_alert(errors, max_chars=500)
    try:
        alert_fn(
            severity="P1",
            title="planner: invalid plan after retry",
            body=(
                f"execution_id={execution_id}\n"
                f"tenant_id=[REDACTED]\n"
                f"user excerpt: {redacted_user}\n"
                f"errors: {redacted_errors}\n"
                f"trace: see #68 LLM traces by execution_id"
            ),
            dedupe_key=f"planner_invalid:{execution_id[:16]}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("planner orchestrator: admin alert failed")


def _execution_plan_to_json(ep: ExecutionPlan) -> dict:
    """ExecutionPlan dataclass → JSON-serialisable dict for storage.

    Layers as list-of-lists; fail_modes as plain dict. Don't include the
    embedded Plan (already stored in plan_json column separately)."""
    return {
        "layers": [list(layer) for layer in ep.layers],
        "fail_modes": dict(ep.fail_modes),
    }


__all__ = [
    "PlannerContext",
    "PlannerResult",
    "run",
]
