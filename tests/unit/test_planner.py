"""R-39: тесты планировщика."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from sreda.agents.contracts import (
    Clarification,
    ConversationTurn,
    ExecutionPlan,
    NoAction,
    ResultKind,
    ToolJournalEntry,
    TurnContext,
)
from sreda.agents.correction_resolver import (
    AmbiguousCorrection,
    ResolvedCorrection,
)
from sreda.agents.planner import PlanRequest, plan_action
from sreda.services.natural_time_parser import (
    TimeAmbiguous,
    TimeInvalid,
    TimeResolved,
    TimeUnrecognized,
)
from sreda.services.turn_intent_classifier import TurnIntent


MSK = ZoneInfo("Europe/Moscow")


def _ctx() -> TurnContext:
    return TurnContext(turn_id="t-001", tenant_id="42")


def _resolved_time() -> TimeResolved:
    target_utc = datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc)
    target_msk = target_utc.astimezone(MSK)
    return TimeResolved(
        iso_utc=target_utc,
        iso_user_tz=target_msk,
        source_span=(0, 10),
        timezone_source="user_profile",
    )


# ─── Short-circuit: CHITCHAT ──────────────────────────────────────────


def test_chitchat_returns_no_action_without_llm() -> None:
    request = PlanRequest(
        user_text="Как дела?",
        intent=TurnIntent.CHITCHAT,
        parser_result=None,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)  # invoke_llm не передан
    assert isinstance(result, NoAction)
    assert result.rationale == "chitchat_short_circuit"


# ─── Short-circuit: ResolvedCorrection → update_reminder ────────────


def test_resolved_correction_builds_update_plan() -> None:
    """Кати-сценарий: target из истории + parser дал новое время."""
    target = ResolvedCorrection(
        target_entity_id="rem_old",
        target_title="Разбудить",
        target_tool="schedule_reminder",
        source_turn_id="t-original",
    )
    request = PlanRequest(
        user_text="Нет, не на 2 а на 14 разбудить Катю",
        intent=TurnIntent.MUTATION,
        parser_result=_resolved_time(),
        correction_target=target,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, ExecutionPlan)
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.tool_name == "update_reminder"
    assert call.args["reminder_id"] == "rem_old"
    # parser-resolved trigger_iso подставлен детерминированно
    assert call.args["trigger_iso"].startswith("2026-05-17T14:00")
    assert "разбудить" in call.args["title"].lower()


def test_resolved_correction_without_resolved_time_falls_through_to_llm() -> None:
    """Без TimeResolved нужно идти в LLM (он сам разберётся с временем)."""
    captured: dict = {}

    def fake_llm(_sys: str, _user: str) -> dict:
        captured["called"] = True
        return {"kind": "no_action", "ack_message": "uhm"}

    target = ResolvedCorrection(
        target_entity_id="rem_old",
        target_title="X",
        target_tool="schedule_reminder",
        source_turn_id="t-1",
    )
    request = PlanRequest(
        user_text="не так, поправь",
        intent=TurnIntent.MUTATION,
        parser_result=None,  # parser ничего не нашёл
        correction_target=target,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    # Падаем в LLM — LLM вернула no_action
    assert captured.get("called")
    assert isinstance(result, NoAction)


# ─── Short-circuit: AmbiguousCorrection ──────────────────────────────


def test_ambiguous_correction_returns_clarification() -> None:
    candidates = (
        ResolvedCorrection("rem_a", "Принять таблетки", "schedule_reminder", "t-1"),
        ResolvedCorrection("rem_b", "Купить хлеб", "schedule_reminder", "t-2"),
    )
    target = AmbiguousCorrection(candidates=candidates, reason="multiple_recent_reminders (2)")
    request = PlanRequest(
        user_text="нет, не то",
        intent=TurnIntent.MUTATION,
        parser_result=None,
        correction_target=target,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, Clarification)
    assert "Принять таблетки" in result.question
    assert "Купить хлеб" in result.question


# ─── Short-circuit: TimeAmbiguous / TimeInvalid ──────────────────────


def test_time_ambiguous_returns_clarification() -> None:
    ambiguous = TimeAmbiguous(candidates=[], reason="через_N_часов_или_в_N")
    request = PlanRequest(
        user_text="на 2 часа",
        intent=TurnIntent.MUTATION,
        parser_result=ambiguous,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, Clarification)
    assert "14:00" in result.question or "через" in result.question.lower()


def test_time_invalid_past_returns_clarification() -> None:
    invalid = TimeInvalid(raw="вчера в 9", reason="past_date")
    request = PlanRequest(
        user_text="напомни вчера в 9",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, Clarification)
    assert "прошл" in result.question.lower()


def test_time_invalid_out_of_range_returns_clarification() -> None:
    invalid = TimeInvalid(raw="в 25:00", reason="out_of_range")
    request = PlanRequest(
        user_text="в 25:00 напомни",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, Clarification)
    assert "не разобрала" in result.question.lower()


# ─── LLM-вызов и подмена trigger_iso ────────────────────────────────


def test_llm_action_with_trigger_iso_gets_overwritten_by_parser() -> None:
    """LLM вернула какое-то trigger_iso, но parser дал точный — наш побеждает."""
    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "schedule_reminder",
                "args": {
                    "title": "Разбудить Катю",
                    "trigger_iso": "2099-01-01T00:00:00+00:00",  # сломанное от LLM
                },
            }],
        }

    request = PlanRequest(
        user_text="разбуди Катю в 14:00",
        intent=TurnIntent.MUTATION,
        parser_result=_resolved_time(),
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, ExecutionPlan)
    assert result.calls[0].args["trigger_iso"].startswith("2026-05-17T14:00")


def test_llm_no_action() -> None:
    def fake_llm(_s: str, _u: str) -> dict:
        return {"kind": "no_action", "ack_message": "Спасибо"}

    request = PlanRequest(
        user_text="спасибо",
        intent=TurnIntent.READ,
        parser_result=None,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, NoAction)
    assert result.ack_message == "Спасибо"


def test_llm_clarification() -> None:
    def fake_llm(_s: str, _u: str) -> dict:
        return {"kind": "clarification", "question": "Какое именно?"}

    request = PlanRequest(
        user_text="отмени напоминание",
        intent=TurnIntent.MUTATION,
        parser_result=None,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, Clarification)
    assert "Какое" in result.question


# ─── Защита от плохих LLM-ответов ────────────────────────────────────


def test_llm_returns_none_yields_no_action() -> None:
    def fake_llm(_s: str, _u: str) -> None:
        return None

    request = PlanRequest(
        user_text="что-то", intent=TurnIntent.UNCERTAIN,
        parser_result=None, correction_target=None,
        conversation_history=(), turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, NoAction)
    assert result.rationale == "llm_returned_none"


def test_llm_exception_yields_no_action() -> None:
    def fake_llm(_s: str, _u: str) -> dict:
        raise TimeoutError("simulated")

    request = PlanRequest(
        user_text="x", intent=TurnIntent.UNCERTAIN,
        parser_result=None, correction_target=None,
        conversation_history=(), turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, NoAction)
    assert result.rationale == "llm_exception"


def test_llm_malformed_kind_yields_no_action() -> None:
    def fake_llm(_s: str, _u: str) -> dict:
        return {"kind": "wat", "calls": []}

    request = PlanRequest(
        user_text="x", intent=TurnIntent.UNCERTAIN,
        parser_result=None, correction_target=None,
        conversation_history=(), turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, NoAction)


def test_llm_empty_action_yields_no_action() -> None:
    """LLM сказала action, но не дала ни одного call."""
    def fake_llm(_s: str, _u: str) -> dict:
        return {"kind": "action", "calls": []}

    request = PlanRequest(
        user_text="x", intent=TurnIntent.UNCERTAIN,
        parser_result=None, correction_target=None,
        conversation_history=(), turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, NoAction)
    assert result.rationale == "llm_empty_action_plan"


def test_no_invoke_llm_yields_no_action() -> None:
    request = PlanRequest(
        user_text="x", intent=TurnIntent.MUTATION,
        parser_result=None, correction_target=None,
        conversation_history=(), turn_context=_ctx(),
    )
    result = plan_action(request, invoke_llm=None)
    assert isinstance(result, NoAction)


# ─── Кати-сценарий end-to-end ────────────────────────────────────────


def test_title_hint_strips_leading_negation() -> None:
    """R-39 review MAJOR 1: title не должен содержать ведущее «не»."""
    target = ResolvedCorrection(
        target_entity_id="rem_x",
        target_title="Старое",
        target_tool="schedule_reminder",
        source_turn_id="t-1",
    )
    request = PlanRequest(
        user_text="не на 14:00 а на 15:00 разбудить Катю",
        intent=TurnIntent.MUTATION,
        parser_result=_resolved_time(),
        correction_target=target,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, ExecutionPlan)
    title = result.calls[0].args["title"].lower()
    # «не» не должно быть в начале — иначе сохраним инвертированный title
    assert not title.startswith("не ")
    assert "разбудить" in title
    assert "катю" in title


def test_kati_correction_full_pipeline() -> None:
    """Полный сценарий: ResolvedCorrection + TimeResolved → ExecutionPlan."""
    target = ResolvedCorrection(
        target_entity_id="rem_old",
        target_title="Разбудить",
        target_tool="schedule_reminder",
        source_turn_id="t-original",
    )
    request = PlanRequest(
        user_text="Нет, не на 2 а на 14 разбудить Катю",
        intent=TurnIntent.MUTATION,
        parser_result=_resolved_time(),
        correction_target=target,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, ExecutionPlan)
    assert result.calls[0].tool_name == "update_reminder"
    assert result.calls[0].args["reminder_id"] == "rem_old"
    # trigger_iso детерминирован, не от LLM
    assert "2026-05-17T14:00" in result.calls[0].args["trigger_iso"]
