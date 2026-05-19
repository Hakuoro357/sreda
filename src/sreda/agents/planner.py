"""R-39: планировщик — решение что делать в ответ на ход пользователя.

Контракт планировщика трёхтипный: ``ExecutionPlan`` (вызвать
инструменты), ``NoAction`` (болтовня — реплика без инструментов),
``Clarification`` (уточнение у пользователя).

Архитектурный выбор: планировщик НЕ ходит к LLM «слепо». До LLM-вызова
он применяет детерминированные short-circuit'ы:

1. **CHITCHAT** → ``NoAction`` без LLM (экономия одного вызова).
2. **ResolvedCorrection** → ``ExecutionPlan(replace_reminder)`` —
   target известен из conversation_history, parser дал новое время.
3. **AmbiguousCorrection** → ``Clarification`` (несколько активных
   напоминаний, спрашиваем какое).
4. **TimeAmbiguous** → ``Clarification`` («на 2 часа» = через 2ч или
   в 14:00?).
5. **TimeInvalid** → ``Clarification`` (вчера / out_of_range).
6. Иначе — LLM-вызов с типизированным выводом. Результат проходит
   защитную подмену: если parser нашёл ``TimeResolved``, мы перезаписываем
   ``trigger_iso`` от LLM на детерминированный. Это закрывает класс
   багов когда LLM «округлила» время.

Реальный LLM-клиент инжектится через ``invoke_llm``. В day 5 prod
интеграция подключит ``get_chat_llm`` с json_schema выводом.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from sreda.agents.contracts import (
    Clarification,
    ConversationTurn,
    ExecutionPlan,
    NoAction,
    ToolCall,
    TurnContext,
)
from sreda.agents.correction_resolver import (
    AmbiguousCorrection,
    NoCorrectionTarget,
    ResolvedCorrection,
)
from sreda.services.natural_time_parser import (
    ParseResult,
    TimeAmbiguous,
    TimeInvalid,
    TimeResolved,
    TimeUnrecognized,
)
from sreda.services.turn_intent_classifier import TurnIntent


logger = logging.getLogger(__name__)


# ─── Запрос к планировщику ────────────────────────────────────────────


@dataclass(frozen=True)
class PlanRequest:
    """Входные данные планировщика.

    Все детерминированные предобработки (intent, parser_result,
    correction_target) уже произведены — планировщик их использует
    для short-circuit'ов и для подмены полей в LLM-ответе.
    """

    user_text: str
    intent: TurnIntent
    parser_result: ParseResult | None
    correction_target: object  # ResolverResult | None
    conversation_history: tuple[ConversationTurn, ...]
    turn_context: TurnContext


# ─── Главная функция ──────────────────────────────────────────────────


PlannerOutput = ExecutionPlan | NoAction | Clarification

InvokeLLMFn = Callable[[str, str], dict[str, Any] | None]


def plan_action(
    request: PlanRequest,
    *,
    invoke_llm: InvokeLLMFn | None = None,
) -> PlannerOutput:
    """Принять решение о действии для текущего хода.

    Args:
        request: вход с уже-вычисленными intent/parser/correction.
        invoke_llm: callable LLM-вызова. Возвращает dict с разобранным
            json_schema выводом или ``None`` при сбое.

    Returns:
        ExecutionPlan | NoAction | Clarification.
    """
    # Pre-decision trace — какой вход получил planner.
    # NB: для TG long-poll R-39 logs идут в /var/log/sreda/telegram-poller.log
    # (не uvicorn.log) — это process boundary, sreda-telegram-poller
    # service. Для MAX webhook — в uvicorn.log.
    parser_type = (
        type(request.parser_result).__name__
        if request.parser_result is not None else "None"
    )
    target_type = (
        type(request.correction_target).__name__
        if request.correction_target is not None else "None"
    )
    logger.info(
        "planner.entry: intent=%s parser=%s target=%s text_snip=%r",
        request.intent.name if hasattr(request.intent, "name") else str(request.intent),
        parser_type, target_type, (request.user_text or "")[:80],
    )

    # 1. Чистая болтовня → no-op без LLM
    if request.intent is TurnIntent.CHITCHAT:
        logger.info("planner.decision: chitchat_short_circuit → NoAction")
        return NoAction(
            ack_message="",
            rationale="chitchat_short_circuit",
        )

    # 2. Однозначная коррекция → replace_reminder без LLM
    target = request.correction_target
    if isinstance(target, ResolvedCorrection):
        plan = _build_update_plan(request, target)
        if plan is not None:
            logger.info(
                "planner.decision: ResolvedCorrection → ExecutionPlan(update_reminder) "
                "target=%s parser=%s",
                target.target_entity_id, parser_type,
            )
            return plan
        # Если не получилось собрать (нет parser_result) — падаем на LLM ниже.
        logger.info(
            "planner.decision: ResolvedCorrection но _build_update_plan=None "
            "(parser=%s не TimeResolved) → fall through на LLM",
            parser_type,
        )

    # 3. Несколько кандидатов на коррекцию → уточнение
    if isinstance(target, AmbiguousCorrection):
        logger.info("planner.decision: AmbiguousCorrection → Clarification(disambig)")
        return _build_disambiguation_question(target)

    # 4. Двусмысленное время → уточнение
    if isinstance(request.parser_result, TimeAmbiguous):
        logger.info("planner.decision: TimeAmbiguous → Clarification(parser_ambiguous)")
        return Clarification(
            question="Уточни, пожалуйста: ты имеешь в виду через 2 часа или в 14:00?",
            rationale="parser_ambiguous",
        )

    # 5. Недопустимое время → уточнение
    if isinstance(request.parser_result, TimeInvalid):
        logger.info(
            "planner.decision: TimeInvalid reason=%s → Clarification(parser_invalid)",
            request.parser_result.reason,
        )
        reason_text = {
            "past_date": "Это время уже прошло. На какое поставить?",
            "out_of_range": "Не разобрала время — можешь сказать иначе? "
                            "Например «в 14:00» или «завтра в 9 утра».",
        }.get(request.parser_result.reason, "Не разобрала время.")
        return Clarification(question=reason_text, rationale="parser_invalid")

    # 6. Иначе — LLM
    if invoke_llm is None:
        # Тестовый режим / нет LLM в окружении — ack + лог
        logger.info(
            "planner.decision: no_llm_available → NoAction "
            "(invoke_llm callable не передан)",
        )
        return NoAction(ack_message="", rationale="no_llm_available")

    logger.info("planner.decision: invoking LLM (no short-circuit matched)")
    try:
        raw = invoke_llm(_PLANNER_SYSTEM_PROMPT, _build_user_prompt(request))
    except Exception as exc:  # noqa: BLE001 — таймаут/сеть/JSON parse
        logger.warning(
            "planner: invoke_llm бросил %s — NoAction fallback",
            type(exc).__name__,
        )
        return NoAction(ack_message="", rationale="llm_exception")

    if raw is None:
        logger.info("planner: invoke_llm вернул None → NoAction(llm_returned_none)")
        return NoAction(ack_message="", rationale="llm_returned_none")

    result = _parse_llm_output(raw, request)
    logger.info("planner.decision: LLM returned → %s", type(result).__name__)
    return result


# ─── Short-circuit построители ────────────────────────────────────────


def _build_update_plan(
    request: PlanRequest,
    target: ResolvedCorrection,
) -> ExecutionPlan | None:
    """Собрать update_reminder план из target + parser_result.

    R-39 R4: используем in-place update вместо cancel+create — проще,
    атомарен на стороне FamilyReminder service, сохраняет id.
    Возвращает None если parser_result не дал ``TimeResolved`` (тогда
    нужен LLM-call для извлечения title/времени).
    """
    if not isinstance(request.parser_result, TimeResolved):
        return None

    # Новое название берём из user_text по эвристике (последовательность
    # «разбудить Катю», «купить хлеб» и т.п.). Если не удалось — берём
    # старое из target.
    new_title = _extract_title_hint(request.user_text) or target.target_title or ""
    trigger_iso = request.parser_result.iso_user_tz.isoformat()

    call = ToolCall(
        tool_name="update_reminder",
        args={
            "reminder_id": target.target_entity_id,
            "title": new_title,
            "trigger_iso": trigger_iso,
        },
        action_index=0,
    )
    return ExecutionPlan(calls=(call,))


def _build_disambiguation_question(target: AmbiguousCorrection) -> Clarification:
    """Сформировать короткое уточнение для двусмысленной коррекции."""
    titles = [c.target_title or c.target_entity_id for c in target.candidates[:3]]
    listing = ", ".join(f"«{t}»" for t in titles)
    return Clarification(
        question=f"У тебя несколько активных напоминаний — какое поправить? {listing}",
        rationale=target.reason,
    )


# ─── LLM-обвязка ──────────────────────────────────────────────────────


_PLANNER_SYSTEM_PROMPT = (
    "Ты планировщик: получаешь сообщение пользователя и решаешь "
    "ОДНО из трёх:\n"
    "1) action — вызвать инструменты (вернуть calls=[{tool,args}]).\n"
    "2) no_action — короткая реплика без инструментов.\n"
    "3) clarification — задать пользователю один уточняющий вопрос.\n\n"
    "Возвращай JSON со схемой:\n"
    '{"kind":"action|no_action|clarification",'
    '"calls":[{"tool":"schedule_reminder","args":{...}}]|null,'
    '"ack_message":"..."|null,'
    '"question":"..."|null}'
)


# ─── Tool-name whitelist + aliases (P0.A, 2026-05-19) ─────────────────
#
# Bench R-39 planner на 6 моделях (plans/r39-planner-bench-2026-05-19.md)
# показал 100% hallucination rate на «list_request» сценарии — каждая
# модель назвала tool по-своему («read_reminders», «get_reminders»).
# А на «hallucination_trap_weather» 3/6 моделей выдумали `get_weather`.
# Без whitelist hallucinated tool_name → executor unknown_tool → user
# видит «технические сбои».
#
# Whitelist строится из real registry (housewife_chat_tools.py +
# runtime/tools.py memory tools). Aliases — soft-correction для очевидных
# синонимов которые LLM эмитят. Любой tool вне whitelist+aliases →
# отбрасывается с warning.
KNOWN_TOOLS: frozenset[str] = frozenset({
    # Reminders
    "schedule_reminder", "cancel_reminder", "update_reminder",
    "replace_reminder", "list_reminders",
    # Shopping
    "add_shopping_items", "list_shopping", "remove_shopping_items",
    "clear_shopping",
    # Recipes / Menu
    "save_recipe", "list_recipes", "delete_recipe", "search_recipes",
    "plan_week_menu", "list_menu", "delete_menu_item",
    # Tasks
    "add_task", "list_tasks", "complete_task", "delete_task",
    # Memory
    "save_core_fact", "save_episode", "recall_memory",
    # Web
    "web_search", "fetch_url",
    # Family
    "list_family_members", "add_family_member", "remove_family_member",
    "update_family_member",
    # Profile
    "update_profile_field",
    # Misc
    "log_unsupported_request",
    # R-39 specific
    "reply_with_buttons",
})

# Soft-aliases: LLM эмитит близкое-но-неверное имя → normalize. Каждый
# alias verified из bench production logs или logical synonym map.
TOOL_ALIASES: dict[str, str] = {
    # list synonyms
    "read_reminders": "list_reminders",
    "get_reminders": "list_reminders",
    "show_reminders": "list_reminders",
    "fetch_reminders": "list_reminders",
    # set/create synonyms
    "set_reminder": "schedule_reminder",
    "create_reminder": "schedule_reminder",
    "add_reminder": "schedule_reminder",
    # delete synonyms
    "remove_reminder": "cancel_reminder",
    "delete_reminder": "cancel_reminder",
    # shopping synonyms
    "add_to_shopping_list": "add_shopping_items",
    "add_to_shopping": "add_shopping_items",
    "list_shopping_items": "list_shopping",
    # task synonyms
    "create_task": "add_task",
    "set_task": "add_task",
    # memory synonyms
    "remember_fact": "save_core_fact",
    "save_fact": "save_core_fact",
    "search_memory": "recall_memory",
}


def _build_user_prompt(request: PlanRequest) -> str:
    """Собрать user-промпт с контекстом."""
    parts = [f"Сообщение: «{request.user_text}»", f"Намерение: {request.intent.value}"]
    if isinstance(request.parser_result, TimeResolved):
        parts.append(
            f"Время (детерминированно): {request.parser_result.iso_user_tz.isoformat()}"
        )
    elif isinstance(request.parser_result, TimeUnrecognized):
        parts.append("Время: не распознано парсером.")
    if request.conversation_history:
        last = request.conversation_history[-1]
        parts.append(f"Прошлая реплика пользователя: «{last.user_text}»")
    return "\n".join(parts)


def _parse_llm_output(
    raw: dict[str, Any],
    request: PlanRequest,
) -> PlannerOutput:
    """Распарсить ответ LLM в один из трёх типов.

    Защитные меры:
      - Если LLM вернула action с trigger_iso, а parser дал
        ``TimeResolved`` — перезаписываем trigger_iso на детерминированный.
      - kind не из ожидаемых → NoAction.
    """
    kind = raw.get("kind")
    if kind == "no_action":
        return NoAction(
            ack_message=str(raw.get("ack_message") or ""),
            rationale="llm_no_action",
        )
    if kind == "clarification":
        return Clarification(
            question=str(raw.get("question") or ""),
            rationale="llm_clarification",
        )
    if kind == "action":
        raw_calls = raw.get("calls") or []
        if not isinstance(raw_calls, list):
            return NoAction(ack_message="", rationale="llm_malformed_calls")
        calls: list[ToolCall] = []
        hallucinated: list[str] = []
        aliased: list[tuple[str, str]] = []
        for i, c in enumerate(raw_calls):
            if not isinstance(c, dict):
                continue
            tool = c.get("tool")
            args = c.get("args") or {}
            if not tool or not isinstance(args, dict):
                continue
            # P0.A (2026-05-19): tool whitelist + alias normalization.
            # bench показал 100% list_request hallucinations и 50%
            # get_weather hallucinations на gemini.
            if tool in TOOL_ALIASES:
                aliased.append((tool, TOOL_ALIASES[tool]))
                tool = TOOL_ALIASES[tool]
            if tool not in KNOWN_TOOLS:
                hallucinated.append(tool)
                continue  # drop call, не добавляем в plan
            # Защита: если parser дал TimeResolved, перезаписываем trigger_iso
            if (
                "trigger_iso" in args
                and isinstance(request.parser_result, TimeResolved)
            ):
                args["trigger_iso"] = request.parser_result.iso_user_tz.isoformat()
            calls.append(ToolCall(tool_name=tool, args=args, action_index=i))
        if aliased:
            logger.info(
                "planner: tool aliases normalised %s", aliased,
            )
        if hallucinated:
            logger.warning(
                "planner: hallucinated tools dropped %s (kept=%d)",
                hallucinated, len(calls),
            )
        if not calls:
            # Все calls dropped (hallucinated) → fall to clarification
            # вместо silent NoAction, чтобы user не получил empty reply.
            if hallucinated:
                return Clarification(
                    question="Не уверена что могу помочь с этим запросом. "
                            "Можешь сформулировать иначе?",
                    rationale=f"all_calls_hallucinated:{hallucinated}",
                )
            return NoAction(ack_message="", rationale="llm_empty_action_plan")
        return ExecutionPlan(calls=tuple(calls))
    return NoAction(ack_message="", rationale=f"llm_unknown_kind:{kind}")


# ─── Эвристика извлечения title из user_text ─────────────────────────


def _extract_title_hint(user_text: str) -> str:
    """Попытка вытащить title из текста типа «поставь на 14:00 разбудить Катю».

    Берём «хвост» после последнего глагола действия или времени. Грубая
    эвристика — точное извлечение делает LLM в Day 5.
    """
    text = user_text.strip()
    if not text:
        return ""
    # Срезаем числа времени из хвоста — типично цепочка действий в конце
    # Например «разбудить Катю» в «нет, поставь на 14:00 разбудить Катю»
    # Все цифровые блоки + предлоги «на N» «в N» — удалим, оставшийся хвост
    # обрезаем до 80 char для безопасности
    cleaned = re.sub(r"\b(?:на|в)\s+\d+(?::\d+)?", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d+(?::\d+)?\b", "", cleaned)
    cleaned = re.sub(r"\bнет[,.]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bпоставь\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bне\s+на\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bа\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:")
    # Strip остаточный leading «не » — после удаления времени «не на N»
    # превратилось в «не», что инвертирует смысл title
    cleaned = re.sub(r"^\s*не\s+", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned[:80]
