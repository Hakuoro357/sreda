"""R-39: главный orchestrator — pure-pipeline от user_text до final_text.

Собирает воедино все семь модулей (intent_classifier, natural_time_parser,
correction_resolver, planner, executor, first_line_renderer,
live_phrase_composer) + post_response_audit как L4.

Контракт интеграции (день 5 — pre-prod):

    PipelineResult = process_turn(
        user_text, conversation_history, turn_context,
        now_utc, tool_callables, detector,
        invoke_planner_llm, invoke_composer_llm,
        admin_alert_fn, correction_pending,
    )

Pipeline pure — все внешние зависимости (LLM, инструменты, БД,
детектор unbacked-claim, админ-канал) инжектятся как callables.
Реальная подключка в ``handlers.execute_conversation_chat`` — день 6+
после явного одобрения Бориса Аркадьевича (правило #3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from sreda.agents.contracts import (
    Clarification,
    ConversationTurn,
    ExecutionPlan,
    NoAction,
    TurnContext,
)
from sreda.agents.correction_resolver import resolve_correction_target
from sreda.agents.executor import execute_plan
from sreda.agents.journal import ToolJournal
from sreda.agents.live_phrase_composer import compose_live_phrase
from sreda.agents.planner import PlanRequest, plan_action
from sreda.agents.post_response_audit import AuditResult, audit_response
from sreda.services.first_line_renderer import render_journal
from sreda.services.natural_time_parser import parse_natural_time
from sreda.services.turn_intent_classifier import TurnIntent, classify_turn


logger = logging.getLogger(__name__)


# ─── Контракт результата ─────────────────────────────────────────────


@dataclass
class PipelineResult:
    """Итог обработки одного хода.

    ``final_text`` — то что отправляем пользователю. ``journal`` —
    что реально сделано (для записи в историю). ``audit`` — итог L4.
    ``halted_early`` — был ли fail-fast в executor (caller может
    показать «частично выполнено»). ``correction_pending`` — явная
    копия из ``audit.correction_pending`` для удобства caller'а
    (handlers.py инжектит в системное сообщение next turn'а).
    """

    final_text: str
    journal: ToolJournal
    audit: AuditResult
    halted_early: bool = False
    intent: TurnIntent | None = None
    plan_kind: str = ""  # "execution_plan" | "no_action" | "clarification"
    correction_pending: str | None = None  # дубль из audit для дискаверабельности
    trace: list[str] = field(default_factory=list)


# ─── Главная функция ──────────────────────────────────────────────────


PlannerLLMFn = Callable[[str, str], dict[str, Any] | None]
ComposerLLMFn = Callable[[str, str], str | None]
ToolCallable = Callable[..., Any]
DetectorFn = Callable[[str, set[str]], bool]
AdminAlertFn = Callable[[str], None]
ResultDataExtractor = Callable[[str, dict[str, Any], Any], dict[str, Any]]


def process_turn(
    *,
    user_text: str,
    conversation_history: tuple[ConversationTurn, ...],
    turn_context: TurnContext,
    now_utc: datetime,
    tool_callables: dict[str, ToolCallable],
    detector: DetectorFn,
    invoke_planner_llm: PlannerLLMFn | None = None,
    invoke_composer_llm: ComposerLLMFn | None = None,
    result_data_extractor: ResultDataExtractor | None = None,
    admin_alert_fn: AdminAlertFn | None = None,
    correction_pending: str | None = None,
    user_tz: str = "Europe/Moscow",
    correction_target_override: object | None = None,
) -> PipelineResult:
    """Полный R-39 pipeline: user_text → final_text.

    Возвращает PipelineResult с финальным текстом и журналом для записи
    в историю.
    """
    trace: list[str] = []

    # 1. Классификация намерения
    intent_result = classify_turn(user_text)
    trace.append(f"intent={intent_result.intent.value} confidence={intent_result.confidence:.2f}")

    # 2. Парсинг времени (если применимо)
    parser_result = None
    if intent_result.intent in (TurnIntent.MUTATION, TurnIntent.UNCERTAIN):
        parser_result = parse_natural_time(user_text, user_tz, now_utc)
        trace.append(f"parser={type(parser_result).__name__}")

    # 3. Разрешение коррекции (если применимо).
    # R-39 R7-2: caller (handlers _r39_try_live) может precompute target
    # через _resolve_correction_target_with_db_fallback и передать через
    # correction_target_override — для случаев когда journal пустой, но
    # в БД есть pending FamilyReminder который мы хотим найти.
    if correction_target_override is not None:
        correction_target = correction_target_override
        trace.append(f"correction={type(correction_target).__name__} (override)")
    elif intent_result.intent in (TurnIntent.MUTATION, TurnIntent.UNCERTAIN):
        correction_target = None
        if conversation_history:
            correction_target = resolve_correction_target(
                user_text, list(conversation_history)
            )
            trace.append(f"correction={type(correction_target).__name__}")
    else:
        correction_target = None

    # 4. Планировщик
    request = PlanRequest(
        user_text=user_text,
        intent=intent_result.intent,
        parser_result=parser_result,
        correction_target=correction_target,
        conversation_history=conversation_history,
        turn_context=turn_context,
    )
    decision = plan_action(request, invoke_llm=invoke_planner_llm)
    trace.append(f"plan_kind={type(decision).__name__}")

    # 5. Развилка: NoAction / Clarification / ExecutionPlan
    if isinstance(decision, NoAction):
        return _build_no_action_result(
            decision=decision,
            intent=intent_result.intent,
            trace=trace,
            detector=detector,
            admin_alert_fn=admin_alert_fn,
            turn_context=turn_context,
        )

    if isinstance(decision, Clarification):
        return _build_clarification_result(
            decision=decision,
            intent=intent_result.intent,
            trace=trace,
            detector=detector,
            admin_alert_fn=admin_alert_fn,
            turn_context=turn_context,
        )

    assert isinstance(decision, ExecutionPlan)

    # 5.5. Защита от пустого плана: LLM/планировщик вернули action,
    # но без calls. Без этого guard'а final_text будет пустой строкой.
    if decision.is_empty:
        trace.append("plan_empty → no_action_fallback")
        empty_no_action = NoAction(
            ack_message="",
            rationale="empty_execution_plan",
        )
        return _build_no_action_result(
            decision=empty_no_action,
            intent=intent_result.intent,
            trace=trace,
            detector=detector,
            admin_alert_fn=admin_alert_fn,
            turn_context=turn_context,
        )

    # 6. Executor
    exec_result = execute_plan(
        decision,
        tenant_id=turn_context.tenant_id,
        turn_id=turn_context.turn_id,
        tool_callables=tool_callables,
        result_data_extractor=result_data_extractor,
    )
    journal = exec_result.journal
    trace.append(
        f"executor halted={exec_result.halted_early} entries={len(journal)} "
        f"overall={journal.overall_kind.value}"
    )

    # 7. Рендер первой строки (детерминированно)
    first_line = render_journal(journal.entries, turn_context)
    trace.append(f"first_line_len={len(first_line)}")

    # 8. Композитор живой фразы (если есть LLM)
    live_phrase = ""
    if invoke_composer_llm is not None and first_line:
        composer_result = compose_live_phrase(
            user_text=user_text,
            first_line=first_line,
            journal_summary=_summarize_journal(journal),
            invoke_llm=invoke_composer_llm,
            admin_alert_fn=admin_alert_fn,
        )
        live_phrase = composer_result.phrase
        trace.append(f"composer phrase_len={len(live_phrase)}")

    # 9. Склейка final_text + correction prefix если есть pending
    final_text = _assemble_final_text(
        first_line=first_line,
        live_phrase=live_phrase,
        correction_pending=correction_pending,
    )

    # 10. L4 audit
    audit = audit_response(
        final_text=final_text,
        journal=journal,
        detector=detector,
        admin_alert_fn=admin_alert_fn,
        tenant_id=turn_context.tenant_id,
        turn_id=turn_context.turn_id,
    )
    trace.append(f"audit unbacked={audit.is_unbacked}")

    return PipelineResult(
        final_text=final_text,
        journal=journal,
        audit=audit,
        halted_early=exec_result.halted_early,
        intent=intent_result.intent,
        plan_kind="execution_plan",
        correction_pending=audit.correction_pending,
        trace=trace,
    )


# ─── Развилки NoAction / Clarification ────────────────────────────────


def _build_no_action_result(
    *,
    decision: NoAction,
    intent: TurnIntent,
    trace: list[str],
    detector: DetectorFn,
    admin_alert_fn: AdminAlertFn | None,
    turn_context: TurnContext,
) -> PipelineResult:
    final_text = decision.ack_message
    journal = ToolJournal()
    audit = audit_response(
        final_text=final_text,
        journal=journal,
        detector=detector,
        admin_alert_fn=admin_alert_fn,
        tenant_id=turn_context.tenant_id,
        turn_id=turn_context.turn_id,
    )
    return PipelineResult(
        final_text=final_text,
        journal=journal,
        audit=audit,
        intent=intent,
        plan_kind="no_action",
        correction_pending=audit.correction_pending,
        trace=trace,
    )


def _build_clarification_result(
    *,
    decision: Clarification,
    intent: TurnIntent,
    trace: list[str],
    detector: DetectorFn,
    admin_alert_fn: AdminAlertFn | None,
    turn_context: TurnContext,
) -> PipelineResult:
    final_text = decision.question
    journal = ToolJournal()
    audit = audit_response(
        final_text=final_text,
        journal=journal,
        detector=detector,
        admin_alert_fn=admin_alert_fn,
        tenant_id=turn_context.tenant_id,
        turn_id=turn_context.turn_id,
    )
    return PipelineResult(
        final_text=final_text,
        journal=journal,
        audit=audit,
        intent=intent,
        plan_kind="clarification",
        correction_pending=audit.correction_pending,
        trace=trace,
    )


# ─── Внутренние помощники ─────────────────────────────────────────────


def _summarize_journal(journal: ToolJournal) -> str:
    """Краткое описание журнала для контекста composer'а."""
    if journal.is_empty:
        return ""
    parts: list[str] = []
    for e in journal.entries:
        kind = e.result_kind.value
        parts.append(f"{e.tool_name}={kind}")
    return "; ".join(parts)


def _assemble_final_text(
    *,
    first_line: str,
    live_phrase: str,
    correction_pending: str | None,
) -> str:
    """Склеить ответ пользователю.

    Порядок (если есть всё):
      1. correction prefix (признание из L4 предыдущего хода)
      2. first_line (детерминированная)
      3. live_phrase (через перевод строки)
    """
    parts: list[str] = []
    if correction_pending:
        # correction_pending — это инструкция для LLM, не текст для
        # пользователя. В сыром виде НЕ отправляем; caller должен решать.
        # Здесь оставляем заметку для проверки в тестах.
        # На практике handlers.py при наличии correction_pending инжектит
        # его как system message в LLM-вход следующего хода, не в текст.
        pass
    if first_line:
        parts.append(first_line)
    if live_phrase:
        parts.append(live_phrase)
    return "\n".join(parts)
