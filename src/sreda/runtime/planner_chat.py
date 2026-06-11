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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sreda.services import trace
from sreda.services.admin_alerts import send_admin_alert

logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")

# Честные тексты на случай, если даже реестр шаблонов недоступен.
_FALLBACK_INVALID = (
    "Не получилось разобрать что ты хочешь. Попробуй переформулировать, "
    "например: «купи молоко» или «покажи список покупок»."
)
_FALLBACK_ERROR = "Ой, не получилось обработать запрос. Попробуй ещё раз через минуту."
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


def _history_snapshots(messages: list[Any], limit: int = 6) -> tuple:
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
        with trace.step("planner.plan") as _meta:
            plan_result = await orchestrator_run(
                ctx,
                session_factory=None,  # срез 1: без записи planner_executions —
                # FK run_id↔agent_runs требует строки текущего run'а ДО хода;
                # включается следующим срезом вместе с ledger
                admin_alert_fn=None,
            )
            _meta["attempts"] = plan_result.final_attempt_no
            _meta["ok"] = plan_result.success
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner_chat: plan stage crashed")
        _alert("план", action, type(exc).__name__, str(exc))
        return _result(_FALLBACK_ERROR)

    if not plan_result.success or plan_result.execution_plan is None:
        _alert("план", action, plan_result.error_summary or "invalid",
               f"attempts={plan_result.final_attempt_no}")
        return _result(_render_or(
            "invalid_plan_fallback",
            {"attempt_count": plan_result.final_attempt_no},
            _FALLBACK_INVALID,
        ))

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
        return _result(_FALLBACK_ERROR)

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
        _alert("исполнение", action, exec_log.outcome,
               "; ".join(f"{st.step_id}:{st.tool}:{st.status}"
                         for st in exec_log.steps)[:300])
        # aborted ДО первой записи / все шаги упали → честная ошибка
        return _result(_FALLBACK_ERROR, called=called, counts=counts)

    if exec_log.outcome == "aborted_partial":
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
                       {"execution_summary": ", ".join(ok_tools)},
                       _FALLBACK_ERROR),
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

        def _tracking_voice(**kw):
            _voice_used["v"] = True
            return _voice_mod.DEFAULT_LLM_COMPOSER(**kw)

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
        # Codex #121 R1 (оба, MAJOR): рот обязателен и после НЕголосовой
        # деградации сборки (шаблон/реестр) — её текст тоже сырьё, не финал.
        # Предикат шагов = called (без skipped и до-вызовных arg_violation).
        if not _voice_used["v"] and not _voice_broken and called:
            # #121 (правило владельца, скриншоты 2026-06-10): шаблонный рендер
            # НЕ финал — отдаём его рту как факт-сырьё («сделай красиво, не
            # теряя ни одной позиции» — Ф4-промпт humanize_result). Сбой
            # голоса → пользователю уходит детерминированный текст (страховка)
            # + алерт владельцу. Ходы без исполненных шагов (уточнения,
            # болтовня, identity) не прихорашиваются — там перефраз вредит.
            with trace.step("planner.voice") as _vmeta:
                try:
                    voiced = _voice_mod.DEFAULT_LLM_COMPOSER(
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
                    )
                    voiced_text = (getattr(voiced, "text", "") or "").strip()
                    _vmeta["ok"] = bool(voiced_text)
                    _vmeta["latency_ms"] = getattr(voiced, "latency_ms", None)
                    if voiced_text:
                        text = voiced_text
                    else:
                        # Codex R1 (оба): пустой голос = сбой по контракту —
                        # алерт владельцу, пользователю — страховка шаблоном
                        _alert("голос-прихорашивание", action, "blank_output",
                               "рот вернул пустой текст; ушла страховка")
                except Exception as exc:  # noqa: BLE001 — страховка шаблоном
                    _vmeta["ok"] = False
                    logger.warning(
                        "planner_chat: voice beautify failed (%s) — "
                        "falling back to template text",
                        type(exc).__name__,
                    )
                    _alert("голос-прихорашивание", action,
                           type(exc).__name__, str(exc))
        return _result(text, called=called, counts=counts)
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner_chat: compose stage crashed")
        _alert("голос", action, type(exc).__name__, str(exc))
        if exec_log.outcome in ("completed", "partial_failure", "aborted_partial") and called:
            # записи совершены — честно признать действия без красивого текста
            return _result(
                _render_or("partial_with_compose_error",
                           {"execution_summary": ", ".join(ok_tools)},
                           _FALLBACK_ERROR),
                called=called, counts=counts,
            )
        return _result(_FALLBACK_ERROR, called=called, counts=counts)
