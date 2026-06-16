"""#120 — плановый контур как сменный цикл чата (хребет PR-2a-минимум).

Вторая ветка шва ``execute_conversation_chat`` (рядом с
``_run_legacy_react_loop``): для тенантов из ``SREDA_PLANNER_ENABLED_TENANTS``
ход обслуживает план-исполни-собери конвейер вместо легаси-цикла.

Решения владельца (issue #120, чат 2026-06-10):
- БЕЗ отката в легаси: при любом сбое пользователь получает честный шаблон
  («Не получилось…»), а владельцу уходит алерт в админ-аккаунт
  (``send_admin_alert``) с тенантом/стадией/кодом.
- Скорость оцениваем по прод-trace: стадии план/исполнение/голос пишутся
  через ``trace.step`` в тот же журнал, что и легаси.

Сознательные упрощения первого среза (зафиксированы в #120):
- профиль — минимальный снимок (обращение «ты», Москва); полноценный снимок
  профиля/памяти — следующий срез. Память доступна планам через инструмент
  ``recall_memory``.
- история — дословные пары (юзер/среда) из уже собранных preflight-сообщений
  (тот же материал, что видит легаси) — без отдельного чтения базы.
- биллинг плановых LLM-вызовов не резервируется (семейные аккаунты, решение
  владельца; PR-2b residual #3).

Функция НИКОГДА не поднимает исключение наружу — контракт ветки шва:
ответ пользователю есть всегда, легаси не трогаем.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sreda.services import trace
from sreda.services.admin_alerts import send_admin_alert

logger = logging.getLogger(__name__)

# #133: тестовый шов планировщика. Функциональный харнес кладёт сюда
# очередь записанных ответов (call_planner_fn-совместимую); в проде —
# всегда None. ContextVar, а не модульная переменная: не течёт между
# конкурентными запросами/сценариями (Codex R3/R4 плана #133).
_planner_call_override: ContextVar[Any] = ContextVar(
    "planner_call_override", default=None,
)

_MSK = ZoneInfo("Europe/Moscow")

# Честные тексты на случай, если даже реестр шаблонов недоступен.
# Правило Бориса 2026-06-11: любой технический отказ — живая «поломка»
# из пула (случайная), не сухой канцелярит. Модульная косвенность через
# функции — чтобы каждый показ был свежим случайным выбором.
from sreda.services.composer.breakdown_messages import (  # noqa: E402
    breakdown_phrase as _breakdown_phrase,
)


_BREAKDOWN_LAST_RESORT = ("Ой… у меня внутри что-то сломалось. "
                          "Создатели уже чинят — попробуй чуть позже!")


def _fallback_invalid() -> str:
    try:
        return _breakdown_phrase()
    except Exception:  # noqa: BLE001 — контракт «никогда не поднимаем»
        return _BREAKDOWN_LAST_RESORT


def _fallback_error() -> str:
    try:
        return _breakdown_phrase()
    except Exception:  # noqa: BLE001
        return _BREAKDOWN_LAST_RESORT
_FALLBACK_UNCERTAIN = (
    "Не уверена, что всё получилось сохранить — проверь, пожалуйста, "
    "и повтори при необходимости."
)


def _render_or(template_id: str, data: dict, fallback: str) -> str:
    try:
        from sreda.services.composer.registry import REGISTRY
        return REGISTRY.render(template_id, data)
    except Exception:  # noqa: BLE001 — фолбэк-текст обязан появиться всегда
        return fallback


def _alert(stage: str, action: Any, code: str, detail: str) -> None:
    """Алерт владельцу (решение #120: ошибки — в админ-аккаунт, не в легаси)."""
    try:
        send_admin_alert(
            "P1",
            f"планировщик: сбой стадии «{stage}»",
            f"tenant={action.tenant_id} stage={stage} code={code}\n{detail[:400]}",
            dedupe_key=f"planner_chat:{action.tenant_id}:{stage}:{code}",
        )
    except Exception:  # noqa: BLE001 — алерт не должен ронять ответ
        logger.exception("planner_chat: alert delivery failed")


def _ordered_subsequence(haystack: str, needles: list[str]) -> bool:
    """True если ``needles`` встречаются в ``haystack`` в ТОМ ЖЕ порядке
    (как подпоследовательность). Пустой список → True (нечего проверять)."""
    pos = 0
    for n in needles:
        if not n:
            continue
        idx = haystack.find(n, pos)
        if idx < 0:
            return False
        pos = idx + len(n)
    return True


def _selector_clarification_reply(
    *,
    exec_log: Any,
    plan_result: Any,
    action: Any,
    pf: Any,
    session: Any,
    registry_snapshot_hash: str,
) -> str | None:
    """#153 Фаза 1 (ЯДРО) — generic-проводка уточнения селекторной
    неоднозначности (`.only` на 0/>1 → ``abort_kind="selector_ambiguity"``).

    Возвращает ГОТОВЫЙ к показу текст уточнения (через рот, со страховкой) ЛИБО
    ``None`` — если это НЕ селекторный аборт (тогда вызывающий идёт прежним
    breakdown-путём БЕЗ изменений; см. риск p-001).

    Шаги (зеркалит стадию-3 compose + блок прихорашивания):
    1. ``abort_kind != "selector_ambiguity"`` → ``None`` (не-селекторные аборты
       не трогаем — регресс-инвариант).
    2. ``compose_for_execution(plan.compose, exec_log, …)`` детерминированно
       роутит в ``selector_ambiguity_clarification`` (compose() игнорирует
       plan.compose для не-completed исходов — поэтому нужен именно этот шов).
    3. Озвучить детерминированный текст через ``humanize_result`` — ДАЖЕ при
       пустом ``called`` (селекторный шаг = arg_violation, исключён из called),
       минуя гейт ``and called`` (иначе уточнение ушло бы сухим шаблоном —
       регресс правила «все ответы через рот»).
    4. ORDER-PRESERVING guard: ДО рта снимаем ``options`` (упорядоченные,
       id-stripped, cap 5 — selector.py:337) + ``count``. ПОСЛЕ рта проверяем,
       что озвученный текст содержит count и КАЖДЫЙ лейбл как УПОРЯДОЧЕННУЮ
       подпоследовательность (а для aborted_partial — ещё и done_summary
       «часть действий уже выполнила»). Пусто/исключение рта ИЛИ провал guard →
       fallback на ДЕТЕРМИНИРОВАННЫЙ текст (варианты не теряем) + owner-alert.

    ПРИМЕЧАНИЕ: guard покрывает только первые 5 вариантов (cap selector.py:337);
    лейблов больше 5 в abort_details_public не бывает.
    """
    if getattr(exec_log, "abort_kind", None) != "selector_ambiguity":
        return None

    from sreda.services.composer import llm_composer as _voice_mod
    from sreda.services.composer.compose import ComposerContext
    from sreda.runtime.planner.selector import compose_for_execution

    public = getattr(exec_log, "abort_details_public", None) or {}
    options: list[str] = list(public.get("options", []) or [])
    count = public.get("count", 0)
    partial = getattr(exec_log, "outcome", None) == "aborted_partial"
    # done_summary — ровно тот генерик-литерал, что кладёт
    # clarification_compose_for_abort для aborted_partial (selector.py).
    done_summary = "часть действий уже выполнила" if partial else ""

    ctx2 = ComposerContext(
        tenant_id=action.tenant_id,
        run_id=pf.run_id,
        user_message=pf.user_text,
        locale="ru-RU",
        timezone="Europe/Moscow",
        persona_preset=_persona_preset_or_none(session, action, pf),
    )

    # (2) Детерминированный текст уточнения через единый шов.
    try:
        clar = compose_for_execution(
            plan_result.plan.compose,
            exec_log,
            llm_composer=_voice_mod.DEFAULT_LLM_COMPOSER,
            ctx=ctx2,
            expected_registry_snapshot_hash=registry_snapshot_hash,
        )
        clar_text = (getattr(clar, "text", "") or "").strip()
    except Exception:  # noqa: BLE001 — уточнение не должно ронять ход
        logger.exception("planner_chat: selector clarification compose failed")
        return None
    if not clar_text:
        return None

    def _guard_ok(text: str) -> bool:
        if not text:
            return False
        if str(count) not in text:
            return False
        # Лейблы вариантов — ТОЧНО и в ПОРЯДКЕ (анти-перестановка/анти-потеря,
        # лейблы уже id-stripped продюсером).
        if not _ordered_subsequence(text, options):
            return False
        # done_summary — honesty-сигнал; рот естественно меняет регистр первой
        # буквы, поэтому проверяем подстроку без учёта регистра (важно, что
        # «часть … уже выполнила» НЕ потерялась, а не байт-точное совпадение).
        if partial and done_summary.lower() not in text.lower():
            return False
        return True

    # (3) Озвучить детерминированный clar-текст — минуя гейт `and called`.
    try:
        voiced = _voice_mod.DEFAULT_LLM_COMPOSER(
            llm_prompt_key="humanize_result",
            template_data={
                "intent": (pf.user_text or "запрос пользователя")[:300],
                "actions": [{
                    "user_visible_summary": clar_text,
                    "status": "ok",
                }],
            },
            execution_log=exec_log,
            ctx=ctx2,
        )
        voiced_text = (getattr(voiced, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — страховка детерминированным текстом
        logger.warning(
            "planner_chat: selector clarification voice failed (%s) — "
            "детерминированный текст", type(exc).__name__,
        )
        _alert("голос-уточнение", action, type(exc).__name__, str(exc))
        return clar_text

    # (4) ORDER-PRESERVING guard. Провал → детерминированный текст + alert.
    if not _guard_ok(voiced_text):
        _alert(
            "голос-уточнение", action, "options_guard_failed",
            "рот потерял/переставил варианты или count/partial — "
            "ушёл детерминированный текст",
        )
        return clar_text
    return voiced_text


def _persona_preset_or_none(session: Any, action: Any, pf: Any) -> str | None:
    """#126 п.3: тон персоны для рта. Сбой чтения НИКОГДА не валит ход —
    None означает «дефолтный голос» (warm_practical-поведение)."""
    if not getattr(pf, "user_id", None):
        return None
    try:
        from sreda.services.housewife_persona import get_persona_preset
        return get_persona_preset(
            session, tenant_id=action.tenant_id, user_id=pf.user_id,
        )
    except Exception:  # noqa: BLE001 — тон не стоит ошибки хода
        logger.warning("planner_chat: persona preset read failed", exc_info=True)
        return None


# #124 (Codex R1 medium CRITICAL): было limit=6 — прод обрезал историю
# ДО блока ПОСЛЕДНИЕ_РЕПЛИКИ_ЮЗЕРА, и 10 надиктованных пунктов физически
# не доходили (тест блока зелёный, прод-путь другой). Поднято до 10 —
# совпадает с budget.max_recent_utterances; _render_history режет
# отдельно своим max_history_turns.
def _history_snapshots(messages: list[Any], limit: int = 10) -> tuple:
    """Дословные пары (юзер/среда) из preflight-сообщений → TurnSnapshot'ы.

    Берём те же данные, что видит легаси (без повторного чтения базы):
    Human/AI-пары до текущего сообщения. Текущий ход (последний Human)
    не включаем — он уходит как ``user_message``.
    """
    from langchain_core.messages import AIMessage, HumanMessage
    from sreda.runtime.planner.prompt_builder import TurnMessage, TurnSnapshot

    def _text(content: Any) -> str:
        if isinstance(content, list):  # multi-part (кэш-префиксы)
            return "\n".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p)
                for p in content
            )
        return str(content or "")

    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for m in messages:
        if isinstance(m, HumanMessage):
            pending_user = _text(m.content)
        elif isinstance(m, AIMessage) and pending_user is not None:
            reply = _text(m.content)
            if reply.strip():
                pairs.append((pending_user, reply))
            pending_user = None
    pairs = pairs[-limit:]
    now_iso = datetime.now(_MSK).isoformat(timespec="seconds")
    return tuple(
        TurnSnapshot(
            turn_id=f"h{i}",
            started_at=now_iso,
            summary=None,
            is_active=False,
            messages=[
                TurnMessage(role="юзер", text=u[:1000], ts=now_iso),
                TurnMessage(role="среда", text=a[:1000], ts=now_iso),
            ],
        )
        for i, (u, a) in enumerate(pairs, 1)
    )


def _record_usage_safe(
    session: Any,
    *,
    tenant_id: str,
    feature_key: str | None,
    run_id: str,
    provider_key: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    task_type: str,
) -> None:
    """#151: best-effort запись ОДНОЙ наблюдательной строки usage в skill_ai_executions.

    Учёт НИКОГДА не валит ход. Пропускаем, если нет сессии (юнит-швы) или нулевые
    токены. provider_key пишем настоящий (планировщик: inception-mercury2 и т.п.;
    рот: openrouter-…) — денежные страницы считают USD = токены×прайс по
    (provider_key, model).

    Изоляция (Codex R2 MAJOR, оба ревьюера): пишем в ОТДЕЛЬНУЮ session на том же
    engine и коммитим САМИ — НЕ трогаем общую session хода. `begin_nested` на ней
    флашил бы ВСЁ её pending-состояние (preflight/инструменты) и мог отравить
    сессию; плановые инструменты к тому же сами коммитят mid-turn. Отдельная
    транзакция: строка durable сразу, независимо от исхода хода — для учёта верно.

    `credits_override=0` (Codex R2 MAJOR high): credits_for — MiMo-only, Mercury/
    Gemini попали бы в fallback 2×MiMo и исказили бы кредит-квоту. Наблюдательные
    строки квоту НЕ потребляют (provider-aware квота — отдельная задача)."""
    if session is None:
        return
    if (prompt_tokens or 0) <= 0 and (completion_tokens or 0) <= 0:
        return
    if not provider_key:
        return
    try:
        from sqlalchemy.orm import Session as _SASession

        from sreda.services.budget import BudgetService

        bind = session.get_bind()
        acct = _SASession(bind=bind)
        try:
            BudgetService(acct).record_llm_usage(
                tenant_id=tenant_id,
                feature_key=feature_key or "housewife_assistant",
                model=model or provider_key,
                prompt_tokens=max(prompt_tokens or 0, 0),
                completion_tokens=max(completion_tokens or 0, 0),
                run_id=run_id,
                provider_key=provider_key,
                task_type=task_type,
                credits_override=0,
            )
            acct.commit()
        finally:
            acct.close()
    except Exception:  # noqa: BLE001 — учёт не валит ответ пользователю
        logger.warning(
            "planner_chat: usage record failed (%s)", task_type, exc_info=True,
        )


async def run_planner_chat_loop(
    *,
    session: Any,
    action: Any,
    pf: Any,  # ChatPreflight (duck-typed, без импорта handlers на уровне модуля)
    context: dict[str, Any],
) -> Any:  # ChatLoopResult
    """Один ход через плановый контур. Никогда не поднимает исключений."""
    from langchain_core.messages import AIMessage
    from sreda.runtime.handlers import ChatLoopResult  # цикла нет: handlers
    # импортирует этот модуль только локально, в точке развилки

    def _result(text: str, *, called: set[str] | None = None,
                counts: dict[str, int] | None = None) -> ChatLoopResult:
        # Codex R2 high (срез поломок): ИНВАРИАНТ «каждый показ поломки
        # записан» держим в единой точке выхода ответа — любой текст из
        # пула фиксируется ERROR-логом (alert=False: ранние пути уже
        # алертят через _alert стадии; branch_compose алертит отдельно).
        try:
            from sreda.services.composer.breakdown_messages import (
                BREAKDOWN_POOL as _bp, note_breakdown as _nb,
            )
            if text in _bp or text == _BREAKDOWN_LAST_RESORT:
                _nb(f"planner_chat:final:{action.tenant_id}",
                    "пользователю ушла «поломка» (единая точка выхода)",
                    alert=False)
        except Exception:  # noqa: BLE001 — фиксация не валит ответ
            logger.exception("planner_chat: breakdown note failed")
        final = AIMessage(content=text)
        return ChatLoopResult(
            final_ai=final,
            messages=[*pf.messages, final],
            turn_msg_start_idx=pf._turn_msg_start_idx,
            called_tools=called or set(),
            hallucination_nudged=False,
            turn_timed_out=False,
            successful_tool_counts=counts or {},
            onboarding_resolution_called=False,
        )

    # --- стадия 1: план -----------------------------------------------------
    try:
        from sreda.runtime.planner.few_shot_examples import render_few_shot_block
        from sreda.runtime.planner.orchestrator import (
            PlannerContext,
            run as orchestrator_run,
        )
        from sreda.runtime.planner.prompt_builder import NowMoment, ProfileSnapshot
        from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY
        from sreda.services.composer.registry import REGISTRY as COMPOSER_REGISTRY
        from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

        proposed_llm_keys = tuple(LLM_PROMPT_REGISTRY.prompt_keys())
        registry_snapshot_hash = COMPOSER_REGISTRY.snapshot_hash()
        ctx = PlannerContext(
            tenant_id=action.tenant_id,
            run_id=pf.run_id,
            feature_key=pf.feature_key or "housewife_assistant",
            user_message=pf.user_text,
            voice_meta=None,
            now=NowMoment(datetime.now(_MSK).replace(tzinfo=None)),
            profile=ProfileSnapshot(address="ты"),
            memories=(),
            active_turn=None,
            closed_turns=_history_snapshots(pf.messages),
            available_tools=tuple(MIGRATED_TOOL_SPECS),
            composer_template_ids=tuple(COMPOSER_REGISTRY.template_ids()),
            composer_llm_prompt_keys=proposed_llm_keys,
            composer_registry_snapshot_hash=registry_snapshot_hash,
            tool_registry_version="planner-chat-v1",
            few_shot_block=render_few_shot_block(
                effective_llm_keys=frozenset(proposed_llm_keys)
            ),
        )
        # #133: дефолтный аргумент call_planner_fn связан при определении
        # run() — прокидываем override явно, только когда он установлен
        _planner_kwargs: dict[str, Any] = {}
        _override = _planner_call_override.get()
        if _override is not None:
            _planner_kwargs["call_planner_fn"] = _override
        with trace.step("planner.plan") as _meta:
            plan_result = await orchestrator_run(
                ctx,
                session_factory=None,  # срез 1: без записи planner_executions —
                # FK run_id↔agent_runs требует строки текущего run'а ДО хода;
                # включается следующим срезом вместе с ledger
                admin_alert_fn=None,
                **_planner_kwargs,
            )
            _meta["attempts"] = plan_result.final_attempt_no
            _meta["ok"] = plan_result.success
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner_chat: plan stage crashed")
        _alert("план", action, type(exc).__name__, str(exc))
        return _result(_fallback_error())

    if not plan_result.success or plan_result.execution_plan is None:
        _alert("план", action, plan_result.error_summary or "invalid",
               f"attempts={plan_result.final_attempt_no}")
        return _result(_render_or(
            "invalid_plan_fallback",
            {"attempt_count": plan_result.final_attempt_no},
            _fallback_invalid(),
        ))

    # F-1 (#151): план валиден → LLM планировщика отработал. Пишем usage СЕЙЧАС
    # (а не только на полном успехе), чтобы траты Mercury учитывались и когда
    # исполнение/сборка ниже падают. getattr-дефолты — для тест-швов без полей.
    _record_usage_safe(
        session,
        tenant_id=action.tenant_id, feature_key=pf.feature_key, run_id=pf.run_id,
        provider_key=getattr(plan_result, "provider", "") or "",
        model=getattr(plan_result, "model", "") or "",
        prompt_tokens=getattr(plan_result, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(plan_result, "completion_tokens", 0) or 0,
        task_type="planner.plan",
    )

    # --- стадия 2: исполнение настоящими инструментами ----------------------
    try:
        from sreda.runtime.planner.executor import execute_plan

        registry_map = {s.name: s for s in MIGRATED_TOOL_SPECS}
        with trace.step("planner.execute") as _meta:
            exec_log = await execute_plan(
                plan_result.execution_plan, pf.tools_by_name, registry_map,
            )
            _meta["outcome"] = exec_log.outcome
            _meta["steps"] = len(exec_log.steps)
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner_chat: execute stage crashed")
        _alert("исполнение", action, type(exc).__name__, str(exc))
        return _result(_fallback_error())

    # called_tools = физически ВЫЗВАННЫЕ инструменты (семантика легаси и
    # стража честности): error/timeout/unknown_outcome/plan_gap — пост-вызовные
    # статусы, запись могла совершиться. Исключаем только до-вызовные
    # arg_violation и skipped (Codex R1 MAJOR, оба ревьюера).
    called = {
        st.tool for st in exec_log.steps
        if st.tool and st.status not in ("skipped", "arg_violation")
    }
    counts: dict[str, int] = {}
    for st in exec_log.steps:
        if st.status == "ok" and st.tool:
            counts[st.tool] = counts.get(st.tool, 0) + 1
    ok_tools = sorted(counts)

    if exec_log.outcome in ("aborted", "failed"):
        # #153 Фаза 1: селекторная неоднозначность (.only на 0/>1) → уточнение
        # через рот ДО прежнего breakdown. None → не-селекторный аборт, прежний
        # путь БЕЗ изменений (регресс-инвариант p-001).
        _clar = _selector_clarification_reply(
            exec_log=exec_log, plan_result=plan_result, action=action,
            pf=pf, session=session, registry_snapshot_hash=registry_snapshot_hash,
        )
        if _clar is not None:
            return _result(_clar, called=called, counts=counts)
        _alert("исполнение", action, exec_log.outcome,
               "; ".join(f"{st.step_id}:{st.tool}:{st.status}"
                         for st in exec_log.steps)[:300])
        # aborted ДО первой записи / все шаги упали → честная ошибка
        return _result(_fallback_error(), called=called, counts=counts)

    if exec_log.outcome == "aborted_partial":
        # #153 Фаза 1: селекторная неоднозначность на aborted_partial → уточнение
        # (с сохранённым «часть уже сделала») через рот ДО прежней partial-ветки.
        # None → не-селекторный partial, прежний путь БЕЗ изменений.
        _clar = _selector_clarification_reply(
            exec_log=exec_log, plan_result=plan_result, action=action,
            pf=pf, session=session, registry_snapshot_hash=registry_snapshot_hash,
        )
        if _clar is not None:
            return _result(_clar, called=called, counts=counts)
        # Контракт compose(): aborted_partial обязан быть подменён честным
        # фолбэком ДО сборки — часть записей совершена, обычная сборка может
        # отрендерить «успех» и спрятать неопределённость (Codex R1 CRITICAL).
        _alert("исполнение", action, "aborted_partial",
               "; ".join(f"{st.step_id}:{st.tool}:{st.status}"
                         for st in exec_log.steps)[:300])
        if not ok_tools:
            # Codex R2 MAJOR: ни одного чистого успеха (только unknown/timeout/
            # plan_gap) — «Сделала что просила» было бы самоуверенной ложью.
            # Честно: риск записи есть, доказательств нет.
            return _result(_FALLBACK_UNCERTAIN, called=called, counts=counts)
        return _result(
            _render_or("partial_with_compose_error",
                       {},  # имена инструментов — внутренние (аудит 2026-06-11); D.3 даст человеческие сводки
                       _fallback_error()),
            called=called, counts=counts,
        )

    # --- стадия 3: голос ------------------------------------------------------
    try:
        from sreda.services.composer import llm_composer as _voice_mod
        from sreda.services.composer.compose import ComposerContext, compose

        # #121: отмечаем, вызывался ли живой голос ВНУТРИ сборки — чтобы
        # шаблонные ветки потом прогнать через рот (правило владельца:
        # ВСЕ ответы через живой голос; шаблон — сырьё и страховка).
        _voice_used = {"v": False}
        _beautify_failed = {"v": False}  # R1 medium: не выставляет fallback_used
        # F-1 (#151): копим usage рта (обе ветки — внутри compose и прихорашивание).
        _rot_usage = {"prompt": 0, "completion": 0, "provider": "", "model": ""}

        def _capture_rot(res: Any) -> Any:
            _rot_usage["prompt"] += getattr(res, "prompt_tokens", 0) or 0
            _rot_usage["completion"] += getattr(res, "completion_tokens", 0) or 0
            if getattr(res, "provider", ""):
                _rot_usage["provider"] = res.provider
                _rot_usage["model"] = getattr(res, "model", "") or ""
            return res

        def _tracking_voice(**kw):
            _voice_used["v"] = True
            return _capture_rot(_voice_mod.DEFAULT_LLM_COMPOSER(**kw))

        with trace.step("planner.compose") as _meta:
            reply = compose(
                plan_result.plan.compose,
                exec_log,
                llm_composer=_tracking_voice,
                ctx=(ctx2 := ComposerContext(
                    tenant_id=action.tenant_id,
                    run_id=pf.run_id,
                    user_message=pf.user_text,
                    locale="ru-RU",
                    timezone="Europe/Moscow",
                    persona_preset=_persona_preset_or_none(session, action, pf),
                )),
                # тот же снимок, что видел планировщик при валидации (Codex R1
                # MINOR: хэш «на момент сборки» делает проверку гонки тавтологией)
                expected_registry_snapshot_hash=registry_snapshot_hash,
            )
            _meta["fallback"] = reply.fallback_used or "-"
        text = (reply.text or "").strip()
        if not text:
            raise RuntimeError("composer returned blank text")
        # Падение самого ГОЛОСА внутри сборки — прихорашивать тем же сломанным
        # голосом бессмысленно: остаёмся на детерминированной страховке.
        # Codex R2 (medium): прод-compose() кладёт голосовой сбой в error_code
        # («llm_composer_error:…»), а fallback_used становится generic_error /
        # conversational_fallback — классифицируем по ОБОИМ полям.
        _voice_broken = (
            str(getattr(reply, "error_code", "") or "").startswith("llm_composer")
            or str(reply.fallback_used or "").startswith("llm_composer")
        )
        if reply.fallback_used:
            _alert("голос" if _voice_broken else "сборка", action,
                   str(reply.fallback_used),
                   f"деградация сборки; план={getattr(plan_result.plan.compose, 'template_id', None)}"
                   f"/{getattr(plan_result.plan.compose, 'llm_prompt_key', None)}")
        # Субагент R1 MAJOR (срез «поломок»): плановая ветка ошибки
        # (generic_tool_error терминальной веткой — few-shot этому УЧИТ)
        # для конвейера «успех»: fallback_used пуст, алерта выше нет —
        # ровно класс тихого 15:08. Фиксируем по членству финального
        # текста в пуле; и поломку НЕ отдаём рту (иначе «я сломалась»
        # прихорашивается как успешное действие).
        from sreda.services.composer.breakdown_messages import (
            BREAKDOWN_POOL as _POOL,
            note_breakdown as _note_breakdown,
        )
        _breakdown_shown = text in _POOL
        if _breakdown_shown and not reply.fallback_used:
            _note_breakdown(
                f"branch_compose:"
                f"{getattr(reply, 'effective_template_id', None)}"
                f":{action.tenant_id}",
                "плановая ветка ошибки отдала «поломку» при успешном "
                "конвейере — фиксация по тексту",
            )
        # Codex #121 R1 (оба, MAJOR): рот обязателен и после НЕголосовой
        # деградации сборки (шаблон/реестр) — её текст тоже сырьё, не финал.
        # Предикат шагов = called (без skipped и до-вызовных arg_violation).
        if (not _voice_used["v"] and not _voice_broken and called
                and not _breakdown_shown):
            # #121 (правило владельца, скриншоты 2026-06-10): шаблонный рендер
            # НЕ финал — отдаём его рту как факт-сырьё («сделай красиво, не
            # теряя ни одной позиции» — Ф4-промпт humanize_result). Сбой
            # голоса → пользователю уходит детерминированный текст (страховка)
            # + алерт владельцу. Ходы без исполненных шагов (уточнения,
            # болтовня, identity) не прихорашиваются — там перефраз вредит.
            with trace.step("planner.voice") as _vmeta:
                try:
                    voiced = _capture_rot(_voice_mod.DEFAULT_LLM_COMPOSER(
                        llm_prompt_key="humanize_result",
                        template_data={
                            "intent": (pf.user_text or "запрос пользователя")[:300],
                            "actions": [{
                                "user_visible_summary": text,
                                "status": "ok",
                            }],
                        },
                        execution_log=exec_log,
                        ctx=ctx2,
                    ))
                    voiced_text = (getattr(voiced, "text", "") or "").strip()
                    _vmeta["ok"] = bool(voiced_text)
                    _vmeta["latency_ms"] = getattr(voiced, "latency_ms", None)
                    if voiced_text:
                        text = voiced_text
                    else:
                        # Codex R1 (оба): пустой голос = сбой по контракту —
                        # алерт владельцу, пользователю — страховка шаблоном
                        _beautify_failed["v"] = True
                        _alert("голос-прихорашивание", action, "blank_output",
                               "рот вернул пустой текст; ушла страховка")
                except Exception as exc:  # noqa: BLE001 — страховка шаблоном
                    _vmeta["ok"] = False
                    logger.warning(
                        "planner_chat: voice beautify failed (%s) — "
                        "falling back to template text",
                        type(exc).__name__,
                    )
                    _beautify_failed["v"] = True
                    _alert("голос-прихорашивание", action,
                           type(exc).__name__, str(exc))
        # #135 (за гейтом, отсоединённо): успешный ход — кандидат в
        # библиотеку планов + теневой отбор. Вне гейта — ноль побочек.
        try:
            from sreda.runtime.planner.plan_library import (
                schedule_candidate_recording,
            )
            schedule_candidate_recording(
                tenant_id=action.tenant_id, run_id=pf.run_id,
                plan=plan_result.plan, exec_outcome=exec_log.outcome,
                fallback_used=(reply.fallback_used
                               or ("voice_beautify_failed"
                                   if _beautify_failed["v"] else None)),
                breakdown_shown=_breakdown_shown,
            )
        except Exception:  # noqa: BLE001 — библиотека не валит ход
            logger.warning("plan_library: schedule failed", exc_info=True)
        # F-1 (#151): запись usage рта (provider/model — из реального ответа
        # композера, захвачены _capture_rot в обеих ветках голоса).
        _record_usage_safe(
            session,
            tenant_id=action.tenant_id, feature_key=pf.feature_key, run_id=pf.run_id,
            provider_key=_rot_usage["provider"], model=_rot_usage["model"],
            prompt_tokens=_rot_usage["prompt"], completion_tokens=_rot_usage["completion"],
            task_type="composer.voice",
        )
        return _result(text, called=called, counts=counts)
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner_chat: compose stage crashed")
        _alert("голос", action, type(exc).__name__, str(exc))
        if exec_log.outcome in ("completed", "partial_failure", "aborted_partial") and called:
            # записи совершены — честно признать действия без красивого текста
            return _result(
                _render_or("partial_with_compose_error",
                           {},  # имена инструментов — внутренние (аудит 2026-06-11); D.3 даст человеческие сводки
                           _fallback_error()),
                called=called, counts=counts,
            )
        return _result(_fallback_error(), called=called, counts=counts)
