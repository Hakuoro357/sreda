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
4. **TimeAmbiguous** → fall through к LLM (silent skip). Hardcoded
   short-circuit (commit 6d383ec) удалён — прилетал в нерелевантный
   контекст. LLM сама disambig из history/intent.
5. **TimeInvalid**:
   - ``past_date`` → fall through к LLM (e.g. «Сделала зарядку вчера»
     — НЕ reminder request, LLM эмитит ``save_episode``). Post-LLM
     past-trigger safety enforce'ит actual ``trigger_iso`` через
     ``is_past_iso`` хелпер.
   - ``out_of_range`` → ``Clarification`` (nonsensical input типа
     «25:00» — LLM не поможет).
   - unknown reason → ``Clarification`` (fail-closed для future parser
     versions).
6. Иначе — LLM-вызов с типизированным выводом. Результат проходит
   защитную подмену: если parser нашёл ``TimeResolved``, мы перезаписываем
   ``trigger_iso`` от LLM на детерминированный. Это закрывает класс
   багов когда LLM «округлила» время. Если LLM эмитит past
   ``trigger_iso`` (по факту, не по parser hint) — drop call'а с
   fail-whole-plan Clarification (anti-confabulation).

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

    # 4. TimeAmbiguous — удалён hardcoded short-circuit (был bug в R-39
    # Day 4 design 2026-05-18 commit 082bdbb). Hardcoded вопрос «через 2
    # часа или в 14:00?» прилетал на ЛЮБОЙ TimeAmbiguous (e.g. parser
    # ambigous'нул на «пятницу» или «два часа дня»), даже когда вопрос
    # совершенно нерелевантный. Прод data 2026-05-19 12:20 показала
    # 3/5 voice turn'ов пилота получали этот же вопрос.
    #
    # Сейчас TimeAmbiguous → fall through на LLM (как TimeUnrecognized).
    # LLM сама решит: disambig из context (history, intent, time-of-day)
    # или попросит ОСМЫСЛЕННОЕ clarification.

    # 5. Недопустимое время — разделено по reason (2026-05-19 cleanup).
    # Старый код hardcoded'ил "Это время уже прошло. На какое поставить?"
    # для past_date — assumed reminder context. Прод bug: «Сделала зарядку
    # вчера» НЕ reminder request, но получала «На какое поставить?».
    # Теперь past_date → fall through к LLM; safety на actual trigger_iso
    # enforce'ится в _parse_llm_output через is_past_iso.
    if isinstance(request.parser_result, TimeInvalid):
        reason = request.parser_result.reason
        if reason == "past_date":
            logger.info(
                "planner.decision: TimeInvalid(past_date) → fall through to LLM",
            )
            # ничего не return — продолжаем step 6 (LLM)
        elif reason == "out_of_range":
            logger.info(
                "planner.decision: TimeInvalid(out_of_range) → "
                "Clarification(parser_invalid:out_of_range)",
            )
            return Clarification(
                question="Не разобрала время — можешь сказать иначе? "
                         "Например «в 14:00» или «завтра в 9 утра».",
                rationale="parser_invalid:out_of_range",
            )
        else:
            # Unknown reasons (future parser versions) — fail-closed
            logger.info(
                "planner.decision: TimeInvalid(unknown:%s) → "
                "Clarification(parser_invalid:unknown)",
                reason,
            )
            return Clarification(
                question="Не разобрала время. Уточни, пожалуйста.",
                rationale=f"parser_invalid:unknown:{reason}",
            )

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

# Required args per tool — для P0.B (2026-05-19). Bug A воспроизведён
# на gemini-3.1-flash-lite и gemini-2.5-flash в production: LLM эмитит
# `schedule_reminder` без обязательного `title` → executor fail-closed
# с `missing_idempotency_field`. Прод инцидент с Borya 2026-05-18 15:41
# и 2026-05-19 08:22 (стоматолог).
#
# Минимальный critical set — только наиболее частые tools и поля без
# которых tool **точно** падает. Tools не в этом dict не enforce'аются
# (permissive по умолчанию). Add when prod data shows new failure mode.
REQUIRED_ARGS_PER_TOOL: dict[str, frozenset[str]] = {
    # Reminders — title + trigger_iso обязательны для schedule_reminder
    # (бывшая Bug A). update_reminder / cancel_reminder / replace_reminder
    # не enforce'аются — там tool сам делает search-by-id или search-by-title.
    "schedule_reminder": frozenset({"title", "trigger_iso"}),
    # Memory writes — text content обязателен
    "save_core_fact": frozenset({"content"}),
    "save_episode": frozenset({"summary"}),
    "recall_memory": frozenset({"query"}),
    # Web
    "web_search": frozenset({"query"}),
    "fetch_url": frozenset({"url"}),
    # Profile updates
    "update_profile_field": frozenset({"field_name"}),
}


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
    """Собрать user-промпт с контекстом для LLM planner call.

    Включает positive TimeResolved hint когда parser справился. Для
    остальных parser-результатов:

    - ``TimeUnrecognized`` → silent skip (LLM сама извлекает из текста).
    - ``TimeAmbiguous`` → silent skip (после 2026-05-19 cleanup).
      Раньше был hardcoded short-circuit «через 2 часа или в 14:00?»
      ДО этой функции — удалён, теперь fall through к LLM.
    - ``TimeInvalid(past_date)`` → silent skip (после 2026-05-19 cleanup).
      Раньше был hardcoded short-circuit «На какое поставить?» ДО этой
      функции — удалён, теперь fall through к LLM. Post-LLM safety
      enforces actual ``trigger_iso`` через ``is_past_iso``.
    - ``TimeInvalid(out_of_range)`` и unknown reasons остаются
      hardcoded short-circuit'ами в ``plan_action`` — LLM там не поможет.

    Rationale silent skip см. commit message + bench v4 results
    (plans/r39-planner-bench-2026-05-19.md).
    """
    parts = [f"Сообщение: «{request.user_text}»", f"Намерение: {request.intent.value}"]
    if isinstance(request.parser_result, TimeResolved):
        parts.append(
            f"Время (детерминированно): {request.parser_result.iso_user_tz.isoformat()}"
        )
    if request.conversation_history:
        last = request.conversation_history[-1]
        parts.append(f"Прошлая реплика пользователя: «{last.user_text}»")
    return "\n".join(parts)


_FIELD_HUMAN_NAME: dict[str, str] = {
    "title": "название",
    "trigger_iso": "время",
    "content": "что именно запомнить",
    "summary": "что произошло",
    "query": "что искать",
    "url": "адрес",
    "field_name": "какое поле менять",
    "items": "список",
}


def _format_missing_fields_question(tool: str, missing: list[str]) -> str:
    """Human-friendly clarification question для missing required args."""
    human = [_FIELD_HUMAN_NAME.get(f, f) for f in missing]
    if len(human) == 1:
        return f"Уточни, пожалуйста: {human[0]}?"
    items = ", ".join(human[:-1]) + f" и {human[-1]}"
    return f"Уточни, пожалуйста: {items}?"


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
        missing_fields: list[tuple[str, list[str]]] = []
        past_trigger_drops: list[tuple[str, str]] = []
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
            # 2026-05-19 cleanup (Codex MAJOR R2): validate actual trigger_iso.
            # Раньше drop'ал по parser_result.reason — это давало false-positive
            # (parser past_date + LLM future trigger корректно) и false-negative
            # (parser Unrecognized + LLM past trigger). Use is_past_iso helper
            # (sreda.agents.r39_tool_adapter) который сравнивает с now минус
            # grace_minutes=2 (NTP drift + network latency tolerance).
            if (
                "trigger_iso" in args
                and tool in {"schedule_reminder", "update_reminder", "replace_reminder"}
            ):
                from sreda.agents.r39_tool_adapter import is_past_iso
                trigger_value = args.get("trigger_iso")
                if isinstance(trigger_value, str) and is_past_iso(
                    trigger_value, grace_minutes=2
                ):
                    past_trigger_drops.append((tool, trigger_value))
                    continue  # drop call — full-plan reject ниже
            # P0.B (2026-05-19): required fields enforcement.
            # Bug A воспроизведён на gemini в production: schedule_reminder
            # без `title`. Drop вместо fail-closed на executor side.
            required = REQUIRED_ARGS_PER_TOOL.get(tool, frozenset())
            missing = required - {k for k, v in args.items() if v}
            if missing:
                missing_fields.append((tool, sorted(missing)))
                continue  # drop call с missing required args
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
        if missing_fields:
            logger.warning(
                "planner: tools dropped due to missing required fields %s "
                "(kept=%d)",
                missing_fields, len(calls),
            )
        # 2026-05-19 cleanup (Codex MAJOR R2 + R3 ordering note):
        # ЛЮБОЙ past_trigger drop → fail whole plan, не partial. Иначе
        # LLM вернул [save_episode, schedule_reminder(past)] → silent
        # partial loss (save выполнен, reminder потерян) = R-39 confab
        # class regression. Whole-plan reject + clarification.
        # КРИТИЧНО: этот блок ДО `if not calls:` — иначе single-call
        # past_iso (calls пустой ПОСЛЕ continue) сваливался бы на
        # llm_empty_action_plan с неверной рационализацией.
        if past_trigger_drops:
            logger.warning(
                "planner: past_trigger drops %s → whole-plan Clarification",
                past_trigger_drops,
            )
            return Clarification(
                question="Это время уже прошло. Назвать другое?",
                rationale=f"past_trigger_drop_all:{past_trigger_drops}",
            )
        if not calls:
            # Все calls dropped → Clarification вместо silent NoAction
            if missing_fields:
                # Bug A path: модель забыла required поле (title etc.).
                # Просим конкретно поле которого не хватает.
                first_tool, first_missing = missing_fields[0]
                return Clarification(
                    question=_format_missing_fields_question(
                        first_tool, first_missing,
                    ),
                    rationale=f"missing_required:{first_tool}:{first_missing}",
                )
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
