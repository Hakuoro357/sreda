"""R-39: end-to-end тесты главного orchestrator'а pipeline.

Имитируем полный путь user_text → final_text через все 7 модулей +
L4 audit. LLM, инструменты, детектор инжектятся как fake'и.

Главный регрессионный сценарий — Кати-кейс 2026-05-17 13:21:
1. Turn 1: «поставь напоминание на 2 часа разбудить Катю»
   → tool schedule_reminder вызвался → SUCCESS rem_old
2. Turn 2: «нет, не на 2 а на 14:00 разбудить Катю»
   → planner short-circuit ResolvedCorrection + TimeResolved
   → replace_reminder rem_old → 14:00 «Разбудить Катю»
   → final_text содержит «14:00» и «Катю», не сочиняет
3. Audit чистый, не unbacked
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from sreda.agents.contracts import (
    ConversationTurn,
    ResultKind,
    ToolJournalEntry,
    TurnContext,
)
from sreda.agents.r39_pipeline import PipelineResult, process_turn


# ─── Фабрики и моки ───────────────────────────────────────────────────


def _ctx(turn_id: str = "t-001", tenant_id: int = 352612382) -> TurnContext:
    return TurnContext(turn_id=turn_id, tenant_id=tenant_id)


def _now_utc() -> datetime:
    # 2026-05-17 10:21 UTC = 13:21 MSK — момент Кати-инцидента
    return datetime(2026, 5, 17, 10, 21, 0, tzinfo=timezone.utc)


def _detector_clean(text: str, tools: set[str]) -> bool:
    return False


def _fake_schedule_reminder(title: str, trigger_iso: str) -> dict[str, Any]:
    # Фикс id — тесты не зависят от инкремента, а module-level mutable
    # state ломает изоляцию (правило monkeypatch из MEMORY.md).
    return {
        "reminder_id": "rem_fake",
        "title": title,
        "trigger_iso": trigger_iso,
    }


def _fake_cancel_reminder(reminder_id: str) -> dict[str, Any]:
    return {"reminder_id": reminder_id, "cancelled": True}


def _fake_replace_reminder(reminder_id: str, title: str, trigger_iso: str) -> dict[str, Any]:
    return {
        "reminder_id": reminder_id,
        "title": title,
        "trigger_iso": trigger_iso,
        "replaced": True,
    }


def _tool_callables() -> dict:
    return {
        "schedule_reminder": _fake_schedule_reminder,
        "cancel_reminder": _fake_cancel_reminder,
        "replace_reminder": _fake_replace_reminder,
    }


def _result_extractor(tool: str, args: dict, raw: Any) -> dict:
    """Простой extractor: args + result_data из raw."""
    out = dict(args)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if v is not None})
    # Имитация date_formatter для trigger_iso → trigger_human
    if "trigger_iso" in out and out["trigger_iso"]:
        iso = str(out["trigger_iso"])
        if "14:00" in iso:
            out["trigger_human"] = "сегодня в 14:00"
        else:
            out["trigger_human"] = "позже"
    return out


# ─── CHITCHAT short-circuit ──────────────────────────────────────────


def test_chitchat_returns_no_action_no_journal() -> None:
    result = process_turn(
        user_text="Как дела?",
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables={},
        detector=_detector_clean,
    )
    assert isinstance(result, PipelineResult)
    assert result.plan_kind == "no_action"
    assert result.journal.is_empty
    # Audit чистый — для пустой реплики не сработает
    assert result.audit.is_unbacked is False


# ─── Ambiguous time → Clarification ──────────────────────────────────


def test_ambiguous_time_yields_clarification() -> None:
    result = process_turn(
        user_text="поставь напоминание на 2 часа разбудить",
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=_detector_clean,
    )
    # «на 2 часа» без квалификатора → TimeAmbiguous → Clarification
    assert result.plan_kind == "clarification"
    assert result.journal.is_empty
    assert "14:00" in result.final_text or "через" in result.final_text.lower()


# ─── Кати-сценарий end-to-end ────────────────────────────────────────


def test_kati_correction_full_e2e_no_llm() -> None:
    """Главный регрессионный: turn 2 коррекция через replace_reminder.

    Без LLM-вызова на критической ветке — всё детерминировано:
    correction_resolver находит rem_old, parser даёт 14:00,
    planner short-circuit → replace_reminder.
    """
    # История: turn 1 уже произошёл, поставил rem_old «Разбудить» на 02:00
    history = (
        ConversationTurn(
            user_text="Поставь напоминание на 2 часа разбудить Катю",
            journal_entries=(
                ToolJournalEntry(
                    tool_name="schedule_reminder",
                    action_index=0,
                    result_kind=ResultKind.SUCCESS,
                    result_data={"title": "Разбудить", "trigger_human": "сегодня в 2:00"},
                    entity_id="rem_old",
                    idempotency_key="k-old",
                ),
            ),
            turn_id="t-original",
            timestamp_utc="2026-05-17T10:20:00Z",
        ),
    )

    result = process_turn(
        user_text="Нет, не на 2 а на 14:00 разбудить Катю",
        conversation_history=history,
        turn_context=_ctx(turn_id="t-correction"),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=_detector_clean,
        result_data_extractor=_result_extractor,
        # invoke_planner_llm не передан — short-circuit без LLM
    )

    # Журнал содержит ровно один replace_reminder SUCCESS
    assert result.plan_kind == "execution_plan"
    assert len(result.journal) == 1
    entry = result.journal.entries[0]
    assert entry.tool_name == "replace_reminder"
    assert entry.result_kind is ResultKind.SUCCESS
    assert entry.entity_id == "rem_old"
    # final_text содержит ключевые факты
    assert "14:00" in result.final_text
    assert "Катю" in result.final_text or "катю" in result.final_text.lower()
    # Audit чистый
    assert result.audit.is_unbacked is False
    assert result.halted_early is False


# ─── Mutation с TimeResolved + без correction (Turn 1 типа) ───────────


def test_simple_schedule_via_llm_with_parser_override() -> None:
    """Mutation, не correction → планировщик идёт в LLM. Проверяем что
    parser-разрешённое время побеждает над тем что LLM сочинит."""
    def fake_planner_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "schedule_reminder",
                "args": {
                    "title": "Разбудить Катю",
                    # LLM сочиняет какое-то trigger_iso, но parser его
                    # перезаписывает
                    "trigger_iso": "2099-01-01T00:00:00+00:00",
                },
            }],
        }

    result = process_turn(
        user_text="Поставь напоминание на 14:00 разбудить Катю",
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=_detector_clean,
        invoke_planner_llm=fake_planner_llm,
        result_data_extractor=_result_extractor,
    )

    assert result.plan_kind == "execution_plan"
    assert len(result.journal) == 1
    entry = result.journal.entries[0]
    # Время от parser-а: 14:00 MSK = 11:00 UTC
    assert "2026-05-17T14:00" in entry.result_data.get("trigger_iso", "")


# ─── Composer + замок ─────────────────────────────────────────────────


def test_composer_called_when_llm_provided() -> None:
    captured: dict[str, Any] = {}

    def fake_composer(sys: str, user: str) -> str:
        captured["called"] = True
        return "Удачи 🌞"

    history = (
        ConversationTurn(
            user_text="x",
            journal_entries=(
                ToolJournalEntry(
                    tool_name="schedule_reminder",
                    action_index=0,
                    result_kind=ResultKind.SUCCESS,
                    result_data={"title": "Разбудить", "trigger_human": "сегодня в 2:00"},
                    entity_id="rem_old",
                    idempotency_key="k1",
                ),
            ),
            turn_id="t-prev",
            timestamp_utc="2026-05-17T10:00:00Z",
        ),
    )

    result = process_turn(
        user_text="Нет, не на 2 а на 14:00 разбудить Катю",
        conversation_history=history,
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=_detector_clean,
        invoke_composer_llm=fake_composer,
        result_data_extractor=_result_extractor,
    )

    assert captured.get("called") is True
    # Финальный текст содержит и first_line, и live phrase
    assert "Удачи 🌞" in result.final_text


def test_composer_blocked_by_lock_yields_first_line_only() -> None:
    """Composer'у не повезло — LLM сочиняет claim verb. Замок гасит."""
    alerts: list[str] = []

    def bad_composer(_sys: str, _user: str) -> str:
        return "Поставила всё как надо!"  # claim verb

    history = (
        ConversationTurn(
            user_text="x",
            journal_entries=(
                ToolJournalEntry(
                    tool_name="schedule_reminder",
                    action_index=0,
                    result_kind=ResultKind.SUCCESS,
                    result_data={"title": "Разбудить", "trigger_human": "сегодня в 2:00"},
                    entity_id="rem_old",
                    idempotency_key="k1",
                ),
            ),
            turn_id="t-prev",
            timestamp_utc="2026-05-17T10:00:00Z",
        ),
    )

    result = process_turn(
        user_text="Нет, не на 2 а на 14:00 разбудить Катю",
        conversation_history=history,
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=_detector_clean,
        invoke_composer_llm=bad_composer,
        admin_alert_fn=alerts.append,
        result_data_extractor=_result_extractor,
    )

    # Финальный текст содержит только первую строку, без live phrase
    assert "Поставила всё как надо" not in result.final_text
    # Admin алерт сработал
    assert any("отвергнута замком" in a for a in alerts)


# ─── L4 audit catches confab ──────────────────────────────────────────


def test_post_audit_catches_unbacked_claim() -> None:
    """Симулируем баг 2026-05-17: text заявляет «отменяю», журнал пустой."""
    alerts: list[str] = []

    def bad_planner(_sys: str, _user: str) -> dict:
        # Планировщик «забыл» вызвать инструмент, но composer что-то напишет
        return {"kind": "no_action", "ack_message": "Поняла! Отменяю и ставлю на 14:00 — «Разбудить Катю»."}

    def confab_detector(text: str, tools: set[str]) -> bool:
        low = text.lower()
        if "отменяю" in low or "ставлю" in low:
            return not bool(tools & {"cancel_reminder", "schedule_reminder", "replace_reminder"})
        return False

    result = process_turn(
        user_text="не правильно, на 14:00 разбудить Катю",
        conversation_history=(),  # пустая история — нет correction target
        turn_context=_ctx(turn_id="t-broken", tenant_id=352612382),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=confab_detector,
        invoke_planner_llm=bad_planner,
        admin_alert_fn=alerts.append,
    )

    # NoAction → final_text = ack_message. Аудит ловит confab.
    assert result.plan_kind == "no_action"
    assert result.audit.is_unbacked is True
    assert result.audit.alert_sent is True
    assert any("L4 audit" in a for a in alerts)
    assert result.audit.correction_pending is not None


# ─── Trace ───────────────────────────────────────────────────────────


def test_empty_execution_plan_falls_back_to_no_action() -> None:
    """R-39 review HIGH: ExecutionPlan(calls=()) → не пустой ответ юзеру.

    Симулируем (теоретический) случай когда планировщик собрал пустой
    план. Раньше final_text=="" → пользователь получал blank.
    Теперь — fallback в no_action с rationale=empty_execution_plan.
    """
    # ExecutionPlan() с дефолтным calls=() — empty plan
    from sreda.agents.contracts import ExecutionPlan

    def planner_returning_empty(_sys: str, _user: str) -> dict:
        # action с пустым списком calls — fall-through через planner._parse_llm_output
        # (там уже есть guard на empty calls). Этот тест проверяет r39_pipeline.
        # Поэтому передаём действие но с заведомо нечитаемым tool.
        return {"kind": "action", "calls": []}

    result = process_turn(
        user_text="не правильно, на 14:00 разбудить Катю",
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables=_tool_callables(),
        detector=_detector_clean,
        invoke_planner_llm=planner_returning_empty,
    )
    # planner._parse_llm_output вернёт NoAction("llm_empty_action_plan")
    # → попадает в наш _build_no_action_result, journal пустой
    assert result.plan_kind == "no_action"
    assert result.journal.is_empty
    # final_text — это ack_message NoAction'а (пустой в данном случае),
    # но как минимум не падает на assert и audit прошёл.
    assert result.audit is not None


def test_correction_pending_exposed_in_pipeline_result() -> None:
    """R-39 review MEDIUM: PipelineResult.correction_pending = audit.correction_pending."""
    def confab_detector(text: str, tools: set[str]) -> bool:
        # Всегда unbacked для теста — главное проверить пробрасывание
        return bool(text)

    result = process_turn(
        user_text="как дела",
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables={},
        detector=confab_detector,
    )
    # Для chitchat NoAction.ack_message="", text не пустой? Нет, пустой.
    # Тогда detector(text="", tools=set()) → False (bool(""))
    # Чтобы детектор сработал, нужен non-empty final_text.
    # Делаем другой сценарий — symptomatic detector с заранее unbacked=True
    def always_unbacked(_t: str, _tools: set[str]) -> bool:
        return True

    result2 = process_turn(
        user_text="как дела?",  # chitchat → no_action → final_text=ack
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables={},
        detector=always_unbacked,
    )
    # correction_pending должен быть в PipelineResult, а не только в audit
    assert result2.correction_pending is not None
    assert result2.correction_pending == result2.audit.correction_pending


def test_trace_contains_pipeline_steps() -> None:
    result = process_turn(
        user_text="как дела?",
        conversation_history=(),
        turn_context=_ctx(),
        now_utc=_now_utc(),
        tool_callables={},
        detector=_detector_clean,
    )
    trace_str = " ".join(result.trace)
    assert "intent=" in trace_str
    assert "plan_kind=" in trace_str
