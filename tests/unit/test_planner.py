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
    # 2026-05-19 (R-39 cleanup): bumped 2026-05-17 → 2030-05-17 because
    # planner._parse_llm_output теперь дропает past trigger_iso через
    # is_past_iso. Stale fixture даты pre-cleanup ловились этим guard'ом.
    # 14:00 MSK == 11:00 UTC — фиксируем для assert-stable.
    target_utc = datetime(2030, 5, 17, 11, 0, tzinfo=timezone.utc)
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
    assert call.args["trigger_iso"].startswith("2030-05-17T14:00")
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


def test_time_ambiguous_falls_through_to_llm() -> None:
    """2026-05-19: removed hardcoded TimeAmbiguous → Clarification(«через 2 часа
    или в 14:00?»). Hardcoded text прилетал на ЛЮБОЙ TimeAmbiguous (even
    «пятницу», «два часа дня») — wrong question on voice. Теперь fall through
    на LLM который сам disambig из context."""
    ambiguous = TimeAmbiguous(candidates=[], reason="через_N_часов_или_в_N")
    request = PlanRequest(
        user_text="на 2 часа",
        intent=TurnIntent.MUTATION,
        parser_result=ambiguous,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    # Без invoke_llm — short-circuit нет, проваливается на step 6 (no LLM)
    result = plan_action(request)
    assert isinstance(result, NoAction)
    assert result.rationale == "no_llm_available"


def test_time_ambiguous_invokes_llm_when_available() -> None:
    """TimeAmbiguous с invoke_llm callable — LLM вызывается (не hardcoded)."""
    ambiguous = TimeAmbiguous(candidates=[], reason="через_N_часов_или_в_N")
    request = PlanRequest(
        user_text="на 2 часа дня",
        intent=TurnIntent.MUTATION,
        parser_result=ambiguous,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    invoke_count = 0
    def fake_llm(system: str, user: str) -> dict:
        nonlocal invoke_count
        invoke_count += 1
        # Verify negative parser hint НЕ в prompt'е
        assert "не распознан" not in user.lower()
        return {"kind": "action", "calls": [{"tool": "schedule_reminder", "args": {"title": "x", "trigger_iso": "2030-05-19T14:00:00+03:00"}}]}
    result = plan_action(request, invoke_llm=fake_llm)
    assert invoke_count == 1, "LLM должна быть вызвана (TimeAmbiguous не short-circuit'ит)"
    assert isinstance(result, ExecutionPlan)


def test_time_invalid_past_falls_through_to_llm_when_available() -> None:
    """2026-05-19 cleanup: past_date + invoke_llm → LLM вызывается, может
    вернуть save_episode (real prod case «Сделала зарядку вчера» — НЕ
    reminder request). Hardcoded «На какое поставить?» удалён.

    Codex MAJOR R1 lock-in: prevents future re-introduction of short-circuit.
    OpenCode MINOR R1: assert user_text preserved + no leaked clarification wording.
    """
    invalid = TimeInvalid(raw="вчера", reason="past_date")
    request = PlanRequest(
        user_text="Сделала зарядку вчера",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    invoke_count = 0

    def fake_llm(system: str, user: str) -> dict:
        nonlocal invoke_count
        invoke_count += 1
        # User-text preserved в prompt (OpenCode MINOR R1):
        assert "Сделала зарядку вчера" in user
        # NO hardcoded «уже прошло» wording leaked в prompt
        assert "уже прошло" not in user.lower()
        return {
            "kind": "action",
            "calls": [{"tool": "save_episode", "args": {"summary": "Зарядка"}}],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    assert invoke_count == 1, "LLM должна быть вызвана (past_date не short-circuit'ит)"
    assert isinstance(result, ExecutionPlan)
    assert result.calls[0].tool_name == "save_episode"


def test_time_invalid_past_without_llm_returns_no_action() -> None:
    """2026-05-19 cleanup: past_date без invoke_llm → NoAction(no_llm_available),
    НЕ hardcoded Clarification «уже прошло». Regression на removal."""
    invalid = TimeInvalid(raw="вчера", reason="past_date")
    request = PlanRequest(
        user_text="Сделала зарядку вчера",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)  # invoke_llm=None
    assert isinstance(result, NoAction)
    assert result.rationale == "no_llm_available"


def test_time_invalid_out_of_range_returns_clarification() -> None:
    """out_of_range остаётся hardcoded — nonsensical input («25:00»), LLM
    не поможет."""
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
    assert result.rationale == "parser_invalid:out_of_range"
    assert "сказать иначе" in result.question.lower()


def test_time_invalid_unknown_reason_returns_clarification() -> None:
    """Codex MAJOR R1: future TimeInvalid.reason values → fail-closed
    Clarification с distinct rationale (НЕ fall through на LLM —
    parser failure mode неизвестен, risk-averse path)."""
    invalid = TimeInvalid(raw="something", reason="future_unknown_reason")
    request = PlanRequest(
        user_text="x",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    result = plan_action(request)
    assert isinstance(result, Clarification)
    assert "unknown" in result.rationale
    assert "future_unknown_reason" in result.rationale


def test_llm_schedule_with_past_trigger_dropped() -> None:
    """Codex MAJOR R1+R2: LLM попытался schedule_reminder с past trigger_iso
    → drop по actual ISO check (is_past_iso), не по parser_result. Single-call
    путь — calls пустой ПОСЛЕ continue, fail на past_trigger_drop_all
    (НЕ llm_empty_action_plan — Codex R3 ordering note)."""
    invalid = TimeInvalid(raw="вчера в 9", reason="past_date")
    request = PlanRequest(
        user_text="Напомни вчера в 9",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    past_iso = "2026-01-01T09:00:00+03:00"  # явно в прошлом

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "schedule_reminder",
                "args": {"title": "x", "trigger_iso": past_iso},
            }],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, Clarification)
    assert "past_trigger_drop_all" in result.rationale
    assert "уже прошло" in result.question.lower()


def test_past_date_marker_but_future_trigger_kept() -> None:
    """Codex MAJOR R2: «Вчера понял, напомни завтра в 9» — parser выдал
    past_date marker (из-за «вчера»), но LLM правильно эмитит future
    trigger. Drop НЕ должен сработать (validate actual ISO, не parser hint)."""
    from datetime import timedelta

    invalid = TimeInvalid(raw="вчера", reason="past_date")
    request = PlanRequest(
        user_text="Вчера понял, напомни завтра в 9",
        intent=TurnIntent.MUTATION,
        parser_result=invalid,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    future_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "schedule_reminder",
                "args": {"title": "Понял что-то", "trigger_iso": future_iso},
            }],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, ExecutionPlan)
    assert result.calls[0].tool_name == "schedule_reminder"


def test_recurring_reminder_with_past_anchor_kept() -> None:
    """Codex MAJOR R1 code-review: recurring reminder (с `recurrence_rule`)
    может иметь past anchor — RRULE сама находит next future occurrence.
    Service layer (housewife_chat_tools.py:247) тоже skip'ит past-date
    check для recurring. Planner ДОЛЖЕН keep'ать такие calls."""
    request = PlanRequest(
        user_text="напомни каждый день в 9 утра пить таблетки",
        intent=TurnIntent.MUTATION,
        parser_result=None,  # parser не извлёк "каждый день в 9" как single time
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )
    # Past anchor (e.g. user сказал утром 10:00 поставить «каждый день в 9»)
    past_anchor = "2026-01-01T09:00:00+03:00"

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "schedule_reminder",
                "args": {
                    "title": "Пить таблетки",
                    "trigger_iso": past_anchor,  # past, но RRULE найдёт next
                    "recurrence_rule": "FREQ=DAILY;BYHOUR=6;BYMINUTE=0",  # 09:00 MSK
                },
            }],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    # НЕ должен dropped — recurrence_rule валиден, RRULE сама next-future
    assert isinstance(result, ExecutionPlan)
    assert result.calls[0].tool_name == "schedule_reminder"
    assert result.calls[0].args["recurrence_rule"] == "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"


def test_update_recurring_with_past_anchor_kept() -> None:
    """Codex MINOR R3 code-review: positive coverage в planner для
    update_reminder happy path. Past trigger + active recurrence_rule
    (без clear_recurrence) → ExecutionPlan (НЕ Clarification).
    Symmetric к test_recurring_reminder_with_past_anchor_kept."""
    request = PlanRequest(
        user_text="обнови напоминание про таблетки на каждый день в 9",
        intent=TurnIntent.MUTATION,
        parser_result=None,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "update_reminder",
                "args": {
                    "reminder_id": "rem_x",
                    "trigger_iso": "2026-01-01T09:00:00+03:00",  # past anchor
                    "recurrence_rule": "FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
                    # clear_recurrence absent — recurrence active остаётся
                },
            }],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, ExecutionPlan)
    assert result.calls[0].tool_name == "update_reminder"
    assert result.calls[0].args["recurrence_rule"] == "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"


def test_update_with_clear_recurrence_and_past_trigger_dropped() -> None:
    """Codex MINOR R2 code-review: net effect important. update_reminder
    с recurrence_rule + clear_recurrence=True снимает recurrence на стороне
    сервиса → получаем past one-shot reminder. Planner ДОЛЖЕН drop'нуть
    (нельзя bypass past-trigger check по recurrence_rule одному)."""
    request = PlanRequest(
        user_text="убери повторение, поставь на вчера 9 утра",
        intent=TurnIntent.MUTATION,
        parser_result=None,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "update_reminder",
                "args": {
                    "reminder_id": "rem_x",
                    "trigger_iso": "2026-01-01T09:00:00+03:00",  # past
                    "recurrence_rule": "FREQ=DAILY;BYHOUR=6",  # ignored
                    "clear_recurrence": True,  # net: становится one-shot past
                },
            }],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    # ДОЛЖЕН drop'нуться: net recurrence отключена + past trigger = anti-pattern
    assert isinstance(result, Clarification)
    assert "past_trigger_drop_all" in result.rationale


def test_update_reminder_with_past_trigger_dropped() -> None:
    """OpenCode MINOR R1 code-review: tool-set check покрывает не только
    schedule_reminder. Regression test: update_reminder с past trigger_iso
    тоже должен dropped (если кто-то поломает tool set frozen frozen{}, sched
    единственным проверяемым tool'ом, мы бы пропустили это)."""
    request = PlanRequest(
        user_text="перенеси таблетки на вчера",  # nonsense, тест на edge case
        intent=TurnIntent.MUTATION,
        parser_result=None,
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [{
                "tool": "update_reminder",
                "args": {
                    "reminder_id": "rem_x",
                    "title": "Таблетки",
                    "trigger_iso": "2026-01-01T09:00:00+03:00",  # past
                },
            }],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    assert isinstance(result, Clarification)
    assert "past_trigger_drop_all" in result.rationale
    # update_reminder в списке drop'ов:
    assert "update_reminder" in result.rationale


def test_mixed_calls_with_past_trigger_full_clarification() -> None:
    """Codex MAJOR R2: LLM returns [save_episode(valid), schedule_reminder(past)]
    → ВСЯ plan rejected (Clarification), НЕ partial ExecutionPlan с save_episode.
    Silent partial drop = R-39 confab class regression (user думает запрос
    обработан, а reminder потерян)."""
    request = PlanRequest(
        user_text="Сделала зарядку вчера, и напомни вчера же сходить",
        intent=TurnIntent.MUTATION,
        parser_result=TimeInvalid(raw="вчера", reason="past_date"),
        correction_target=None,
        conversation_history=(),
        turn_context=_ctx(),
    )

    def fake_llm(_sys: str, _user: str) -> dict:
        return {
            "kind": "action",
            "calls": [
                {"tool": "save_episode", "args": {"summary": "Зарядка вчера"}},
                {
                    "tool": "schedule_reminder",
                    "args": {
                        "title": "Сходить",
                        "trigger_iso": "2026-01-01T09:00:00+03:00",
                    },
                },
            ],
        }

    result = plan_action(request, invoke_llm=fake_llm)
    # Whole plan rejected, NOT partial ExecutionPlan
    assert isinstance(result, Clarification)
    assert "past_trigger_drop_all" in result.rationale


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
    assert result.calls[0].args["trigger_iso"].startswith("2030-05-17T14:00")


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
    assert "2030-05-17T14:00" in result.calls[0].args["trigger_iso"]


# ─── _build_user_prompt — silent skip negative parser_hint ─────────────
# 2026-05-19: убрали «Время: не распознано парсером.» строку — она
# триггерила fast LLMs в overly-cautious clarification. Bench v4
# подтвердил: 0 hallucinated_time на truly_ambiguous turns даже
# без negative hint.


def _build_request(parser_result, user_text="Поставь напоминание на 12", history=()):
    """Helper для построения PlanRequest с varied parser_result."""
    from sreda.agents.contracts import TurnContext
    return PlanRequest(
        user_text=user_text,
        intent=TurnIntent.MUTATION,
        parser_result=parser_result,
        correction_target=None,
        conversation_history=history,
        turn_context=TurnContext(turn_id="t-test", tenant_id="42"),
    )


def test_build_user_prompt_includes_positive_time_hint() -> None:
    """TimeResolved → положительный hint «Время (детерминированно): ...»"""
    from sreda.agents.planner import _build_user_prompt
    request = _build_request(parser_result=_resolved_time())
    prompt = _build_user_prompt(request)
    assert "Сообщение:" in prompt
    assert "Намерение: mutation" in prompt
    assert "Время (детерминированно):" in prompt
    assert "2030-05-17T14:00" in prompt  # iso_user_tz


def test_build_user_prompt_silent_skip_on_unrecognized() -> None:
    """TimeUnrecognized → ОТСУТСТВУЕТ строка «не распознано парсером»."""
    from sreda.agents.planner import _build_user_prompt
    request = _build_request(parser_result=TimeUnrecognized(raw="на пятницу"))
    prompt = _build_user_prompt(request)
    assert "Сообщение:" in prompt
    assert "Намерение: mutation" in prompt
    # Критично: НЕ должно быть negative hint строки
    assert "не распознано" not in prompt
    assert "не распознан" not in prompt
    # Time-related guidance отсутствует — LLM сама извлекает
    assert "Время" not in prompt


def test_build_user_prompt_silent_skip_on_ambiguous() -> None:
    """TimeAmbiguous → silent skip (handled через short-circuit ДО LLM)."""
    from sreda.agents.planner import _build_user_prompt
    ambiguous = TimeAmbiguous(candidates=[], reason="через_N_часов_или_в_N")
    request = _build_request(parser_result=ambiguous)
    prompt = _build_user_prompt(request)
    assert "Время" not in prompt
    assert "не распознан" not in prompt


def test_build_user_prompt_silent_skip_on_invalid() -> None:
    """TimeInvalid → silent skip (handled через short-circuit ДО LLM)."""
    from sreda.agents.planner import _build_user_prompt
    invalid = TimeInvalid(raw="вчера", reason="past_date")
    request = _build_request(parser_result=invalid)
    prompt = _build_user_prompt(request)
    assert "Время" not in prompt


def test_build_user_prompt_preserves_user_text() -> None:
    """Original user_text сохраняется в кавычках в prompt."""
    from sreda.agents.planner import _build_user_prompt
    request = _build_request(
        parser_result=TimeUnrecognized(raw=""),
        user_text="на пятницу на два часа дня поставь напоминание поесть",
    )
    prompt = _build_user_prompt(request)
    assert "на пятницу на два часа дня поставь напоминание поесть" in prompt


def test_build_user_prompt_includes_history_when_present() -> None:
    """conversation_history добавляет «Прошлая реплика пользователя»."""
    from sreda.agents.planner import _build_user_prompt
    prior = ConversationTurn(
        turn_id="t-prev",
        user_text="Поставь на пятницу 12:00 поесть",
        timestamp_utc=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
        journal_entries=(),
    )
    request = _build_request(
        parser_result=TimeUnrecognized(raw=""),
        user_text="Ну, на ближайшую, конечно.",
        history=(prior,),
    )
    prompt = _build_user_prompt(request)
    assert "Прошлая реплика пользователя" in prompt
    assert "Поставь на пятницу 12:00 поесть" in prompt
    # Negative hint всё равно не должен появиться
    assert "не распознан" not in prompt
